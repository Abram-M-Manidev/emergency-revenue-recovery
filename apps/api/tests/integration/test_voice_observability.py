"""End-to-end assertions on the H2 voice-path telemetry, driven through the
real webhook against a real database — the only way to prove that
correlation survives the StreamingResponse boundary, where the generator
body runs after the handler has already returned.

Fixtures mirror `test_voice_webhook_flow.py` exactly; the difference is
that these tests assert on emitted events rather than on responses.
"""

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
from tests.log_capture import capture_events, names, only

_TEST_VAPI_SECRET = "test-vapi-secret-observability"
_ASSISTANT_ID = "asst_observability"

# Deliberately distinctive so a substring search cannot produce a false
# negative. Every one of these is caller data and must never be logged.
_PII_NAME = "Zebediah Quorthon"
_PII_PHONE = "+15550009999"
_PII_ADDRESS = "77 Nowhere Lane, Springfield"
_PII_UTTERANCE = "My furnace at 77 Nowhere Lane is dead and I am Zebediah Quorthon"
_PII_REPLY = "Thank you Zebediah, a technician is heading to 77 Nowhere Lane."
_PII_SUMMARY = "Zebediah Quorthon at 77 Nowhere Lane has no heat."


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


async def _register(client: AsyncClient, org_name: str, email: str):
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


def _vapi_headers() -> dict[str, str]:
    return {"x-vapi-secret": _TEST_VAPI_SECRET}


