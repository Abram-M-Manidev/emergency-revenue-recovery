"""What was actually offered to a caller, per conversation.

Exists because "available" and "offered" are different facts, and only the
second one may be booked. A live call on 2026-08-23 booked a slot the caller
was never read: the model had a valid time in its prompt, the caller gave
their details, and it called `book_appointment` directly — no
`check_availability`, no options spoken, no choice made. The appointment was
`SCHEDULED` for a Monday morning nobody had agreed to.

Re-deriving availability at booking time cannot catch that, because the slot
genuinely *was* free; the engine would have approved it. The only thing that
distinguishes a legitimate booking is whether that specific slot was handed
to the caller earlier in the same conversation, and nothing in the request
carries that.

Why this is persisted rather than held in memory
------------------------------------------------
The offer and the booking arrive as separate HTTP requests, and
`docker-compose.prod.yml` runs four uvicorn workers accepting from a shared
socket. A per-process cache would let a booking handled by one worker bypass
an offer recorded by another — a safety check that fails open, which is
worse than none. No existing table has anywhere to put this:
`conversation_outcomes` has fixed columns, `appointments` holds one row of
scheduling state, and `conversation_messages` holds prose. Hence its own
small table.

Offered is still not chosen
---------------------------
Recording offers alone left a second hole, which a real-model run then walked
straight through: the assistant offered three times and booked one in the
same turn, with no caller utterance between the offer and the write. All
three were genuinely offered, so an offered-only check approves the booking.

The same table therefore also records *selection*, keyed to the conversation
turn it happened in — see `app/domain/entities/offered_slot.py` for why the
turn index is the part a model cannot fake. Selection state lives here rather
than in a second table because it is an attribute of an offer ("this is the
one they picked"), and splitting it would make the two rows drift.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime

from app.domain.entities.availability import AvailabilitySlot
from app.domain.entities.offered_slot import OfferedSlot


class OfferedSlotRepository(ABC):
    @abstractmethod
    async def record_offered(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        slots: Sequence[AvailabilitySlot],
        turn_index: int,
    ) -> None:
        """Remembers the slots just returned to a caller, and the turn they
        were read in.

        Idempotent by `(conversation_id, slot_start_at)`: `check_availability`
        is routinely called several times in one conversation and will
        re-offer the same times, which must not accumulate rows or fail. A
        re-offer refreshes the duration, because the matched service — and so
        the visit length — can change mid-conversation.

        A re-offer must NOT move `offered_turn_index` forward, and must not
        disturb an existing selection. The index records the turn the caller
        *first* heard this time, which is what makes an already-recorded
        selection legitimate; advancing it on a later re-offer would
        retroactively invalidate a choice the caller really made."""
        ...

    @abstractmethod
    async def list_offered_starts(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[datetime]:
        """Every start instant this conversation was offered, ascending.

        Read-only, and used only to explain a refusal: a `SLOT_NOT_OFFERED`
        that logs nothing but its own name is undiagnosable, which is exactly
        what happened on 2026-08-23 — the requested time was never recorded,
        so whether it was a 12/24-hour slip or a timezone slip could not be
        established afterwards. Enforcement does not consult this."""
        ...

    @abstractmethod
    async def offered_duration_minutes(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        start_at: datetime,
    ) -> int | None:
        """The duration this conversation was offered for `start_at`, or None
        if that time was never offered to it.

        Returning the duration rather than a bare boolean is deliberate: it
        makes the booking use the length the caller was actually quoted,
        instead of one re-derived at booking time that might differ.

        `organization_id` is applied in the query, so an offer made to one
        tenant can never authorise a booking in another."""
        ...

    @abstractmethod
    async def get_offered(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        start_at: datetime,
    ) -> OfferedSlot | None:
        """The offer record for one instant in this conversation, or None if
        this caller was never read that time.

        Scoped by organization as well as conversation, so an offer made on
        one tenant's call can never be read — let alone selected — through
        another's."""
        ...

    @abstractmethod
    async def mark_selected(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        start_at: datetime,
        turn_index: int,
    ) -> OfferedSlot | None:
        """Records that the caller chose `start_at`, clearing any previous
        choice in the same conversation. Returns the updated row, or None if
        that instant was never offered here.

        Clearing first is what makes "no, actually make it 10" work: a
        conversation has at most one live selection, so the superseded time
        stops being bookable the moment a new one is chosen. Both writes
        belong to one statement pair inside the caller's transaction, and the
        database additionally holds a partial unique index over selected rows
        — so two turns racing on the same call cannot both leave a selection
        behind.

        Does NOT validate turn ordering. That is a business rule and lives in
        `AppointmentService.select_slot_for_conversation`, alongside the
        booking rules it exists to protect."""
        ...

    @abstractmethod
    async def get_active_selection(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> OfferedSlot | None:
        """The one slot this conversation currently has chosen, or None.

        Read immediately before the booking write, inside the booking lock —
        the check that makes "the caller actually picked this" a fact about
        stored state rather than a claim in a tool argument."""
        ...

    @abstractmethod
    async def clear_selection(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None:
        """Drops this conversation's selection, leaving the offers intact.

        Used when a chosen time stops being bookable — it was taken while the
        caller was talking, or the business turned out to be closed then. The
        offer stays on record (it really was read out), but the stale choice
        must not survive to authorise a later retry of a time the caller can
        no longer have."""
        ...
