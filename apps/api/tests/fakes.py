"""Test doubles shared across unit and integration tests. Kept in one place
so the AI Brain's dependencies (the LLM call, in particular) never have to
hit a real, paid, non-deterministic API in CI. Also holds the in-memory
repository fakes used to build a real `AIBrainService` for tests (both its
own unit tests and `VoiceService`'s, which wraps it) without a database."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from app.domain.ai.provider import AIProvider, AIReply, AIRequest
from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.domain.entities.business_profile import BusinessProfile
from app.domain.entities.conversation import Conversation, ConversationStatus
from app.domain.entities.conversation_message import ConversationMessage, MessageRole
from app.domain.entities.conversation_outcome import (
    CallClassification,
    ConversationOutcome,
    RecommendedAction,
)
from app.domain.entities.emergency_keyword import EmergencyKeyword
from app.domain.entities.faq_entry import FAQEntry
from app.domain.entities.service import Service
from app.domain.entities.service_area import ServiceArea
from app.domain.repositories.business_hours_repository import BusinessHoursRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.emergency_keyword_repository import EmergencyKeywordRepository
from app.domain.repositories.faq_repository import FAQRepository
from app.domain.repositories.service_area_repository import ServiceAreaRepository
from app.domain.repositories.service_repository import ServiceRepository


class FakeConversationRepository(ConversationRepository):
    def __init__(self) -> None:
        self._conversations: dict[uuid.UUID, Conversation] = {}
        self._messages: dict[uuid.UUID, list[ConversationMessage]] = {}

    async def create(self, *, organization_id, channel, caller_phone_number):
        now = datetime.now(timezone.utc)
        conversation = Conversation(
            id=uuid.uuid4(),
            organization_id=organization_id,
            channel=channel,
            status=ConversationStatus.ACTIVE,
            caller_phone_number=caller_phone_number,
            started_at=now,
            ended_at=None,
            created_at=now,
            updated_at=now,
        )
        self._conversations[conversation.id] = conversation
        self._messages[conversation.id] = []
        return conversation

    async def get_by_id(self, organization_id, conversation_id):
        conversation = self._conversations.get(conversation_id)
        if conversation is None or conversation.organization_id != organization_id:
            return None
        return conversation

    async def list_for_organization(self, organization_id, *, limit, offset):
        matches = [c for c in self._conversations.values() if c.organization_id == organization_id]
        matches.sort(key=lambda c: c.started_at, reverse=True)
        return matches[offset : offset + limit]

    async def add_message(self, conversation_id, *, role, content):
        message = ConversationMessage(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            role=role,
            content=content,
            created_at=datetime.now(timezone.utc),
        )
        self._messages[conversation_id].append(message)
        return message

    async def list_messages(self, conversation_id):
        return list(self._messages.get(conversation_id, []))

    async def complete(self, conversation_id):
        conversation = self._conversations[conversation_id]
        updated = replace(
            conversation,
            status=ConversationStatus.COMPLETED,
            ended_at=datetime.now(timezone.utc),
        )
        self._conversations[conversation_id] = updated
        return updated


class FakeConversationOutcomeRepository(ConversationOutcomeRepository):
    def __init__(self) -> None:
        self._outcomes: dict[uuid.UUID, ConversationOutcome] = {}

    async def upsert(
        self,
        conversation_id,
        *,
        classification,
        confidence,
        recommended_action,
        matched_service_id,
        customer_name,
        customer_phone,
        customer_address,
        summary,
    ):
        existing = self._outcomes.get(conversation_id)
        outcome = ConversationOutcome(
            id=existing.id if existing else uuid.uuid4(),
            conversation_id=conversation_id,
            classification=classification,
            confidence=confidence,
            recommended_action=recommended_action,
            matched_service_id=matched_service_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_address=customer_address,
            summary=summary,
            updated_at=datetime.now(timezone.utc),
        )
        self._outcomes[conversation_id] = outcome
        return outcome

    async def get_by_conversation_id(self, conversation_id):
        return self._outcomes.get(conversation_id)


class FakeBusinessProfileRepository(BusinessProfileRepository):
    async def get_by_organization_id(self, organization_id) -> BusinessProfile | None:
        return None

    async def upsert(self, **kwargs):
        raise NotImplementedError


class FakeBusinessHoursRepository(BusinessHoursRepository):
    async def get_weekly(self, organization_id) -> list[WeeklyHours]:
        return []

    async def replace_weekly(self, organization_id, entries):
        raise NotImplementedError

    async def list_exceptions(self, organization_id) -> list[HoursException]:
        return []

    async def add_exception(self, **kwargs):
        raise NotImplementedError

    async def delete_exception(self, organization_id, exception_id):
        raise NotImplementedError


class FakeServiceRepository(ServiceRepository):
    def __init__(self, services: list[Service] | None = None) -> None:
        self._services = services or []

    async def list(self, organization_id):
        return self._services

    async def create(self, **kwargs):
        raise NotImplementedError

    async def update(self, *args, **kwargs):
        raise NotImplementedError

    async def delete(self, *args, **kwargs):
        raise NotImplementedError


class FakeServiceAreaRepository(ServiceAreaRepository):
    async def list(self, organization_id) -> list[ServiceArea]:
        return []

    async def create(self, **kwargs):
        raise NotImplementedError

    async def delete(self, *args, **kwargs):
        raise NotImplementedError


class FakeFAQRepository(FAQRepository):
    async def list(self, organization_id) -> list[FAQEntry]:
        return []

    async def create(self, **kwargs):
        raise NotImplementedError

    async def update(self, *args, **kwargs):
        raise NotImplementedError

    async def delete(self, *args, **kwargs):
        raise NotImplementedError


class FakeEmergencyKeywordRepository(EmergencyKeywordRepository):
    def __init__(self, keywords: list[EmergencyKeyword] | None = None) -> None:
        self._keywords = keywords or []

    async def list(self, organization_id):
        return self._keywords

    async def create(self, **kwargs):
        raise NotImplementedError

    async def delete(self, *args, **kwargs):
        raise NotImplementedError


def default_reply(**overrides) -> AIReply:
    kwargs = dict(
        message_to_customer="Thanks for calling — how else can I help?",
        classification=CallClassification.NON_EMERGENCY,
        confidence=0.9,
        recommended_action=RecommendedAction.ANSWER_FAQ,
        matched_service_name=None,
        customer_name=None,
        customer_phone=None,
        customer_address=None,
        is_conversation_complete=False,
        summary="Routine inquiry.",
    )
    kwargs.update(overrides)
    return AIReply(**kwargs)


class FakeAIProvider(AIProvider):
    """Replies are scripted via `queue_reply`; if the queue is empty a
    benign non-emergency reply is returned so tests that don't care about
    the AI's specific output don't have to script every turn."""

    def __init__(self) -> None:
        self._queue: list[AIReply] = []
        self.requests: list[AIRequest] = []

    def queue_reply(self, reply: AIReply) -> None:
        self._queue.append(reply)

    async def generate_reply(self, request: AIRequest) -> AIReply:
        self.requests.append(request)
        if self._queue:
            return self._queue.pop(0)
        return default_reply()
