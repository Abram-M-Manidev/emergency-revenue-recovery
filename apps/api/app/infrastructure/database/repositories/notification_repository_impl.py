"""SQL implementations of the two notification ports."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.notifications.emergency import (
    DeliveryStatus,
    NotificationChannel,
    NotificationDelivery,
)
from app.domain.repositories.notification_repository import (
    NotificationDeliveryRepository,
    NotificationSettingsRepository,
)
from app.infrastructure.database.models.notification import (
    EmergencyNotificationDeliveryModel,
    OrganizationNotificationSettingsModel,
)


class SqlAlchemyNotificationSettingsRepository(NotificationSettingsRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_destination(
        self, organization_id: uuid.UUID
    ) -> tuple[NotificationChannel, str] | None:
        result = await self._session.execute(
            select(
                OrganizationNotificationSettingsModel.channel,
                OrganizationNotificationSettingsModel.destination,
            ).where(
                OrganizationNotificationSettingsModel.organization_id == organization_id,
                # Disabled is indistinguishable from unconfigured on purpose:
                # both mean "do not claim anyone was alerted", and an operator
                # who switched alerting off for maintenance should not have to
                # delete the destination to make that true.
                OrganizationNotificationSettingsModel.is_enabled.is_(True),
            )
        )
        row = result.first()
        if row is None:
            return None
        channel, destination = row
        if not destination or not destination.strip():
            return None
        return channel, destination


class SqlAlchemyNotificationDeliveryRepository(NotificationDeliveryRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_ticket(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> NotificationDelivery | None:
        result = await self._session.execute(
            select(EmergencyNotificationDeliveryModel).where(
                EmergencyNotificationDeliveryModel.organization_id == organization_id,
                EmergencyNotificationDeliveryModel.emergency_ticket_id == ticket_id,
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def claim(
        self,
        organization_id: uuid.UUID,
        ticket_id: uuid.UUID,
        *,
        channel: NotificationChannel | None,
        provider: str,
    ) -> tuple[NotificationDelivery, bool]:
        # INSERT ... ON CONFLICT DO NOTHING against the unique index on the
        # ticket. This is the whole idempotency guarantee, and it has to be
        # one statement: a SELECT-then-INSERT would let two workers both see
        # no row and both send, which for an emergency means paging a human
        # twice for one incident. `RETURNING` yields a row only when the
        # insert actually happened, so an empty result *is* the signal that
        # someone else owns this ticket.
        statement = (
            pg_insert(EmergencyNotificationDeliveryModel)
            .values(
                id=uuid.uuid4(),
                organization_id=organization_id,
                emergency_ticket_id=ticket_id,
                channel=channel,
                provider=provider,
                status=DeliveryStatus.PENDING,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["emergency_ticket_id"])
            .returning(EmergencyNotificationDeliveryModel.id)
        )
        inserted_id = (await self._session.execute(statement)).scalar_one_or_none()
        await self._session.flush()

        existing = await self.get_for_ticket(organization_id, ticket_id)
        if existing is None:
            # Only reachable when the conflicting row belongs to another
            # organization — impossible while ticket ids are unique per
            # tenant, but a cross-tenant read must never be synthesised to
            # paper over it. Report a not-configured record rather than
            # inventing one that could authorise a claim.
            return (
                _unpersisted(organization_id, ticket_id, provider),
                False,
            )
        return existing, inserted_id is not None

    async def record_result(
        self,
        organization_id: uuid.UUID,
        ticket_id: uuid.UUID,
        *,
        status: DeliveryStatus,
        error_code: str | None,
        provider: str,
    ) -> NotificationDelivery:
        values: dict[str, object] = {
            "status": status,
            "error_code": error_code,
            "provider": provider,
            "attempts": EmergencyNotificationDeliveryModel.attempts + 1,
        }
        if status is DeliveryStatus.DELIVERED:
            values["delivered_at"] = datetime.now(timezone.utc)

        await self._session.execute(
            update(EmergencyNotificationDeliveryModel)
            .where(
                EmergencyNotificationDeliveryModel.organization_id == organization_id,
                EmergencyNotificationDeliveryModel.emergency_ticket_id == ticket_id,
            )
            .values(**values)
        )
        await self._session.flush()

        updated = await self.get_for_ticket(organization_id, ticket_id)
        if updated is None:
            return _unpersisted(organization_id, ticket_id, provider)
        return updated


def _unpersisted(
    organization_id: uuid.UUID, ticket_id: uuid.UUID, provider: str
) -> NotificationDelivery:
    """A delivery record for a row we could not read back.

    Reports `FAILED`, never `DELIVERED`: the caller uses this value to decide
    whether the assistant may say a human was alerted, so the safe direction
    when storage is behaving unexpectedly is to under-claim."""
    now = datetime.now(timezone.utc)
    return NotificationDelivery(
        id=uuid.uuid4(),
        organization_id=organization_id,
        ticket_id=ticket_id,
        channel=None,
        provider=provider,
        status=DeliveryStatus.FAILED,
        attempts=0,
        error_code="delivery_record_unavailable",
        delivered_at=None,
        created_at=now,
        updated_at=now,
    )


def _to_entity(model: EmergencyNotificationDeliveryModel) -> NotificationDelivery:
    return NotificationDelivery(
        id=model.id,
        organization_id=model.organization_id,
        ticket_id=model.emergency_ticket_id,
        channel=model.channel,
        provider=model.provider,
        status=model.status,
        attempts=model.attempts,
        error_code=model.error_code,
        delivered_at=model.delivered_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )
