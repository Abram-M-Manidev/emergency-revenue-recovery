from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.customer import Customer
from app.domain.exceptions import EntityAlreadyExistsError
from app.domain.repositories.customer_repository import CustomerRepository
from app.infrastructure.database.models.customer import CustomerModel


def _to_entity(model: CustomerModel) -> Customer:
    return Customer(
        id=model.id,
        organization_id=model.organization_id,
        full_name=model.full_name,
        phone_number=model.phone_number,
        email=model.email,
        address=model.address,
        notes=model.notes,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyCustomerRepository(CustomerRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        full_name: str | None,
        phone_number: str,
        email: str | None = None,
        address: str | None = None,
        notes: str | None = None,
    ) -> Customer:
        model = CustomerModel(
            organization_id=organization_id,
            full_name=full_name,
            phone_number=phone_number,
            email=email,
            address=address,
            notes=notes,
        )
        try:
            # A SAVEPOINT (not the outer transaction) so a unique-constraint
            # conflict on (organization_id, phone_number) — a concurrent
            # retry of the same AI Brain turn, or a staff member manually
            # creating a customer that already exists — only rolls back
            # this insert, leaving the session usable to raise a clean
            # domain error instead of poisoning the whole request's
            # transaction. Same pattern as SqlAlchemyAppointmentRepository.
            async with self._session.begin_nested():
                self._session.add(model)
                await self._session.flush()
        except IntegrityError as exc:
            raise EntityAlreadyExistsError("Customer", "phone_number", phone_number) from exc
        await self._session.refresh(model)
        return _to_entity(model)

    # --- Analytics (Milestone 8) aggregate queries ---

    async def count_new_in_range(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> int:
        query = select(func.count()).select_from(CustomerModel).where(
            CustomerModel.organization_id == organization_id,
            CustomerModel.created_at < end,
        )
        if start is not None:
            query = query.where(CustomerModel.created_at >= start)
        return (await self._session.execute(query)).scalar_one()

    async def count_total(self, organization_id: uuid.UUID) -> int:
        query = select(func.count()).select_from(CustomerModel).where(
            CustomerModel.organization_id == organization_id
        )
        return (await self._session.execute(query)).scalar_one()

    async def get_by_id(
        self, organization_id: uuid.UUID, customer_id: uuid.UUID
    ) -> Customer | None:
        result = await self._session.execute(
            select(CustomerModel).where(
                CustomerModel.id == customer_id,
                CustomerModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def get_by_phone_number(
        self, organization_id: uuid.UUID, phone_number: str
    ) -> Customer | None:
        result = await self._session.execute(
            select(CustomerModel).where(
                CustomerModel.organization_id == organization_id,
                CustomerModel.phone_number == phone_number,
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        search: str | None = None,
        limit: int,
        offset: int,
    ) -> list[Customer]:
        query = select(CustomerModel).where(CustomerModel.organization_id == organization_id)
        if search:
            pattern = f"%{search}%"
            query = query.where(
                or_(
                    CustomerModel.full_name.ilike(pattern),
                    CustomerModel.phone_number.ilike(pattern),
                )
            )
        query = query.order_by(CustomerModel.created_at.desc()).offset(offset).limit(limit)
        result = await self._session.execute(query)
        return [_to_entity(model) for model in result.scalars().all()]

    async def update(
        self,
        customer_id: uuid.UUID,
        *,
        full_name: str | None,
        phone_number: str,
        email: str | None,
        address: str | None,
        notes: str | None,
    ) -> Customer:
        result = await self._session.execute(
            select(CustomerModel).where(CustomerModel.id == customer_id)
        )
        model = result.scalar_one()
        model.full_name = full_name
        model.phone_number = phone_number
        model.email = email
        model.address = address
        model.notes = notes
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError as exc:
            raise EntityAlreadyExistsError("Customer", "phone_number", phone_number) from exc
        await self._session.refresh(model)
        return _to_entity(model)
