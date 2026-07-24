"""End-to-end Voice webhook flow against a real Postgres database — mirrors
`test_ai_conversation_flow.py`'s structure and fixtures.

The Vapi shared-secret dependency (`verify_vapi_secret`) is overridden with
a fixed test secret rather than relying on `VAPI_SERVER_SECRET` env/timing
(the real setting is read once, at first `get_settings()` call, which can
happen as early as another test module's import — same reasoning as why
`fake_ai_provider` overrides `get_ai_provider` instead of setting
`OPENAI_API_KEY`). The AIProvider dependency is overridden with
`FakeAIProvider` for the same reason `test_ai_conversation_flow.py` does:
never make a real, paid, non-deterministic OpenAI call in CI."""

import uuid

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.deps import get_ai_provider, verify_vapi_secret
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_line import VoiceProvider
from app.domain.exceptions import InvalidTokenError
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app, fastapi_app
from tests.fakes import FakeAIProvider, default_reply

_TEST_VAPI_SECRET = "test-vapi-secret"
_ASSISTANT_ID = "asst_voice_flow_1"


def _verify_vapi_secret_override(x_vapi_secret: str | None = Header(default=None)) -> None:
    if x_vapi_secret != _TEST_VAPI_SECRET:
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
        await conn.execute(text("SELECT 1"))
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(loop_scope="session")
async def fake_ai_provider():
    provider = FakeAIProvider()
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: provider
    fastapi_app.dependency_overrides[verify_vapi_secret] = _verify_vapi_secret_override
    yield provider
    fastapi_app.dependency_overrides.pop(get_ai_provider, None)
    fastapi_app.dependency_overrides.pop(verify_vapi_secret, None)


@pytest_asyncio.fixture(loop_scope="session")
async def client(database_ready, fake_ai_provider):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _register(client: AsyncClient, org_name: str, email: str) -> tuple[str, uuid.UUID]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": org_name,
            "full_name": "Test Owner",
            "email": email,
            "password": "super-secret-123",
        },
    )
    assert response.status_code == 201
    body = response.json()
    return body["tokens"]["access_token"], uuid.UUID(body["user"]["organization_id"])


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _vapi_headers() -> dict[str, str]:
    return {"x-vapi-secret": _TEST_VAPI_SECRET}


async def _seed_voice_line(organization_id: uuid.UUID, *, assistant_id: str) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            VoiceLineModel(
                organization_id=organization_id,
                provider=VoiceProvider.VAPI,
                vapi_assistant_id=assistant_id,
                vapi_phone_number_id=None,
                phone_number="+15005550006",
                is_active=True,
            )
        )
        await session.commit()


