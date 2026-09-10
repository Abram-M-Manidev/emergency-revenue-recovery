"""The first `AvailabilityProvider`: slots derived from the organization's
own data, with no external calendar involved.

Everything it needs already exists in the database and is already
maintained by staff through Business Knowledge (Milestone 2) and the
appointment queue (Milestone 6):

- `BusinessHours` + `HoursException` — when the business is open at all.
- `BusinessProfile.timezone` — what "Monday at 10" means for this business.
- `Service.default_duration_minutes` — how long the visit needs.
- Appointments already `SCHEDULED` — what is already taken.
- `TechnicianProfile` — how many jobs can run at once.

This is deliberately the *simplest correct* engine, not a dispatch
optimiser: it does not model drive time, skills, parts, or territory. Those
belong to a real field-service system, and the `AvailabilityProvider` port
exists precisely so one can replace this class without touching the tools,
`SchedulingService`, or the AI Brain.
"""

from __future__ import annotations

import uuid
from datetime import date as py_date
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.core.config import Settings
from app.domain.availability import AvailabilityProvider
from app.domain.entities.availability import (
    AvailabilityQuery,
    AvailabilityResult,
    AvailabilitySlot,
    SlotVerdict,
)
from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.domain.repositories.appointment_repository import AppointmentRepository
from app.domain.repositories.business_hours_repository import BusinessHoursRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.service_repository import ServiceRepository
from app.domain.repositories.technician_profile_repository import TechnicianProfileRepository

logger = structlog.get_logger("app.scheduling")

_UTC = timezone.utc


