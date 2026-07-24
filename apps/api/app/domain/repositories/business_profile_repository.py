from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.business_profile import BusinessProfile, BusinessType


class BusinessProfileRepository(ABC):
    @abstractmethod
    async def get_by_organization_id(self, organization_id: uuid.UUID) -> BusinessProfile | None: ...

    @abstractmethod
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
        """Create the profile if none exists for this organization, otherwise
        replace it in place."""
        ...
