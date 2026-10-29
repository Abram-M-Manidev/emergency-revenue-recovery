"""Usage metering (M1) end to end: real Vapi webhooks, real services, real
Postgres. Only the model is replaced — by one that reports token usage the
way `OpenAIProvider` does.

Pins down:
- every ended call gets exactly one usage record, including a call ERRS
  never held a conversation on, a call no voice line matches, a call whose
  duration is missing, and a report Vapi delivers twice;
- per-conversation token usage is recorded from the provider's figures;
- a metering failure that is a REAL database error — one that aborts the
  statement — never costs the caller anything: the turn is still spoken and
  persisted, and the end-of-call report still marks the call ended, opens
  the follow-up, the emergency ticket and its alert.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.api.deps import get_ai_provider, verify_vapi_secret
from app.domain.ai.provider import AIProvider, AIReply, AIRequest
from app.domain.ai.usage import TokenUsage
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_line import VoiceProvider
from app.domain.exceptions import InvalidTokenError
from app.domain.usage import DurationSource, ExclusionReason, UsageAttribution
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.call_follow_up import CallFollowUpModel
from app.infrastructure.database.models.conversation_message import ConversationMessageModel
from app.infrastructure.database.models.emergency_ticket import EmergencyTicketModel
from app.infrastructure.database.models.notification import EmergencyNotificationDeliveryModel
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.usage import (
    CallUsageRecordModel,
    ConversationLLMUsageModel,
)
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.repositories import usage_repository_impl
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app, fastapi_app
from tests.fakes import default_reply

_SECRET = "test-vapi-secret-usage"
_COMPLETIONS = "/api/v1/voice/vapi/chat/completions"
_EVENTS = "/api/v1/voice/vapi/events"
_CALLER = "+16305550177"  # 555-01xx: reserved for fiction.


class _MeteredProvider(AIProvider):
    """Reports usage to `request.usage_sink` exactly as `OpenAIProvider`
    does: once per model request, with the provider's own counts."""

    def __init__(self, reply: AIReply | None = None) -> None:
        self.reply = reply or default_reply(message_to_customer="How can I help?")

    async def generate_reply(self, request: AIRequest) -> AIReply:
        if request.usage_sink is not None:
            request.usage_sink.record(
                TokenUsage(model="test-model", prompt_tokens=1500, cached_prompt_tokens=1024,
                           completion_tokens=60)
            )
        return self.reply


class _Holder:
    provider: AIProvider = _MeteredProvider()


def _secret_override(x_vapi_secret: str | None = Header(default=None)) -> None:
    if x_vapi_secret != _SECRET:
        raise InvalidTokenError("Missing or invalid Vapi webhook secret.")


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def database_ready():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Database not reachable, skipping integration test: {exc}")
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(loop_scope="session")
async def client(database_ready):
    _Holder.provider = _MeteredProvider()
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: _Holder.provider
    fastapi_app.dependency_overrides[verify_vapi_secret] = _secret_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    fastapi_app.dependency_overrides.pop(get_ai_provider, None)
    fastapi_app.dependency_overrides.pop(verify_vapi_secret, None)


async def _tenant() -> tuple[uuid.UUID, str]:
    organization_id = uuid.uuid4()
    assistant = f"asst_um_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(OrganizationModel(id=organization_id, name=f"UM {organization_id.hex[:6]}",
                                      slug=f"um-{organization_id.hex[:10]}"))
        await session.flush()
        session.add(VoiceLineModel(organization_id=organization_id, provider=VoiceProvider.VAPI,
                                   vapi_assistant_id=assistant, is_active=True))
        await session.commit()
    return organization_id, assistant


def _call_id() -> str:
    return f"call_um_{uuid.uuid4().hex[:8]}"


