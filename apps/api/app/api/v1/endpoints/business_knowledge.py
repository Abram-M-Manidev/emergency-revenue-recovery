"""Business Knowledge endpoints: the per-organization data (profile, hours,
service areas, offered services, emergency keywords, FAQs) that later
milestones (AI Brain, Voice) will read from instead of hardcoded strings.

Every route derives its organization scope from `current_user.organization_id`
— never from a client-supplied id — and single-item mutations 404 rather
than 403 on a cross-tenant id, since `BusinessKnowledgeService` raises
`EntityNotFoundError` for both "doesn't exist" and "belongs to another org."
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from app.api.deps import get_business_knowledge_service, require_permission
from app.application.schemas.business_knowledge import (
    BusinessProfileResponse,
    CreateEmergencyKeywordRequest,
    CreateFAQRequest,
    CreateHoursExceptionRequest,
    CreateServiceAreaRequest,
    CreateServiceRequest,
    EmergencyKeywordResponse,
    FAQEntryResponse,
    HoursExceptionResponse,
    ServiceAreaResponse,
    ServiceResponse,
    UpdateBusinessProfileRequest,
    UpdateFAQRequest,
    UpdateServiceRequest,
    UpdateWeeklyHoursRequest,
    WeeklyHoursResponse,
)
from app.application.services.business_knowledge_service import BusinessKnowledgeService
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User
from app.domain.exceptions import EntityNotFoundError
from app.domain.repositories.business_hours_repository import WeeklyHoursInput

router = APIRouter(prefix="/business-knowledge", tags=["business-knowledge"])

_read_user = require_permission(Permissions.BUSINESS_KNOWLEDGE_READ)
_manage_user = require_permission(Permissions.BUSINESS_KNOWLEDGE_MANAGE)


# --- Business profile ---


@router.get("/profile", response_model=BusinessProfileResponse)
async def get_profile(
    user: User = Depends(_read_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> BusinessProfileResponse:
    profile = await service.get_profile(user.organization_id)
    if profile is None:
        raise EntityNotFoundError("BusinessProfile", str(user.organization_id))
    return BusinessProfileResponse.model_validate(profile)


@router.put("/profile", response_model=BusinessProfileResponse)
async def update_profile(
    payload: UpdateBusinessProfileRequest,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> BusinessProfileResponse:
    profile = await service.upsert_profile(user.organization_id, **payload.model_dump())
    return BusinessProfileResponse.model_validate(profile)


# --- Hours (weekly + exceptions) ---


@router.get("/hours", response_model=list[WeeklyHoursResponse])
async def get_weekly_hours(
    user: User = Depends(_read_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> list[WeeklyHoursResponse]:
    entries = await service.get_weekly_hours(user.organization_id)
    return [WeeklyHoursResponse.model_validate(entry) for entry in entries]


@router.put("/hours", response_model=list[WeeklyHoursResponse])
async def replace_weekly_hours(
    payload: UpdateWeeklyHoursRequest,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> list[WeeklyHoursResponse]:
    inputs = [
        WeeklyHoursInput(
            day_of_week=entry.day_of_week,
            is_closed=entry.is_closed,
            open_time=entry.open_time,
            close_time=entry.close_time,
        )
        for entry in payload.entries
    ]
    entries = await service.replace_weekly_hours(user.organization_id, inputs)
    return [WeeklyHoursResponse.model_validate(entry) for entry in entries]


@router.get("/hours/exceptions", response_model=list[HoursExceptionResponse])
async def list_hours_exceptions(
    user: User = Depends(_read_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> list[HoursExceptionResponse]:
    exceptions = await service.list_hours_exceptions(user.organization_id)
    return [HoursExceptionResponse.model_validate(exc) for exc in exceptions]


@router.post(
    "/hours/exceptions", response_model=HoursExceptionResponse, status_code=status.HTTP_201_CREATED
)
async def add_hours_exception(
    payload: CreateHoursExceptionRequest,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> HoursExceptionResponse:
    exception = await service.add_hours_exception(
        user.organization_id,
        exception_date=payload.date,
        is_closed=payload.is_closed,
        open_time=payload.open_time,
        close_time=payload.close_time,
        label=payload.label,
    )
    return HoursExceptionResponse.model_validate(exception)


@router.delete(
    "/hours/exceptions/{exception_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_hours_exception(
    exception_id: uuid.UUID,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> None:
    await service.delete_hours_exception(user.organization_id, exception_id)


# --- Service areas ---


@router.get("/service-areas", response_model=list[ServiceAreaResponse])
async def list_service_areas(
    user: User = Depends(_read_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> list[ServiceAreaResponse]:
    areas = await service.list_service_areas(user.organization_id)
    return [ServiceAreaResponse.model_validate(area) for area in areas]


@router.post(
    "/service-areas", response_model=ServiceAreaResponse, status_code=status.HTTP_201_CREATED
)
async def create_service_area(
    payload: CreateServiceAreaRequest,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> ServiceAreaResponse:
    area = await service.create_service_area(user.organization_id, **payload.model_dump())
    return ServiceAreaResponse.model_validate(area)


@router.delete(
    "/service-areas/{area_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_service_area(
    area_id: uuid.UUID,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> None:
    await service.delete_service_area(user.organization_id, area_id)


# --- Services offered ---


@router.get("/services", response_model=list[ServiceResponse])
async def list_services(
    user: User = Depends(_read_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> list[ServiceResponse]:
    services = await service.list_services(user.organization_id)
    return [ServiceResponse.model_validate(s) for s in services]


@router.post("/services", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_service(
    payload: CreateServiceRequest,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> ServiceResponse:
    created = await service.create_service(user.organization_id, **payload.model_dump())
    return ServiceResponse.model_validate(created)


@router.patch("/services/{service_id}", response_model=ServiceResponse)
async def update_service(
    service_id: uuid.UUID,
    payload: UpdateServiceRequest,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> ServiceResponse:
    updated = await service.update_service(
        user.organization_id, service_id, **payload.model_dump()
    )
    return ServiceResponse.model_validate(updated)


@router.delete("/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_service(
    service_id: uuid.UUID,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> None:
    await service.delete_service(user.organization_id, service_id)


# --- Emergency keywords ---


@router.get("/emergency-keywords", response_model=list[EmergencyKeywordResponse])
async def list_emergency_keywords(
    user: User = Depends(_read_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> list[EmergencyKeywordResponse]:
    keywords = await service.list_emergency_keywords(user.organization_id)
    return [EmergencyKeywordResponse.model_validate(k) for k in keywords]


@router.post(
    "/emergency-keywords",
    response_model=EmergencyKeywordResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_emergency_keyword(
    payload: CreateEmergencyKeywordRequest,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> EmergencyKeywordResponse:
    keyword = await service.create_emergency_keyword(user.organization_id, **payload.model_dump())
    return EmergencyKeywordResponse.model_validate(keyword)


@router.delete(
    "/emergency-keywords/{keyword_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_emergency_keyword(
    keyword_id: uuid.UUID,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> None:
    await service.delete_emergency_keyword(user.organization_id, keyword_id)


# --- FAQs ---


@router.get("/faqs", response_model=list[FAQEntryResponse])
async def list_faqs(
    user: User = Depends(_read_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> list[FAQEntryResponse]:
    faqs = await service.list_faqs(user.organization_id)
    return [FAQEntryResponse.model_validate(faq) for faq in faqs]


@router.post("/faqs", response_model=FAQEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_faq(
    payload: CreateFAQRequest,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> FAQEntryResponse:
    faq = await service.create_faq(user.organization_id, **payload.model_dump())
    return FAQEntryResponse.model_validate(faq)


@router.patch("/faqs/{faq_id}", response_model=FAQEntryResponse)
async def update_faq(
    faq_id: uuid.UUID,
    payload: UpdateFAQRequest,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> FAQEntryResponse:
    updated = await service.update_faq(user.organization_id, faq_id, **payload.model_dump())
    return FAQEntryResponse.model_validate(updated)


@router.delete("/faqs/{faq_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_faq(
    faq_id: uuid.UUID,
    user: User = Depends(_manage_user),
    service: BusinessKnowledgeService = Depends(get_business_knowledge_service),
) -> None:
    await service.delete_faq(user.organization_id, faq_id)