class DatabaseAvailabilityProvider(AvailabilityProvider):
    def __init__(
        self,
        *,
        appointment_repository: AppointmentRepository,
        business_hours_repository: BusinessHoursRepository,
        business_profile_repository: BusinessProfileRepository,
        service_repository: ServiceRepository,
        technician_profile_repository: TechnicianProfileRepository,
        settings: Settings,
        now: datetime | None = None,
    ) -> None:
        self._appointments = appointment_repository
        self._hours = business_hours_repository
        self._profiles = business_profile_repository
        self._services = service_repository
        self._technicians = technician_profile_repository
        self._settings = settings
        # Injectable clock: slot generation is entirely a function of "now",
        # so tests need to pin it rather than skew every fixture date.
        self._fixed_now = now

    def _now(self) -> datetime:
        return self._fixed_now or datetime.now(_UTC)

    # --- AvailabilityProvider ---

    async def find_slots(
        self, organization_id: uuid.UUID, query: AvailabilityQuery
    ) -> AvailabilityResult:
        zone = await self._timezone_for(organization_id)
        duration_minutes = await self._duration_for(organization_id, query.service_id)
        capacity = await self._capacity_for(organization_id)

        weekly = await self._hours.get_weekly(organization_id)
        exceptions = await self._hours.list_exceptions(organization_id)

        if not weekly:
            # Hours were never configured. Deliberately returns nothing
            # rather than inventing a 9-to-5: an assistant offering times the
            # business never agreed to is exactly the fabrication this whole
            # change exists to remove. `AppointmentService` makes the
            # opposite call for *staff* scheduling (it does not block on
            # missing setup), which is safe because a human chose that time.
            logger.info("availability_no_business_hours", organization_id=str(organization_id))
            return AvailabilityResult(slots=(), timezone=str(zone), duration_minutes=duration_minutes)

        limit = _bounded(
            query.limit, default=self._settings.SCHEDULING_MAX_SLOTS, low=1,
            high=self._settings.SCHEDULING_MAX_SLOTS,
        )
        days_to_search = _bounded(
            query.days_to_search, default=self._settings.SCHEDULING_DEFAULT_SEARCH_DAYS, low=1,
            high=self._settings.SCHEDULING_MAX_SEARCH_DAYS,
        )

        earliest_start = self._earliest_bookable(zone)
        first_day = query.preferred_date or earliest_start.astimezone(zone).date()
        last_day = first_day + timedelta(days=days_to_search)

        # One query for the whole search window, rather than one per
        # candidate slot. A week at 30-minute granularity produces well over
        # a hundred candidates, and asking the database about each
        # separately put that many sequential round-trips inside a live
        # call's request. The set of *booked* jobs in a week is small even
        # when the set of candidates is not, so this is the same answer far
        # more cheaply.
        booked = await self._appointments.list_scheduled_in_range(
            organization_id,
            start_at=_local(first_day, time(0, 0), zone),
            end_at=_local(last_day + timedelta(days=1), time(0, 0), zone),
        )
        occupied = [
            (
                appointment.scheduled_start_at,
                appointment.scheduled_start_at
                + timedelta(
                    minutes=appointment.duration_minutes
                    or self._settings.SCHEDULING_DEFAULT_DURATION_MINUTES
                ),
            )
            for appointment in booked
            if appointment.scheduled_start_at is not None
            # `!=` against None excludes nothing, so an unset query counts
            # exactly what it counted before.
            and appointment.id != query.exclude_appointment_id
        ]

        slots: list[AvailabilitySlot] = []
        for offset in range(days_to_search):
            if len(slots) >= limit:
                break
            day = first_day + timedelta(days=offset)
            for candidate in self._candidate_starts(
                day=day,
                zone=zone,
                weekly=weekly,
                exceptions=exceptions,
                duration_minutes=duration_minutes,
                earliest_time=query.earliest_time,
                latest_time=query.latest_time,
            ):
                if len(slots) >= limit:
                    break
                if candidate < earliest_start:
                    continue
                candidate_end = candidate + timedelta(minutes=duration_minutes)
                # Half-open, matching `count_overlapping`'s SQL exactly, so
                # a slot this search offers is a slot `verify_slot` will
                # accept — the two must never disagree or the assistant
                # offers times that then fail to book.
                taken = sum(
                    1
                    for booked_start, booked_end in occupied
                    if booked_start < candidate_end and booked_end > candidate
                )
                if taken >= capacity:
                    continue
                slots.append(
                    AvailabilitySlot(start_at=candidate, duration_minutes=duration_minutes)
                )

        logger.info(
            "availability_searched",
            organization_id=str(organization_id),
            days_searched=days_to_search,
            duration_minutes=duration_minutes,
            capacity=capacity,
            slots_found=len(slots),
        )
        return AvailabilityResult(
            slots=tuple(slots), timezone=str(zone), duration_minutes=duration_minutes
        )

    async def verify_slot(
        self,
        organization_id: uuid.UUID,
        *,
        start_at: datetime,
        duration_minutes: int,
        exclude_appointment_id: uuid.UUID | None = None,
    ) -> SlotVerdict:
        zone = await self._timezone_for(organization_id)

        if start_at < self._earliest_bookable(zone):
            return SlotVerdict.IN_THE_PAST

        weekly = await self._hours.get_weekly(organization_id)
        if weekly:
            # Same asymmetry as `find_slots`: an org with no configured hours
            # is not blocked from booking (a human may still know it is
            # fine), but it is never *offered* a slot in the first place.
            exceptions = await self._hours.list_exceptions(organization_id)
            if not self._within_open_hours(
                start_at=start_at,
                zone=zone,
                weekly=weekly,
                exceptions=exceptions,
                duration_minutes=duration_minutes,
            ):
                return SlotVerdict.OUTSIDE_BUSINESS_HOURS

        capacity = await self._capacity_for(organization_id)
        taken = await self._appointments.count_overlapping(
            organization_id,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=duration_minutes),
            exclude_appointment_id=exclude_appointment_id,
        )
        if taken >= capacity:
            return SlotVerdict.FULL
        return SlotVerdict.BOOKABLE

    # --- Resolution helpers ---

    async def _timezone_for(self, organization_id: uuid.UUID) -> ZoneInfo:
        profile = await self._profiles.get_by_organization_id(organization_id)
        if profile is None:
            return ZoneInfo("UTC")
        try:
            return ZoneInfo(profile.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            # A malformed timezone is org configuration this code cannot
            # fix, and failing the lookup would fail a live call. UTC at
            # least yields consistent, explainable times.
            logger.warning(
                "availability_invalid_timezone",
                organization_id=str(organization_id),
                configured=profile.timezone,
            )
            return ZoneInfo("UTC")

    async def _duration_for(
        self, organization_id: uuid.UUID, service_id: uuid.UUID | None
    ) -> int:
        if service_id is not None:
            service = await self._services.get_by_id(organization_id, service_id)
            if service is not None and service.default_duration_minutes:
                return service.default_duration_minutes
        return self._settings.SCHEDULING_DEFAULT_DURATION_MINUTES

    async def _capacity_for(self, organization_id: uuid.UUID) -> int:
        """How many appointments may overlap one slot.

        The roster is the source of truth when there is one: capacity is the
        number of technicians currently on call. When an organization has no
        technician profiles *at all* it has simply not modelled its workforce
        yet, and treating that as "capacity zero" would make the assistant
        say the business is fully booked forever — so a configured default
        applies instead. An org that has technicians but has taken them all
        off call is a different statement, and is honoured: nothing is
        offered."""
        all_technicians = await self._technicians.list_for_organization(organization_id)
        if not all_technicians:
            return max(1, self._settings.SCHEDULING_DEFAULT_CAPACITY)
        return sum(1 for technician in all_technicians if technician.is_on_call)

    def _earliest_bookable(self, zone: ZoneInfo) -> datetime:
        """No slot may start sooner than the configured lead time — a caller
        must not be able to book a technician for four minutes from now."""
        return self._now() + timedelta(minutes=self._settings.SCHEDULING_MIN_LEAD_MINUTES)

    # --- Hours arithmetic ---

    def _open_window(
        self,
        *,
        day: py_date,
        weekly: list[WeeklyHours],
        exceptions: list[HoursException],
    ) -> tuple[time, time] | None:
        """The (open, close) local times for one calendar day, or None when
        closed. A dated exception always wins over the weekly schedule —
        that is the entire point of an exception."""
        exception = next((e for e in exceptions if e.date == day), None)
        if exception is not None:
            if exception.is_closed or exception.open_time is None or exception.close_time is None:
                return None
            return exception.open_time, exception.close_time

        hours = next((w for w in weekly if w.day_of_week == day.weekday()), None)
        if hours is None or hours.is_closed or hours.open_time is None or hours.close_time is None:
            return None
        return hours.open_time, hours.close_time

    def _candidate_starts(
        self,
        *,
        day: py_date,
        zone: ZoneInfo,
        weekly: list[WeeklyHours],
        exceptions: list[HoursException],
        duration_minutes: int,
        earliest_time: time | None,
        latest_time: time | None,
    ) -> list[datetime]:
        window = self._open_window(day=day, weekly=weekly, exceptions=exceptions)
        if window is None:
            return []
        open_time, close_time = window

        # The visit must *finish* by closing time, so the last legal start is
        # close minus the duration — not close itself.
        day_open = _local(day, open_time, zone)
        day_close = _local(day, close_time, zone)
        last_start = day_close - timedelta(minutes=duration_minutes)
        if last_start < day_open:
            # The open window is shorter than the job. Nothing on this day
            # can hold it, which is a legitimate answer, not an error.
            return []

        if earliest_time is not None:
            day_open = max(day_open, _local(day, earliest_time, zone))
        if latest_time is not None:
            last_start = min(last_start, _local(day, latest_time, zone))

        granularity = timedelta(minutes=self._settings.SCHEDULING_SLOT_GRANULARITY_MINUTES)
        starts: list[datetime] = []
        cursor = _round_up(day_open, granularity, zone)
        while cursor <= last_start:
            starts.append(cursor.astimezone(_UTC))
            cursor += granularity
        return starts

    def _within_open_hours(
        self,
        *,
        start_at: datetime,
        zone: ZoneInfo,
        weekly: list[WeeklyHours],
        exceptions: list[HoursException],
        duration_minutes: int,
    ) -> bool:
        local_start = start_at.astimezone(zone)
        window = self._open_window(day=local_start.date(), weekly=weekly, exceptions=exceptions)
        if window is None:
            return False
        open_time, close_time = window
        day_open = _local(local_start.date(), open_time, zone)
        day_close = _local(local_start.date(), close_time, zone)
        local_end = local_start + timedelta(minutes=duration_minutes)
        return day_open <= local_start and local_end <= day_close


def _local(day: py_date, at: time, zone: ZoneInfo) -> datetime:
    """A wall-clock time in the org's zone, as an aware instant.

    `replace(tzinfo=...)` rather than `localize`-style conversion: ZoneInfo
    resolves the offset (including DST) from the wall-clock value itself,
    which is exactly the semantics wanted here — "9am local" means 9am
    local on both sides of a DST change."""
    return datetime.combine(day, at).replace(tzinfo=zone)


def _round_up(value: datetime, step: timedelta, zone: ZoneInfo) -> datetime:
    """Rounds a local time up onto the slot grid, so offered times land on
    tidy boundaries (10:00, 10:30) rather than wherever opening time happens
    to fall."""
    local = value.astimezone(zone)
    step_seconds = int(step.total_seconds())
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((local - midnight).total_seconds())
    remainder = elapsed % step_seconds
    if remainder == 0:
        return local
    return local + timedelta(seconds=step_seconds - remainder)


def _bounded(value: int | None, *, default: int, low: int, high: int) -> int:
    """Clamps a model-supplied number into a sane range. The model can emit
    anything the JSON Schema allows (`days_to_search: 3650`), and a search
    that wide is a slow query on a live call, not a useful answer."""
    if value is None:
        return default
    return max(low, min(high, value))