async def _turn(client: AsyncClient, call_id: str, assistant: str, utterance: str) -> str:
    response = await client.post(
        _COMPLETIONS,
        headers={"x-vapi-secret": _SECRET},
        json={"call": {"id": call_id, "assistantId": assistant, "customer": {"number": _CALLER}},
              "messages": [{"role": "user", "content": utterance}], "stream": True},
    )
    assert response.status_code == 200, response.text
    return response.text


async def _end(client: AsyncClient, call_id: str, assistant: str | None, **fields) -> None:
    call: dict = {"id": call_id}
    if assistant is not None:
        call["assistantId"] = assistant
    message = {"type": "end-of-call-report", "call": call, "endedReason": "customer-ended-call",
               "durationSeconds": 95, **fields}
    response = await client.post(_EVENTS, json={"message": message},
                                 headers={"x-vapi-secret": _SECRET})
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}


async def _usage(call_id: str) -> list[CallUsageRecordModel]:
    async with AsyncSessionLocal() as session:
        return list((await session.execute(select(CallUsageRecordModel).where(
            CallUsageRecordModel.vapi_call_id == call_id))).scalars().all())


async def _voice_call(call_id: str) -> VoiceCallModel:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(VoiceCallModel).where(
            VoiceCallModel.vapi_call_id == call_id))).scalar_one()


async def _llm_usage(conversation_id: uuid.UUID) -> list[ConversationLLMUsageModel]:
    async with AsyncSessionLocal() as session:
        return list((await session.execute(select(ConversationLLMUsageModel).where(
            ConversationLLMUsageModel.conversation_id == conversation_id))).scalars().all())


@pytest.mark.asyncio(loop_scope="session")
async def test_a_call_with_no_conversation_is_still_metered(client):
    """The caller hung up before saying anything: no turn, no `voice_calls`
    row — and Vapi bills the call anyway."""
    organization_id, assistant = await _tenant()
    call_id = _call_id()

    await _end(client, call_id, assistant)

    [record] = await _usage(call_id)
    assert record.organization_id == organization_id
    assert record.conversation_id is None
    assert record.attribution is UsageAttribution.VOICE_LINE
    assert (record.billable_minutes, record.is_billable) == (2, True)
    assert record.report_count == 1
    assert record.vapi_reported_cost is None  # not in the report, so not invented


