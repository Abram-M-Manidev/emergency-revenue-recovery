"""Orchestrates the Voice module: admin-facing reads of an org's voice line
and call metadata, plus the Vapi Custom-LLM webhook adapter that feeds real
call transcripts into the *existing* AI Brain (Milestone 3).

This service never does any reasoning itself — no prompt building, no LLM
calls, no business-knowledge lookups. Every conversational decision is
delegated to `AIBrainService.start_conversation` / `.send_message`, exactly
as `ARCHITECTURE.md` and the Milestone 4 scope require ("keep AI reasoning
inside the AI Brain"). This service only knows how to: (1) resolve which
organization an inbound call belongs to, (2) keep a `VoiceCall` row
correlated with the `Conversation` the AI Brain already owns, and (3)
translate between Vapi's wire format and the AI Brain's plain method
signatures."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog
from structlog.contextvars import bind_contextvars

from app.application.services.ai_brain_service import (
    AIBrainService,
    ConversationTextDelta,
    ConversationToolPhase,
)
from app.domain.entities.conversation import ConversationChannel, ConversationStatus
from app.domain.entities.conversation_message import ConversationMessage, MessageRole
from app.domain.entities.voice_call import VoiceCall
from app.domain.entities.voice_line import VoiceLine
from app.domain.exceptions import (
    EntityNotFoundError,
    VoiceAssistantDisabledError,
    VoiceLineNotFoundError,
)
from app.domain.locks import CallLock, NullCallLock
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.organization_repository import OrganizationRepository
from app.domain.repositories.voice_call_repository import VoiceCallRepository
from app.domain.repositories.voice_line_repository import VoiceLineRepository

logger = structlog.get_logger("app.voice")

# Spoken while the AI Brain's tools run. Deliberately says nothing about
# what the tools will *find* or whether they succeed: the caller must never
# hear an outcome before one exists. Each one describes the work being
# started, in the future tense, which is a promise of effort rather than of
# result.
#
# Kept here in the voice transport rather than in the AI Brain because it is
# a telephony concern — the text/simulation path has no dead air to cover
# and ignores the event entirely.
#
# Per tool rather than one phrase for everything, because a single generic
# line misdescribes the work. On the live call of 2026-08-22 the caller said
# "I see it's 9, but please book that" and heard "Let me check that for you,
# one moment" — which sounds like a second availability lookup, not the
# booking they had just asked for.
#
# "One moment." is its own sentence, and the word is spelled out: a
# comma-joined ", one moment" was rendered by TTS as a clipped fragment.
_PROGRESS_PHRASES: dict[str, str] = {
    "book_appointment": "Absolutely. I'll book that appointment now. One moment.",
    # Selection almost always runs in the same round as the booking, where
    # the line below loses to `book_appointment` anyway. It matters for the
    # round where it runs alone: the caller has just named a time, and the
    # only thing worse than dead air there is a phrase implying the time is
    # already theirs. This promises the attempt, nothing more.
    "select_appointment_slot": "Absolutely. I'll book that time for you now. One moment.",
    "check_availability": "Let me check our schedule for you. One moment.",
    "create_service_request": "Let me get that logged for you. One moment.",
}

_DEFAULT_PROGRESS_PHRASE = "One moment while I take care of that."


def _progress_phrase(tool_names: tuple[str, ...]) -> str:
    """What to say while these tools run.

    Ordered by how specifically the caller is waiting on each outcome, so a
    round that bundles several tools describes the one they actually asked
    for: booking beats an availability lookup, which beats recording the
    request. An unrecognised tool falls back to a phrase that commits to
    nothing at all."""
    for name in (
        "book_appointment",
        "select_appointment_slot",
        "check_availability",
        "create_service_request",
    ):
        if name in tool_names:
            return _PROGRESS_PHRASES[name]
    return _DEFAULT_PROGRESS_PHRASE


class TranscriptSupersession:
    """Tracks, per Vapi call, which in-flight request carries the newest
    transcript.

    Vapi re-sends a Custom-LLM request every time its transcription grows,
    so one spoken sentence produced five requests whose utterances were
    strict prefixes of one another — never identical, which is why exact
    match dedupe caught none of them. The only thing that distinguishes a
    doomed request from a useful one is whether a newer one has since
    arrived; a monotonic ticket per call captures exactly that.

    `claim` performs no `await`, so the read-modify-write cannot be
    interleaved by another coroutine on the same event loop.

    Scope: one process. Combined with `CallLock` (which serialises across
    workers), a request is skipped when a newer one arrived *on the same
    worker*; requests spread across workers are still serialised and remain
    correct, they just each generate. See `VoiceService.handle_chat_completion`."""

    def __init__(self) -> None:
        self._latest: dict[str, int] = {}

    def claim(self, call_id: str) -> int:
        seq = self._latest.get(call_id, 0) + 1
        self._latest[call_id] = seq
        return seq

    def is_current(self, call_id: str, seq: int) -> bool:
        return self._latest.get(call_id) == seq

    def forget(self, call_id: str) -> None:
        """Called when the call ends, so the registry cannot grow without
        bound across a long-lived process."""
        self._latest.pop(call_id, None)


# Process-wide: `VoiceService` is constructed per request by `deps.py`, so
# per-instance state would reset on every webhook and track nothing.
_SUPERSESSION = TranscriptSupersession()


@dataclass(frozen=True, slots=True)
class ChatCompletionResult:
    reply_text: str
    should_end_call: bool
    organization_id: uuid.UUID
    conversation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class VoiceTextDelta:
    """A fragment of the assistant's reply, ready to be forwarded to Vapi as
    an SSE content chunk."""

    text: str


@dataclass(frozen=True, slots=True)
class VoiceTurnComplete:
    """Terminal event: the turn is persisted and `should_end_call` is
    authoritative."""

    result: ChatCompletionResult


VoiceStreamEvent = VoiceTextDelta | VoiceTurnComplete


@dataclass(frozen=True, slots=True)
class _AnsweredTurn:
    customer_utterance: str
    reply_text: str


def _last_answered_turn(history: list[ConversationMessage]) -> _AnsweredTurn | None:
    """The most recent completed customer->assistant exchange, or None when
    the conversation has not produced one yet."""
    if (
        len(history) >= 2
        and history[-1].role is MessageRole.ASSISTANT
        and history[-2].role is MessageRole.CUSTOMER
    ):
        return _AnsweredTurn(
            customer_utterance=history[-2].content, reply_text=history[-1].content
        )
    return None


class VoiceService:
    def __init__(
        self,
        *,
        voice_line_repository: VoiceLineRepository,
        voice_call_repository: VoiceCallRepository,
        conversation_repository: ConversationRepository,
        ai_brain_service: AIBrainService,
        call_lock: CallLock | None = None,
        supersession: TranscriptSupersession | None = None,
        # The per-tenant voice kill switch is read from here. Optional so
        # every pre-existing construction site keeps working; absent, the
        # switch simply cannot be consulted and calls proceed exactly as
        # they did before it existed.
        organization_repository: OrganizationRepository | None = None,
    ) -> None:
        self._voice_lines = voice_line_repository
        self._voice_calls = voice_call_repository
        self._conversations = conversation_repository
        self._ai_brain = ai_brain_service
        self._call_lock = call_lock or NullCallLock()
        self._supersession = supersession or _SUPERSESSION
        self._organizations = organization_repository

    # --- Admin-facing reads ---

    async def get_voice_line(self, organization_id: uuid.UUID) -> VoiceLine | None:
        return await self._voice_lines.get_by_organization_id(organization_id)

    async def get_voice_call(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> VoiceCall:
        voice_call = await self._voice_calls.get_by_conversation_id(conversation_id)
        if voice_call is None or voice_call.organization_id != organization_id:
            # Cross-tenant id: from the caller's point of view, another
            # org's call simply doesn't exist — same convention as
            # AIBrainService.get_conversation.
            raise EntityNotFoundError("VoiceCall", str(conversation_id))
        return voice_call

    # --- Vapi webhook adapter ---

    async def handle_chat_completion(
        self,
        *,
        vapi_call_id: str,
        assistant_id: str | None,
        phone_number_id: str | None,
        customer_number: str | None,
        customer_utterance: str,
    ) -> ChatCompletionResult:
        # Claimed BEFORE the lock is awaited: the ticket must record arrival
        # order, not acquisition order, or a request that queued first would
        # look newer than the one that overtook it.
        sequence = self._supersession.claim(vapi_call_id)

        # Held until this request's transaction commits (see the
        # PostgresAdvisoryCallLock docstring), so no two requests for one
        # call read the same conversation history and answer it twice.
        async with self._call_lock.hold(vapi_call_id):
            return await self._handle_chat_completion_locked(
                vapi_call_id=vapi_call_id,
                assistant_id=assistant_id,
                phone_number_id=phone_number_id,
                customer_number=customer_number,
                customer_utterance=customer_utterance,
                sequence=sequence,
            )

    async def handle_chat_completion_stream(
        self,
        *,
        vapi_call_id: str,
        assistant_id: str | None,
        phone_number_id: str | None,
        customer_number: str | None,
        customer_utterance: str,
    ) -> AsyncIterator[VoiceStreamEvent]:
        """Streaming twin of `handle_chat_completion`, preserving P1 whole.

        This is an async *generator*, which is what keeps the concurrency
        guarantee intact: `async with self._call_lock.hold(...)` stays
        entered for as long as the consumer is iterating, so the
        transaction-scoped advisory lock is still held while the model is
        generating and while the turn is persisted. Had this been a normal
        coroutine returning a generator, the lock would have released the
        moment the function returned — before a single token arrived — and
        two overlapping requests for one call could both generate."""
        # Still the first statement in the coroutine: the claim must record
        # arrival order, so nothing — including telemetry — may await before
        # it. `claim` and the binding below are both synchronous.
        sequence = self._supersession.claim(vapi_call_id)

        async with self._call_lock.hold(vapi_call_id):
            voice_line = await self._resolve_voice_line(assistant_id, phone_number_id)
            organization_id = voice_line.organization_id
            conversation_id = await self._conversation_id_for(
                organization_id, vapi_call_id, customer_number
            )
            # Bound here, inside the generator, rather than relying on the
            # request-scoped context propagating into a StreamingResponse
            # body that Starlette iterates after the handler has returned.
            # Everything downstream — provider, persistence — inherits it.
            bind_contextvars(
                conversation_id=str(conversation_id),
                organization_id=str(organization_id),
                turn_sequence=sequence,
            )
            logger.info("voice_turn_started", streaming=True)

            history = await self._conversations.list_messages(conversation_id)
            cached_reply = _last_answered_turn(history)
            redundant = cached_reply is not None and (
                cached_reply.customer_utterance == customer_utterance
                or not self._supersession.is_current(vapi_call_id, sequence)
            )
            if cached_reply is not None and redundant:
                # Identical reasoning to the non-streaming path: an exact
                # retry or a request already overtaken by a newer transcript
                # must not reach the model. Emitted as a single delta so the
                # transport shape stays the same either way.
                logger.info(
                    "vapi_chat_completion_stream_reused_cached_reply",
                    vapi_call_id=vapi_call_id,
                    sequence=sequence,
                )
                result = await self._cached_result(
                    organization_id, conversation_id, cached_reply.reply_text
                )
                yield VoiceTextDelta(cached_reply.reply_text)
                yield VoiceTurnComplete(result)
                return

            # Tracks whether the caller has heard anything at all this turn,
            # not merely whether the holding phrase was used. A turn can run
            # several tool rounds, and only the round the model narrated
            # reports `model_already_spoke` — so a per-round check let the
            # phrase follow the model's own announcement on the *next* round
            # ("...while I check available times." "Let me check that for
            # you, one moment."). Dead air is a property of the turn.
            caller_has_heard_speech = False
            async for event in self._ai_brain.send_message_stream(
                organization_id, conversation_id, customer_utterance
            ):
                if isinstance(event, ConversationTextDelta):
                    if event.text:
                        caller_has_heard_speech = True
                    yield VoiceTextDelta(event.text)
                elif isinstance(event, ConversationToolPhase):
                    # A tool round is a second model call, and the caller
                    # hears that gap as silence — the same silence that
                    # already ended one live call on `silence-timed-out`.
                    # This is a fixed, system-authored line, not model
                    # output: it states only that work is happening, so it
                    # cannot become the kind of unbacked claim
                    # ("I've booked that for you") this whole change exists
                    # to eliminate.
                    #
                    # Only when the caller would otherwise hear nothing.
                    # `model_already_spoke` covers the response that carried
                    # both speech and a tool call; `caller_has_heard_speech`
                    # covers anything said earlier in the same turn,
                    # including the phrase itself. Together they make this
                    # "say something only if silence is what follows".
                    if not caller_has_heard_speech and not event.model_already_spoke:
                        caller_has_heard_speech = True
                        logger.info("voice_holding_phrase_emitted", tools=list(event.tool_names))
                        yield VoiceTextDelta(_progress_phrase(event.tool_names))
                else:
                    yield VoiceTurnComplete(
                        ChatCompletionResult(
                            reply_text=event.result.reply_message.content,
                            should_end_call=(
                                event.result.conversation.status is ConversationStatus.COMPLETED
                            ),
                            organization_id=organization_id,
                            conversation_id=conversation_id,
                        )
                    )

    async def _conversation_id_for(
        self, organization_id: uuid.UUID, vapi_call_id: str, customer_number: str | None
    ) -> uuid.UUID:
        voice_call = await self._voice_calls.get_by_vapi_call_id(vapi_call_id)
        if voice_call is None:
            conversation = await self._ai_brain.start_conversation(
                organization_id,
                caller_phone_number=customer_number,
                channel=ConversationChannel.VOICE,
            )
            voice_call = await self._voice_calls.create(
                organization_id=organization_id,
                conversation_id=conversation.id,
                vapi_call_id=vapi_call_id,
                caller_number=customer_number,
            )
        return voice_call.conversation_id

    async def _handle_chat_completion_locked(
        self,
        *,
        vapi_call_id: str,
        assistant_id: str | None,
        phone_number_id: str | None,
        customer_number: str | None,
        customer_utterance: str,
        sequence: int,
    ) -> ChatCompletionResult:
        voice_line = await self._resolve_voice_line(assistant_id, phone_number_id)
        organization_id = voice_line.organization_id

        voice_call = await self._voice_calls.get_by_vapi_call_id(vapi_call_id)
        if voice_call is None:
            conversation = await self._ai_brain.start_conversation(
                organization_id,
                caller_phone_number=customer_number,
                channel=ConversationChannel.VOICE,
            )
            voice_call = await self._voice_calls.create(
                organization_id=organization_id,
                conversation_id=conversation.id,
                vapi_call_id=vapi_call_id,
                caller_number=customer_number,
            )
        conversation_id = voice_call.conversation_id
        bind_contextvars(
            conversation_id=str(conversation_id),
            organization_id=str(organization_id),
            turn_sequence=sequence,
        )
        logger.info("voice_turn_started", streaming=False)

        history = await self._conversations.list_messages(conversation_id)
        cached_reply = _last_answered_turn(history)

        if cached_reply is not None and cached_reply.customer_utterance == customer_utterance:
            # Vapi retried this turn (e.g. after a timeout) with the exact
            # same trailing utterance we already answered. Replaying
            # `send_message` would duplicate the transcript and re-bill the
            # AI provider for an identical turn — return the cached reply.
            return await self._cached_result(
                organization_id, conversation_id, cached_reply.reply_text
            )

        if cached_reply is not None and not self._supersession.is_current(
            vapi_call_id, sequence
        ):
            # A newer request for this same call arrived while this one was
            # queued on the lock. Vapi re-sends the full transcript every
            # time, so the newer request's utterance is a superset of this
            # one — generating here would burn a paid call on text that is
            # already stale before it is spoken.
            #
            # The cached reply is returned rather than an empty completion
            # because it is the only response shape with production
            # evidence behind it: this same value is already returned on the
            # retry path above, and the last live call showed Vapi silently
            # discarding six superseded responses that carried real content.
            # An empty completion has never been sent to Vapi, so its
            # behaviour on a live emergency call is unknown.
            #
            # Requires a cached reply to exist: with nothing prior to echo
            # there is no safe thing to say, so such a request falls through
            # and generates normally.
            logger.info(
                "vapi_chat_completion_superseded",
                vapi_call_id=vapi_call_id,
                sequence=sequence,
            )
            return await self._cached_result(
                organization_id, conversation_id, cached_reply.reply_text
            )

        result = await self._ai_brain.send_message(organization_id, conversation_id, customer_utterance)
        return ChatCompletionResult(
            reply_text=result.reply_message.content,
            should_end_call=result.conversation.status is ConversationStatus.COMPLETED,
            organization_id=organization_id,
            conversation_id=conversation_id,
        )

    async def _cached_result(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        reply_text: str,
    ) -> ChatCompletionResult:
        conversation = await self._ai_brain.get_conversation(organization_id, conversation_id)
        return ChatCompletionResult(
            reply_text=reply_text,
            should_end_call=conversation.status is ConversationStatus.COMPLETED,
            organization_id=organization_id,
            conversation_id=conversation_id,
        )

    async def handle_end_of_call_report(
        self,
        *,
        vapi_call_id: str,
        ended_reason: str | None,
        duration_seconds: int | None,
        recording_url: str | None,
    ) -> None:
        # The call is over, so its supersession ticket can never be consulted
        # again. Dropped first so an early return below cannot leak it.
        self._supersession.forget(vapi_call_id)

        voice_call = await self._voice_calls.get_by_vapi_call_id(vapi_call_id)
        if voice_call is None:
            # Not a caller-facing failure — Vapi's report arrived for a call
            # id we never processed a chat-completion for (e.g. the caller
            # hung up before saying anything). Nothing to reconcile.
            logger.warning("voice_call_not_found_for_end_of_call_report", vapi_call_id=vapi_call_id)
            return

        # `ended_reason` is Vapi's own enum string (`assistant-ended-call`,
        # `silence-timed-out`, ...), not caller data, and is the single most
        # useful field for telling a healthy hang-up from a failed turn.
        # `recording_url` is deliberately not logged: it dereferences to
        # caller audio.
        logger.info(
            "voice_end_of_call_report",
            conversation_id=str(voice_call.conversation_id),
            organization_id=str(voice_call.organization_id),
            ended_reason=ended_reason,
            duration_seconds=duration_seconds,
            has_recording=recording_url is not None,
        )
        await self._voice_calls.mark_ended(
            vapi_call_id,
            ended_reason=ended_reason,
            duration_seconds=duration_seconds,
            recording_url=recording_url,
        )

        conversation = await self._conversations.get_by_id(
            voice_call.organization_id, voice_call.conversation_id
        )
        if conversation is not None and conversation.status is not ConversationStatus.COMPLETED:
            # The caller hung up (or the line dropped) before the AI Brain
            # itself decided the conversation was complete.
            await self._conversations.complete(voice_call.conversation_id)

    async def _resolve_voice_line(
        self, assistant_id: str | None, phone_number_id: str | None
    ) -> VoiceLine:
        voice_line: VoiceLine | None = None
        if assistant_id:
            voice_line = await self._voice_lines.get_by_vapi_assistant_id(assistant_id)
        if voice_line is None and phone_number_id:
            voice_line = await self._voice_lines.get_by_vapi_phone_number_id(phone_number_id)
        if voice_line is None or not voice_line.is_active:
            logger.error(
                "voice_line_not_found",
                assistant_id=assistant_id,
                phone_number_id=phone_number_id,
            )
            raise VoiceLineNotFoundError()

        await self._require_voice_assistant_enabled(voice_line.organization_id)

        # Logged here rather than at each call site so the streaming and
        # non-streaming paths cannot report tenant resolution differently.
        # `matched_on` is what a PSTN investigation will actually need:
        # `phone_number_id` resolution has never been exercised live, and a
        # failure there is invisible in the request itself.
        logger.info(
            "voice_line_resolved",
            organization_id=str(voice_line.organization_id),
            matched_on="assistant_id" if assistant_id else "phone_number_id",
        )
        return voice_line

    async def _require_voice_assistant_enabled(self, organization_id: uuid.UUID) -> None:
        """The per-tenant kill switch, enforced server-side.

        Placed inside `_resolve_voice_line` rather than at each transport's
        entry point, because that is the one function both the streaming and
        non-streaming paths already share — so the switch cannot be honoured
        on one transport and forgotten on the other, which is exactly the
        kind of divergence a safety control must not have.

        Enforced *after* the line resolves and *before* any conversation row
        is created, so a disabled tenant accumulates no conversations, no
        outcomes, and no LLM spend from calls it has switched off.

        Fails OPEN when no repository is wired in, and only then. That is a
        deliberate asymmetry: this control exists to stop a misbehaving
        assistant, not to be a second way for a deployment mistake to take a
        business's phone line down. A wiring error therefore restores the
        pre-switch behaviour rather than silently disabling every tenant.
        The lookup failing is different — that is treated as unknown, and an
        unknown switch state is not a reason to drop a live call either."""
        if self._organizations is None:
            return
        try:
            organization = await self._organizations.get_by_id(organization_id)
        except Exception:
            logger.warning(
                "voice_kill_switch_lookup_failed",
                organization_id=str(organization_id),
                exc_info=True,
            )
            return
        if organization is not None and not organization.voice_assistant_enabled:
            logger.warning(
                "voice_assistant_disabled_for_organization",
                organization_id=str(organization_id),
            )
            raise VoiceAssistantDisabledError()
