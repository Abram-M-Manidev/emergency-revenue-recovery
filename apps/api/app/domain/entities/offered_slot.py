"""A slot this conversation was read aloud, and whether the caller chose it.

The persisted half of the appointment-consent invariant. `AvailabilitySlot`
(see `availability.py`) is a *derived* offer — what the engine could sell.
This is the *record* of what was actually said to one caller, and of which
one of those times they then picked.

Why "offered" is not enough
---------------------------
`conversation_offered_slots` originally recorded offers only, which stopped
the model booking a time the caller was never read. It did not stop the
model booking a time the caller was read but never *chose*: on a real-model
run the assistant offered three times and immediately booked one, with no
caller utterance in between. Every one of those three was genuinely offered,
so an offered-only check approves all three.

The turn index is what closes that
----------------------------------
`offered_turn_index` and `selected_turn_index` count persisted conversation
messages at the start of the turn that wrote them — a number the backend
derives from its own message history and that no tool argument can
influence. A caller cannot respond to an offer they have not heard yet, so a
selection is credible only when it lands in a *strictly later* turn than the
offer. That single comparison is the deterministic core of consent: it holds
regardless of what the model claims the caller said, because the model
cannot manufacture a conversation turn.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


@dataclass(frozen=True, slots=True)
class OfferedSlot:
    """One (conversation, start time) pair the caller was read.

    `selected_at is None` means offered but not chosen — the state every row
    starts in. At most one row per conversation may be selected at a time; a
    caller who changes their mind clears the previous selection rather than
    adding a second (enforced in the database by a partial unique index, so
    two concurrent transcription-driven turns cannot both win)."""

    organization_id: uuid.UUID
    conversation_id: uuid.UUID
    start_at: datetime
    duration_minutes: int
    offered_turn_index: int
    selected_at: datetime | None = None
    selected_turn_index: int | None = None

    @property
    def is_selected(self) -> bool:
        return self.selected_at is not None


class SlotSelectionVerdict(str, Enum):
    """Why a caller's stated choice was or was not accepted.

    Separate codes rather than a bool because each one needs a different
    sentence from the assistant, and collapsing them would force the voice
    layer to guess which recovery to offer."""

    RECORDED = "recorded"
    #: No row for that instant in this conversation — the caller was never
    #: read this time, so "choosing" it is the model inventing a choice.
    NOT_OFFERED = "not_offered"
    #: Offered during this very turn. The caller has not spoken since, so
    #: there is no utterance that could be a choice. This is the exact shape
    #: of the observed failure: offer and book inside one turn.
    NOT_YET_HEARD = "not_yet_heard"
