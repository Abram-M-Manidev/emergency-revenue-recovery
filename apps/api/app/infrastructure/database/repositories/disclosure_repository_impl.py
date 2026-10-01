"""SQLAlchemy implementation of the caller-disclosure settings repository."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.disclosure import DisclosurePolicy, DisclosureSettings
from app.domain.repositories.disclosure_repository import DisclosureSettingsRepository
from app.infrastructure.database.models.disclosure import OrganizationDisclosureSettingsModel


def _to_entity(model: OrganizationDisclosureSettingsModel) -> DisclosureSettings:
    return DisclosureSettings(
        organization_id=model.organization_id,
        policy=DisclosurePolicy(
            ai_disclosure=model.ai_disclosure_enabled,
            recording_notice=model.recording_notice_enabled,
        ),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyDisclosureSettingsRepository(DisclosureSettingsRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, organization_id: uuid.UUID) -> DisclosureSettings | None:
        model = (
            await self._session.execute(
                select(OrganizationDisclosureSettingsModel)
                .where(OrganizationDisclosureSettingsModel.organization_id == organization_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return _to_entity(model) if model else None

    async def upsert(
        self, organization_id: uuid.UUID, policy: DisclosurePolicy
    ) -> DisclosureSettings:
        # ON CONFLICT on the unique organization_id: two Owners saving at once
        # resolve to one row rather than a unique violation.
        statement = pg_insert(OrganizationDisclosureSettingsModel).values(
            id=uuid.uuid4(),
            organization_id=organization_id,
            ai_disclosure_enabled=policy.ai_disclosure,
            recording_notice_enabled=policy.recording_notice,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["organization_id"],
                set_={
                    "ai_disclosure_enabled": statement.excluded.ai_disclosure_enabled,
                    "recording_notice_enabled": statement.excluded.recording_notice_enabled,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
        await self._session.flush()
        stored = await self.get(organization_id)
        assert stored is not None  # just written, in this transaction
        return stored

    async def delete(self, organization_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(OrganizationDisclosureSettingsModel).where(
                OrganizationDisclosureSettingsModel.organization_id == organization_id
            )
        )
        await self._session.flush()
