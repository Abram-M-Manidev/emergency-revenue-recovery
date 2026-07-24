"""Unit tests for VoiceService using in-memory fakes — no database, no real
LLM or Vapi call. `VoiceService` is built on top of a *real* `AIBrainService`
(wired with the same fakes `test_ai_brain_service.py` uses) rather than a
fake AI Brain, so these tests exercise the real hand-off between the two
services, not a mocked stand-in for it."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.application.services.ai_brain_service import AIBrainService
from app.application.services.voice_service import VoiceService
from app.domain.entities.conversation import ConversationChannel, ConversationStatus
from app.domain.entities.voice_call import VoiceCall
from app.domain.entities.voice_line import VoiceLine, VoiceProvider
from app.domain.exceptions import EntityNotFoundError, VoiceLineNotFoundError
from app.domain.repositories.voice_call_repository import VoiceCallRepository
from app.domain.repositories.voice_line_repository import VoiceLineRepository
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
_ASSISTANT_ID = "asst_test_1"
_PHONE_NUMBER_ID = "pn_test_1"


class FakeVoiceLineRepository(VoiceLineRepository):
    def __init__(self, lines: list[VoiceLine] | None = None) -> None:
        self._lines: dict[uuid.UUID, VoiceLine] = {line.id: line for line in (lines or [])}

    async def get_by_organization_id(self, organization_id):
        return next(
            (l for l in self._lines.values() if l.organization_id == organization_id), None
        )

    async def get_by_vapi_assistant_id(self, assistant_id):
        return next((l for l in self._lines.values() if l.vapi_assistant_id == assistant_id), None)

    async def get_by_vapi_phone_number_id(self, phone_number_id):
        return next(
            (l for l in self._lines.values() if l.vapi_phone_number_id == phone_number_id), None
        )

    async def create(
        self, *, organization_id, provider, vapi_assistant_id, vapi_phone_number_id, phone_number
    ):
        now = datetime.now(timezone.utc)
        line = VoiceLine(
            id=uuid.uuid4(),
            organization_id=organization_id,
            provider=provider,
            vapi_assistant_id=vapi_assistant_id,
            vapi_phone_number_id=vapi_phone_number_id,
            phone_number=phone_number,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        self._lines[line.id] = line
        return line


class FakeVoiceCallRepository(VoiceCallRepository):
    def __init__(self) -> None:
        self._calls: dict[str, VoiceCall] = {}

    async def get_by_vapi_call_id(self, vapi_call_id):
        return self._calls.get(vapi_call_id)

    async def get_by_conversation_id(self, conversation_id):
        return next((c for c in self._calls.values() if c.conversation_id == conversation_id), None)

    async def create(self, *, organization_id, conversation_id, vapi_call_id, caller_number):
        now = datetime.now(timezone.utc)
        call = VoiceCall(
            id=uuid.uuid4(),
            organization_id=organization_id,
            conversation_id=conversation_id,
            vapi_call_id=vapi_call_id,
            caller_number=caller_number,
            started_at=now,
            ended_at=None,
            ended_reason=None,
            duration_seconds=None,
            recording_url=None,
            created_at=now,
            updated_at=now,
        )
        self._calls[vapi_call_id] = call
        return call

    async def mark_ended(self, vapi_call_id, *, ended_reason, duration_seconds, recording_url):
        call = self._calls[vapi_call_id]
        updated = replace(
            call,
            ended_at=call.ended_at or datetime.now(timezone.utc),
            ended_reason=ended_reason,
            duration_seconds=duration_seconds,
            recording_url=recording_url,
        )
        self._calls[vapi_call_id] = updated
        return updated


def _make_voice_service(
    *, ai_provider: FakeAIProvider | None = None, voice_lines: list[VoiceLine] | None = None
):
    provider = ai_provider or FakeAIProvider()
    conversation_repo = FakeConversationRepository()
    ai_brain = AIBrainService(
        conversation_repository=conversation_repo,
        conversation_outcome_repository=FakeConversationOutcomeRepository(),
        ai_provider=provider,
        business_profile_repository=FakeBusinessProfileRepository(),
        business_hours_repository=FakeBusinessHoursRepository(),
        service_repository=FakeServiceRepository(),
        service_area_repository=FakeServiceAreaRepository(),
        faq_repository=FakeFAQRepository(),
        emergency_keyword_repository=FakeEmergencyKeywordRepository(),
        settings=SimpleNamespace(AI_MAX_CONVERSATION_TURNS=20),
    )
    voice_line_repo = FakeVoiceLineRepository(voice_lines)
    voice_call_repo = FakeVoiceCallRepository()
    service = VoiceService(
        voice_line_repository=voice_line_repo,
        voice_call_repository=voice_call_repo,
        conversation_repository=conversation_repo,
        ai_brain_service=ai_brain,
    )
    return service, provider, conversation_repo, voice_call_repo, voice_line_repo


def _voice_line(**overrides) -> VoiceLine:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        provider=VoiceProvider.VAPI,
        vapi_assistant_id=_ASSISTANT_ID,
        vapi_phone_number_id=_PHONE_NUMBER_ID,
        phone_number="+15005550006",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return VoiceLine(**defaults)


@pytest.mark.asyncio
async def test_first_turn_resolves_org_and_creates_conversation():
    service, _, conversation_repo, voice_call_repo, _ = _make_voice_service(
        voice_lines=[_voice_line()]
    )

    result = await service.handle_chat_completion(
        vapi_call_id="call_1",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number="+15551234567",
        customer_utterance="My basement is flooding!",
    )

    assert result.reply_text
    voice_call = await voice_call_repo.get_by_vapi_call_id("call_1")
    assert voice_call is not None
    assert voice_call.organization_id == _ORG_ID
    conversation = await conversation_repo.get_by_id(_ORG_ID, voice_call.conversation_id)
    assert conversation is not None
    assert conversation.channel is ConversationChannel.VOICE
    assert conversation.caller_phone_number == "+15551234567"


@pytest.mark.asyncio
async def test_unmapped_assistant_raises_voice_line_not_found():
    service, _, _, _, _ = _make_voice_service(voice_lines=[])

    with pytest.raises(VoiceLineNotFoundError):
        await service.handle_chat_completion(
            vapi_call_id="call_1",
            assistant_id="unknown-assistant",
            phone_number_id=None,
            customer_number=None,
            customer_utterance="Hello?",
        )


@pytest.mark.asyncio
async def test_resolves_by_phone_number_id_when_assistant_id_absent():
    service, _, _, voice_call_repo, _ = _make_voice_service(voice_lines=[_voice_line()])

    await service.handle_chat_completion(
        vapi_call_id="call_1",
        assistant_id=None,
        phone_number_id=_PHONE_NUMBER_ID,
        customer_number=None,
        customer_utterance="Hello?",
    )

    assert await voice_call_repo.get_by_vapi_call_id("call_1") is not None


@pytest.mark.asyncio
async def test_second_turn_continues_same_conversation():
    service, _, conversation_repo, voice_call_repo, _ = _make_voice_service(
        voice_lines=[_voice_line()]
    )

    await service.handle_chat_completion(
        vapi_call_id="call_1",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number="+15551234567",
        customer_utterance="What are your hours?",
    )
    await service.handle_chat_completion(
        vapi_call_id="call_1",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number="+15551234567",
        customer_utterance="Great, thanks!",
    )

    voice_call = await voice_call_repo.get_by_vapi_call_id("call_1")
    messages = await conversation_repo.list_messages(voice_call.conversation_id)
    assert len(messages) == 4


@pytest.mark.asyncio
async def test_idempotent_retry_does_not_call_ai_provider_twice():
    provider = FakeAIProvider()
    service, _, _, _, _ = _make_voice_service(ai_provider=provider, voice_lines=[_voice_line()])

    first = await service.handle_chat_completion(
        vapi_call_id="call_1",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number=None,
        customer_utterance="What are your hours?",
    )
    retry = await service.handle_chat_completion(
        vapi_call_id="call_1",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number=None,
        customer_utterance="What are your hours?",
    )

    assert len(provider.requests) == 1
    assert retry.reply_text == first.reply_text


@pytest.mark.asyncio
async def test_should_end_call_true_when_ai_marks_conversation_complete():
    provider = FakeAIProvider()
    provider.queue_reply(default_reply(is_conversation_complete=True))
    service, _, _, _, _ = _make_voice_service(ai_provider=provider, voice_lines=[_voice_line()])

    result = await service.handle_chat_completion(
        vapi_call_id="call_1",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number=None,
        customer_utterance="That's all, thanks.",
    )

    assert result.should_end_call is True


@pytest.mark.asyncio
async def test_end_of_call_report_updates_voice_call_and_completes_conversation():
    service, _, conversation_repo, voice_call_repo, _ = _make_voice_service(
        voice_lines=[_voice_line()]
    )
    await service.handle_chat_completion(
        vapi_call_id="call_1",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number=None,
        customer_utterance="Hello?",
    )

    await service.handle_end_of_call_report(
        vapi_call_id="call_1",
        ended_reason="customer-ended-call",
        duration_seconds=42,
        recording_url="https://recordings.example/call_1.mp3",
    )

    voice_call = await voice_call_repo.get_by_vapi_call_id("call_1")
    assert voice_call.ended_at is not None
    assert voice_call.ended_reason == "customer-ended-call"
    assert voice_call.duration_seconds == 42
    conversation = await conversation_repo.get_by_id(_ORG_ID, voice_call.conversation_id)
    assert conversation.status is ConversationStatus.COMPLETED


@pytest.mark.asyncio
async def test_end_of_call_report_for_unknown_call_is_a_noop():
    service, _, _, _, _ = _make_voice_service(voice_lines=[_voice_line()])

    await service.handle_end_of_call_report(
        vapi_call_id="never-seen",
        ended_reason="customer-ended-call",
        duration_seconds=10,
        recording_url=None,
    )


@pytest.mark.asyncio
async def test_get_voice_line_returns_none_when_not_configured():
    service, _, _, _, _ = _make_voice_service(voice_lines=[])

    assert await service.get_voice_line(_ORG_ID) is None


@pytest.mark.asyncio
async def test_get_voice_call_raises_not_found_for_cross_tenant():
    service, _, _, voice_call_repo, _ = _make_voice_service(voice_lines=[_voice_line()])
    await service.handle_chat_completion(
        vapi_call_id="call_1",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number=None,
        customer_utterance="Hello?",
    )
    voice_call = await voice_call_repo.get_by_vapi_call_id("call_1")

    with pytest.raises(EntityNotFoundError):
        await service.get_voice_call(uuid.uuid4(), voice_call.conversation_id)
