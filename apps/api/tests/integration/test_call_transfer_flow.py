"""Human fallback end to end: the real Vapi Custom-LLM webhook, the real tool
executor and `CallTransferService`, real Postgres — with only the model
(scripted) and the provider call (a fake `CallTransferPort`) replaced.

What only this level can show: that an accepted transfer never also emits
`endCall` (which would hang the caller up mid-transfer), that the attempt row
commits with the turn — including after Vapi drops the stream — and that a
failed transfer cannot roll back an emergency ticket recorded a moment
earlier in the same turn.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

import app.api.deps as deps_module
from app.api.deps import get_ai_provider, verify_vapi_secret
from app.domain.call_transfer.attempt import TransferFailure, TransferStatus
from app.domain.call_transfer.port import CallTransferPort, TransferInitiation
from app.domain.entities.conversation import ConversationStatus
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_line import VoiceProvider
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.call_transfer import (
    CallTransferAttemptModel,
    OrganizationCallTransferSettingsModel,
)
from app.infrastructure.database.models.conversation import ConversationModel
from app.infrastructure.database.models.emergency_ticket import EmergencyTicketModel
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app, fastapi_app
from tests.fakes import ScriptedToolAIProvider, default_reply

_SECRET = "test-vapi-secret-transfer"
_PATH = "/api/v1/voice/vapi/chat/completions"
_CONTROL = "https://phone-call-websocket.aws-us-west-2-backend-production1.vapi.ai/r7x/control"
_OFFICE = "+15550101000"
_ON_CALL = "+15550102000"
_AI_LINE = "+15550199000"
_TRANSFER = {"reason": "caller_requested", "is_emergency": False}
_EMERGENCY = {
    "customer_name": "Kim",
    "customer_phone": "6305550188",
    "service_address": "3 Pine Street, Lisle",
    "problem_description": "Sparks and smoke from the furnace.",
    "classification": "emergency",
    "service_name": None,
}


class RecordingPort(CallTransferPort):
    """Stands in for Vapi Live Call Control."""

    outcome = TransferInitiation(True)
    delay = 0.0
    calls: list[dict[str, str]] = []

    async def transfer(self, *, call_control, destination_number, announcement):
        if self.delay:
            await asyncio.sleep(self.delay)
        RecordingPort.calls.append(
            {"call_control": call_control, "number": destination_number, "announcement": announcement}
        )
        return RecordingPort.outcome


def _secret_override(x_vapi_secret: str | None = Header(default=None)) -> None:
    if x_vapi_secret != _SECRET:
        from app.domain.exceptions import InvalidTokenError

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
async def scripted(database_ready, monkeypatch):
    provider = ScriptedToolAIProvider()
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: provider
    fastapi_app.dependency_overrides[verify_vapi_secret] = _secret_override
    RecordingPort.outcome = TransferInitiation(True)
    RecordingPort.delay = 0.0
    RecordingPort.calls = []
    monkeypatch.setattr(deps_module, "VapiCallControlTransfer", RecordingPort)
    yield provider
    fastapi_app.dependency_overrides.pop(get_ai_provider, None)
    fastapi_app.dependency_overrides.pop(verify_vapi_secret, None)


async def _tenant(*, configured: bool = True, transfer_emergencies: bool = False) -> str:
    org_id = uuid.uuid4()
    assistant = f"asst_xfer_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(OrganizationModel(id=org_id, name=f"Xfer {org_id.hex[:6]}", slug=f"xf-{org_id.hex[:10]}"))
        await session.flush()
        session.add(VoiceLineModel(organization_id=org_id, provider=VoiceProvider.VAPI,
                                   vapi_assistant_id=assistant, phone_number=_AI_LINE, is_active=True))
        if configured:
            session.add(OrganizationCallTransferSettingsModel(
                organization_id=org_id, business_hours_number=_OFFICE, after_hours_number=_ON_CALL,
                transfer_emergencies=transfer_emergencies, is_enabled=True))
        await session.commit()
    return assistant


def _body(call_id: str, assistant: str, *, control: str | None = _CONTROL,
          utterance: str = "Can I talk to a real person please?") -> dict:
    # 555-01xx: reserved for fiction, so no real subscriber can be implied.
    call: dict = {"id": call_id, "assistantId": assistant, "customer": {"number": "+16305550188"}}
    if control is not None:
        call["monitor"] = {"controlUrl": control}
    return {"call": call, "messages": [{"role": "user", "content": utterance}], "stream": True}


async def _turn(body: dict) -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_PATH, json=body, headers={"x-vapi-secret": _SECRET})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return response.text


async def _state(call_id: str):
    async with AsyncSessionLocal() as session:
        voice_call = (await session.execute(
            select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == call_id))).scalar_one()
        conversation = await session.get(ConversationModel, voice_call.conversation_id)
        attempts = (await session.execute(select(CallTransferAttemptModel).where(
            CallTransferAttemptModel.conversation_id == voice_call.conversation_id))).scalars().all()
        ticket = (await session.execute(select(EmergencyTicketModel).where(
            EmergencyTicketModel.conversation_id == voice_call.conversation_id))).scalar_one_or_none()
    return conversation, list(attempts), ticket


def _after_transfer_reply():
    # The model claims completion; the gate must refuse it on a transfer turn.
    return default_reply(message_to_customer="", is_conversation_complete=True,
                         recommended_action=RecommendedAction.ESCALATE_TO_HUMAN)


@pytest.mark.asyncio(loop_scope="session")
async def test_accepted_transfer_is_recorded_and_never_hangs_the_caller_up(scripted):
    assistant = await _tenant()
    call_id = f"call_xfer_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round([("transfer_to_human", _TRANSFER)])
    scripted.queue_reply(_after_transfer_reply())

    sse = await _turn(_body(call_id, assistant))

    assert sse.rstrip().endswith("data: [DONE]")
    assert "endCall" not in sse, "an endCall during a transfer would hang the caller up"
    assert RecordingPort.calls == [{"call_control": _CONTROL, "number": _OFFICE,
                                    "announcement": "I'm connecting you with someone at the office now."}]
    conversation, attempts, _ = await _state(call_id)
    assert [a.status for a in attempts] == [TransferStatus.INITIATED]
    assert attempts[0].destination_number == _OFFICE
    assert conversation.status is not ConversationStatus.COMPLETED


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_call_control_is_unavailable_and_nothing_is_dialled(scripted):
    assistant = await _tenant()
    call_id = f"call_xfer_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round([("transfer_to_human", _TRANSFER)])
    scripted.queue_reply(default_reply(
        message_to_customer="I can't connect you to a person right now, but I can take your details."))

    sse = await _turn(_body(call_id, assistant, control=None))

    assert "take your details" in sse and RecordingPort.calls == []
    _, attempts, _ = await _state(call_id)
    assert [(a.status, a.error_code) for a in attempts] == [
        (TransferStatus.UNAVAILABLE, TransferFailure.CALL_CONTROL_UNAVAILABLE)]


@pytest.mark.asyncio(loop_scope="session")
async def test_unconfigured_tenant_gets_an_honest_unavailable(scripted):
    assistant = await _tenant(configured=False)
    call_id = f"call_xfer_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round([("transfer_to_human", _TRANSFER)])
    scripted.queue_reply(default_reply(message_to_customer="I can't connect you right now."))
    await _turn(_body(call_id, assistant))
    _, attempts, _ = await _state(call_id)
    assert [a.error_code for a in attempts] == [TransferFailure.NOT_CONFIGURED]
    assert RecordingPort.calls == []
    # The executor's tool result is what the model saw: success false.
    transfer_result = next(r for r in scripted.results if r.name == "transfer_to_human")
    assert transfer_result.content["success"] is False


@pytest.mark.asyncio(loop_scope="session")
async def test_failed_transfer_never_rolls_back_the_emergency_ticket(scripted):
    assistant = await _tenant()
    RecordingPort.outcome = TransferInitiation(False, TransferFailure.PROVIDER_REJECTED)
    call_id = f"call_xfer_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round([("create_service_request", _EMERGENCY)])
    scripted.queue_tool_round([("transfer_to_human", {"reason": "caller_requested", "is_emergency": True})])
    scripted.queue_reply(default_reply(
        message_to_customer="I couldn't connect you, but your emergency is recorded.",
        classification=CallClassification.EMERGENCY,
        recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET))

    sse = await _turn(_body(call_id, assistant, utterance="Sparks and smoke! Get me a person!"))

    assert sse.rstrip().endswith("data: [DONE]")
    _, attempts, ticket = await _state(call_id)
    assert ticket is not None, "a failed transfer must not erase the emergency ticket"
    assert [(a.status, a.error_code, a.is_emergency) for a in attempts] == [
        (TransferStatus.FAILED, TransferFailure.PROVIDER_REJECTED, True)]
    transfer_result = next(r for r in scripted.results if r.name == "transfer_to_human")
    assert "already recorded" in transfer_result.content["next_step"]


@pytest.mark.asyncio(loop_scope="session")
async def test_emergency_policy_transfer_follows_the_recorded_ticket(scripted):
    assistant = await _tenant(transfer_emergencies=True)
    call_id = f"call_xfer_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round([("create_service_request", _EMERGENCY)])
    scripted.queue_tool_round([("transfer_to_human", {"reason": "emergency_policy", "is_emergency": True})])
    scripted.queue_reply(_after_transfer_reply())

    await _turn(_body(call_id, assistant, utterance="Sparks and smoke from my furnace!"))

    _, attempts, ticket = await _state(call_id)
    assert ticket is not None and [a.status for a in attempts] == [TransferStatus.INITIATED]


@pytest.mark.asyncio(loop_scope="session")
async def test_8_a_caller_who_changes_their_mind_is_not_transferred(scripted):
    assistant = await _tenant()
    call_id = f"call_xfer_{uuid.uuid4().hex[:8]}"
    scripted.queue_reply(default_reply(message_to_customer="No problem, let's keep going."))
    await _turn(_body(call_id, assistant, utterance="Actually never mind, you can help me."))
    _, attempts, _ = await _state(call_id)
    assert attempts == [] and RecordingPort.calls == []


@pytest.mark.asyncio(loop_scope="session")
async def test_a_second_request_in_the_same_call_does_not_redial(scripted):
    assistant = await _tenant()
    call_id = f"call_xfer_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round([("transfer_to_human", _TRANSFER)])
    scripted.queue_reply(_after_transfer_reply())
    await _turn(_body(call_id, assistant))
    scripted.queue_tool_round([("transfer_to_human", _TRANSFER)])
    scripted.queue_reply(_after_transfer_reply())
    await _turn(_body(call_id, assistant, utterance="Hello? Is anyone there?"))
    _, attempts, _ = await _state(call_id)
    assert len(RecordingPort.calls) == 1 and [a.status for a in attempts] == [TransferStatus.INITIATED]


@pytest.mark.asyncio(loop_scope="session")
async def test_vapi_dropping_the_stream_after_transfer_still_commits_the_attempt(scripted):
    """The provider moves the call; Vapi tears down the Custom-LLM stream.
    The hang-up drain must still commit the turn — the attempt row included —
    and leave no transaction open."""
    assistant = await _tenant()
    RecordingPort.delay = 0.5
    call_id = f"call_xfer_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round([("transfer_to_human", _TRANSFER)], speak="One moment.")
    scripted.queue_reply(_after_transfer_reply())
    body = json.dumps(_body(call_id, assistant)).encode()
    spoke = asyncio.Event()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await spoke.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and b'"content"' in message.get("body", b""):
            spoke.set()

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": _PATH, "raw_path": _PATH.encode(), "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 5555), "server": ("test", 80),
             "headers": [(b"content-type", b"application/json"), (b"x-vapi-secret", _SECRET.encode()),
                         (b"content-length", str(len(body)).encode())]}
    await asyncio.wait_for(app(scope, receive, send), 30)
    await asyncio.sleep(0.8)

    _, attempts, _ = await _state(call_id)
    assert [a.status for a in attempts] == [TransferStatus.INITIATED]
    async with engine.connect() as conn:
        idle = (await conn.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND state = 'idle in transaction' AND pid <> pg_backend_pid()"))).scalar_one()
    assert idle == 0


# --- configuration API ---------------------------------------------------------------


async def _owner(client: AsyncClient) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    response = await client.post("/api/v1/auth/register", json={
        "organization_name": f"Xfer Owner {suffix}", "full_name": "Owner Owner",
        "email": f"owner-{suffix}@example.com", "password": "super-secret-123"})
    assert response.status_code == 201, response.text
    return response.json()["tokens"]["access_token"], response.json()["user"]["organization_id"]


@pytest.mark.asyncio(loop_scope="session")
async def test_settings_api_validates_numbers_and_refuses_the_ai_line(database_ready):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token, org_id = await _owner(client)
        auth = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/api/v1/organizations/current/call-transfer", headers=auth)).json() is None

        async with AsyncSessionLocal() as session:
            session.add(VoiceLineModel(organization_id=uuid.UUID(org_id), provider=VoiceProvider.VAPI,
                                       vapi_assistant_id=f"asst_api_{uuid.uuid4().hex[:8]}",
                                       phone_number=_AI_LINE, is_active=True))
            await session.commit()

        loop = await client.put("/api/v1/organizations/current/call-transfer", headers=auth,
                                json={"business_hours_number": "+1 (555) 019-9000"})
        assert loop.status_code == 422 and "AI voice line" in loop.text
        bad = await client.put("/api/v1/organizations/current/call-transfer", headers=auth,
                               json={"business_hours_number": "555-0101"})
        assert bad.status_code == 422
        empty = await client.put("/api/v1/organizations/current/call-transfer", headers=auth, json={})
        assert empty.status_code == 422

        ok = await client.put("/api/v1/organizations/current/call-transfer", headers=auth,
                              json={"business_hours_number": "+1 555 010 1000",
                                    "after_hours_number": "+15550102000", "transfer_emergencies": True})
        assert ok.status_code == 200, ok.text
        assert ok.json()["business_hours_number"] == _OFFICE and ok.json()["transfer_emergencies"] is True
        again = await client.put("/api/v1/organizations/current/call-transfer", headers=auth,
                                 json={"after_hours_number": "+15550102000"})
        assert again.json()["business_hours_number"] is None  # PUT replaces, one row per tenant

        assert (await client.delete("/api/v1/organizations/current/call-transfer", headers=auth)).status_code == 204
        assert (await client.get("/api/v1/organizations/current/call-transfer", headers=auth)).json() is None
        assert (await client.get("/api/v1/organizations/current/call-transfer")).status_code == 401
