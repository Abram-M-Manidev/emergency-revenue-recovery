"""The AI Brain's running decision about a conversation: is this an
emergency, what should happen next, and what has been learned about the
caller so far. One outcome per conversation, upserted after every assistant
turn as more information becomes available.

`recommended_action` is a signal only — Emergency Dispatch (Milestone 5),
Appointment Management (Milestone 6), and CRM Integrations (Milestone 7)
don't exist yet, so nothing in this milestone actually creates a ticket,
books a slot, or writes a CRM contact. Those milestones read
`conversation_outcomes` directly to find work to act on."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

# The storage width of `customer_phone`, declared here because it is a
# constraint on the outcome itself rather than a detail of one mapper: the
# AI Brain has to know what will fit *before* it decides what to persist.
#
# A live PSTN call on 2026-09-24 proved why that matters. The caller said
# their number as words, the model reported it verbatim ("one two three …"
# — 44 characters), and the value was written straight through. Postgres
# raised `StringDataRightTruncationError`, the request transaction rolled
# back, and the whole turn was lost: the caller had already been read three
# appointment times that the backend then had no record of offering, so the
# next turn restarted intake. The overflow was never the caller's fault or
# the model's — nothing on the write path knew the ceiling existed.
CUSTOMER_PHONE_MAX_LENGTH = 32

# The same failure exists for the other two model-reported contact fields,
# just at a higher ceiling: nothing stopped a name longer than 255 characters
# or an address longer than 500 from reaching the column, and an overflow on
# either rolls back the turn exactly as the phone number did. Unlike a phone
# number these are prose, so cutting one to fit keeps what the caller
# actually said at the start and loses only a tail no human would read —
# see `bounded_contact_text`.
CUSTOMER_NAME_MAX_LENGTH = 255
CUSTOMER_ADDRESS_MAX_LENGTH = 500


def bounded_contact_text(value: str | None, max_length: int) -> str | None:
    """A model-reported name or address, cut to fit its column.

    Truncation is right here and deliberately wrong for phone numbers: a
    truncated address still points at the same street, while a truncated
    phone number is a different, fabricated number. Anything within the limit
    is returned untouched, so ordinary values are byte-identical to before."""
    if value is None or len(value) <= max_length:
        return value
    return value[:max_length]


class CallClassification(str, Enum):
    EMERGENCY = "emergency"
    NON_EMERGENCY = "non_emergency"
    UNKNOWN = "unknown"


class RecommendedAction(str, Enum):
    CREATE_EMERGENCY_TICKET = "create_emergency_ticket"
    BOOK_APPOINTMENT = "book_appointment"
    ANSWER_FAQ = "answer_faq"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ConversationOutcome:
    id: uuid.UUID
    conversation_id: uuid.UUID
    classification: CallClassification
    confidence: float
    recommended_action: RecommendedAction
    matched_service_id: uuid.UUID | None
    customer_name: str | None
    customer_phone: str | None
    customer_address: str | None
    summary: str
    updated_at: datetime
