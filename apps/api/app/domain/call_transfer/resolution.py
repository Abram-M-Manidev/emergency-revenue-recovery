"""Which number a call goes to right now — pure, so every branch is tested
without a clock, a database or a phone line.

Business hours: the office. After hours: the on-call line. Two deliberate
fallbacks, and one deliberate refusal:

- In hours with no office number, the on-call number is used: a caller who
  asked for a person reaching the on-call line is better than reaching no
  one.
- Hours never configured: treated as in-hours for the same reason — the
  office if there is one, otherwise on-call. A tenant that has not entered
  its hours has not told us it is closed.
- After hours with no on-call number there is NO fallback to the office:
  the office is closed, and ringing an empty office is not a human exit —
  it is a longer way of reaching nobody.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.domain.call_transfer.attempt import DestinationKind, TransferFailure
from app.domain.call_transfer.settings import CallTransferSettings
from app.domain.entities.business_hours import HoursException, WeeklyHours


def is_open_at(
    local_dt: datetime,
    weekly: Sequence[WeeklyHours],
    exceptions: Sequence[HoursException],
) -> bool | None:
    """Whether the business is open at this local wall-clock time.

    None when no weekly hours are configured at all — "unknown", which the
    resolver treats differently from "closed". Same semantics as the
    appointment service's business-hours check: a dated exception overrides
    the weekly row, and the window is [open, close)."""
    if not weekly:
        return None
    exception = next((e for e in exceptions if e.date == local_dt.date()), None)
    if exception is not None:
        if exception.is_closed or exception.open_time is None or exception.close_time is None:
            return False
        return exception.open_time <= local_dt.time() < exception.close_time
    day = next((w for w in weekly if w.day_of_week == local_dt.weekday()), None)
    if day is None or day.is_closed or day.open_time is None or day.close_time is None:
        return False
    return day.open_time <= local_dt.time() < day.close_time


@dataclass(frozen=True, slots=True)
class ResolvedDestination:
    kind: DestinationKind
    number: str


@dataclass(frozen=True, slots=True)
class NoDestination:
    error_code: str


def resolve_destination(
    settings: CallTransferSettings | None,
    *,
    open_now: bool | None,
    ai_line_numbers: frozenset[str],
) -> ResolvedDestination | NoDestination:
    if settings is None:
        return NoDestination(TransferFailure.NOT_CONFIGURED)
    if not settings.is_enabled:
        return NoDestination(TransferFailure.DISABLED)

    if open_now is False:
        candidates = [(DestinationKind.AFTER_HOURS, settings.after_hours_number)]
    else:  # open, or hours unknown
        candidates = [
            (DestinationKind.BUSINESS_HOURS, settings.business_hours_number),
            (DestinationKind.AFTER_HOURS, settings.after_hours_number),
        ]
    for kind, number in candidates:
        if not number:
            continue
        # Re-checked at call time, not only when saved: a voice line can be
        # mapped to a number AFTER the transfer settings were written.
        if number in ai_line_numbers:
            return NoDestination(TransferFailure.DESTINATION_IS_AI_LINE)
        return ResolvedDestination(kind, number)
    return NoDestination(TransferFailure.NO_DESTINATION_NOW)