async def _stream_turn(client, *, call_id, assistant_id, utterance="My furnace died."):
    return await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json={
            "call": {"id": call_id, "assistantId": assistant_id},
            "messages": [{"role": "user", "content": utterance}],
            "stream": True,
        },
        headers=_vapi_headers(),
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_streaming_turn_emits_the_full_lifecycle(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    assistant = _ASSISTANT_ID + "-life"
    _, org_id = await _register(client, "Obs Org A", "owner-obs-a@example.com")
    await _seed_voice_line(org_id, assistant_id=assistant)
    fake_ai_provider.queue_reply(default_reply(message_to_customer="Help is coming."))

    with capture_events() as entries:
        response = await _stream_turn(client, call_id="call_obs_a", assistant_id=assistant)

    assert response.status_code == 200
    emitted = names(entries)
    for expected in (
        "voice_request_received",
        "voice_stream_started",
        "voice_line_resolved",
        "voice_turn_started",
        "conversation_turn_persisted",
        "voice_outcome_syncs_completed",
        "voice_stream_completed",
    ):
        assert expected in emitted, f"missing {expected}; got {emitted}"

    # Ordering that matters for reading an incident timeline.
    assert emitted.index("voice_request_received") < emitted.index("voice_stream_started")
    assert emitted.index("voice_turn_started") < emitted.index("conversation_turn_persisted")
    assert emitted.index("conversation_turn_persisted") < emitted.index("voice_stream_completed")


@pytest.mark.asyncio(loop_scope="session")
async def test_correlation_ids_are_consistent_across_the_whole_turn(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    """The property that removes the database archaeology: one grep by
    vapi_call_id returns the entire turn, including events emitted from
    inside the StreamingResponse body."""
    assistant = _ASSISTANT_ID + "-corr"
    _, org_id = await _register(client, "Obs Org B", "owner-obs-b@example.com")
    await _seed_voice_line(org_id, assistant_id=assistant)
    fake_ai_provider.queue_reply(default_reply(message_to_customer="Understood."))

    with capture_events() as entries:
        await _stream_turn(client, call_id="call_obs_b", assistant_id=assistant)

    correlated = [e for e in entries if "vapi_call_id" in e]
    assert len(correlated) >= 5
    assert {e["vapi_call_id"] for e in correlated} == {"call_obs_b"}
    assert len({e["turn_id"] for e in correlated}) == 1

    # Bound mid-turn, so it appears on the later events only — and must be
    # a single consistent value wherever it appears.
    with_conversation = [e for e in entries if "conversation_id" in e]
    assert with_conversation, "conversation_id never reached the log context"
    assert len({e["conversation_id"] for e in with_conversation}) == 1
    assert len({e["organization_id"] for e in with_conversation}) == 1

    persisted = only(entries, "conversation_turn_persisted")
    assert persisted["vapi_call_id"] == "call_obs_b"
    assert "elapsed_ms" in persisted


@pytest.mark.asyncio(loop_scope="session")
async def test_end_call_is_observable_when_the_conversation_completes(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    assistant = _ASSISTANT_ID + "-end"
    _, org_id = await _register(client, "Obs Org C", "owner-obs-c@example.com")
    await _seed_voice_line(org_id, assistant_id=assistant)
    fake_ai_provider.queue_reply(
        default_reply(message_to_customer="Goodbye.", is_conversation_complete=True)
    )

    with capture_events() as entries:
        response = await _stream_turn(client, call_id="call_obs_c", assistant_id=assistant)

    assert "voice_end_call_emitted" in names(entries)
    assert only(entries, "voice_stream_completed")["end_call_emitted"] is True
    assert "endCall" in response.text


@pytest.mark.asyncio(loop_scope="session")
async def test_turn_without_end_call_records_it_as_such(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    assistant = _ASSISTANT_ID + "-noend"
    _, org_id = await _register(client, "Obs Org D", "owner-obs-d@example.com")
    await _seed_voice_line(org_id, assistant_id=assistant)
    fake_ai_provider.queue_reply(default_reply(message_to_customer="Still listening."))

    with capture_events() as entries:
        await _stream_turn(client, call_id="call_obs_d", assistant_id=assistant)

    assert "voice_end_call_emitted" not in names(entries)
    assert only(entries, "voice_stream_completed")["end_call_emitted"] is False


@pytest.mark.asyncio(loop_scope="session")
async def test_end_of_call_report_is_observable(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    assistant = _ASSISTANT_ID + "-eocr"
    _, org_id = await _register(client, "Obs Org E", "owner-obs-e@example.com")
    await _seed_voice_line(org_id, assistant_id=assistant)
    fake_ai_provider.queue_reply(default_reply(message_to_customer="Noted."))
    await _stream_turn(client, call_id="call_obs_e", assistant_id=assistant)

    with capture_events() as entries:
        await client.post(
            "/api/v1/voice/vapi/events",
            json={
                "message": {
                    "type": "end-of-call-report",
                    "call": {"id": "call_obs_e"},
                    "endedReason": "assistant-ended-call",
                    "durationSeconds": 42,
                    "recordingUrl": "https://example.invalid/recordings/secret-audio.wav",
                }
            },
            headers=_vapi_headers(),
        )

    report = only(entries, "voice_end_of_call_report")
    assert report["ended_reason"] == "assistant-ended-call"
    assert report["duration_seconds"] == 42
    # The URL dereferences to caller audio — presence only, never the value.
    assert report["has_recording"] is True
    assert "secret-audio" not in str(report)


@pytest.mark.asyncio(loop_scope="session")
async def test_no_pii_or_secrets_appear_in_any_log_field(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    """The strongest form available: run a turn whose every caller field is
    a distinctive sentinel, then assert none of them appear anywhere in the
    captured events."""
    assistant = _ASSISTANT_ID + "-pii"
    _, org_id = await _register(client, "Obs Org F", "owner-obs-f@example.com")
    await _seed_voice_line(org_id, assistant_id=assistant)
    fake_ai_provider.queue_reply(
        default_reply(
            message_to_customer=_PII_REPLY,
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
            customer_name=_PII_NAME,
            customer_phone=_PII_PHONE,
            customer_address=_PII_ADDRESS,
            summary=_PII_SUMMARY,
        )
    )

    with capture_events() as entries:
        response = await client.post(
            "/api/v1/voice/vapi/chat/completions",
            json={
                "call": {
                    "id": "call_obs_f",
                    "assistantId": assistant,
                    "customer": {"number": _PII_PHONE},
                },
                "messages": [{"role": "user", "content": _PII_UTTERANCE}],
                "stream": True,
            },
            headers=_vapi_headers(),
        )

    assert response.status_code == 200
    # The reply really did reach the caller — proving the turn ran and the
    # absence below is not simply an empty log.
    assert _PII_REPLY in response.text

    haystack = "\n".join(repr(entry) for entry in entries)
    for forbidden in (
        _PII_NAME,
        _PII_PHONE,
        _PII_ADDRESS,
        _PII_UTTERANCE,
        _PII_REPLY,
        _PII_SUMMARY,
        _TEST_VAPI_SECRET,
        "Nowhere Lane",
        "Zebediah",
    ):
        assert forbidden not in haystack, f"{forbidden!r} leaked into telemetry"

    # But the turn is still diagnosable: the AI's own decision is recorded.
    persisted = only(entries, "conversation_turn_persisted")
    assert persisted["classification"] == CallClassification.EMERGENCY.value
    assert persisted["recommended_action"] == RecommendedAction.CREATE_EMERGENCY_TICKET.value


@pytest.mark.asyncio(loop_scope="session")
async def test_logging_does_not_change_the_sse_response(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    """P2 preservation at the HTTP level: same media type, same framing,
    exactly one terminator, same spoken text."""
    assistant = _ASSISTANT_ID + "-sse"
    _, org_id = await _register(client, "Obs Org G", "owner-obs-g@example.com")
    await _seed_voice_line(org_id, assistant_id=assistant)
    fake_ai_provider.queue_reply(default_reply(message_to_customer="Stay on the line."))

    with capture_events():
        response = await _stream_turn(client, call_id="call_obs_g", assistant_id=assistant)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("data: [DONE]") == 1
    assert "Stay on the line." in response.text
