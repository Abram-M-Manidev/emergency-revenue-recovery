from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.business_profile import BusinessProfile, BusinessType
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.infrastructure.database.models.business_profile import BusinessProfileModel


def _to_entity(model: BusinessProfileModel) -> BusinessProfile:
    return BusinessProfile(
        id=model.id,
        organization_id=model.organization_id,
        business_type=BusinessType(model.business_type),
        display_name=model.display_name,
        phone_number=model.phone_number,
        timezone=model.timezone,
        address_line1=model.address_line1,
        address_line2=model.address_line2,
        city=model.city,
        state=model.state,
        postal_code=model.postal_code,
        country=model.country,
        website=model.website,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyBusinessProfileRepository(BusinessProfileRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_organization_id(self, organization_id: uuid.UUID) -> BusinessProfile | None:
        result = await self._session.execute(
            select(BusinessProfileModel).where(
                BusinessProfileModel.organization_id == organization_id
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def upsert(
        self,
        *,
        organization_id: uuid.UUID,
        business_type: BusinessType,
        display_name: str,
        phone_number: str | None,
        timezone: str,
        address_line1: str | None,
        address_line2: str | None,
        city: str | None,
        state: str | None,
        postal_code: str | None,
        country: str,
        website: str | None,
    ) -> BusinessProfile:
        result = await self._session.execute(
            select(BusinessProfileModel).where(
                BusinessProfileModel.organization_id == organization_id
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            model = BusinessProfileModel(organization_id=organization_id)
            self._session.add(model)

        model.business_type = business_type
        model.display_name = display_name
        model.phone_number = phone_number
        model.timezone = timezone
        model.address_line1 = address_line1
        model.address_line2 = address_line2
        model.city = city
        model.state = state
        model.postal_code = postal_code
        model.country = country
        model.website = website

        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)
