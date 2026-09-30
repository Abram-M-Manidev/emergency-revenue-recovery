"""SQLAlchemy implementations of the emergency-paging repositories.

Every read that can follow a Core UPDATE in the same session uses
`populate_existing`, so an entity is never built from a stale identity-map
copy of a row this transaction has just changed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.paging.page import (
    AcknowledgementMethod,
    EmergencyPage,
    NewPageNotification,
    PageNotification,
    PageNotificationStatus,
    PageStatus,
    RecipientRole,
)
from app.domain.paging.settings import PagingSettings
from app.domain.repositories.paging_repository import (
    EmergencyPageRepository,
    PagingSettingsRepository,
)
from app.infrastructure.database.models.paging import (
    EmergencyPageModel,
    EmergencyPageNotificationModel,
    OrganizationPagingSettingsModel,
)

_UNSENT = (PageNotificationStatus.QUEUED, PageNotificationStatus.RETRYING)
_CLAIMABLE = (*_UNSENT, PageNotificationStatus.SENDING)
_ESCALATING = (PageStatus.PAGING_PRIMARY, PageStatus.PAGING_BACKUP)


def _settings_to_entity(model: OrganizationPagingSettingsModel) -> PagingSettings:
    return PagingSettings(
        organization_id=model.organization_id,
        is_enabled=model.is_enabled,
        primary_number=model.primary_number,
        backup_number=model.backup_number,
        sms_enabled=model.sms_enabled,
        voice_enabled=model.voice_enabled,
        ack_timeout_seconds=model.ack_timeout_seconds,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _page_to_entity(model: EmergencyPageModel) -> EmergencyPage:
    return EmergencyPage(
        id=model.id,
        organization_id=model.organization_id,
        ticket_id=model.emergency_ticket_id,
        status=model.status,
        escalate_at=model.escalate_at,
        ack_timeout_seconds=model.ack_timeout_seconds,
        escalated_at=model.escalated_at,
        acknowledged_at=model.acknowledged_at,
        acknowledged_by_role=model.acknowledged_by_role,
        acknowledged_via=model.acknowledged_via,
        acknowledged_by_user_id=model.acknowledged_by_user_id,
        unresolved_at=model.unresolved_at,
        unresolved_reason=model.unresolved_reason,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _notification_to_entity(model: EmergencyPageNotificationModel) -> PageNotification:
    return PageNotification(
        id=model.id,
        organization_id=model.organization_id,
        page_id=model.page_id,
        role=model.role,
        channel=model.channel,
        destination=model.destination,
        status=model.status,
        attempts=model.attempts,
        next_attempt_at=model.next_attempt_at,
        lease_expires_at=model.lease_expires_at,
        provider=model.provider,
        provider_message_id=model.provider_message_id,
        error_code=model.error_code,
        sent_at=model.sent_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyPagingSettingsRepository(PagingSettingsRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, organization_id: uuid.UUID) -> PagingSettings | None:
        model = (
            await self._session.execute(
                select(OrganizationPagingSettingsModel)
                .where(OrganizationPagingSettingsModel.organization_id == organization_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return _settings_to_entity(model) if model else None

    async def upsert(self, settings: PagingSettings) -> PagingSettings:
        # ON CONFLICT on the unique organization_id: two Owners saving at once
        # resolve to one row rather than a unique violation that would poison
        # the request's transaction.
        statement = pg_insert(OrganizationPagingSettingsModel).values(
            id=uuid.uuid4(),
            organization_id=settings.organization_id,
            is_enabled=settings.is_enabled,
            primary_number=settings.primary_number,
            backup_number=settings.backup_number,
            sms_enabled=settings.sms_enabled,
            voice_enabled=settings.voice_enabled,
            ack_timeout_seconds=settings.ack_timeout_seconds,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["organization_id"],
                set_={
                    "is_enabled": statement.excluded.is_enabled,
                    "primary_number": statement.excluded.primary_number,
                    "backup_number": statement.excluded.backup_number,
                    "sms_enabled": statement.excluded.sms_enabled,
                    "voice_enabled": statement.excluded.voice_enabled,
                    "ack_timeout_seconds": statement.excluded.ack_timeout_seconds,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
        await self._session.flush()
        stored = await self.get(settings.organization_id)
        assert stored is not None  # just written, in this transaction
        return stored

    async def delete(self, organization_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(OrganizationPagingSettingsModel).where(
                OrganizationPagingSettingsModel.organization_id == organization_id
            )
        )
        await self._session.flush()


class SqlAlchemyEmergencyPageRepository(EmergencyPageRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Creation ---------------------------------------------------------------

    async def create(
        self,
        organization_id: uuid.UUID,
        ticket_id: uuid.UUID,
        *,
        ack_timeout_seconds: int,
        escalate_at: datetime,
        notifications: Sequence[NewPageNotification],
        now: datetime,
    ) -> tuple[EmergencyPage, bool]:
        # One statement against the unique index on the ticket: a
        # SELECT-then-INSERT would let two writers both see no page.
        page_id = uuid.uuid4()
        inserted = (
            await self._session.execute(
                pg_insert(EmergencyPageModel)
                .values(
                    id=page_id,
                    organization_id=organization_id,
                    emergency_ticket_id=ticket_id,
                    status=PageStatus.PAGING_PRIMARY,
                    ack_timeout_seconds=ack_timeout_seconds,
                    escalate_at=escalate_at,
                )
                .on_conflict_do_nothing(index_elements=["emergency_ticket_id"])
                .returning(EmergencyPageModel.id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            await self.add_notifications(organization_id, page_id, notifications, now=now)
        await self._session.flush()
        page = await self.get_for_ticket(organization_id, ticket_id)
        if page is None:
            # Only reachable if the conflicting page belongs to another
            # organization — never synthesise a cross-tenant read.
            raise RuntimeError("emergency page exists for a ticket outside this organization")
        return page, inserted is not None

    # --- Reads ---------------------------------------------------------------------

    async def get_for_ticket(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> EmergencyPage | None:
        model = (
            await self._session.execute(
                select(EmergencyPageModel)
                .where(
                    EmergencyPageModel.organization_id == organization_id,
                    EmergencyPageModel.emergency_ticket_id == ticket_id,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return _page_to_entity(model) if model else None

    async def get_by_id(
        self, organization_id: uuid.UUID, page_id: uuid.UUID
    ) -> EmergencyPage | None:
        model = (
            await self._session.execute(
                select(EmergencyPageModel)
                .where(
                    EmergencyPageModel.organization_id == organization_id,
                    EmergencyPageModel.id == page_id,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return _page_to_entity(model) if model else None

    async def list_for_organization(
        self, organization_id: uuid.UUID, *, limit: int
    ) -> list[EmergencyPage]:
        models = (
            await self._session.execute(
                select(EmergencyPageModel)
                .where(EmergencyPageModel.organization_id == organization_id)
                .order_by(EmergencyPageModel.created_at.desc())
                .limit(limit)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        return [_page_to_entity(model) for model in models]

    async def list_notifications(
        self, organization_id: uuid.UUID, page_id: uuid.UUID
    ) -> list[PageNotification]:
        models = (
            await self._session.execute(
                select(EmergencyPageNotificationModel)
                .where(
                    EmergencyPageNotificationModel.organization_id == organization_id,
                    EmergencyPageNotificationModel.page_id == page_id,
                )
                .order_by(
                    EmergencyPageNotificationModel.created_at,
                    EmergencyPageNotificationModel.role,
                    EmergencyPageNotificationModel.channel,
                )
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        return [_notification_to_entity(model) for model in models]

    # --- Locking the page ------------------------------------------------------------

    async def lock_for_ticket(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> EmergencyPage | None:
        model = (
            await self._session.execute(
                select(EmergencyPageModel)
                .where(
                    EmergencyPageModel.organization_id == organization_id,
                    EmergencyPageModel.emergency_ticket_id == ticket_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return _page_to_entity(model) if model else None

    async def lock_by_id(self, page_id: uuid.UUID) -> EmergencyPage | None:
        model = (
            await self._session.execute(
                select(EmergencyPageModel)
                .where(EmergencyPageModel.id == page_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return _page_to_entity(model) if model else None

    async def lock_next_escalation_due(self, *, now: datetime) -> EmergencyPage | None:
        model = (
            await self._session.execute(
                select(EmergencyPageModel)
                .where(
                    EmergencyPageModel.escalate_at.is_not(None),
                    EmergencyPageModel.escalate_at <= now,
                    EmergencyPageModel.status.in_(_ESCALATING),
                )
                .order_by(EmergencyPageModel.escalate_at)
                .limit(1)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return _page_to_entity(model) if model else None

    async def _update_page(self, page_id: uuid.UUID, **values: object) -> EmergencyPage:
        model = (
            await self._session.execute(
                update(EmergencyPageModel)
                .where(EmergencyPageModel.id == page_id)
                .values(**values)
                .returning(EmergencyPageModel)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one()
        await self._session.flush()
        await self._session.refresh(model)
        return _page_to_entity(model)

    async def mark_acknowledged(
        self,
        page_id: uuid.UUID,
        *,
        at: datetime,
        role: RecipientRole | None,
        via: AcknowledgementMethod,
        user_id: uuid.UUID | None,
    ) -> EmergencyPage:
        return await self._update_page(
            page_id,
            status=PageStatus.ACKNOWLEDGED,
            escalate_at=None,
            acknowledged_at=at,
            acknowledged_by_role=role,
            acknowledged_via=via,
            acknowledged_by_user_id=user_id,
        )

    async def mark_escalated(
        self, page_id: uuid.UUID, *, at: datetime, escalate_at: datetime
    ) -> EmergencyPage:
        return await self._update_page(
            page_id,
            status=PageStatus.PAGING_BACKUP,
            escalated_at=at,
            escalate_at=escalate_at,
        )

    async def mark_unresolved(
        self, page_id: uuid.UUID, *, at: datetime, reason: str
    ) -> EmergencyPage:
        return await self._update_page(
            page_id,
            status=PageStatus.UNRESOLVED,
            escalate_at=None,
            unresolved_at=at,
            unresolved_reason=reason,
        )

    async def bring_escalation_forward(
        self, page_id: uuid.UUID, *, expected_status: PageStatus, at: datetime
    ) -> bool:
        # One conditional statement: no read-then-write window in which an
        # escalation could slip in and have its fresh deadline cut short.
        result = await self._session.execute(
            update(EmergencyPageModel)
            .where(
                EmergencyPageModel.id == page_id,
                EmergencyPageModel.status == expected_status,
                EmergencyPageModel.escalate_at > at,
            )
            .values(escalate_at=at)
            .returning(EmergencyPageModel.id)
            .execution_options(synchronize_session=False)
        )
        moved = result.scalar_one_or_none() is not None
        await self._session.flush()
        return moved

    # --- Notifications ------------------------------------------------------------------

    async def add_notifications(
        self,
        organization_id: uuid.UUID,
        page_id: uuid.UUID,
        notifications: Sequence[NewPageNotification],
        *,
        now: datetime,
    ) -> int:
        created = 0
        for notification in notifications:
            inserted = (
                await self._session.execute(
                    pg_insert(EmergencyPageNotificationModel)
                    .values(
                        id=uuid.uuid4(),
                        organization_id=organization_id,
                        page_id=page_id,
                        role=notification.role,
                        channel=notification.channel,
                        destination=notification.destination,
                        status=PageNotificationStatus.QUEUED,
                        attempts=0,
                        next_attempt_at=now,
                    )
                    .on_conflict_do_nothing(
                        constraint="uq_emergency_page_notifications_page_role_channel"
                    )
                    .returning(EmergencyPageNotificationModel.id)
                )
            ).scalar_one_or_none()
            if inserted is not None:
                created += 1
        await self._session.flush()
        return created

    async def cancel_unsent(self, page_id: uuid.UUID) -> int:
        result = await self._session.execute(
            update(EmergencyPageNotificationModel)
            .where(
                EmergencyPageNotificationModel.page_id == page_id,
                EmergencyPageNotificationModel.status.in_(_UNSENT),
            )
            .values(status=PageNotificationStatus.CANCELED, next_attempt_at=None)
            .returning(EmergencyPageNotificationModel.id)
            .execution_options(synchronize_session=False)
        )
        canceled = len(result.scalars().all())
        await self._session.flush()
        return canceled

    async def claim_next_notification(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        page_id: uuid.UUID | None = None,
    ) -> PageNotification | None:
        query = (
            select(EmergencyPageNotificationModel.id)
            .join(EmergencyPageModel, EmergencyPageModel.id == EmergencyPageNotificationModel.page_id)
            .where(
                EmergencyPageNotificationModel.next_attempt_at.is_not(None),
                EmergencyPageNotificationModel.next_attempt_at <= now,
                EmergencyPageNotificationModel.status.in_(_CLAIMABLE),
                # Never send for a page someone has already taken.
                EmergencyPageModel.status != PageStatus.ACKNOWLEDGED,
                # Belt and braces for tenancy: the notification and its page
                # must agree on the organization.
                EmergencyPageModel.organization_id == EmergencyPageNotificationModel.organization_id,
            )
            .order_by(EmergencyPageNotificationModel.next_attempt_at)
            .limit(1)
            # Lock only the notification: the page lock belongs to
            # acknowledgement and escalation, and taking it here would make a
            # slow claim block an acknowledgement.
            .with_for_update(skip_locked=True, of=EmergencyPageNotificationModel)
        )
        if page_id is not None:
            query = query.where(EmergencyPageNotificationModel.page_id == page_id)
        claimed_id = (await self._session.execute(query)).scalar_one_or_none()
        if claimed_id is None:
            return None
        model = (
            await self._session.execute(
                update(EmergencyPageNotificationModel)
                .where(EmergencyPageNotificationModel.id == claimed_id)
                .values(
                    status=PageNotificationStatus.SENDING,
                    attempts=EmergencyPageNotificationModel.attempts + 1,
                    # While sending, "due again" means "the lease ran out".
                    next_attempt_at=lease_until,
                    lease_expires_at=lease_until,
                )
                .returning(EmergencyPageNotificationModel)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one()
        await self._session.flush()
        await self._session.refresh(model)
        return _notification_to_entity(model)

    async def record_notification_result(
        self,
        notification_id: uuid.UUID,
        *,
        claimed_attempt: int,
        status: PageNotificationStatus,
        provider: str,
        provider_message_id: str | None,
        error_code: str | None,
        next_attempt_at: datetime | None,
        sent_at: datetime | None,
    ) -> PageNotification | None:
        model = (
            await self._session.execute(
                update(EmergencyPageNotificationModel)
                .where(
                    EmergencyPageNotificationModel.id == notification_id,
                    # The fence: only the claim that is still current may
                    # write its result.
                    EmergencyPageNotificationModel.status == PageNotificationStatus.SENDING,
                    EmergencyPageNotificationModel.attempts == claimed_attempt,
                )
                .values(
                    status=status,
                    provider=provider[:50],
                    provider_message_id=provider_message_id[:64] if provider_message_id else None,
                    error_code=error_code[:50] if error_code else None,
                    next_attempt_at=next_attempt_at,
                    lease_expires_at=None,
                    sent_at=sent_at,
                )
                .returning(EmergencyPageNotificationModel)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
        if model is None:
            return None
        await self._session.flush()
        await self._session.refresh(model)
        return _notification_to_entity(model)
