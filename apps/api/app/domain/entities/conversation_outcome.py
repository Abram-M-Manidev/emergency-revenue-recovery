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
