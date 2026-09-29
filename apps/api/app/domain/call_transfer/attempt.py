"""One attempt to hand a live call to a person, and the states it may pass
through.

    REQUESTED ──► DESTINATION_RESOLVED ──► INITIATED
        │                  │
        └──► UNAVAILABLE   └──► FAILED

INITIATED means exactly one thing: the voice provider accepted the transfer
command. It does not mean a person answered — Vapi documents that even its
own `assistant-forwarded-call` end reason "does not confirm that the
downstream telephony provider completed it". There is therefore deliberately
no ANSWERED/CONNECTED state: this system has no evidence that could ever set
it, and a state nothing can truthfully reach is a state something will
eventually set untruthfully.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum


class TransferStatus(str, Enum):
    REQUESTED = "requested"
    DESTINATION_RESOLVED = "destination_resolved"
    INITIATED = "initiated"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class TransferReason(str, Enum):
    """Why the assistant is handing the call over. Chosen by the model from a
    closed set, so reporting can count them and policy can branch on them."""

    CALLER_REQUESTED = "caller_requested"
    CALLER_FRUSTRATED = "caller_frustrated"
    OUT_OF_SCOPE = "out_of_scope"
    EMERGENCY_POLICY = "emergency_policy"


class DestinationKind(str, Enum):
    BUSINESS_HOURS = "business_hours"
    AFTER_HOURS = "after_hours"


class TransferFailure:
    """Stable error codes, mirrored into tool results and the attempt row."""

    NOT_CONFIGURED = "TRANSFER_NOT_CONFIGURED"
    DISABLED = "TRANSFER_DISABLED"
    NO_DESTINATION_NOW = "NO_DESTINATION_NOW"
    DESTINATION_IS_AI_LINE = "DESTINATION_IS_AI_LINE"
    CALL_CONTROL_UNAVAILABLE = "CALL_CONTROL_UNAVAILABLE"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    EMERGENCY_TICKET_REQUIRED = "EMERGENCY_TICKET_REQUIRED"


_ALLOWED: dict[TransferStatus, frozenset[TransferStatus]] = {
    TransferStatus.REQUESTED: frozenset(
        {TransferStatus.DESTINATION_RESOLVED, TransferStatus.UNAVAILABLE}
    ),
    TransferStatus.DESTINATION_RESOLVED: frozenset(
        {TransferStatus.INITIATED, TransferStatus.FAILED}
    ),
    TransferStatus.INITIATED: frozenset(),
    TransferStatus.UNAVAILABLE: frozenset(),
    TransferStatus.FAILED: frozenset(),
}

TERMINAL_STATUSES = frozenset(
    {TransferStatus.INITIATED, TransferStatus.UNAVAILABLE, TransferStatus.FAILED}
)


class InvalidTransferTransitionError(RuntimeError):
    """A programming error, never a runtime condition: the service moved an
    attempt along an edge the state machine does not have."""


@dataclass(frozen=True, slots=True)
class CallTransferAttempt:
    id: uuid.UUID
    organization_id: uuid.UUID
    conversation_id: uuid.UUID
    reason: TransferReason
    is_emergency: bool
    status: TransferStatus = TransferStatus.REQUESTED
    destination_kind: DestinationKind | None = None
    #: Canonical E.164. Stored for the audit trail (which line was dialled);
    #: never logged unmasked, never placed in a prompt or tool result.
    destination_number: str | None = None
    error_code: str | None = None
    created_at: datetime | None = field(default=None, compare=False)
    updated_at: datetime | None = field(default=None, compare=False)

    def _to(self, status: TransferStatus, **changes: object) -> CallTransferAttempt:
        if status not in _ALLOWED[self.status]:
            raise InvalidTransferTransitionError(f"{self.status.value} -> {status.value}")
        return replace(self, status=status, **changes)  # type: ignore[arg-type]

    def resolve(self, kind: DestinationKind, number: str) -> CallTransferAttempt:
        return self._to(TransferStatus.DESTINATION_RESOLVED, destination_kind=kind,
                        destination_number=number)

    def unavailable(self, error_code: str) -> CallTransferAttempt:
        return self._to(TransferStatus.UNAVAILABLE, error_code=error_code)

    def initiated(self) -> CallTransferAttempt:
        return self._to(TransferStatus.INITIATED)

    def failed(self, error_code: str) -> CallTransferAttempt:
        return self._to(TransferStatus.FAILED, error_code=error_code)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES
