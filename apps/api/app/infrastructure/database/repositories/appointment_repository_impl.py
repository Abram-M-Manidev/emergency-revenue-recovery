from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.analytics import BucketCount, DailyRevenue
from app.domain.entities.appointment import Appointment, AppointmentStatus
from app.domain.exceptions import EntityNotFoundError
from app.domain.repositories.appointment_repository import AppointmentRepository
from app.infrastructure.database.models.appointment import AppointmentModel

# Only reachable by a SCHEDULED row with a NULL duration, which
# `schedule()` makes unrepresentable. See `count_overlapping`.
_FALLBACK_DURATION_MINUTES = 60


def _to_entity(model: AppointmentModel) -> Appointment:
    return Appointment(
        id=model.id,
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        matched_service_id=model.matched_service_id,
        status=model.status,
        customer_name=model.customer_name,
        customer_phone=model.customer_phone,
        customer_address=model.customer_address,
        summary=model.summary,
        scheduled_start_at=model.scheduled_start_at,
        duration_minutes=model.duration_minutes,
        assigned_technician_user_id=model.assigned_technician_user_id,
        assigned_at=model.assigned_at,
        closed_at=model.closed_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
        customer_id=model.customer_id,
        actual_value=model.actual_value,
    )


class SqlAlchemyAppointmentRepository(AppointmentRepository):
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
        duration_minutes: int | None,
    ) -> Appointment:
        model = AppointmentModel(
            organization_id=organization_id,
            conversation_id=conversation_id,
            matched_service_id=matched_service_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_address=customer_address,
            summary=summary,
            duration_minutes=duration_minutes,
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
        self, organization_id: uuid.UUID, appointment_id: uuid.UUID
    ) -> Appointment | None:
        result = await self._session.execute(
            select(AppointmentModel).where(
                AppointmentModel.id == appointment_id,
                AppointmentModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def get_by_conversation_id(self, conversation_id: uuid.UUID) -> Appointment | None:
        result = await self._session.execute(
            select(AppointmentModel).where(AppointmentModel.conversation_id == conversation_id)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        status: AppointmentStatus | None = None,
        limit: int,
        offset: int,
    ) -> list[Appointment]:
        query = select(AppointmentModel).where(
            AppointmentModel.organization_id == organization_id
        )
        if status is not None:
            query = query.where(AppointmentModel.status == status)
        query = (
            query.order_by(
                AppointmentModel.scheduled_start_at.asc().nulls_last(),
                AppointmentModel.created_at.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(query)
        return [_to_entity(model) for model in result.scalars().all()]

    async def schedule(
        self,
        organization_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        scheduled_start_at: datetime,
        duration_minutes: int,
        technician_user_id: uuid.UUID | None,
        assigned_at: datetime,
    ) -> Appointment:
        result = await self._session.execute(
            select(AppointmentModel).where(
                AppointmentModel.id == appointment_id,
                AppointmentModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one()
        model.scheduled_start_at = scheduled_start_at
        model.duration_minutes = duration_minutes
        model.assigned_technician_user_id = technician_user_id
        model.assigned_at = assigned_at
        model.status = AppointmentStatus.SCHEDULED
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def update_status(
        self,
        organization_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        status: AppointmentStatus,
        closed_at: datetime | None = None,
        actual_value: Decimal | None = None,
    ) -> Appointment:
        result = await self._session.execute(
            select(AppointmentModel).where(
                AppointmentModel.id == appointment_id,
                AppointmentModel.organization_id == organization_id,
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
        self, organization_id: uuid.UUID, appointment_id: uuid.UUID, *, customer_id: uuid.UUID
    ) -> Appointment:
        result = await self._session.execute(
            select(AppointmentModel).where(
                AppointmentModel.id == appointment_id,
                AppointmentModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one()
        model.customer_id = customer_id
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def backfill_contact_details(
        self,
        organization_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        customer_name: str | None = None,
        customer_phone: str | None = None,
        customer_address: str | None = None,
    ) -> Appointment:
        result = await self._session.execute(
            select(AppointmentModel).where(
                AppointmentModel.id == appointment_id,
                AppointmentModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            # Cross-tenant id (or a deleted appointment): surfaced as a
            # domain error rather than a raw NoResultFound, so the API
            # layer's existing `except DomainError` around the sync calls
            # logs and continues instead of failing a live call turn. Same
            # reasoning as the emergency-ticket equivalent.
            raise EntityNotFoundError("Appointment", str(appointment_id))

        if customer_name is not None:
            model.customer_name = customer_name
        if customer_phone is not None:
            model.customer_phone = customer_phone
        if customer_address is not None:
            model.customer_address = customer_address

        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def list_scheduled_in_range(
        self, organization_id: uuid.UUID, *, start_at: datetime, end_at: datetime
    ) -> list[Appointment]:
        occupied_end = AppointmentModel.scheduled_start_at + func.make_interval(
            0,
            0,
            0,
            0,
            0,
            func.coalesce(AppointmentModel.duration_minutes, _FALLBACK_DURATION_MINUTES),
        )
        result = await self._session.execute(
            select(AppointmentModel)
            .where(
                AppointmentModel.organization_id == organization_id,
                AppointmentModel.status == AppointmentStatus.SCHEDULED,
                AppointmentModel.scheduled_start_at.is_not(None),
                AppointmentModel.scheduled_start_at < end_at,
                occupied_end > start_at,
            )
            .order_by(AppointmentModel.scheduled_start_at.asc())
        )
        return [_to_entity(model) for model in result.scalars().all()]

    async def count_overlapping(
        self,
        organization_id: uuid.UUID,
        *,
        start_at: datetime,
        end_at: datetime,
        exclude_appointment_id: uuid.UUID | None = None,
    ) -> int:
        # `make_interval(0, 0, 0, 0, 0, mins)` rather than a Python
        # timedelta: each row's end is `scheduled_start_at + its own
        # duration_minutes`, so the arithmetic has to happen in SQL. The
        # positional zeros are make_interval's years/months/weeks/days/hours.
        #
        # `coalesce(duration_minutes, _FALLBACK_DURATION_MINUTES)` is
        # defensive only — `schedule()` requires a duration, so a SCHEDULED
        # row without one should not exist. Treating such a row as
        # zero-length would silently make it invisible to every conflict
        # check, which is the one outcome worth ruling out here.
        occupied_end = AppointmentModel.scheduled_start_at + func.make_interval(
            0,
            0,
            0,
            0,
            0,
            func.coalesce(AppointmentModel.duration_minutes, _FALLBACK_DURATION_MINUTES),
        )
        query = (
            select(func.count())
            .select_from(AppointmentModel)
            .where(
                AppointmentModel.organization_id == organization_id,
                AppointmentModel.status == AppointmentStatus.SCHEDULED,
                AppointmentModel.scheduled_start_at.is_not(None),
                # Half-open overlap: touching endpoints do not collide, so
                # back-to-back appointments remain bookable.
                AppointmentModel.scheduled_start_at < end_at,
                occupied_end > start_at,
            )
        )
        if exclude_appointment_id is not None:
            query = query.where(AppointmentModel.id != exclude_appointment_id)
        return (await self._session.execute(query)).scalar_one()

    async def list_by_customer_id(
        self, organization_id: uuid.UUID, customer_id: uuid.UUID
    ) -> list[Appointment]:
        result = await self._session.execute(
            select(AppointmentModel)
            .where(
                AppointmentModel.organization_id == organization_id,
                AppointmentModel.customer_id == customer_id,
            )
            .order_by(AppointmentModel.created_at.desc())
        )
        return [_to_entity(model) for model in result.scalars().all()]

    # --- Analytics (Milestone 8) aggregate queries ---

    async def count_created_in_range(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> int:
        query = select(func.count()).select_from(AppointmentModel).where(
            AppointmentModel.organization_id == organization_id,
            AppointmentModel.created_at < end,
        )
        if start is not None:
            query = query.where(AppointmentModel.created_at >= start)
        return (await self._session.execute(query)).scalar_one()

    async def count_closed_in_range(
        self,
        organization_id: uuid.UUID,
        *,
        status: AppointmentStatus,
        start: datetime | None,
        end: datetime,
    ) -> int:
        query = select(func.count()).select_from(AppointmentModel).where(
            AppointmentModel.organization_id == organization_id,
            AppointmentModel.status == status,
            AppointmentModel.closed_at.is_not(None),
            AppointmentModel.closed_at < end,
        )
        if start is not None:
            query = query.where(AppointmentModel.closed_at >= start)
        return (await self._session.execute(query)).scalar_one()

    async def sum_actual_value_in_range(
        self,
        organization_id: uuid.UUID,
        *,
        status: AppointmentStatus,
        start: datetime | None,
        end: datetime,
    ) -> Decimal:
        query = select(func.coalesce(func.sum(AppointmentModel.actual_value), 0)).where(
            AppointmentModel.organization_id == organization_id,
            AppointmentModel.status == status,
            AppointmentModel.closed_at.is_not(None),
            AppointmentModel.closed_at < end,
        )
        if start is not None:
            query = query.where(AppointmentModel.closed_at >= start)
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
        status: AppointmentStatus,
        start: datetime | None,
        end: datetime,
    ) -> list[DailyRevenue]:
        day = func.date_trunc("day", AppointmentModel.closed_at).label("day")
        query = (
            select(day, func.coalesce(func.sum(AppointmentModel.actual_value), 0))
            .where(
                AppointmentModel.organization_id == organization_id,
                AppointmentModel.status == status,
                AppointmentModel.closed_at.is_not(None),
                AppointmentModel.actual_value.is_not(None),
                AppointmentModel.closed_at < end,
            )
            .group_by(day)
            .order_by(day)
        )
        if start is not None:
            query = query.where(AppointmentModel.closed_at >= start)
        rows = (await self._session.execute(query)).all()
        return [DailyRevenue(day=row[0].date(), amount=Decimal(row[1])) for row in rows]

    async def status_breakdown_in_range(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> list[BucketCount]:
        query = (
            select(AppointmentModel.status, func.count())
            .where(
                AppointmentModel.organization_id == organization_id,
                AppointmentModel.created_at < end,
            )
            .group_by(AppointmentModel.status)
        )
        if start is not None:
            query = query.where(AppointmentModel.created_at >= start)
        rows = (await self._session.execute(query)).all()
        return [BucketCount(label=row[0].value, count=row[1]) for row in rows]
