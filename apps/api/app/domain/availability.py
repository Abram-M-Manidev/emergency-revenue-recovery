"""Scheduling port: where bookable time comes from.

Kept in `domain` alongside `locks.py` and `ai/provider.py`, and for the
same reason — zero framework or infrastructure imports, so the application
layer depends on the *capability* ("tell me when we could send someone")
rather than on how it is answered.

The only implementation today is
`infrastructure/scheduling/database_availability_provider.py`, which
derives slots from the organization's own `BusinessHours`, `Service`
durations, and already-`SCHEDULED` appointments. This interface is the seam
where a real external calendar goes later — Google Calendar, Microsoft 365,
ServiceTitan, Jobber — without any change to `SchedulingService`, the
tools, or the AI Brain. That is why `find_slots` returns whole slots rather
than, say, a list of busy intervals for the caller to subtract: an external
system knows its own availability rules (drive time, skills, dispatch
boards) and must be allowed to answer the question itself.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from app.domain.entities.availability import (
    AvailabilityQuery,
    AvailabilityResult,
    SlotVerdict,
)


class AvailabilityProvider(ABC):
    @abstractmethod
    async def find_slots(
        self, organization_id: uuid.UUID, query: AvailabilityQuery
    ) -> AvailabilityResult:
        """Slots that could be booked right now, soonest first.

        Returning an empty tuple is a legitimate, successful answer — a
        fully-booked week is not an error — so implementations must not
        raise to signal "nothing free". Callers distinguish the two by the
        absence of an exception, never by an empty result."""
        ...

    @abstractmethod
    async def verify_slot(
        self,
        organization_id: uuid.UUID,
        *,
        start_at: datetime,
        duration_minutes: int,
        exclude_appointment_id: uuid.UUID | None = None,
    ) -> SlotVerdict:
        """Re-checks one specific time immediately before it is written.

        Separate from `find_slots` on purpose. A slot offered to a caller
        can go stale in the seconds it takes them to say "yes" — another
        call, or a staff member on the dashboard, can take it — so booking
        must never trust the earlier search. This is the check that runs
        inside the booking lock.

        `exclude_appointment_id` omits one appointment from the capacity
        count, so *rescheduling* an appointment onto a time it already
        occupies does not conflict with itself."""
        ...
