"""Customer/CRM endpoints (Milestone 7): the unified customer record view
staff use to look up a caller's history across Emergency Dispatch and
Appointment Management. Every route derives its organization scope from
`current_user.organization_id` — never a client-supplied id — same
convention as every other module.

Listing/viewing customer history only requires `customers:read` (every
role holds it, same as `appointments:read`); creating/editing a customer
by hand requires `customers:manage` (Owner/Admin only)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_customer_service, require_permission
from app.application.schemas.appointment import AppointmentResponse
from app.application.schemas.customer import (
    CreateCustomerRequest,
    CustomerHistoryResponse,
    CustomerResponse,
    UpdateCustomerRequest,
)
from app.application.schemas.dispatch import EmergencyTicketResponse
from app.application.services.customer_service import CustomerService
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User

router = APIRouter(prefix="/customers", tags=["customers"])

_read_user = require_permission(Permissions.CUSTOMERS_READ)
_manage_user = require_permission(Permissions.CUSTOMERS_MANAGE)


@router.get("", response_model=list[CustomerResponse])
async def list_customers(
    search: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(_read_user),
    service: CustomerService = Depends(get_customer_service),
) -> list[CustomerResponse]:
    customers = await service.list_customers(
        user.organization_id, search=search, limit=limit, offset=offset
    )
    return [CustomerResponse.model_validate(c) for c in customers]


@router.get("/{customer_id}", response_model=CustomerHistoryResponse)
async def get_customer_history(
    customer_id: uuid.UUID,
    user: User = Depends(_read_user),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerHistoryResponse:
    history = await service.get_customer_history(user.organization_id, customer_id)
    return CustomerHistoryResponse(
        customer=CustomerResponse.model_validate(history.customer),
        tickets=[EmergencyTicketResponse.model_validate(t) for t in history.tickets],
        appointments=[AppointmentResponse.model_validate(a) for a in history.appointments],
    )


@router.post("", response_model=CustomerResponse, status_code=201)
async def create_customer(
    payload: CreateCustomerRequest,
    user: User = Depends(_manage_user),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    customer = await service.create_customer(
        user.organization_id,
        full_name=payload.full_name,
        phone_number=payload.phone_number,
        email=payload.email,
        address=payload.address,
        notes=payload.notes,
    )
    return CustomerResponse.model_validate(customer)


@router.patch("/{customer_id}", response_model=CustomerResponse)
async def update_customer(
    customer_id: uuid.UUID,
    payload: UpdateCustomerRequest,
    user: User = Depends(_manage_user),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerResponse:
    customer = await service.update_customer(
        user.organization_id,
        customer_id,
        full_name=payload.full_name,
        phone_number=payload.phone_number,
        email=payload.email,
        address=payload.address,
        notes=payload.notes,
    )
    return CustomerResponse.model_validate(customer)
