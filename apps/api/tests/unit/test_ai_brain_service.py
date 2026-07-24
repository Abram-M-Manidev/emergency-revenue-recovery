"""Unit tests for AIBrainService using in-memory fake repositories and a
scripted FakeAIProvider — no database, no real LLM call. Establishes the
"in-memory fakes" testability AuthService's docstring already aspires to."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.application.services.ai_brain_service import AIBrainService
from app.domain.entities.conversation import Conversation, ConversationStatus
from app.domain.entities.conversation_message import ConversationMessage, MessageRole
from app.domain.entities.conversation_outcome import (
    CallClassification,
    ConversationOutcome,
    RecommendedAction,
)
from app.domain.entities.emergency_keyword import EmergencyKeyword
from app.domain.entities.service import Service
from app.domain.exceptions import ConversationCompletedError, ConversationLimitExceededError
from app.domain.repositories.business_hours_repository import BusinessHoursRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.emergency_keyword_repository import EmergencyKeywordRepository
from app.domain.repositories.faq_repository import FAQRepository
from app.domain.repositories.service_area_repository import ServiceAreaRepository
from app.domain.repositories.service_repository import ServiceRepository
from tests.fakes import FakeAIProvider, default_reply

_ORG_ID = uuid.uuid4()


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
    async def get_by_organization_id(self, organization_id):
        return None

    async def upsert(self, **kwargs):
        raise NotImplementedError


class FakeBusinessHoursRepository(BusinessHoursRepository):
    async def get_weekly(self, organization_id):
        return []

    async def replace_weekly(self, organization_id, entries):
        raise NotImplementedError

    async def list_exceptions(self, organization_id):
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
    async def list(self, organization_id):
        return []

    async def create(self, **kwargs):
        raise NotImplementedError

    async def delete(self, *args, **kwargs):
        raise NotImplementedError


class FakeFAQRepository(FAQRepository):
    async def list(self, organization_id):
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


def _make_service(
    *, ai_provider: FakeAIProvider | None = None, services=None, emergency_keywords=None, max_turns=20
) -> tuple[AIBrainService, FakeAIProvider]:
    provider = ai_provider or FakeAIProvider()
    service = AIBrainService(
        conversation_repository=FakeConversationRepository(),
        conversation_outcome_repository=FakeConversationOutcomeRepository(),
        ai_provider=provider,
        business_profile_repository=FakeBusinessProfileRepository(),
        business_hours_repository=FakeBusinessHoursRepository(),
        service_repository=FakeServiceRepository(services),
        service_area_repository=FakeServiceAreaRepository(),
        faq_repository=FakeFAQRepository(),
        emergency_keyword_repository=FakeEmergencyKeywordRepository(emergency_keywords),
        settings=SimpleNamespace(AI_MAX_CONVERSATION_TURNS=max_turns),
    )
    return service, provider


@pytest.mark.asyncio
async def test_send_message_persists_customer_and_assistant_messages():
    service, _ = _make_service()
    conversation = await service.start_conversation(_ORG_ID)

    result = await service.send_message(_ORG_ID, conversation.id, "What are your hours?")

    messages = await service.list_messages(_ORG_ID, conversation.id)
    assert [m.role for m in messages] == [MessageRole.CUSTOMER, MessageRole.ASSISTANT]
    assert messages[0].content == "What are your hours?"
    assert result.reply_message.content == messages[1].content


@pytest.mark.asyncio
async def test_send_message_upserts_outcome_from_reply():
    provider = FakeAIProvider()
    provider.queue_reply(
        default_reply(
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
            customer_name="Jane Doe",
        )
    )
    service, _ = _make_service(ai_provider=provider)
    conversation = await service.start_conversation(_ORG_ID)

    result = await service.send_message(_ORG_ID, conversation.id, "My basement is flooding!")

    assert result.outcome.classification is CallClassification.EMERGENCY
    assert result.outcome.recommended_action is RecommendedAction.CREATE_EMERGENCY_TICKET
    assert result.outcome.customer_name == "Jane Doe"

    stored = await service.get_outcome(_ORG_ID, conversation.id)
    assert stored is not None
    assert stored.classification is CallClassification.EMERGENCY


@pytest.mark.asyncio
async def test_conversation_completes_when_reply_says_so():
    provider = FakeAIProvider()
    provider.queue_reply(default_reply(is_conversation_complete=True))
    service, _ = _make_service(ai_provider=provider)
    conversation = await service.start_conversation(_ORG_ID)

    result = await service.send_message(_ORG_ID, conversation.id, "That's all, thanks.")

    assert result.conversation.status is ConversationStatus.COMPLETED
    assert result.conversation.ended_at is not None


@pytest.mark.asyncio
async def test_sending_message_to_completed_conversation_raises():
    provider = FakeAIProvider()
    provider.queue_reply(default_reply(is_conversation_complete=True))
    service, _ = _make_service(ai_provider=provider)
    conversation = await service.start_conversation(_ORG_ID)
    await service.send_message(_ORG_ID, conversation.id, "That's all, thanks.")

    with pytest.raises(ConversationCompletedError):
        await service.send_message(_ORG_ID, conversation.id, "One more thing...")


@pytest.mark.asyncio
async def test_turn_cap_exceeded_raises():
    service, _ = _make_service(max_turns=1)
    conversation = await service.start_conversation(_ORG_ID)

    await service.send_message(_ORG_ID, conversation.id, "First message")
    with pytest.raises(ConversationLimitExceededError):
        await service.send_message(_ORG_ID, conversation.id, "Second message")


@pytest.mark.asyncio
async def test_emergency_keyword_hint_is_passed_to_provider_when_matched():
    keywords = [EmergencyKeyword(id=uuid.uuid4(), organization_id=_ORG_ID, phrase="no heat", notes=None)]
    service, provider = _make_service(emergency_keywords=keywords)
    conversation = await service.start_conversation(_ORG_ID)

    await service.send_message(_ORG_ID, conversation.id, "We have no heat and it's freezing!")

    assert "emergency keyword" in provider.requests[-1].system_prompt.lower()


@pytest.mark.asyncio
async def test_matched_service_id_is_resolved_from_service_name():
    service_row = Service(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        name="Furnace Repair",
        description=None,
        category=None,
        is_emergency_eligible=True,
        is_active=True,
    )
    provider = FakeAIProvider()
    provider.queue_reply(default_reply(matched_service_name="Furnace Repair"))
    service, _ = _make_service(ai_provider=provider, services=[service_row])
    conversation = await service.start_conversation(_ORG_ID)

    result = await service.send_message(_ORG_ID, conversation.id, "My furnace is broken")

    assert result.outcome.matched_service_id == service_row.id
