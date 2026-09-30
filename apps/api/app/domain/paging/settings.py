"""Who is paged about an emergency, how, and how long they have to answer.

Kept apart from `BusinessProfile` for the same reason notification and
call-transfer settings are: the business profile is assembled into the LLM
system prompt on every turn, and an on-call number is usually a technician's
personal mobile. A separate table keeps it out of prompts, tool results and
logs by construction — the assistant is told *whether* the on-call person has
been paged, never *who* or *where*.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.shared.utils.phone import normalize_phone_number

# E.164: a leading +, a non-zero country-code digit, 8-15 digits in total.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

#: Bounds on how long a recipient has to acknowledge before the page moves
#: on. Under a minute is shorter than it takes to find a phone at 3am; over an
#: hour is not paging any more.
MIN_ACK_TIMEOUT_SECONDS = 60
MAX_ACK_TIMEOUT_SECONDS = 3600
DEFAULT_ACK_TIMEOUT_SECONDS = 300


class PagingChannel(str, Enum):
    """How a recipient is reached. Both are "the provider accepted it" at
    best — neither ever means a person read or heard it."""

    SMS = "sms"
    VOICE = "voice"


class InvalidPagingSettingsError(ValueError):
    """Paging configuration that must not be stored.

    A `ValueError` subclass (like `InvalidTransferNumberError`) so the API
    layer reports it as a 422 on an authenticated Owner request. Messages
    name the rule broken and never echo the submitted number back."""


@dataclass(frozen=True, slots=True)
class PagingSettings:
    organization_id: uuid.UUID
    is_enabled: bool
    #: Paged first, on every enabled channel.
    primary_number: str | None
    #: Paged only if the primary has not acknowledged within the timeout (or
    #: every attempt to reach the primary failed). None: nobody to escalate
    #: to, so an unacknowledged page ends `unresolved`.
    backup_number: str | None
    sms_enabled: bool
    voice_enabled: bool
    ack_timeout_seconds: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def channels(self) -> tuple[PagingChannel, ...]:
        """Enabled channels in a fixed order, so the notifications a page
        creates are deterministic."""
        return tuple(
            channel
            for channel, enabled in (
                (PagingChannel.SMS, self.sms_enabled),
                (PagingChannel.VOICE, self.voice_enabled),
            )
            if enabled
        )

    @property
    def is_operational(self) -> bool:
        """Whether an emergency right now would page anyone at all."""
        return self.is_enabled and self.primary_number is not None and bool(self.channels)


def validate_paging_number(raw: str | None, *, forbidden: frozenset[str]) -> str | None:
    """The canonical E.164 form of a paging number, or None for "not set".

    `forbidden` is every number that reaches this organization's AI voice
    line: a voice page to it would ring the assistant, which would answer an
    emergency page with an intake greeting — a page that "connected" and
    reached nobody."""
    if raw is None or not raw.strip():
        return None
    normalized = normalize_phone_number(raw)
    if normalized is None or not _E164.match(normalized):
        raise InvalidPagingSettingsError(
            "Paging numbers must be full international (E.164) numbers, e.g. +15551234567."
        )
    if normalized in forbidden:
        raise InvalidPagingSettingsError(
            "That number is this business's AI voice line. A page to it would reach the "
            "assistant, not a person; use a number a person answers."
        )
    return normalized


def validate_paging_settings(
    *,
    organization_id: uuid.UUID,
    is_enabled: bool,
    primary_number: str | None,
    backup_number: str | None,
    sms_enabled: bool,
    voice_enabled: bool,
    ack_timeout_seconds: int,
    forbidden_numbers: frozenset[str],
) -> PagingSettings:
    """Normalises and checks a whole configuration, or raises.

    A disabled configuration may be incomplete (an Owner switching paging
    off should not first have to fix a number), but numbers that ARE given
    are still validated, so nothing unusable is ever stored."""
    primary = validate_paging_number(primary_number, forbidden=forbidden_numbers)
    backup = validate_paging_number(backup_number, forbidden=forbidden_numbers)
    if not MIN_ACK_TIMEOUT_SECONDS <= ack_timeout_seconds <= MAX_ACK_TIMEOUT_SECONDS:
        raise InvalidPagingSettingsError(
            f"The acknowledgement timeout must be between {MIN_ACK_TIMEOUT_SECONDS} and "
            f"{MAX_ACK_TIMEOUT_SECONDS} seconds."
        )
    if backup is not None and primary is None:
        raise InvalidPagingSettingsError("A backup recipient needs a primary recipient first.")
    if backup is not None and backup == primary:
        raise InvalidPagingSettingsError(
            "The backup must be a different number from the primary — escalating to the "
            "same phone would reach nobody new."
        )
    if is_enabled:
        if primary is None:
            raise InvalidPagingSettingsError(
                "Enter a primary on-call number, or switch emergency paging off."
            )
        if not (sms_enabled or voice_enabled):
            raise InvalidPagingSettingsError(
                "Choose at least one paging channel (text message or voice call)."
            )
    return PagingSettings(
        organization_id=organization_id,
        is_enabled=is_enabled,
        primary_number=primary,
        backup_number=backup,
        sms_enabled=sms_enabled,
        voice_enabled=voice_enabled,
        ack_timeout_seconds=ack_timeout_seconds,
    )


def mask_paging_number(number: str | None) -> str | None:
    """Enough to recognise which phone was paged, without publishing a
    technician's personal mobile: `+1•••••4567`."""
    if not number:
        return None
    return f"{number[:2]}{'•' * max(len(number) - 6, 0)}{number[-4:]}"
