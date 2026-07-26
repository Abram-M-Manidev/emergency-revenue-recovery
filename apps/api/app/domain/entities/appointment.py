"""A staff-scheduled job requested from an AI Brain `ConversationOutcome`
(see `app/domain/entities/conversation_outcome.py`) once it is flagged
`recommended_action=BOOK_APPOINTMENT`. `conversation_id` is unique — at most
one appointment per conversation, which is what makes
`AppointmentService.sync_appointment_from_outcome` idempotent no matter how
many AI Brain turns re-flag the same conversation.

Unlike Emergency Dispatch, the AI Brain never picks a concrete time slot —
it only captures the intent. Every appointment starts life `REQUESTED`, with
`scheduled_start_at`/`duration_minutes`/`assigned_technician_user_id` all
unset, and only moves to `SCHEDULED` once a staff member picks a real time
(see `AppointmentService.schedule_appointment`).

Customer fields and `summary` are a point-in-time copy of what the AI Brain
had learned when the appointment was created — not a live link back to the
conversation, so the appointment keeps its own operational history
independent of anything that happens in the conversation afterward.

`customer_id` (Milestone 7) is a best-effort link to the unified
`Customer` record for the same caller — set after the fact by
`CustomerService.sync_customer_from_outcome`, not at appointment-creation
time, so it starts `None` and may stay `None` if the AI Brain never
captured a phone number for this conversation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class AppointmentStatus(str, Enum):
    REQUESTED = "requested"
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELED = "canceled"
    NO_SHOW = "no_show"


@dataclass(frozen=True, slots=True)
class Appointment:
    id: uuid.UUID
    organization_id: uuid.UUID
    conversation_id: uuid.UUID
    matched_service_id: uuid.UUID | None
    status: AppointmentStatus
    customer_name: str | None
    customer_phone: str | None
    customer_address: str | None
    summary: str
    scheduled_start_at: datetime | None
    duration_minutes: int | None
    assigned_technician_user_id: uuid.UUID | None
    assigned_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    customer_id: uuid.UUID | None = None
    actual_value: Decimal | None = None
