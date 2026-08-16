"""The unified customer record Milestone 6's `Appointment` docstring
deferred to "Milestone 7": one row per distinct caller (deduplicated by
`(organization_id, phone_number)`) instead of the denormalized
`customer_name`/`customer_phone`/`customer_address` snapshot every
`EmergencyTicket`/`Appointment` already carries.

A `Customer` is created (or matched) automatically the first time an AI
Brain `ConversationOutcome` (see `app/domain/entities/conversation_outcome.py`)
carries a `customer_phone` — see `CustomerService.sync_customer_from_outcome`
— and can also be created/edited by staff directly. Once created, a
`Customer` is never *overwritten* by later AI Brain turns: it is the
durable record a caller's history (tickets, appointments) links back to,
so it is staff-owned data from that point on, not a live mirror of the
AI's most-recent guess.

The one exception is strictly additive and cannot destroy anything: a
field still blank (`None`, empty, or whitespace-only) is filled in when a
later turn learns it, because a customer is created on the first turn
carrying a phone number — routinely before the caller has given their
name or address, which would otherwise leave the record permanently
blank even though the same detail did reach the emergency ticket. A
non-blank value always wins, so a staff correction is never at risk.
`phone_number` (the dedupe key), `email`, and `notes` are excluded
entirely. See `CustomerService._backfill_contact_details`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Customer:
    id: uuid.UUID
    organization_id: uuid.UUID
    full_name: str | None
    phone_number: str
    email: str | None
    address: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
