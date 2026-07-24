from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.service import Service
from app.domain.exceptions import EntityAlreadyExistsError
from app.domain.repositories.service_repository import ServiceRepository
from app.infrastructure.database.models.service import ServiceModel


def _to_entity(model: ServiceModel) -> Service:
    return Service(
        id=model.id,
        organization_id=model.organization_id,
        name=model.name,
        description=model.description,
        category=model.category,
        is_emergency_eligible=model.is_emergency_eligible,
        is_active=model.is_active,
    )


class SqlAlchemyServiceRepository(ServiceRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, organization_id: uuid.UUID) -> list[Service]:
        result = await self._session.execute(
            select(ServiceModel)
            .where(ServiceModel.organization_id == organization_id)
            .order_by(ServiceModel.name)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        name: str,
        description: str | None,
        category: str | None,
        is_emergency_eligible: bool,
        is_active: bool,
    ) -> Service:
        model = ServiceModel(
            organization_id=organization_id,
            name=name,
            description=description,
            category=category,
            is_emergency_eligible=is_emergency_eligible,
            is_active=is_active,
        )
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise EntityAlreadyExistsError("Service", "name", name) from exc
        await self._session.refresh(model)
        return _to_entity(model)

    async def update(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        *,
        name: str,
        description: str | None,
        category: str | None,
        is_emergency_eligible: bool,
        is_active: bool,
    ) -> Service | None:
        result = await self._session.execute(
            select(ServiceModel).where(
                ServiceModel.id == service_id, ServiceModel.organization_id == organization_id
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None

        model.name = name
        model.description = description
        model.category = category
        model.is_emergency_eligible = is_emergency_eligible
        model.is_active = is_active

        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise EntityAlreadyExistsError("Service", "name", name) from exc
        await self._session.refresh(model)
        return _to_entity(model)

    async def delete(self, organization_id: uuid.UUID, service_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(ServiceModel).where(
                ServiceModel.id == service_id, ServiceModel.organization_id == organization_id
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            return False
        await self._session.delete(model)
        await self._session.flush()
        return True
