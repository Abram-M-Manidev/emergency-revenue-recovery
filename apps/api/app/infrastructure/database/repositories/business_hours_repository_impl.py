from __future__ import annotations

import uuid
from datetime import date, time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.domain.exceptions import EntityAlreadyExistsError
from app.domain.repositories.business_hours_repository import (
    BusinessHoursRepository,
    WeeklyHoursInput,
)
from app.infrastructure.database.models.business_hours import (
    HoursExceptionModel,
    WeeklyHoursModel,
)


def _weekly_to_entity(model: WeeklyHoursModel) -> WeeklyHours:
    return WeeklyHours(
        id=model.id,
        organization_id=model.organization_id,
        day_of_week=model.day_of_week,
        is_closed=model.is_closed,
        open_time=model.open_time,
        close_time=model.close_time,
    )


def _exception_to_entity(model: HoursExceptionModel) -> HoursException:
    return HoursException(
        id=model.id,
        organization_id=model.organization_id,
        date=model.exception_date,
        is_closed=model.is_closed,
        open_time=model.open_time,
        close_time=model.close_time,
        label=model.label,
    )


class SqlAlchemyBusinessHoursRepository(BusinessHoursRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_weekly(self, organization_id: uuid.UUID) -> list[WeeklyHours]:
        result = await self._session.execute(
            select(WeeklyHoursModel)
            .where(WeeklyHoursModel.organization_id == organization_id)
            .order_by(WeeklyHoursModel.day_of_week)
        )
        return [_weekly_to_entity(m) for m in result.scalars().all()]

    async def replace_weekly(
        self, organization_id: uuid.UUID, entries: list[WeeklyHoursInput]
    ) -> list[WeeklyHours]:
        result = await self._session.execute(
            select(WeeklyHoursModel).where(WeeklyHoursModel.organization_id == organization_id)
        )
        existing_by_day = {m.day_of_week: m for m in result.scalars().all()}

        updated: list[WeeklyHoursModel] = []
        for entry in entries:
            model = existing_by_day.get(entry.day_of_week)
            if model is None:
                model = WeeklyHoursModel(
                    organization_id=organization_id, day_of_week=entry.day_of_week
                )
                self._session.add(model)
            model.is_closed = entry.is_closed
            model.open_time = entry.open_time
            model.close_time = entry.close_time
            updated.append(model)

        await self._session.flush()
        for model in updated:
            await self._session.refresh(model)
        updated.sort(key=lambda m: m.day_of_week)
        return [_weekly_to_entity(m) for m in updated]

    async def list_exceptions(self, organization_id: uuid.UUID) -> list[HoursException]:
        result = await self._session.execute(
            select(HoursExceptionModel)
            .where(HoursExceptionModel.organization_id == organization_id)
            .order_by(HoursExceptionModel.exception_date)
        )
        return [_exception_to_entity(m) for m in result.scalars().all()]

    async def add_exception(
        self,
        *,
        organization_id: uuid.UUID,
        exception_date: date,
        is_closed: bool,
        open_time: time | None,
        close_time: time | None,
        label: str | None,
    ) -> HoursException:
        model = HoursExceptionModel(
            organization_id=organization_id,
            exception_date=exception_date,
            is_closed=is_closed,
            open_time=open_time,
            close_time=close_time,
            label=label,
        )
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise EntityAlreadyExistsError(
                "HoursException", "date", str(exception_date)
            ) from exc
        await self._session.refresh(model)
        return _exception_to_entity(model)

    async def delete_exception(self, organization_id: uuid.UUID, exception_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(HoursExceptionModel).where(
                HoursExceptionModel.id == exception_id,
                HoursExceptionModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            return False
        await self._session.delete(model)
        await self._session.flush()
        return True
