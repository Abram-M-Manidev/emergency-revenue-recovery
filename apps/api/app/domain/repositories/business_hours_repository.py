from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, time

from app.domain.entities.business_hours import HoursException, WeeklyHours


@dataclass(frozen=True, slots=True)
class WeeklyHoursInput:
    day_of_week: int
    is_closed: bool
    open_time: time | None
    close_time: time | None


class BusinessHoursRepository(ABC):
    """Owns both the weekly recurring schedule and its one-off date
    exceptions (holidays/closures) — a single domain concept ('the org's
    operating hours') that is always read and edited together."""

    @abstractmethod
    async def get_weekly(self, organization_id: uuid.UUID) -> list[WeeklyHours]: ...

    @abstractmethod
    async def replace_weekly(
        self, organization_id: uuid.UUID, entries: list[WeeklyHoursInput]
    ) -> list[WeeklyHours]:
        """Replace the full weekly schedule (all 7 days) in one operation,
        so a caller can never leave the week in a partially-updated state."""
        ...

    @abstractmethod
    async def list_exceptions(self, organization_id: uuid.UUID) -> list[HoursException]: ...

    @abstractmethod
    async def add_exception(
        self,
        *,
        organization_id: uuid.UUID,
        exception_date: date,
        is_closed: bool,
        open_time: time | None,
        close_time: time | None,
        label: str | None,
    ) -> HoursException: ...

    @abstractmethod
    async def delete_exception(self, organization_id: uuid.UUID, exception_id: uuid.UUID) -> bool:
        """Returns True if a row was deleted, False if no matching exception
        exists for this organization."""
        ...
