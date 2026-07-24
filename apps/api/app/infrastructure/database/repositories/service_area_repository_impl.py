from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.service_area import ServiceArea
from app.domain.repositories.service_area_repository import ServiceAreaRepository
from app.infrastructure.database.models.service_area import ServiceAreaModel


def _to_entity(model: ServiceAreaModel) -> ServiceArea:
    return ServiceArea(
        id=model.id,
        organization_id=model.organization_id,
        label=model.label,
        postal_code=model.postal_code,
        city=model.city,
        state=model.state,
    )


class SqlAlchemyServiceAreaRepository(ServiceAreaRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, organization_id: uuid.UUID) -> list[ServiceArea]:
        result = await self._session.execute(
            select(ServiceAreaModel)
            .where(ServiceAreaModel.organization_id == organization_id)
            .order_by(ServiceAreaModel.label)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        label: str,
        postal_code: str | None,
        city: str | None,
        state: str | None,
    ) -> ServiceArea:
        model = ServiceAreaModel(
            organization_id=organization_id,
            label=label,
            postal_code=postal_code,
            city=city,
            state=state,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def delete(self, organization_id: uuid.UUID, area_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(ServiceAreaModel).where(
                ServiceAreaModel.id == area_id,
                ServiceAreaModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            return False
        await self._session.delete(model)
        await self._session.flush()
        return True
