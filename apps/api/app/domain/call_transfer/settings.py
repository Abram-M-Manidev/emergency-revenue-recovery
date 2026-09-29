"""Where one organization's calls may be handed to a person.

Kept apart from `BusinessProfile` for the same reason notification settings
are: the business profile is assembled into the LLM system prompt on every
turn, and an on-call number is frequently a technician's personal mobile. A
separate table keeps it out of prompts, tool results and logs by
construction — the model is told *that* a transfer is possible, never *where*.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.shared.utils.phone import normalize_phone_number

# E.164: a leading +, a non-zero country-code digit, 8-15 digits in total.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class InvalidTransferNumberError(ValueError):
    """A transfer destination that must not be stored.

    A `ValueError` subclass (like `InvalidNotificationDestinationError`) so
    the API layer reports it as a 422 field error on an authenticated admin
    request, not as an operational failure."""


@dataclass(frozen=True, slots=True)
class CallTransferSettings:
    organization_id: uuid.UUID
    #: The office during business hours. None: nobody to transfer to then
    #: (the resolver falls back to the on-call number — see `resolution`).
    business_hours_number: str | None
    #: The on-call line outside business hours. None: no after-hours human.
    after_hours_number: str | None
    #: Business policy: after an emergency ticket is recorded, hand the
    #: caller straight to a person. Off by default — the emergency ticket
    #: and its alert outbox are the primary emergency path either way.
    transfer_emergencies: bool
    is_enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


def validate_transfer_number(raw: str | None, *, forbidden: frozenset[str]) -> str | None:
    """The canonical E.164 form of a transfer number, or None for "not set".

    `forbidden` is every number that reaches this organization's AI voice
    line. Transferring a caller to one of those would dial straight back into
    the assistant — a loop in which the "human exit" never reaches a human —
    so it is refused at configuration time, not discovered on a live call."""
    if raw is None or not raw.strip():
        return None
    normalized = normalize_phone_number(raw)
    if normalized is None or not _E164.match(normalized):
        raise InvalidTransferNumberError(
            "Transfer numbers must be full international (E.164) numbers, e.g. +15551234567."
        )
    if normalized in forbidden:
        raise InvalidTransferNumberError(
            "That number is this business's AI voice line. Transferring to it would send the "
            "caller straight back to the assistant; use a number a person answers."
        )
    return normalized


def mask_number(number: str | None) -> str | None:
    """Enough to recognise which number is configured, without publishing a
    technician's personal mobile in logs: `+1•••••4567`."""
    if not number:
        return None
    return f"{number[:2]}{'•' * max(len(number) - 6, 0)}{number[-4:]}"
