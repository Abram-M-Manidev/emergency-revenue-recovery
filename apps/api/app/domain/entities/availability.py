"""A bookable appointment slot, and the constraints a search for one runs
under.

Not a persisted entity — there is no `slots` table and deliberately so. A
slot is *derived* on demand from three things the organization already
owns: its `BusinessHours` (plus exceptions), the `default_duration_minutes`
of the matched `Service`, and the appointments already `SCHEDULED` against
it. Materialising a slot table would mean a second source of truth that
drifts the moment staff schedule something from the dashboard, and would
have to be regenerated whenever hours changed.

Times are tz-aware UTC throughout, matching every other datetime in this
codebase. The organization's local timezone is a *presentation* concern —
`AvailabilityProvider` returns UTC and the caller renders it — except for
the search constraints below, which are expressed in local terms because
that is how a caller speaks ("anytime from morning, 8 to evening").
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date as py_date
from datetime import datetime, timedelta, timezone
from datetime import time as py_time
from enum import Enum

_UTC = timezone.utc
_SLOT_ID_PREFIX = "slot_"
_SLOT_ID_TIME_FORMAT = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True, slots=True)
class AvailabilitySlot:
    """One offerable start time and how long the visit would run.

    `slot_id` is a readable, self-describing token rather than an opaque
    handle or a database key. Two reasons: there is no row to key against
    (see the module docstring), and booking re-derives and re-validates
    everything the token claims — business hours, whether the time has
    passed, and remaining capacity — before writing anything. The token is
    therefore a convenience for the model to quote back verbatim, never a
    capability: forging one grants nothing that stating the same date and
    time would not."""

    start_at: datetime
    duration_minutes: int

    @property
    def end_at(self) -> datetime:
        return self.start_at + timedelta(minutes=self.duration_minutes)

    @property
    def slot_id(self) -> str:
        stamp = self.start_at.astimezone(_UTC).strftime(_SLOT_ID_TIME_FORMAT)
        return f"{_SLOT_ID_PREFIX}{stamp}_{self.duration_minutes}"

    @staticmethod
    def parse_slot_id(slot_id: str) -> tuple[datetime, int] | None:
        """Decodes a `slot_id` back into `(start_at_utc, duration_minutes)`,
        or None if it is not one this system produced.

        Returning None rather than raising because the input is model
        output: a hallucinated or garbled token is an ordinary, expected
        case that must become a structured `INVALID_SLOT` tool result, not
        an exception that ends a live call."""
        if not slot_id or not slot_id.startswith(_SLOT_ID_PREFIX):
            return None
        body = slot_id[len(_SLOT_ID_PREFIX) :]
        stamp, separator, duration_text = body.rpartition("_")
        if not separator or not stamp:
            return None
        try:
            start_at = datetime.strptime(stamp, _SLOT_ID_TIME_FORMAT).replace(tzinfo=_UTC)
            duration_minutes = int(duration_text)
        except ValueError:
            return None
        if duration_minutes <= 0:
            return None
        return start_at, duration_minutes


class SlotVerdict(str, Enum):
    """Why a specific requested time can or cannot be booked.

    An enum rather than a bare bool because the three failure modes need
    genuinely different handling by the assistant: a time in the past or
    outside business hours means "offer a different day", while FULL means
    "this exact slot went while we were talking, here are others". Collapsing
    them would force the caller-facing layer to guess."""

    BOOKABLE = "bookable"
    IN_THE_PAST = "in_the_past"
    OUTSIDE_BUSINESS_HOURS = "outside_business_hours"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class AvailabilityQuery:
    """What the caller asked for, in the organization's own local terms.

    `preferred_date`/`earliest_time`/`latest_time` are local dates and
    clock times, not instants: a caller saying "Monday morning" means
    Monday morning where the business is, and resolving that to UTC needs
    the organization's timezone, which only the provider knows. All fields
    are optional — an unconstrained query means "the soonest slots you
    have"."""

    service_id: uuid.UUID | None = None
    preferred_date: py_date | None = None
    earliest_time: py_time | None = None
    latest_time: py_time | None = None
    days_to_search: int | None = None
    limit: int | None = None
    # One appointment to ignore when counting what is already taken. A
    # conversation re-checking availability after it has booked must still
    # see its own slot: on 2026-08-27 a caller's own 08:30 booking removed
    # 08:00-09:30 from the recovery search, and the assistant told them the
    # times it had just offered were "not actually available". Mirrors
    # `AvailabilityProvider.verify_slot`'s parameter of the same name, which
    # has always excluded self so a re-book does not conflict with itself.
    exclude_appointment_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class AvailabilityResult:
    """Slots plus the context needed to speak them aloud correctly.

    `timezone` and `duration_minutes` travel with the slots because the
    consumer is a voice assistant that has to say "Monday at 10 AM" — it
    cannot render a UTC instant without knowing which zone to render it in,
    and a mistake there books a caller into the wrong hour."""

    slots: tuple[AvailabilitySlot, ...]
    timezone: str
    duration_minutes: int
