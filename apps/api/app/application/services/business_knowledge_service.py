"""Orchestrates every Business Knowledge sub-resource: the org's profile,
weekly hours + exceptions, service areas, offered services, emergency
keywords, and FAQs.

Every method takes `organization_id` explicitly and every repository call
is scoped by it — callers (the API layer) must always pass
`current_user.organization_id`, never a client-supplied org id. Single-item
mutations (update/delete) raise `EntityNotFoundError` when the repository
reports no match, which also covers a cross-tenant id: from the caller's
point of view, another org's row simply doesn't exist.
"""

from __future__ import annotations

import uuid
from datetime import date, time

from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.domain.entities.business_profile import BusinessProfile, BusinessType
from app.domain.entities.emergency_keyword import EmergencyKeyword
from app.domain.entities.faq_entry import FAQEntry
from app.domain.entities.service import Service
from app.domain.entities.service_area import ServiceArea
from app.domain.exceptions import EntityNotFoundError
from app.domain.repositories.business_hours_repository import (
    BusinessHoursRepository,
    WeeklyHoursInput,
)
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.emergency_keyword_repository import EmergencyKeywordRepository
from app.domain.repositories.faq_repository import FAQRepository
from app.domain.repositories.service_area_repository import ServiceAreaRepository
from app.domain.repositories.service_repository import ServiceRepository


