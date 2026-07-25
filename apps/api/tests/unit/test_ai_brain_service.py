"""Unit tests for AIBrainService using in-memory fake repositories and a
scripted FakeAIProvider — no database, no real LLM call. Establishes the
"in-memory fakes" testability AuthService's docstring already aspires to."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.application.services.ai_brain_service import AIBrainService
from app.domain.entities.conversation import ConversationChannel, ConversationStatus
from app.domain.entities.conversation_message import MessageRole
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.emergency_keyword import EmergencyKeyword
from app.domain.entities.service import Service
from app.domain.exceptions import ConversationCompletedError, ConversationLimitExceededError
from tests.fakes import (
    FakeAIProvider,
    FakeBusinessHoursRepository,
    FakeBusinessProfileRepository,
    FakeConversationOutcomeRepository,
    FakeConversationRepository,
    FakeEmergencyKeywordRepository,
    FakeFAQRepository,
    FakeServiceAreaRepository,
    FakeServiceRepository,
    default_reply,
)

_ORG_ID = uuid.uuid4()


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
        default_duration_minutes=None,
    )
    provider = FakeAIProvider()
    provider.queue_reply(default_reply(matched_service_name="Furnace Repair"))
    service, _ = _make_service(ai_provider=provider, services=[service_row])
    conversation = await service.start_conversation(_ORG_ID)

    result = await service.send_message(_ORG_ID, conversation.id, "My furnace is broken")

    assert result.outcome.matched_service_id == service_row.id


@pytest.mark.asyncio
async def test_start_conversation_defaults_to_text_channel():
    service, _ = _make_service()

    conversation = await service.start_conversation(_ORG_ID)

    assert conversation.channel is ConversationChannel.TEXT


@pytest.mark.asyncio
async def test_start_conversation_accepts_voice_channel():
    service, _ = _make_service()

    conversation = await service.start_conversation(
        _ORG_ID, caller_phone_number="+15551234567", channel=ConversationChannel.VOICE
    )

    assert conversation.channel is ConversationChannel.VOICE
    assert conversation.caller_phone_number == "+15551234567"
