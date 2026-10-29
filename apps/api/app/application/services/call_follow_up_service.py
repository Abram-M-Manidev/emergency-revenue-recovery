"""What happens to a call ERRS could not finish — see `app/domain/follow_ups.py`.

Two entry points, both decided by fixed rules and never by the model:

- `handle_ai_failure` runs on a live turn whose AI provider failed. The model
  cannot help, so this does what it can without one: records a follow-up,
  opens an emergency ticket when the caller's own words describe one (so the
  business's alert and paging run), tries to hand the call to a person, and
  otherwise tells the caller — truthfully — what was done and where to go if
  they are in danger.
- `handle_call_ended` runs on Vapi's end-of-call report. A call that ended
  before its request was recorded gets a follow-up, and a possible emergency
  in it gets a ticket.

Every write is best-effort inside its own savepoint, with the savepoint
INSIDE the `try` (see `domain/transactions.py`): failing to record a
follow-up must never cost the turn, the ticket, or the caller's answer.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from app.application.services.appointment_service import AppointmentService
from app.application.services.call_transfer_service import CallTransferService
from app.application.services.dispatch_service import DispatchService
from app.domain.call_transfer.attempt import TransferReason
from app.domain.entities.conversation_message import MessageRole
from app.domain.exceptions import EntityNotFoundError
from app.domain.follow_ups import CallFollowUp, FollowUpReason
from app.domain.life_safety import (
    Hazard,
    detect_hazards,
    emergency_number_for,
    hazards_in,
    safety_instruction,
)
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.call_follow_up_repository import CallFollowUpRepository
from app.domain.repositories.call_transfer_repository import CallTransferAttemptRepository
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.emergency_keyword_repository import EmergencyKeywordRepository
from app.domain.transactions import NullSavepoints, Savepoints
from app.shared.utils.phone import storable_phone_number

logger = structlog.get_logger("app.call_follow_ups")

_UNKNOWN_EMERGENCY_NUMBER = emergency_number_for(None)

# Plain words for the ticket summary, which a dispatcher reads. Hazard kinds
# and the business's own keyword phrases only — never the caller's words.
_HAZARD_WORDS: dict[Hazard, str] = {
    Hazard.FIRE_SMOKE: "smoke or fire",
    Hazard.GAS: "a possible gas leak",
    Hazard.CARBON_MONOXIDE: "possible carbon monoxide",
    Hazard.ELECTRICAL: "sparking or an electrical danger",
    Hazard.FLOODING: "flooding",
}

#: The most follow-ups one dashboard request returns.
FOLLOW_UP_LIST_LIMIT = 200


@dataclass(frozen=True, slots=True)
class OutageReply:
    """What the caller hears on a turn whose AI provider failed.

    `transfer_initiated` means the call is being moved to a person and the
    provider is speaking the handoff, so the transport must add nothing and
    must not hang up."""

    text: str
    transfer_initiated: bool


@dataclass(frozen=True, slots=True)
class _Signals:
    hazards: frozenset[Hazard]
    keywords: tuple[str, ...]

    @property
    def possible_emergency(self) -> bool:
        return bool(self.hazards or self.keywords)

    def describe(self) -> str:
        parts = [_HAZARD_WORDS[h] for h in Hazard if h in self.hazards]
        parts += [f'"{k}"' for k in self.keywords]
        return ", ".join(parts)


class CallFollowUpService:
    def __init__(
        self,
        *,
        follow_up_repository: CallFollowUpRepository,
        conversation_repository: ConversationRepository,
        emergency_keyword_repository: EmergencyKeywordRepository,
        business_profile_repository: BusinessProfileRepository,
        transfer_attempt_repository: CallTransferAttemptRepository,
        dispatch_service: DispatchService,
        appointment_service: AppointmentService,
        call_transfer_service: CallTransferService | None = None,
        savepoints: Savepoints | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._follow_ups = follow_up_repository
        self._conversations = conversation_repository
        self._keywords = emergency_keyword_repository
        self._profiles = business_profile_repository
        self._attempts = transfer_attempt_repository
        self._dispatch = dispatch_service
        self._appointments = appointment_service
        self._transfers = call_transfer_service
        self._savepoints = savepoints or NullSavepoints()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --- Live call: the AI provider failed --------------------------------

    async def handle_ai_failure(
        self,
        *,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        caller_utterance: str,
        call_control: str | None,
        already_said: str = "",
    ) -> OutageReply:
        """`already_said` is what the caller has heard this turn (the caller
        notice, the fixed safety instruction, a partial reply), so nothing
        is said twice."""
        earlier = await self._caller_words(conversation_id)
        signals = await self._signals(organization_id, [*earlier, caller_utterance])
        emergency_number = await self._emergency_number(organization_id)

        follow_up = await self._record(
            organization_id,
            conversation_id,
            reason=FollowUpReason.AI_UNAVAILABLE,
            signals=signals,
            context="the assistant could not take the call (AI unavailable)",
        )

        if await self._transfer(organization_id, conversation_id, signals, call_control):
            return OutageReply(text="", transfer_initiated=True)

        sentences = [
            "I'm sorry, I'm having trouble connecting to our system right now, "
            "so I can't take your request on this call."
        ]
        instruction = safety_instruction(
            detect_hazards(caller_utterance), emergency_number=emergency_number
        )
        if instruction and instruction not in already_said:
            sentences.append(instruction)
        if follow_up is not None and follow_up.emergency_ticket_id is not None:
            sentences.append("I've flagged your call to the team as a possible emergency.")
        elif follow_up is not None:
            sentences.append("I've logged your call for the team to follow up.")
        sentences.append(
            f"If anyone is in danger, please hang up and call {emergency_number} now. "
            "Otherwise, please try calling back in a few minutes."
        )
        return OutageReply(text=" ".join(sentences), transfer_initiated=False)

    # --- End of call --------------------------------------------------------

    async def handle_call_ended(
        self,
        *,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        completed_by_assistant: bool,
        caller_lines: Iterable[str] = (),
    ) -> CallFollowUp | None:
        """`caller_lines` are the caller's own lines from Vapi's transcript:
        speech that may never have reached ERRS (an utterance still in
        flight at the hang-up, or every utterance of an outage)."""
        words = [*(await self._caller_words(conversation_id)), *caller_lines]
        signals = await self._signals(organization_id, words)

        existing = await self._follow_ups.get_for_conversation(conversation_id)
        if existing is not None:
            # Already flagged on this call (an outage). Only a newly heard
            # emergency changes anything.
            if signals.possible_emergency and not existing.possible_emergency:
                return await self._record(
                    organization_id,
                    conversation_id,
                    reason=existing.reason,
                    signals=signals,
                    context="the call ended before the caller's details were recorded",
                )
            return existing

        if await self._request_recorded(organization_id, conversation_id):
            return None
        if not any(word.strip() for word in words):
            # Nothing was said: a ring and a hang-up, not a request.
            return None

        if await self._attempts.get_initiated_for_conversation(conversation_id) is not None:
            reason = FollowUpReason.TRANSFERRED_BEFORE_REQUEST_RECORDED
        elif completed_by_assistant and not signals.possible_emergency:
            # The assistant itself finished the call with nothing to record:
            # a question answered. Nobody needs to call back.
            return None
        else:
            reason = FollowUpReason.ENDED_BEFORE_REQUEST_RECORDED
        return await self._record(
            organization_id,
            conversation_id,
            reason=reason,
            signals=signals,
            context="the call ended before the caller's details were recorded",
        )

    # --- Dashboard ------------------------------------------------------

    async def list_follow_ups(
        self, organization_id: uuid.UUID, *, include_resolved: bool
    ) -> list[CallFollowUp]:
        return await self._follow_ups.list_for_organization(
            organization_id, include_resolved=include_resolved, limit=FOLLOW_UP_LIST_LIMIT
        )

    async def get_for_conversation(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> CallFollowUp | None:
        follow_up = await self._follow_ups.get_for_conversation(conversation_id)
        if follow_up is None or follow_up.organization_id != organization_id:
            return None
        return follow_up

    async def resolve(
        self, organization_id: uuid.UUID, follow_up_id: uuid.UUID, *, user_id: uuid.UUID
    ) -> CallFollowUp:
        resolved = await self._follow_ups.resolve(
            organization_id, follow_up_id, resolved_by_user_id=user_id, resolved_at=self._clock()
        )
        if resolved is None:
            raise EntityNotFoundError("CallFollowUp", str(follow_up_id))
        return resolved

    # --- helpers ------------------------------------------------------

    async def _record(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        reason: FollowUpReason,
        signals: _Signals,
        context: str,
    ) -> CallFollowUp | None:
        try:
            async with self._savepoints.isolate():
                conversation = await self._conversations.get_by_id(organization_id, conversation_id)
                caller_number = conversation.caller_phone_number if conversation else None
                follow_up = await self._follow_ups.add_if_absent(
                    CallFollowUp(
                        id=uuid.uuid4(),
                        organization_id=organization_id,
                        conversation_id=conversation_id,
                        reason=reason,
                        possible_emergency=False,
                        emergency_ticket_id=None,
                        caller_number=caller_number,
                    )
                )
                if signals.possible_emergency and follow_up.emergency_ticket_id is None:
                    ticket = await self._dispatch.open_unconfirmed_emergency(
                        organization_id,
                        conversation_id,
                        summary=(
                            f"Possible emergency: {context}. The caller mentioned "
                            f"{signals.describe()}. Nobody has spoken to them yet — "
                            "call them back now."
                        ),
                        caller_number=caller_number,
                    )
                    follow_up = await self._follow_ups.mark_possible_emergency(
                        follow_up.id, emergency_ticket_id=ticket.id
                    )
        except Exception as exc:
            logger.error(
                "call_follow_up_record_failed",
                organization_id=str(organization_id),
                conversation_id=str(conversation_id),
                error=type(exc).__name__,
            )
            return None
        # Reason and flags only — never the caller's words or number.
        logger.warning(
            "call_follow_up_recorded",
            organization_id=str(organization_id),
            conversation_id=str(conversation_id),
            reason=follow_up.reason.value,
            possible_emergency=follow_up.possible_emergency,
            has_callback_number=storable_phone_number(follow_up.caller_number) is not None,
        )
        return follow_up

    async def _transfer(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        signals: _Signals,
        call_control: str | None,
    ) -> bool:
        if self._transfers is None:
            return False
        try:
            async with self._savepoints.isolate():
                result = await self._transfers.transfer(
                    organization_id=organization_id,
                    conversation_id=conversation_id,
                    reason=TransferReason.SYSTEM_UNAVAILABLE,
                    is_emergency=signals.possible_emergency,
                    call_control=call_control,
                )
        except Exception as exc:
            logger.error(
                "call_follow_up_transfer_failed",
                organization_id=str(organization_id),
                error=type(exc).__name__,
            )
            return False
        return bool(result.get("success"))

    async def _request_recorded(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> bool:
        if await self._dispatch.get_ticket_for_conversation(organization_id, conversation_id):
            return True
        appointment = await self._appointments.get_appointment_for_conversation(
            organization_id, conversation_id
        )
        return appointment is not None

    async def _caller_words(self, conversation_id: uuid.UUID) -> list[str]:
        messages = await self._conversations.list_messages(conversation_id)
        return [m.content for m in messages if m.role is MessageRole.CUSTOMER]

    async def _signals(self, organization_id: uuid.UUID, words: list[str]) -> _Signals:
        hazards = hazards_in(words)
        try:
            # Savepoint inside the `try`: on Postgres a failed read would
            # otherwise abort the whole transaction even though it is caught.
            async with self._savepoints.isolate():
                keywords = await self._keywords.list(organization_id)
        except Exception:
            # A missing keyword list must not cost the life-safety check.
            logger.warning("call_follow_up_keywords_unavailable", exc_info=True)
            keywords = []
        lowered = " ".join(words).lower()
        matched = tuple(
            dict.fromkeys(k.phrase for k in keywords if k.phrase and k.phrase.lower() in lowered)
        )
        return _Signals(hazards=hazards, keywords=matched)

    async def _emergency_number(self, organization_id: uuid.UUID) -> str:
        try:
            async with self._savepoints.isolate():
                profile = await self._profiles.get_by_organization_id(organization_id)
        except Exception:
            return _UNKNOWN_EMERGENCY_NUMBER
        return emergency_number_for(profile.country if profile else None)
