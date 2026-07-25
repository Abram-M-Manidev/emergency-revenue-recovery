"""Extends a `User` (see `app/domain/entities/user.py`) with the fields
Emergency Dispatch needs once that user holds the `Technician` role: a
contact phone number, whether they're currently taking dispatches, and free-
text notes a dispatcher can read when deciding who to assign a ticket to.

There is no structured skills/service-area matching table — assignment is
manual (a dispatcher picks from the roster), so `notes` is plain text rather
than a taxonomy nothing in this milestone needs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TechnicianProfile:
    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    phone_number: str
    is_on_call: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime
