"""The provider boundary for moving a live call to another number.

The application decides *whether* and *where*; an implementation of this
port only performs the move and reports, honestly, whether the provider
accepted it. `announcement` is spoken to the caller by the provider as part
of the accepted transfer, which is what guarantees the caller never hears
"connecting you" for a transfer that was refused.
"""

from __future__ import annotations

import contextvars
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TransferInitiation:
    #: True only when the provider accepted the command. Not "answered".
    accepted: bool
    #: A `TransferFailure` code when not accepted.
    error_code: str | None = None


class CallTransferPort(ABC):
    @abstractmethod
    async def transfer(
        self, *, call_control: str, destination_number: str, announcement: str
    ) -> TransferInitiation:
        """Must never raise: every failure (rejection, timeout, transport
        error, an unusable `call_control`) comes back as a not-accepted
        `TransferInitiation`, because the caller of this runs inside a live
        voice turn where an exception is a dropped call."""
        ...


# The live call's control handle for the turn being processed, set by the
# voice transport (the Vapi webhook) and read by the transfer tool.
#
# A context variable rather than a parameter threaded through AIBrainService:
# the AI Brain is channel-agnostic and the text/dashboard channel has no live
# call at all, so the absence of a handle is simply "transfer unavailable" —
# which is exactly the honest answer on that channel. Context variables are
# per-request and copied into the tasks a request spawns, so one call's
# handle can never be seen by another call's turn.
#
# The value is a capability (whoever holds it can steer the call): it is
# never logged, persisted, or placed in a prompt or tool result.
current_call_control: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_call_control", default=None
)
