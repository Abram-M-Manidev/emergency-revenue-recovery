"""Two related concepts, always read/edited together as one 'Hours' section:
weekly recurring hours (`WeeklyHours`, one row per weekday) and one-off
calendar overrides (`HoursException`, holidays/closures)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, time


@dataclass(frozen=True, slots=True)
class WeeklyHours:
    id: uuid.UUID
    organization_id: uuid.UUID
    day_of_week: int  # 0 = Monday ... 6 = Sunday
    is_closed: bool
    open_time: time | None
    close_time: time | None


@dataclass(frozen=True, slots=True)
class HoursException:
    id: uuid.UUID
    organization_id: uuid.UUID
    date: date
    is_closed: bool
    open_time: time | None
    close_time: time | None
    label: str | None
