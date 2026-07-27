from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.organization import Organization
from app.domain.repositories.organization_repository import OrganizationRepository
from app.infrastructure.database.models.organization import OrganizationModel


def _to_entity(model: OrganizationModel) -> Organization:
    return Organization(
        id=model.id,
        name=model.name,
        slug=model.slug,
        is_active=model.is_active,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyOrganizationRepository(OrganizationRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, organization_id: uuid.UUID) -> Organization | None:
        model = await self._session.get(OrganizationModel, organization_id)
        return _to_entity(model) if model else None

    async def get_by_slug(self, slug: str) -> Organization | None:
        result = await self._session.execute(
            select(OrganizationModel).where(OrganizationModel.slug == slug)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def create(self, *, name: str, slug: str) -> Organization:
        model = OrganizationModel(name=name, slug=slug)
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def update(
        self,
        organization_id: uuid.UUID,
        *,
        name: str | None = None,
        is_active: bool | None = None,
    ) -> Organization:
        result = await self._session.execute(
            select(OrganizationModel).where(OrganizationModel.id == organization_id)
        )
        model = result.scalar_one()
        if name is not None:
            model.name = name
        if is_active is not None:
            model.is_active = is_active
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)
