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
from dataclasses import dataclass
from datetime import date

from app.core.config import Settings
from app.domain.ai.provider import AIProvider, AIRequest, ConversationTurn
from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.domain.entities.business_profile import BusinessProfile
from app.domain.entities.conversation import Conversation, ConversationChannel, ConversationStatus
from app.domain.entities.conversation_message import ConversationMessage, MessageRole
from app.domain.entities.conversation_outcome import ConversationOutcome
from app.domain.entities.emergency_keyword import EmergencyKeyword
from app.domain.entities.faq_entry import FAQEntry
from app.domain.entities.service import Service
from app.domain.entities.service_area import ServiceArea
from app.domain.exceptions import (
    ConversationCompletedError,
    ConversationLimitExceededError,
    EntityNotFoundError,
)
from app.domain.repositories.business_hours_repository import BusinessHoursRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.emergency_keyword_repository import EmergencyKeywordRepository
from app.domain.repositories.faq_repository import FAQRepository
from app.domain.repositories.service_area_repository import ServiceAreaRepository
from app.domain.repositories.service_repository import ServiceRepository

from .prompt_builder import build_system_prompt


@dataclass(frozen=True, slots=True)
class ConversationTurnResult:
    conversation: Conversation
    reply_message: ConversationMessage
    outcome: ConversationOutcome


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

    async def start_conversation(
        self, organization_id: uuid.UUID, *, caller_phone_number: str | None = None
    ) -> Conversation:
        return await self._conversations.create(
            organization_id=organization_id,
            channel=ConversationChannel.TEXT,
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
        conversation = await self.get_conversation(organization_id, conversation_id)
        if conversation.status is ConversationStatus.COMPLETED:
            raise ConversationCompletedError()

        history = await self._conversations.list_messages(conversation_id)
        if len(history) >= self._settings.AI_MAX_CONVERSATION_TURNS * 2:
            raise ConversationLimitExceededError()

        await self._conversations.add_message(
            conversation_id, role=MessageRole.CUSTOMER, content=customer_message
        )

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
        )

        provider_history = tuple(
            ConversationTurn(
                role="customer" if m.role is MessageRole.CUSTOMER else "assistant",
                content=m.content,
            )
            for m in history
        )
        reply = await self._ai.generate_reply(
            AIRequest(
                system_prompt=system_prompt,
                history=provider_history,
                latest_customer_message=customer_message,
            )
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
