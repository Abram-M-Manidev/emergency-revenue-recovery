"""A dispatchable job created from an AI Brain `ConversationOutcome` (see
`app/domain/entities/conversation_outcome.py`) once it is flagged
`classification=EMERGENCY` / `recommended_action=CREATE_EMERGENCY_TICKET`.
`conversation_id` is unique — at most one ticket per conversation, which is
what makes `DispatchService.sync_ticket_from_outcome` idempotent no matter
how many AI Brain turns re-flag the same conversation as an emergency.

Customer fields and `summary` are a point-in-time copy of what the AI Brain
had learned when the ticket was created — not a live link back to the
conversation, so the ticket keeps its own operational history (status,
assignment) independent of anything that happens in the conversation
afterward.

`customer_id` (Milestone 7) is a best-effort link to the unified
`Customer` record for the same caller — set after the fact by
`CustomerService.sync_customer_from_outcome`, not at ticket-creation time,
so it starts `None` and may stay `None` if the AI Brain never captured a
phone number for this conversation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class TicketStatus(str, Enum):
    NEW = "new"
    ASSIGNED = "assigned"
    EN_ROUTE = "en_route"
    RESOLVED = "resolved"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class EmergencyTicket:
    id: uuid.UUID
    organization_id: uuid.UUID
    conversation_id: uuid.UUID
    matched_service_id: uuid.UUID | None
    status: TicketStatus
    customer_name: str | None
    customer_phone: str | None
    customer_address: str | None
    summary: str
    assigned_technician_user_id: uuid.UUID | None
    assigned_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    customer_id: uuid.UUID | None = None
