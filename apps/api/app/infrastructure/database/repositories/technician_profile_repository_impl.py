from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.technician_profile import TechnicianProfile
from app.domain.repositories.technician_profile_repository import TechnicianProfileRepository
from app.infrastructure.database.models.technician_profile import TechnicianProfileModel


def _to_entity(model: TechnicianProfileModel) -> TechnicianProfile:
    return TechnicianProfile(
        id=model.id,
        organization_id=model.organization_id,
        user_id=model.user_id,
        phone_number=model.phone_number,
        is_on_call=model.is_on_call,
        notes=model.notes,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyTechnicianProfileRepository(TechnicianProfileRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        phone_number: str,
        is_on_call: bool = True,
        notes: str | None = None,
    ) -> TechnicianProfile:
        model = TechnicianProfileModel(
            organization_id=organization_id,
            user_id=user_id,
            phone_number=phone_number,
            is_on_call=is_on_call,
            notes=notes,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def get_by_user_id(self, user_id: uuid.UUID) -> TechnicianProfile | None:
        result = await self._session.execute(
            select(TechnicianProfileModel).where(TechnicianProfileModel.user_id == user_id)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def list_for_organization(
        self, organization_id: uuid.UUID, *, on_call_only: bool = False
    ) -> list[TechnicianProfile]:
        query = select(TechnicianProfileModel).where(
            TechnicianProfileModel.organization_id == organization_id
        )
        if on_call_only:
            query = query.where(TechnicianProfileModel.is_on_call.is_(True))
        result = await self._session.execute(query)
        return [_to_entity(model) for model in result.scalars().all()]

    async def set_on_call(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, is_on_call: bool
    ) -> TechnicianProfile:
        result = await self._session.execute(
            select(TechnicianProfileModel).where(
                TechnicianProfileModel.user_id == user_id,
                TechnicianProfileModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one()
        model.is_on_call = is_on_call
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)