def _chat_completion_payload(
    *,
    call_id: str,
    messages: list[dict],
    assistant_id: str = _ASSISTANT_ID,
    customer_number: str | None = None,
) -> dict:
    return {
        "call": {
            "id": call_id,
            "assistantId": assistant_id,
            "customer": {"number": customer_number} if customer_number else None,
        },
        "messages": messages,
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_first_turn_creates_voice_conversation_visible_in_dashboard(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    token, org_id = await _register(client, "Voice Flow Org A", "owner-voice-a@example.com")
    await _seed_voice_line(org_id, assistant_id=_ASSISTANT_ID)

    response = await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json=_chat_completion_payload(
            call_id="call_a1",
            messages=[{"role": "user", "content": "My basement is flooding!"}],
            customer_number="+15551234567",
        ),
        headers=_vapi_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"]

    list_resp = await client.get("/api/v1/ai/conversations", headers=_auth_headers(token))
    assert list_resp.status_code == 200
    conversations = list_resp.json()
    assert len(conversations) == 1
    assert conversations[0]["channel"] == "voice"
    assert conversations[0]["caller_phone_number"] == "+15551234567"


@pytest.mark.asyncio(loop_scope="session")
async def test_second_turn_continues_same_conversation(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    token, org_id = await _register(client, "Voice Flow Org B", "owner-voice-b@example.com")
    await _seed_voice_line(org_id, assistant_id=_ASSISTANT_ID + "-b")

    payload_1 = {
        "call": {"id": "call_b1", "assistantId": _ASSISTANT_ID + "-b"},
        "messages": [{"role": "user", "content": "What are your hours?"}],
    }
    first = await client.post(
        "/api/v1/voice/vapi/chat/completions", json=payload_1, headers=_vapi_headers()
    )
    assert first.status_code == 200
    first_reply = first.json()["choices"][0]["message"]["content"]

    payload_2 = {
        "call": {"id": "call_b1", "assistantId": _ASSISTANT_ID + "-b"},
        "messages": [
            {"role": "user", "content": "What are your hours?"},
            {"role": "assistant", "content": first_reply},
            {"role": "user", "content": "Great, thanks!"},
        ],
    }
    second = await client.post(
        "/api/v1/voice/vapi/chat/completions", json=payload_2, headers=_vapi_headers()
    )
    assert second.status_code == 200

    list_resp = await client.get("/api/v1/ai/conversations", headers=_auth_headers(token))
    conversations = list_resp.json()
    assert len(conversations) == 1
    conversation_id = conversations[0]["id"]

    detail_resp = await client.get(
        f"/api/v1/ai/conversations/{conversation_id}", headers=_auth_headers(token)
    )
    assert len(detail_resp.json()["messages"]) == 4


@pytest.mark.asyncio(loop_scope="session")
async def test_completed_outcome_returns_end_call_tool_call(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    token, org_id = await _register(client, "Voice Flow Org C", "owner-voice-c@example.com")
    await _seed_voice_line(org_id, assistant_id=_ASSISTANT_ID + "-c")
    fake_ai_provider.queue_reply(
        default_reply(
            message_to_customer="Help is on the way. Goodbye!",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
            is_conversation_complete=True,
        )
    )

    response = await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json=_chat_completion_payload(
            call_id="call_c1",
            assistant_id=_ASSISTANT_ID + "-c",
            messages=[{"role": "user", "content": "My furnace exploded!"}],
        ),
        headers=_vapi_headers(),
    )
    assert response.status_code == 200
    message = response.json()["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "endCall"
    assert response.json()["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio(loop_scope="session")
async def test_end_of_call_report_persists_metadata_and_force_completes(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    token, org_id = await _register(client, "Voice Flow Org D", "owner-voice-d@example.com")
    await _seed_voice_line(org_id, assistant_id=_ASSISTANT_ID + "-d")

    await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json=_chat_completion_payload(
            call_id="call_d1",
            assistant_id=_ASSISTANT_ID + "-d",
            messages=[{"role": "user", "content": "Hello?"}],
        ),
        headers=_vapi_headers(),
    )

    events_resp = await client.post(
        "/api/v1/voice/vapi/events",
        json={
            "message": {
                "type": "end-of-call-report",
                "call": {"id": "call_d1"},
                "endedReason": "customer-ended-call",
                "durationSeconds": 37,
                "recordingUrl": "https://recordings.example/call_d1.mp3",
            }
        },
        headers=_vapi_headers(),
    )
    assert events_resp.status_code == 200

    list_resp = await client.get("/api/v1/ai/conversations", headers=_auth_headers(token))
    conversation_id = list_resp.json()[0]["id"]
    assert list_resp.json()[0]["status"] == "completed"

    call_resp = await client.get(
        f"/api/v1/voice/calls/{conversation_id}", headers=_auth_headers(token)
    )
    assert call_resp.status_code == 200
    call_body = call_resp.json()
    assert call_body["ended_reason"] == "customer-ended-call"
    assert call_body["duration_seconds"] == 37
    assert call_body["recording_url"] == "https://recordings.example/call_d1.mp3"


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_or_wrong_secret_is_rejected(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    payload = _chat_completion_payload(
        call_id="call_e1", messages=[{"role": "user", "content": "Hello?"}]
    )

    no_header = await client.post("/api/v1/voice/vapi/chat/completions", json=payload)
    assert no_header.status_code == 401

    wrong_header = await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json=payload,
        headers={"x-vapi-secret": "not-the-right-secret"},
    )
    assert wrong_header.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_unmapped_assistant_gets_speakable_fallback_not_a_5xx(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    payload = {
        "call": {"id": "call_f1", "assistantId": "totally-unknown-assistant"},
        "messages": [{"role": "user", "content": "Hello?"}],
    }

    response = await client.post(
        "/api/v1/voice/vapi/chat/completions", json=payload, headers=_vapi_headers()
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"]
