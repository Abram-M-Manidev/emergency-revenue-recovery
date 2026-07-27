from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.analytics import DailyRevenue
from app.domain.entities.emergency_ticket import EmergencyTicket, TicketStatus
from app.domain.repositories.emergency_ticket_repository import EmergencyTicketRepository
from app.infrastructure.database.models.emergency_ticket import EmergencyTicketModel


def _to_entity(model: EmergencyTicketModel) -> EmergencyTicket:
    return EmergencyTicket(
        id=model.id,
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        matched_service_id=model.matched_service_id,
        status=model.status,
        customer_name=model.customer_name,
        customer_phone=model.customer_phone,
        customer_address=model.customer_address,
        summary=model.summary,
        assigned_technician_user_id=model.assigned_technician_user_id,
        assigned_at=model.assigned_at,
        closed_at=model.closed_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
        customer_id=model.customer_id,
        actual_value=model.actual_value,
    )


class SqlAlchemyEmergencyTicketRepository(EmergencyTicketRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        matched_service_id: uuid.UUID | None,
        customer_name: str | None,
        customer_phone: str | None,
        customer_address: str | None,
        summary: str,
    ) -> EmergencyTicket:
        model = EmergencyTicketModel(
            organization_id=organization_id,
            conversation_id=conversation_id,
            matched_service_id=matched_service_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_address=customer_address,
            summary=summary,
        )
        try:
            # A SAVEPOINT (not the outer transaction) so a unique-constraint
            # conflict on `conversation_id` — a concurrent retry of the same
            # AI Brain turn — only rolls back this insert, leaving the
            # session usable to fetch the row the other request just
            # created, instead of poisoning the whole request's transaction.
            async with self._session.begin_nested():
                self._session.add(model)
                await self._session.flush()
        except IntegrityError:
            existing = await self.get_by_conversation_id(conversation_id)
            if existing is not None:
                return existing
            raise
        await self._session.refresh(model)
        return _to_entity(model)

    async def get_by_id(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> EmergencyTicket | None:
        result = await self._session.execute(
            select(EmergencyTicketModel).where(
                EmergencyTicketModel.id == ticket_id,
                EmergencyTicketModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def get_by_conversation_id(self, conversation_id: uuid.UUID) -> EmergencyTicket | None:
        result = await self._session.execute(
            select(EmergencyTicketModel).where(
                EmergencyTicketModel.conversation_id == conversation_id
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        status: TicketStatus | None = None,
        limit: int,
        offset: int,
    ) -> list[EmergencyTicket]:
        query = select(EmergencyTicketModel).where(
            EmergencyTicketModel.organization_id == organization_id
        )
        if status is not None:
            query = query.where(EmergencyTicketModel.status == status)
        query = (
            query.order_by(EmergencyTicketModel.created_at.desc()).offset(offset).limit(limit)
        )
        result = await self._session.execute(query)
        return [_to_entity(model) for model in result.scalars().all()]

    async def assign(
        self,
        ticket_id: uuid.UUID,
        *,
        technician_user_id: uuid.UUID,
        assigned_at: datetime,
    ) -> EmergencyTicket:
        result = await self._session.execute(
            select(EmergencyTicketModel).where(EmergencyTicketModel.id == ticket_id)
        )
        model = result.scalar_one()
        model.assigned_technician_user_id = technician_user_id
        model.assigned_at = assigned_at
        model.status = TicketStatus.ASSIGNED
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def update_status(
        self,
        organization_id: uuid.UUID,
        ticket_id: uuid.UUID,
        *,
        status: TicketStatus,
        closed_at: datetime | None = None,
        actual_value: Decimal | None = None,
    ) -> EmergencyTicket:
        result = await self._session.execute(
            select(EmergencyTicketModel).where(
                EmergencyTicketModel.id == ticket_id,
                EmergencyTicketModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one()
        model.status = status
        if closed_at is not None:
            model.closed_at = closed_at
        if actual_value is not None:
            model.actual_value = actual_value
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def set_customer(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID, *, customer_id: uuid.UUID
    ) -> EmergencyTicket:
        result = await self._session.execute(
            select(EmergencyTicketModel).where(
                EmergencyTicketModel.id == ticket_id,
                EmergencyTicketModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one()
        model.customer_id = customer_id
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def list_by_customer_id(
        self, organization_id: uuid.UUID, customer_id: uuid.UUID
    ) -> list[EmergencyTicket]:
        result = await self._session.execute(
            select(EmergencyTicketModel)
            .where(
                EmergencyTicketModel.organization_id == organization_id,
                EmergencyTicketModel.customer_id == customer_id,
            )
            .order_by(EmergencyTicketModel.created_at.desc())
        )
        return [_to_entity(model) for model in result.scalars().all()]

    # --- Analytics (Milestone 8) aggregate queries ---

    async def count_created_in_range(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> int:
        query = select(func.count()).select_from(EmergencyTicketModel).where(
            EmergencyTicketModel.organization_id == organization_id,
            EmergencyTicketModel.created_at < end,
        )
        if start is not None:
            query = query.where(EmergencyTicketModel.created_at >= start)
        return (await self._session.execute(query)).scalar_one()

    async def count_closed_in_range(
        self,
        organization_id: uuid.UUID,
        *,
        status: TicketStatus,
        start: datetime | None,
        end: datetime,
    ) -> int:
        query = select(func.count()).select_from(EmergencyTicketModel).where(
            EmergencyTicketModel.organization_id == organization_id,
            EmergencyTicketModel.status == status,
            EmergencyTicketModel.closed_at.is_not(None),
            EmergencyTicketModel.closed_at < end,
        )
        if start is not None:
            query = query.where(EmergencyTicketModel.closed_at >= start)
        return (await self._session.execute(query)).scalar_one()

    async def sum_actual_value_in_range(
        self,
        organization_id: uuid.UUID,
        *,
        status: TicketStatus,
        start: datetime | None,
        end: datetime,
    ) -> Decimal:
        query = select(func.coalesce(func.sum(EmergencyTicketModel.actual_value), 0)).where(
            EmergencyTicketModel.organization_id == organization_id,
            EmergencyTicketModel.status == status,
            EmergencyTicketModel.closed_at.is_not(None),
            EmergencyTicketModel.closed_at < end,
        )
        if start is not None:
            query = query.where(EmergencyTicketModel.closed_at >= start)
        total = (await self._session.execute(query)).scalar_one()
        # `coalesce(..., 0)` guarantees this is never actually None at
        # runtime; mypy can't infer that through func.sum()'s generic
        # return type, so this satisfies the type checker without
        # changing behavior.
        return Decimal(total) if total is not None else Decimal(0)

    async def revenue_by_day(
        self,
        organization_id: uuid.UUID,
        *,
        status: TicketStatus,
        start: datetime | None,
        end: datetime,
    ) -> list[DailyRevenue]:
        day = func.date_trunc("day", EmergencyTicketModel.closed_at).label("day")
        query = (
            select(day, func.coalesce(func.sum(EmergencyTicketModel.actual_value), 0))
            .where(
                EmergencyTicketModel.organization_id == organization_id,
                EmergencyTicketModel.status == status,
                EmergencyTicketModel.closed_at.is_not(None),
                EmergencyTicketModel.actual_value.is_not(None),
                EmergencyTicketModel.closed_at < end,
            )
            .group_by(day)
            .order_by(day)
        )
        if start is not None:
            query = query.where(EmergencyTicketModel.closed_at >= start)
        rows = (await self._session.execute(query)).all()
        return [DailyRevenue(day=row[0].date(), amount=Decimal(row[1])) for row in rows]

    async def average_resolution_minutes(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> float | None:
        seconds_elapsed = func.extract(
            "epoch", EmergencyTicketModel.closed_at - EmergencyTicketModel.created_at
        )
        query = select(func.avg(seconds_elapsed / 60)).where(
            EmergencyTicketModel.organization_id == organization_id,
            EmergencyTicketModel.status == TicketStatus.RESOLVED,
            EmergencyTicketModel.closed_at.is_not(None),
            EmergencyTicketModel.closed_at < end,
        )
        if start is not None:
            query = query.where(EmergencyTicketModel.closed_at >= start)
        result = (await self._session.execute(query)).scalar_one()
        return float(result) if result is not None else None
