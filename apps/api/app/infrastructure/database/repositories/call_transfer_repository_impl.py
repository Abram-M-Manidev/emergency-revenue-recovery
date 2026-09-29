"""SQLAlchemy implementations of the human-fallback repositories."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.call_transfer.attempt import CallTransferAttempt, TransferStatus
from app.domain.call_transfer.settings import CallTransferSettings
from app.domain.repositories.call_transfer_repository import (
    CallTransferAttemptRepository,
    CallTransferSettingsRepository,
)
from app.infrastructure.database.models.call_transfer import (
    CallTransferAttemptModel,
    OrganizationCallTransferSettingsModel,
)


def _settings_to_entity(model: OrganizationCallTransferSettingsModel) -> CallTransferSettings:
    return CallTransferSettings(
        organization_id=model.organization_id,
        business_hours_number=model.business_hours_number,
        after_hours_number=model.after_hours_number,
        transfer_emergencies=model.transfer_emergencies,
        is_enabled=model.is_enabled,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _attempt_to_entity(model: CallTransferAttemptModel) -> CallTransferAttempt:
    return CallTransferAttempt(
        id=model.id,
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        reason=model.reason,
        is_emergency=model.is_emergency,
        status=model.status,
        destination_kind=model.destination_kind,
        destination_number=model.destination_number,
        error_code=model.error_code,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyCallTransferSettingsRepository(CallTransferSettingsRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, organization_id: uuid.UUID) -> CallTransferSettings | None:
        result = await self._session.execute(
            select(OrganizationCallTransferSettingsModel).where(
                OrganizationCallTransferSettingsModel.organization_id == organization_id
            )
        )
        model = result.scalar_one_or_none()
        return _settings_to_entity(model) if model else None

    async def upsert(self, settings: CallTransferSettings) -> CallTransferSettings:
        # ON CONFLICT on the unique organization_id, so two admins saving at
        # once resolve to one row instead of a unique violation that would
        # poison the request's transaction.
        statement = pg_insert(OrganizationCallTransferSettingsModel).values(
            id=uuid.uuid4(),
            organization_id=settings.organization_id,
            business_hours_number=settings.business_hours_number,
            after_hours_number=settings.after_hours_number,
            transfer_emergencies=settings.transfer_emergencies,
            is_enabled=settings.is_enabled,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["organization_id"],
                set_={
                    "business_hours_number": statement.excluded.business_hours_number,
                    "after_hours_number": statement.excluded.after_hours_number,
                    "transfer_emergencies": statement.excluded.transfer_emergencies,
                    "is_enabled": statement.excluded.is_enabled,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
        await self._session.flush()
        # The upsert bypassed the identity map; drop any stale instance.
        self._session.expire_all()
        stored = await self.get(settings.organization_id)
        assert stored is not None  # just written, in this transaction
        return stored

    async def delete(self, organization_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(OrganizationCallTransferSettingsModel).where(
                OrganizationCallTransferSettingsModel.organization_id == organization_id
            )
        )
        await self._session.flush()


class SqlAlchemyCallTransferAttemptRepository(CallTransferAttemptRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, attempt: CallTransferAttempt) -> CallTransferAttempt:
        model = CallTransferAttemptModel(
            id=attempt.id,
            organization_id=attempt.organization_id,
            conversation_id=attempt.conversation_id,
            status=attempt.status,
            reason=attempt.reason,
            is_emergency=attempt.is_emergency,
            destination_kind=attempt.destination_kind,
            destination_number=attempt.destination_number,
            error_code=attempt.error_code,
        )
        self._session.add(model)
        # Flushed before the provider is called, so the attempt exists in the
        # turn's transaction even if the call is moved (and the stream torn
        # down) the moment the provider accepts.
        await self._session.flush()
        return _attempt_to_entity(model)

    async def save(self, attempt: CallTransferAttempt) -> CallTransferAttempt:
        await self._session.execute(
            update(CallTransferAttemptModel)
            .where(CallTransferAttemptModel.id == attempt.id)
            .values(
                status=attempt.status,
                destination_kind=attempt.destination_kind,
                destination_number=attempt.destination_number,
                error_code=attempt.error_code,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await self._session.flush()
        return attempt

    async def get_initiated_for_conversation(
        self, conversation_id: uuid.UUID
    ) -> CallTransferAttempt | None:
        result = await self._session.execute(
            select(CallTransferAttemptModel)
            .where(
                CallTransferAttemptModel.conversation_id == conversation_id,
                CallTransferAttemptModel.status == TransferStatus.INITIATED,
            )
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _attempt_to_entity(model) if model else None

    async def list_for_conversation(self, conversation_id: uuid.UUID) -> list[CallTransferAttempt]:
        result = await self._session.execute(
            select(CallTransferAttemptModel)
            .where(CallTransferAttemptModel.conversation_id == conversation_id)
            .order_by(CallTransferAttemptModel.created_at)
        )
        return [_attempt_to_entity(m) for m in result.scalars()]
