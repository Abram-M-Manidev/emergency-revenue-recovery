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

from app.application.services.ai_brain_service import AIBrainService, ConversationTextDelta
from app.domain.entities.conversation import ConversationChannel, ConversationStatus
from app.domain.entities.conversation_message import ConversationMessage, MessageRole
from app.domain.entities.voice_call import VoiceCall
from app.domain.entities.voice_line import VoiceLine
from app.domain.exceptions import EntityNotFoundError, VoiceLineNotFoundError
from app.domain.locks import CallLock, NullCallLock
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.voice_call_repository import VoiceCallRepository
from app.domain.repositories.voice_line_repository import VoiceLineRepository

logger = structlog.get_logger("app.voice")


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
    ) -> None:
        self._voice_lines = voice_line_repository
        self._voice_calls = voice_call_repository
        self._conversations = conversation_repository
        self._ai_brain = ai_brain_service
        self._call_lock = call_lock or NullCallLock()
        self._supersession = supersession or _SUPERSESSION

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
        sequence = self._supersession.claim(vapi_call_id)

        async with self._call_lock.hold(vapi_call_id):
            voice_line = await self._resolve_voice_line(assistant_id, phone_number_id)
            organization_id = voice_line.organization_id
            conversation_id = await self._conversation_id_for(
                organization_id, vapi_call_id, customer_number
            )

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

            async for event in self._ai_brain.send_message_stream(
                organization_id, conversation_id, customer_utterance
            ):
                if isinstance(event, ConversationTextDelta):
                    yield VoiceTextDelta(event.text)
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
        return voice_line