class BusinessKnowledgeService:
    def __init__(
        self,
        *,
        business_profile_repository: BusinessProfileRepository,
        business_hours_repository: BusinessHoursRepository,
        service_area_repository: ServiceAreaRepository,
        service_repository: ServiceRepository,
        emergency_keyword_repository: EmergencyKeywordRepository,
        faq_repository: FAQRepository,
    ) -> None:
        self._profile = business_profile_repository
        self._hours = business_hours_repository
        self._service_areas = service_area_repository
        self._services = service_repository
        self._emergency_keywords = emergency_keyword_repository
        self._faqs = faq_repository

    # --- Business profile ---

    async def get_profile(self, organization_id: uuid.UUID) -> BusinessProfile | None:
        return await self._profile.get_by_organization_id(organization_id)

    async def upsert_profile(
        self,
        organization_id: uuid.UUID,
        *,
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
        return await self._profile.upsert(
            organization_id=organization_id,
            business_type=business_type,
            display_name=display_name,
            phone_number=phone_number,
            timezone=timezone,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            state=state,
            postal_code=postal_code,
            country=country,
            website=website,
        )

    # --- Hours (weekly + exceptions) ---

    async def get_weekly_hours(self, organization_id: uuid.UUID) -> list[WeeklyHours]:
        return await self._hours.get_weekly(organization_id)

    async def replace_weekly_hours(
        self, organization_id: uuid.UUID, entries: list[WeeklyHoursInput]
    ) -> list[WeeklyHours]:
        return await self._hours.replace_weekly(organization_id, entries)

    async def list_hours_exceptions(self, organization_id: uuid.UUID) -> list[HoursException]:
        return await self._hours.list_exceptions(organization_id)

    async def add_hours_exception(
        self,
        organization_id: uuid.UUID,
        *,
        exception_date: date,
        is_closed: bool,
        open_time: time | None,
        close_time: time | None,
        label: str | None,
    ) -> HoursException:
        return await self._hours.add_exception(
            organization_id=organization_id,
            exception_date=exception_date,
            is_closed=is_closed,
            open_time=open_time,
            close_time=close_time,
            label=label,
        )

    async def delete_hours_exception(self, organization_id: uuid.UUID, exception_id: uuid.UUID) -> None:
        deleted = await self._hours.delete_exception(organization_id, exception_id)
        if not deleted:
            raise EntityNotFoundError("HoursException", str(exception_id))

    # --- Service areas ---

    async def list_service_areas(self, organization_id: uuid.UUID) -> list[ServiceArea]:
        return await self._service_areas.list(organization_id)

    async def create_service_area(
        self,
        organization_id: uuid.UUID,
        *,
        label: str,
        postal_code: str | None,
        city: str | None,
        state: str | None,
    ) -> ServiceArea:
        return await self._service_areas.create(
            organization_id=organization_id,
            label=label,
            postal_code=postal_code,
            city=city,
            state=state,
        )

    async def delete_service_area(self, organization_id: uuid.UUID, area_id: uuid.UUID) -> None:
        deleted = await self._service_areas.delete(organization_id, area_id)
        if not deleted:
            raise EntityNotFoundError("ServiceArea", str(area_id))

    # --- Services offered ---

    async def list_services(self, organization_id: uuid.UUID) -> list[Service]:
        return await self._services.list(organization_id)

    async def create_service(
        self,
        organization_id: uuid.UUID,
        *,
        name: str,
        description: str | None,
        category: str | None,
        is_emergency_eligible: bool,
        is_active: bool,
        default_duration_minutes: int | None,
    ) -> Service:
        return await self._services.create(
            organization_id=organization_id,
            name=name,
            description=description,
            category=category,
            is_emergency_eligible=is_emergency_eligible,
            is_active=is_active,
            default_duration_minutes=default_duration_minutes,
        )

    async def update_service(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        *,
        name: str,
        description: str | None,
        category: str | None,
        is_emergency_eligible: bool,
        is_active: bool,
        default_duration_minutes: int | None,
    ) -> Service:
        updated = await self._services.update(
            organization_id,
            service_id,
            name=name,
            description=description,
            category=category,
            is_emergency_eligible=is_emergency_eligible,
            is_active=is_active,
            default_duration_minutes=default_duration_minutes,
        )
        if updated is None:
            raise EntityNotFoundError("Service", str(service_id))
        return updated

    async def delete_service(self, organization_id: uuid.UUID, service_id: uuid.UUID) -> None:
        deleted = await self._services.delete(organization_id, service_id)
        if not deleted:
            raise EntityNotFoundError("Service", str(service_id))

    # --- Emergency keywords ---

    async def list_emergency_keywords(self, organization_id: uuid.UUID) -> list[EmergencyKeyword]:
        return await self._emergency_keywords.list(organization_id)

    async def create_emergency_keyword(
        self, organization_id: uuid.UUID, *, phrase: str, notes: str | None
    ) -> EmergencyKeyword:
        return await self._emergency_keywords.create(
            organization_id=organization_id, phrase=phrase, notes=notes
        )

    async def delete_emergency_keyword(
        self, organization_id: uuid.UUID, keyword_id: uuid.UUID
    ) -> None:
        deleted = await self._emergency_keywords.delete(organization_id, keyword_id)
        if not deleted:
            raise EntityNotFoundError("EmergencyKeyword", str(keyword_id))

    # --- FAQs ---

    async def list_faqs(self, organization_id: uuid.UUID) -> list[FAQEntry]:
        return await self._faqs.list(organization_id)

    async def create_faq(
        self,
        organization_id: uuid.UUID,
        *,
        question: str,
        answer: str,
        category: str | None,
        is_active: bool,
    ) -> FAQEntry:
        return await self._faqs.create(
            organization_id=organization_id,
            question=question,
            answer=answer,
            category=category,
            is_active=is_active,
        )

    async def update_faq(
        self,
        organization_id: uuid.UUID,
        faq_id: uuid.UUID,
        *,
        question: str,
        answer: str,
        category: str | None,
        is_active: bool,
    ) -> FAQEntry:
        updated = await self._faqs.update(
            organization_id,
            faq_id,
            question=question,
            answer=answer,
            category=category,
            is_active=is_active,
        )
        if updated is None:
            raise EntityNotFoundError("FAQEntry", str(faq_id))
        return updated

    async def delete_faq(self, organization_id: uuid.UUID, faq_id: uuid.UUID) -> None:
        deleted = await self._faqs.delete(organization_id, faq_id)
        if not deleted:
            raise EntityNotFoundError("FAQEntry", str(faq_id))
