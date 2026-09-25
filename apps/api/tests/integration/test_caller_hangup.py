"""A caller hanging up mid-turn must not leak a database transaction.

Found by the real-model matrix (2026-09-25): a caller disconnected while the
turn's emergency tool was running, the request raised `PendingRollbackError`,
and a connection was left `idle in transaction` — holding whatever locks the
turn had taken (the call's advisory lock; potentially the organization's
booking lock) until the process died. Repeated hang-ups would exhaust the
connection pool; one leaked booking lock would stop every booking for that
business.

Reproduced through the real ASGI app with a genuine `http.disconnect`, the
way uvicorn delivers a hang-up, while a tool is mid-flight inside its
savepoint. Postgres-backed because the defect IS the connection's state.
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

from app.api.deps import get_ai_provider, verify_vapi_secret
from app.application.services.voice_tool_executor import VoiceToolExecutor
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_line import VoiceProvider
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.conversation_message import ConversationMessageModel
from app.infrastructure.database.models.emergency_ticket import EmergencyTicketModel
from app.infrastructure.database.models.notification import EmergencyNotificationDeliveryModel
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app, fastapi_app
from tests.fakes import ScriptedToolAIProvider, default_reply

_SECRET = "test-vapi-secret-hangup"
_PATH = "/api/v1/voice/vapi/chat/completions"

_EMERGENCY = {
    "customer_name": "Kim",
    "customer_phone": "6305550188",
    "service_address": "3 Pine Street, Lisle",
    "problem_description": "Sparks and smoke from the furnace.",
    "classification": "emergency",
    "service_name": None,
}


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
async def scripted(database_ready):
    provider = ScriptedToolAIProvider()
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: provider
    fastapi_app.dependency_overrides[verify_vapi_secret] = _secret_override
    yield provider
    fastapi_app.dependency_overrides.pop(get_ai_provider, None)
    fastapi_app.dependency_overrides.pop(verify_vapi_secret, None)


async def _line() -> str:
    org_id = uuid.uuid4()
    assistant = f"asst_hangup_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(OrganizationModel(id=org_id, name=f"Hangup {org_id.hex[:6]}", slug=f"hu-{org_id.hex[:10]}"))
        await session.flush()
        session.add(
            VoiceLineModel(
                organization_id=org_id,
                provider=VoiceProvider.VAPI,
                vapi_assistant_id=assistant,
                is_active=True,
            )
        )
        await session.commit()
    return assistant


async def _hang_up_after_first_words(call_id: str, assistant: str) -> None:
    """One streamed turn, with the caller disconnecting as soon as the first
    spoken words have been sent — the moment a real caller hangs up on the
    assistant mid-sentence."""
    body = json.dumps(
        {
            "call": {"id": call_id, "assistantId": assistant, "customer": {"number": "+16305550888"}},
            "messages": [{"role": "user", "content": "Sparks and smoke from my furnace!"}],
            "stream": True,
        }
    ).encode()
    spoke = asyncio.Event()
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await spoke.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and b'"content"' in message.get("body", b""):
            spoke.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": _PATH,
        "raw_path": _PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"x-vapi-secret", _SECRET.encode()),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 5555),
        "server": ("test", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), 30)


async def _idle_in_transaction() -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND state = 'idle in transaction' "
                    "AND pid <> pg_backend_pid()"
                )
            )
        ).scalar_one()


@pytest.mark.asyncio(loop_scope="session")
async def test_hanging_up_while_a_tool_runs_leaks_no_transaction(scripted, monkeypatch):
    assistant = await _line()
    call_id = f"call_hangup_{uuid.uuid4().hex[:8]}"
    original = VoiceToolExecutor._create_service_request

    async def slow_tool(self, organization_id, conversation_id, arguments, turn_index):
        # Long enough that the disconnect lands while the tool is inside its
        # savepoint, with writes pending — the exact moment of the leak.
        result = await original(self, organization_id, conversation_id, arguments, turn_index)
        await asyncio.sleep(0.5)
        return result

    monkeypatch.setattr(VoiceToolExecutor, "_create_service_request", slow_tool)
    scripted.queue_tool_round(
        [("create_service_request", _EMERGENCY)], speak="I'm logging this emergency right now."
    )
    scripted.queue_reply(
        default_reply(
            message_to_customer="Your emergency is logged.",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        )
    )

    await _hang_up_after_first_words(call_id, assistant)
    await asyncio.sleep(0.3)

    assert await _idle_in_transaction() == 0, "a hung-up turn leaked an open transaction"

    # The turn was allowed to finish after the hang-up, so the emergency the
    # caller reported is on record, with its alert queued — and the
    # transcript holds the whole exchange, never half of it.
    async with AsyncSessionLocal() as session:
        voice_call = (
            await session.execute(select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == call_id))
        ).scalar_one_or_none()
        assert voice_call is not None, "the hung-up turn was rolled back"
        if voice_call is not None:
            tickets = (
                await session.execute(
                    select(EmergencyTicketModel).where(
                        EmergencyTicketModel.conversation_id == voice_call.conversation_id
                    )
                )
            ).scalars().all()
            assert len(tickets) == 1, "the hung-up caller's emergency ticket was lost"
            for ticket in tickets:
                assert (
                    await session.execute(
                        select(EmergencyNotificationDeliveryModel).where(
                            EmergencyNotificationDeliveryModel.emergency_ticket_id == ticket.id
                        )
                    )
                ).scalar_one_or_none() is not None, "ticket committed without its alert"
            roles = [
                m.role.value
                for m in (
                    await session.execute(
                        select(ConversationMessageModel).where(
                            ConversationMessageModel.conversation_id == voice_call.conversation_id
                        )
                    )
                ).scalars().all()
            ]
            assert sorted(roles) == ["assistant", "customer"]

    # And the call is not wedged: the next utterance on the same call is
    # answered promptly (a leaked transaction would still hold the call's
    # advisory lock and block this forever).
    scripted.queue_reply(default_reply(message_to_customer="Are you still there?"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        follow_up = await asyncio.wait_for(
            client.post(
                _PATH,
                json={
                    "call": {"id": call_id, "assistantId": assistant},
                    "messages": [{"role": "user", "content": "Hello?"}],
                },
                headers={"x-vapi-secret": _SECRET},
            ),
            timeout=15,
        )
    assert follow_up.status_code == 200
