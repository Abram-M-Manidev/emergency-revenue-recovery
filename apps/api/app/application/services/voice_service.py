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
from dataclasses import dataclass

import structlog

from app.application.services.ai_brain_service import AIBrainService
from app.domain.entities.conversation import ConversationChannel, ConversationStatus
from app.domain.entities.conversation_message import MessageRole
from app.domain.entities.voice_call import VoiceCall
from app.domain.entities.voice_line import VoiceLine
from app.domain.exceptions import EntityNotFoundError, VoiceLineNotFoundError
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.voice_call_repository import VoiceCallRepository
from app.domain.repositories.voice_line_repository import VoiceLineRepository

logger = structlog.get_logger("app.voice")


@dataclass(frozen=True, slots=True)
class ChatCompletionResult:
    reply_text: str
    should_end_call: bool


class VoiceService:
    def __init__(
        self,
        *,
        voice_line_repository: VoiceLineRepository,
        voice_call_repository: VoiceCallRepository,
        conversation_repository: ConversationRepository,
        ai_brain_service: AIBrainService,
    ) -> None:
        self._voice_lines = voice_line_repository
        self._voice_calls = voice_call_repository
        self._conversations = conversation_repository
        self._ai_brain = ai_brain_service

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
        if (
            len(history) >= 2
            and history[-1].role is MessageRole.ASSISTANT
            and history[-2].role is MessageRole.CUSTOMER
            and history[-2].content == customer_utterance
        ):
            # Vapi retried this turn (e.g. after a timeout) with the exact
            # same trailing utterance we already answered. Replaying
            # `send_message` would duplicate the transcript and re-bill the
            # AI provider for an identical turn — return the cached reply
            # instead. This does not protect against a genuinely concurrent
            # duplicate request racing the still-in-flight first attempt;
            # it only covers the common sequential-retry-after-completion
            # case, which is the one Vapi's own timeout/retry behavior
            # produces.
            conversation = await self._ai_brain.get_conversation(organization_id, conversation_id)
            return ChatCompletionResult(
                reply_text=history[-1].content,
                should_end_call=conversation.status is ConversationStatus.COMPLETED,
            )

        result = await self._ai_brain.send_message(organization_id, conversation_id, customer_utterance)
        return ChatCompletionResult(
            reply_text=result.reply_message.content,
            should_end_call=result.conversation.status is ConversationStatus.COMPLETED,
        )

    async def handle_end_of_call_report(
        self,
        *,
        vapi_call_id: str,
        ended_reason: str | None,
        duration_seconds: int | None,
        recording_url: str | None,
    ) -> None:
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
