"""Orchestrates a text-based conversation with the AI Brain: loads an
organization's Business Knowledge (Milestone 2), grounds a prompt in it,
calls the configured `AIProvider`, and persists the resulting messages and
outcome.

This service never talks to OpenAI's SDK directly (see
`app/domain/ai/provider.py`) and never touches SQLAlchemy directly (see the
repository interfaces) — both are injected, so it can be unit-tested with
in-memory fakes, same intent as `AuthService`."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date

import structlog

from app.core.config import Settings
from app.domain.ai.provider import (
    AIModelProfile,
    AIProvider,
    AIReply,
    AIRequest,
    AITextDelta,
    ConversationTurn,
)
from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.domain.entities.business_profile import BusinessProfile
from app.domain.entities.conversation import Conversation, ConversationChannel, ConversationStatus
from app.domain.entities.conversation_message import ConversationMessage, MessageRole
from app.domain.entities.conversation_outcome import ConversationOutcome
from app.domain.entities.emergency_keyword import EmergencyKeyword
from app.domain.entities.faq_entry import FAQEntry
from app.domain.entities.known_caller import KnownCaller
from app.domain.entities.service import Service
from app.domain.entities.service_area import ServiceArea
from app.domain.exceptions import (
    AIProviderUnavailableError,
    ConversationCompletedError,
    ConversationLimitExceededError,
    EntityNotFoundError,
)
from app.domain.repositories.business_hours_repository import BusinessHoursRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.caller_identity_repository import CallerIdentityRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.emergency_keyword_repository import EmergencyKeywordRepository
from app.domain.repositories.faq_repository import FAQRepository
from app.domain.repositories.service_area_repository import ServiceAreaRepository
from app.domain.repositories.service_repository import ServiceRepository
from app.shared.logging.timing import elapsed_ms, now

from .prompt_builder import build_system_prompt

logger = structlog.get_logger("app.ai_brain")


@dataclass(frozen=True, slots=True)
class ConversationTurnResult:
    conversation: Conversation
    reply_message: ConversationMessage
    outcome: ConversationOutcome


@dataclass(frozen=True, slots=True)
class ConversationTextDelta:
    """A fragment of the caller-facing reply, forwarded as the model
    produces it."""

    text: str


@dataclass(frozen=True, slots=True)
class ConversationTurnComplete:
    """Terminal event: everything is persisted and the outcome is
    authoritative."""

    result: ConversationTurnResult


ConversationStreamEvent = ConversationTextDelta | ConversationTurnComplete


# A VOICE conversation is a live phone call: every second spent reasoning is
# silence the caller hears, so it gets the latency-optimised profile. TEXT is
# the dashboard simulation, where depth is worth the wait. Derived from the
# `Conversation` the service already loads — no caller has to pass anything,
# and `VoiceService` already creates its conversations with
# `channel=ConversationChannel.VOICE`, so the routing is automatic.
_CHANNEL_PROFILES: dict[ConversationChannel, AIModelProfile] = {
    ConversationChannel.TEXT: AIModelProfile.QUALITY,
    ConversationChannel.VOICE: AIModelProfile.REALTIME,
}


class AIBrainService:
    def __init__(
        self,
        *,
        conversation_repository: ConversationRepository,
        conversation_outcome_repository: ConversationOutcomeRepository,
        ai_provider: AIProvider,
        business_profile_repository: BusinessProfileRepository,
        business_hours_repository: BusinessHoursRepository,
        service_repository: ServiceRepository,
        service_area_repository: ServiceAreaRepository,
        faq_repository: FAQRepository,
        emergency_keyword_repository: EmergencyKeywordRepository,
        settings: Settings,
        # Optional so the text/simulation path, and every existing
        # construction site, are unaffected: without it P5 grounding is
        # simply never attempted.
        caller_identity_repository: CallerIdentityRepository | None = None,
    ) -> None:
        self._conversations = conversation_repository
        self._outcomes = conversation_outcome_repository
        self._ai = ai_provider
        self._profile = business_profile_repository
        self._hours = business_hours_repository
        self._services = service_repository
        self._service_areas = service_area_repository
        self._faqs = faq_repository
        self._emergency_keywords = emergency_keyword_repository
        self._settings = settings
        self._caller_identities = caller_identity_repository

    async def start_conversation(
        self,
        organization_id: uuid.UUID,
        *,
        caller_phone_number: str | None = None,
        channel: ConversationChannel = ConversationChannel.TEXT,
    ) -> Conversation:
        return await self._conversations.create(
            organization_id=organization_id,
            channel=channel,
            caller_phone_number=caller_phone_number,
        )

    async def get_conversation(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Conversation:
        conversation = await self._conversations.get_by_id(organization_id, conversation_id)
        if conversation is None:
            raise EntityNotFoundError("Conversation", str(conversation_id))
        return conversation

    async def list_conversations(
        self, organization_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[Conversation]:
        return await self._conversations.list_for_organization(
            organization_id, limit=limit, offset=offset
        )

    async def list_messages(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[ConversationMessage]:
        await self.get_conversation(organization_id, conversation_id)
        return await self._conversations.list_messages(conversation_id)

    async def get_outcome(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> ConversationOutcome | None:
        await self.get_conversation(organization_id, conversation_id)
        return await self._outcomes.get_by_conversation_id(conversation_id)

    async def send_message(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID, customer_message: str
    ) -> ConversationTurnResult:
        conversation, request, services = await self._prepare_turn(
            organization_id, conversation_id, customer_message
        )
        reply = await self._ai.generate_reply(request)
        return await self._persist_turn(
            conversation, conversation_id, services, reply, customer_message
        )

    async def send_message_stream(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID, customer_message: str
    ) -> AsyncIterator[ConversationStreamEvent]:
        """Streaming twin of `send_message`.

        Yields the caller-facing sentence in fragments as the model writes
        it, then performs the *identical* persistence once the complete,
        validated reply arrives — same messages, same outcome upsert, same
        completion rule. Nothing is written from a partial response, so a
        stream that dies halfway leaves no half-formed outcome behind."""
        conversation, request, services = await self._prepare_turn(
            organization_id, conversation_id, customer_message
        )

        reply: AIReply | None = None
        async for event in self._ai.stream_reply(request):
            if isinstance(event, AITextDelta):
                yield ConversationTextDelta(event.text)
            else:
                reply = event.reply

        if reply is None:
            # A provider that ended its stream without the terminal event
            # produced no authoritative result; treating that as success
            # would persist nothing and silently end the caller's turn.
            raise AIProviderUnavailableError("The AI Brain returned an incomplete response.")

        yield ConversationTurnComplete(
            await self._persist_turn(
                conversation, conversation_id, services, reply, customer_message
            )
        )

    # --- shared turn lifecycle (identical for streaming and non-streaming) ---

    async def _prepare_turn(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID, customer_message: str
    ) -> tuple[Conversation, AIRequest, list[Service]]:
        conversation = await self.get_conversation(organization_id, conversation_id)
        if conversation.status is ConversationStatus.COMPLETED:
            raise ConversationCompletedError()

        history = await self._conversations.list_messages(conversation_id)
        if len(history) >= self._settings.AI_MAX_CONVERSATION_TURNS * 2:
            raise ConversationLimitExceededError()

        # The customer message is deliberately NOT written here. It used to
        # be, and that was the H3 defect: a streamed turn that died between
        # this point and `_persist_turn` left the message flushed into a
        # transaction that FastAPI then committed anyway — because the exit
        # stack closes *cleanly* on a client disconnect, so `get_db` resumes
        # past its `yield` and commits. `GeneratorExit`/`CancelledError`
        # never reach `get_db`'s `except Exception`, so nothing rolled it
        # back. The result was a customer-only turn in the history, which
        # then replayed into every later prompt of that call.
        #
        # Nothing between here and `_persist_turn` needs it persisted:
        # `provider_history` below is built from `history`, which was read
        # *before* this point, and the new utterance travels separately as
        # `latest_customer_message`. Writing both messages together in
        # `_persist_turn` makes "customer message only" unrepresentable
        # rather than merely unlikely — see its docstring.

        (
            profile,
            weekly_hours,
            hours_exceptions,
            services,
            service_areas,
            faqs,
            emergency_keywords,
        ) = await self._load_business_knowledge(organization_id)

        keyword_hint = any(
            keyword.phrase.lower() in customer_message.lower() for keyword in emergency_keywords
        )
        known_caller = await self._resolve_known_caller(conversation)
        system_prompt = build_system_prompt(
            profile=profile,
            weekly_hours=weekly_hours,
            hours_exceptions=hours_exceptions,
            services=services,
            service_areas=service_areas,
            faqs=faqs,
            emergency_keywords=emergency_keywords,
            today=date.today(),
            emergency_keyword_hint=keyword_hint,
            known_caller=known_caller,
        )

        provider_history = tuple(
            ConversationTurn(
                role="customer" if m.role is MessageRole.CUSTOMER else "assistant",
                content=m.content,
            )
            for m in history
        )
        request = AIRequest(
            system_prompt=system_prompt,
            history=provider_history,
            latest_customer_message=customer_message,
            profile=_CHANNEL_PROFILES.get(conversation.channel, AIModelProfile.QUALITY),
        )
        return conversation, request, services

    async def _resolve_known_caller(self, conversation: Conversation) -> KnownCaller | None:
        """Known-caller grounding (P5). Strictly read-only and strictly
        best-effort.

        Returns None — leaving the prompt byte-identical to the pre-P5 one
        — for every case except a caller ID that resolves to exactly one
        customer: no repository wired, no caller ID (the whole text path),
        a blank or malformed number, no association, or an ambiguous one.

        Ambiguity is a deliberate refusal, not a gap. A shared household or
        office line maps to several customers, and there is no honest way
        to pick between them: the caller is whoever picked up the phone.
        Guessing would greet the wrong person by name, so nothing is
        grounded and the ordinary flow collects the details instead.

        A repository failure must never cost the caller their emergency, so
        it is swallowed after logging: classification, ticket creation, C1
        persistence, and `endCall` all continue exactly as they would for
        an unrecognised caller."""
        if self._caller_identities is None or not conversation.caller_phone_number:
            return None

        try:
            customers = await self._caller_identities.find_customers_by_caller_number(
                conversation.organization_id, conversation.caller_phone_number
            )
        except Exception:
            # Deliberately broad: any storage-layer failure degrades to an
            # unknown caller rather than failing a live emergency turn.
            logger.warning("known_caller_lookup_failed", exc_info=True)
            return None

        if len(customers) != 1:
            # No PII in either branch — a count, never a name.
            logger.info("known_caller_not_grounded", matches=len(customers))
            return None

        customer = customers[0]
        logger.info("known_caller_grounded", has_address_on_file=bool(customer.address))
        return KnownCaller(name=customer.full_name, address=customer.address)

    async def _persist_turn(
        self,
        conversation: Conversation,
        conversation_id: uuid.UUID,
        services: list[Service],
        reply: AIReply,
        customer_message: str,
    ) -> ConversationTurnResult:
        # Wraps the assistant message + outcome upsert + completion flag.
        # Bracketed rather than timed as a whole from outside because this
        # is the work that happens *after* the caller has already started
        # hearing the reply — the streamed turn is not finished until it
        # ends, so a slow write here delays `[DONE]` and, on a final turn,
        # the hang-up.
        persistence_started_at = now()
        # Both halves of the turn are written here, in this order, so the
        # pair is only ever created together. The customer message is
        # written first purely to preserve read order: `list_messages`
        # sorts by `created_at`, and both rows take the transaction
        # timestamp (`server_default=func.now()`), so insertion order is
        # what actually separates them — as it already did before H3, when
        # the two writes were in the same transaction but further apart.
        await self._conversations.add_message(
            conversation_id, role=MessageRole.CUSTOMER, content=customer_message
        )
        reply_message = await self._conversations.add_message(
            conversation_id, role=MessageRole.ASSISTANT, content=reply.message_to_customer
        )

        matched_service_id = next(
            (s.id for s in services if s.name == reply.matched_service_name), None
        )
        outcome = await self._outcomes.upsert(
            conversation_id,
            classification=reply.classification,
            confidence=reply.confidence,
            recommended_action=reply.recommended_action,
            matched_service_id=matched_service_id,
            customer_name=reply.customer_name,
            customer_phone=reply.customer_phone,
            customer_address=reply.customer_address,
            summary=reply.summary,
        )

        if reply.is_conversation_complete:
            conversation = await self._conversations.complete(conversation_id)

        # Classification/action/confidence are the AI's decision, not caller
        # data — they are what an incident review needs and carry no PII.
        # The reply text, summary, and extracted contact fields are not
        # logged; `matched_service` is org-owned catalogue data.
        logger.info(
            "conversation_turn_persisted",
            elapsed_ms=elapsed_ms(persistence_started_at),
            classification=outcome.classification.value,
            recommended_action=outcome.recommended_action.value,
            confidence=outcome.confidence,
            matched_service=reply.matched_service_name,
            conversation_complete=reply.is_conversation_complete,
        )

        return ConversationTurnResult(
            conversation=conversation, reply_message=reply_message, outcome=outcome
        )

    async def _load_business_knowledge(
        self, organization_id: uuid.UUID
    ) -> tuple[
        BusinessProfile | None,
        list[WeeklyHours],
        list[HoursException],
        list[Service],
        list[ServiceArea],
        list[FAQEntry],
        list[EmergencyKeyword],
    ]:
        profile = await self._profile.get_by_organization_id(organization_id)
        weekly_hours = await self._hours.get_weekly(organization_id)
        hours_exceptions = await self._hours.list_exceptions(organization_id)
        services = await self._services.list(organization_id)
        service_areas = await self._service_areas.list(organization_id)
        faqs = await self._faqs.list(organization_id)
        emergency_keywords = await self._emergency_keywords.list(organization_id)
        return (
            profile,
            weekly_hours,
            hours_exceptions,
            services,
            service_areas,
            faqs,
            emergency_keywords,
        )