@pytest.mark.asyncio(loop_scope="session")
async def test_a_call_with_a_conversation_is_metered_with_its_tokens(client):
    organization_id, assistant = await _tenant()
    call_id = _call_id()

    await _turn(client, call_id, assistant, "Hi, I need a tune-up.")
    await _turn(client, call_id, assistant, "Next week is fine.")
    await _end(client, call_id, assistant, cost=0.2711,
               costBreakdown={"vapi": 0.0792, "stt": 0.0151, "tts": 0.03, "total": 0.2711})

    voice_call = await _voice_call(call_id)
    [record] = await _usage(call_id)
    assert record.attribution is UsageAttribution.VOICE_CALL
    assert record.organization_id == organization_id
    assert record.conversation_id == voice_call.conversation_id
    assert record.vapi_reported_cost == Decimal("0.271100")
    assert record.vapi_reported_cost_breakdown == {
        "stt": "0.0151", "total": "0.2711", "tts": "0.03", "vapi": "0.0792"}

    [tokens] = await _llm_usage(voice_call.conversation_id)
    assert (tokens.organization_id, tokens.channel, tokens.model) == (
        organization_id, "voice", "test-model")
    assert (tokens.requests, tokens.requests_without_usage) == (2, 0)
    assert (tokens.prompt_tokens, tokens.cached_prompt_tokens, tokens.completion_tokens) == (
        3000, 2048, 120)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_duplicate_report_leaves_one_record(client):
    _, assistant = await _tenant()
    call_id = _call_id()
    await _turn(client, call_id, assistant, "Hello?")

    await _end(client, call_id, assistant)
    await _end(client, call_id, assistant)

    [record] = await _usage(call_id)
    assert record.report_count == 2
    assert record.billable_minutes == 2
    async with AsyncSessionLocal() as session:
        follow_ups = (await session.execute(select(CallFollowUpModel).where(
            CallFollowUpModel.conversation_id == record.conversation_id))).scalars().all()
    assert len(follow_ups) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_a_missing_duration_is_flagged_not_guessed(client):
    _, assistant = await _tenant()
    call_id = _call_id()

    await _end(client, call_id, assistant, durationSeconds=None)

    [record] = await _usage(call_id)
    assert record.duration_source is DurationSource.MISSING
    assert record.exclusion_reason is ExclusionReason.DURATION_UNKNOWN
    assert (record.billable_seconds, record.billable_minutes, record.is_billable) == (0, 0, False)
    assert record.reported_duration_seconds is None


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unmatched_call_is_kept_without_a_tenant(client):
    call_id = _call_id()

    await _end(client, call_id, f"asst_nobody_{uuid.uuid4().hex[:6]}")
    await _end(client, _call_id(), None)  # no routing keys at all

    [record] = await _usage(call_id)
    assert record.attribution is UsageAttribution.UNATTRIBUTED
    assert record.organization_id is None
    assert record.billable_minutes == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_broken_call_metering_never_costs_the_emergency(client, monkeypatch):
    """The end-of-call report's own work — ending the call, the follow-up,
    the emergency ticket and its queued alert — survives a real database
    error inside metering."""

    async def broken_get_for_update(self, vapi_call_id):
        await self._session.execute(text("SELECT 1/0"))

    monkeypatch.setattr(usage_repository_impl.SqlAlchemyCallUsageRepository, "get_for_update",
                        broken_get_for_update)
    _Holder.provider = _MeteredProvider(default_reply(
        message_to_customer="I'm sorry to hear that. Your name?",
        classification=CallClassification.UNKNOWN, recommended_action=RecommendedAction.NONE))
    _, assistant = await _tenant()
    call_id = _call_id()
    await _turn(client, call_id, assistant, "There's smoke coming out of my furnace")

    await _end(client, call_id, assistant)

    voice_call = await _voice_call(call_id)
    assert voice_call.ended_at is not None
    assert voice_call.duration_seconds == 95
    async with AsyncSessionLocal() as session:
        tickets = (await session.execute(select(EmergencyTicketModel).where(
            EmergencyTicketModel.conversation_id == voice_call.conversation_id))).scalars().all()
        follow_ups = (await session.execute(select(CallFollowUpModel).where(
            CallFollowUpModel.conversation_id == voice_call.conversation_id))).scalars().all()
        deliveries = (await session.execute(select(EmergencyNotificationDeliveryModel).where(
            EmergencyNotificationDeliveryModel.emergency_ticket_id == tickets[0].id
        ))).scalars().all() if tickets else []
    assert len(tickets) == 1 and len(deliveries) == 1
    assert follow_ups[0].possible_emergency is True
    assert await _usage(call_id) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_broken_token_metering_never_costs_the_turn(client, monkeypatch):
    async def broken_add(self, **_kwargs):
        await self._session.execute(text("SELECT 1/0"))

    monkeypatch.setattr(usage_repository_impl.SqlAlchemyLLMUsageRepository, "add", broken_add)
    _Holder.provider = _MeteredProvider(default_reply(message_to_customer="Happy to help with that."))
    _, assistant = await _tenant()
    call_id = _call_id()

    spoken = await _turn(client, call_id, assistant, "Can someone look at my water heater?")

    assert "Happy to help with that." in spoken
    voice_call = await _voice_call(call_id)
    async with AsyncSessionLocal() as session:
        messages = (await session.execute(select(ConversationMessageModel).where(
            ConversationMessageModel.conversation_id == voice_call.conversation_id
        ))).scalars().all()
    # Both halves of the turn persisted despite the failed metering write.
    assert len(messages) == 2
    assert await _llm_usage(voice_call.conversation_id) == []

    # The call itself is still metered at its end.
    await _end(client, call_id, assistant)
    assert len(await _usage(call_id)) == 1
