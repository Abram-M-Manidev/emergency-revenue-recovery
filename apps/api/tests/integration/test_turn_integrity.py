"""A failure late in a voice turn must not erase what the turn already did.

A live voice turn is ONE Postgres transaction, and the caller hears its
result long before it commits: the tools book the appointment or open the
emergency ticket (and page a dispatcher about it) in an early round, the
reply streams, and only then are the turn's messages persisted and the
downstream syncs run. On Postgres a single failed statement aborts the whole
transaction. Before this module's fixes, one failed write anywhere after the
tool round — a column overflow, a best-effort lookup that "swallowed" its
error, a malformed model document — rolled back the booking or the ticket
the caller had just been told about.

Why this is Postgres-backed
---------------------------
Every failure here is injected as a REAL database error (a statement Postgres
rejects), not a Python exception from a fake. The in-memory fakes have no
transaction to abort and no column widths to overflow, which is exactly why
the 2026-09-24 overflow passed 761 unit tests. What is being proven is that
the transaction survives, and only a real transaction can show that.

Drives the real Vapi Custom-LLM webhook with the real `deps.py` wiring; only
the language model is scripted (`ScriptedToolAIProvider`), because tool
arguments are what a model contributes and are what a test must control.
"""

from __future__ import annotations

import contextlib
import json
import uuid

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.api.deps import get_ai_provider, verify_vapi_secret
from app.domain.ai.provider import AIProvider, AIReply, AIRequest
from app.domain.ai.tools import ToolInvocation
from app.domain.entities.conversation_outcome import (
    CUSTOMER_ADDRESS_MAX_LENGTH,
    CUSTOMER_NAME_MAX_LENGTH,
    CallClassification,
    RecommendedAction,
)
from app.domain.entities.voice_line import VoiceProvider
from app.domain.exceptions import AIProviderUnavailableError
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.appointment import AppointmentModel
from app.infrastructure.database.models.conversation import ConversationModel
from app.infrastructure.database.models.conversation_message import ConversationMessageModel
from app.infrastructure.database.models.emergency_ticket import EmergencyTicketModel
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.repositories import (
    SqlAlchemyCallerIdentityRepository,
    SqlAlchemyConversationRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.infrastructure.database.transactions import SqlAlchemySavepoints
from app.main import app, fastapi_app
from tests.fakes import ScriptedToolAIProvider, default_reply

_SECRET = "test-vapi-secret-turn-integrity"
_CALLER_ID = "+15551230000"
# The exact string the live model produced on 2026-09-24.
_SPOKEN_PHONE = "one two three four five six seven eight nine"

_EMERGENCY = {
    "customer_name": "Dana",
    "customer_phone": "6305550184",
    "service_address": "12 Elm Street, Lisle",
    "problem_description": "Smoke is coming from the furnace.",
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
        await conn.execute(text("SELECT 1"))
        await conn.run_sync(Base.metadata.drop_all)


class _Holder:
    """Lets a test swap the provider per test while the dependency override
    stays one lambda."""

    provider: AIProvider = ScriptedToolAIProvider()


@pytest_asyncio.fixture(loop_scope="session")
async def client(database_ready):
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: _Holder.provider
    fastapi_app.dependency_overrides[verify_vapi_secret] = _secret_override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    fastapi_app.dependency_overrides.pop(get_ai_provider, None)
    fastapi_app.dependency_overrides.pop(verify_vapi_secret, None)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _organization(client: AsyncClient, label: str) -> tuple[str, uuid.UUID, str]:
    """A registered org with a profile, open hours, one service, and a voice
    line. Returns (token, organization_id, assistant_id)."""
    suffix = uuid.uuid4().hex[:8]
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": f"{label} {suffix}",
            "full_name": "Owner",
            "email": f"{label.lower().replace(' ', '-')}-{suffix}@example.com",
            "password": "super-secret-123",
        },
    )
    assert response.status_code == 201, response.text
    token = response.json()["tokens"]["access_token"]
    org_id = uuid.UUID(response.json()["user"]["organization_id"])

    profile = await client.put(
        "/api/v1/business-knowledge/profile",
        json={
            "business_type": "hvac",
            "display_name": "Integrity HVAC",
            "phone_number": None,
            "timezone": "UTC",
            "address_line1": None,
            "address_line2": None,
            "city": None,
            "state": None,
            "postal_code": None,
            "country": "US",
            "website": None,
        },
        headers=_auth(token),
    )
    assert profile.status_code == 200, profile.text
    hours = await client.put(
        "/api/v1/business-knowledge/hours",
        json={
            "entries": [
                {
                    "day_of_week": day,
                    "is_closed": False,
                    "open_time": "00:00:00",
                    "close_time": "23:59:00",
                }
                for day in range(7)
            ]
        },
        headers=_auth(token),
    )
    assert hours.status_code == 200, hours.text

    assistant_id = f"asst_integrity_{suffix}"
    async with AsyncSessionLocal() as session:
        session.add(
            VoiceLineModel(
                organization_id=org_id,
                provider=VoiceProvider.VAPI,
                vapi_assistant_id=assistant_id,
                vapi_phone_number_id=None,
                phone_number=None,
                is_active=True,
            )
        )
        await session.commit()
    return token, org_id, assistant_id


async def _turn(
    client: AsyncClient,
    *,
    call_id: str,
    assistant_id: str,
    utterance: str,
    caller_number: str | None = _CALLER_ID,
    stream: bool = False,
):
    call: dict[str, object] = {"id": call_id, "assistantId": assistant_id}
    if caller_number is not None:
        call["customer"] = {"number": caller_number}
    return await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json={
            "call": call,
            "messages": [{"role": "user", "content": utterance}],
            "stream": stream,
        },
        headers={"x-vapi-secret": _SECRET},
    )


def _spoken_text(response) -> str:
    """The text the caller hears, from either transport."""
    if response.headers["content-type"].startswith("application/json"):
        return response.json()["choices"][0]["message"]["content"]
    pieces = []
    for line in response.text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        delta = json.loads(line[len("data: ") :])["choices"][0]["delta"]
        pieces.append(delta.get("content") or "")
    return "".join(pieces)


async def _conversation_for_call(call_id: str) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        voice_call = (
            await session.execute(select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == call_id))
        ).scalar_one()
        return voice_call.conversation_id


async def _ticket(conversation_id: uuid.UUID) -> EmergencyTicketModel | None:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(EmergencyTicketModel).where(
                    EmergencyTicketModel.conversation_id == conversation_id
                )
            )
        ).scalar_one_or_none()


async def _appointment(conversation_id: uuid.UUID) -> AppointmentModel | None:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(AppointmentModel).where(AppointmentModel.conversation_id == conversation_id)
            )
        ).scalar_one_or_none()


async def _message_count(conversation_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(ConversationMessageModel).where(
                ConversationMessageModel.conversation_id == conversation_id
            )
        )
        return len(rows.scalars().all())


async def _abort_the_transaction(session) -> None:
    """A real Postgres error: the statement fails and the transaction enters
    the aborted state, exactly as a column overflow or constraint violation
    would leave it."""
    await session.execute(text("SELECT 1/0"))


# --- The savepoint primitive itself ------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_savepoint_undoes_only_its_own_block(database_ready):
    org_suffix = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        from app.infrastructure.database.models.organization import OrganizationModel

        org_id = uuid.uuid4()
        session.add(OrganizationModel(id=org_id, name=f"SP {org_suffix}", slug=f"sp-{org_suffix}"))
        await session.flush()
        conversations = SqlAlchemyConversationRepository(session)
        savepoints = SqlAlchemySavepoints(session)
        from app.domain.entities.conversation import ConversationChannel

        kept = await conversations.create(
            organization_id=org_id, channel=ConversationChannel.VOICE, caller_phone_number=None
        )

        # 1. An error propagating out of the block: the block's write is
        #    undone, the earlier write survives, the session stays usable.
        with pytest.raises(DBAPIError):
            async with savepoints.isolate():
                await conversations.create(
                    organization_id=org_id,
                    channel=ConversationChannel.VOICE,
                    caller_phone_number="9" * 40,  # varchar(32): a real DataError
                )

        # 2. The trap: an error swallowed INSIDE the block. A bare
        #    `begin_nested()` releases over an aborted transaction and leaves
        #    the session unusable; the port must roll back instead.
        with pytest.raises(DBAPIError):
            async with savepoints.isolate():
                with contextlib.suppress(DBAPIError):
                    await _abort_the_transaction(session)

        # 3. A clean block keeps its writes.
        async with savepoints.isolate():
            also_kept = await conversations.create(
                organization_id=org_id, channel=ConversationChannel.VOICE, caller_phone_number=None
            )
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ConversationModel).where(ConversationModel.organization_id == org_id)
            )
        ).scalars().all()
    assert {row.id for row in rows} == {kept.id, also_kept.id}


# --- The 2026-09-24 overflow, second copy: the tool path -----------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_spoken_phone_number_in_the_tool_call_cannot_roll_back_the_turn(
    client: AsyncClient,
):
    """`create_service_request` used to fall back to the raw utterance when
    the number did not canonicalise — the exact defect fixed in the AI Brain
    on 2026-09-24, still present in the tool. 44 characters into varchar(32)
    aborted the transaction and the turn vanished.

    Now the number the call is coming from is used, the request is created,
    and the result says where the number came from so the assistant can
    confirm it with the caller."""
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "Spoken Tool Phone")
    call_id = f"call_spoken_{uuid.uuid4().hex[:8]}"

    provider.queue_tool_round(
        [
            (
                "create_service_request",
                {
                    "customer_name": "Lucky",
                    "customer_phone": _SPOKEN_PHONE,
                    "service_address": "16th Street, Lyle",
                    "problem_description": "AC is running but not cooling.",
                    "classification": "non_emergency",
                    "service_name": None,
                },
            )
        ]
    )
    provider.queue_reply(default_reply(message_to_customer="I've recorded your request."))

    response = await _turn(
        client, call_id=call_id, assistant_id=assistant_id, utterance="my number is ..."
    )
    assert response.status_code == 200, response.text

    created = provider.results[0].content
    assert created["success"] is True, created
    assert created["callback_number_source"] == "caller_id"

    conversation_id = await _conversation_for_call(call_id)
    appointment = await _appointment(conversation_id)
    assert appointment is not None
    assert appointment.customer_phone == _CALLER_ID
    # The turn itself persisted: both halves of the exchange.
    assert await _message_count(conversation_id) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unusable_number_with_no_caller_id_is_asked_for_not_invented(
    client: AsyncClient,
):
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "No Caller Id")
    call_id = f"call_noid_{uuid.uuid4().hex[:8]}"

    provider.queue_tool_round(
        [
            (
                "create_service_request",
                {
                    "customer_name": "Lucky",
                    "customer_phone": _SPOKEN_PHONE,
                    "service_address": "16th Street, Lyle",
                    "problem_description": "AC is running but not cooling.",
                    "classification": "non_emergency",
                    "service_name": None,
                },
            )
        ]
    )
    provider.queue_reply(default_reply(message_to_customer="Could you say your number again?"))

    response = await _turn(
        client,
        call_id=call_id,
        assistant_id=assistant_id,
        utterance="my number is ...",
        caller_number=None,
    )
    assert response.status_code == 200, response.text
    refused = provider.results[0].content
    assert refused["success"] is False
    assert refused["missing_fields"] == ["customer_phone"]
    assert "digits" in refused["detail"]

    conversation_id = await _conversation_for_call(call_id)
    assert await _appointment(conversation_id) is None
    assert await _message_count(conversation_id) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_over_long_names_and_addresses_are_bounded_not_fatal(client: AsyncClient):
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "Long Fields")
    call_id = f"call_long_{uuid.uuid4().hex[:8]}"
    long_name = "Bartholomew " * 40  # ~480 chars, column is 255
    long_address = "Unit 4, 1600 Some Very Long Road Name, " * 20  # ~800, column is 500

    provider.queue_tool_round(
        [
            (
                "create_service_request",
                {
                    "customer_name": long_name,
                    "customer_phone": "6305550184",
                    "service_address": long_address,
                    "problem_description": "No heat.",
                    "classification": "non_emergency",
                    "service_name": None,
                },
            )
        ]
    )
    provider.queue_reply(
        default_reply(
            message_to_customer="Got it.",
            customer_name=long_name,
            customer_address=long_address,
        )
    )
    response = await _turn(client, call_id=call_id, assistant_id=assistant_id, utterance="...")
    assert response.status_code == 200, response.text
    assert provider.results[0].content["success"] is True

    conversation_id = await _conversation_for_call(call_id)
    appointment = await _appointment(conversation_id)
    assert appointment is not None
    assert len(appointment.customer_name) == CUSTOMER_NAME_MAX_LENGTH
    assert len(appointment.customer_address) == CUSTOMER_ADDRESS_MAX_LENGTH
    assert await _message_count(conversation_id) == 2


# --- Emergencies must not wait on details the caller cannot give --------------


@pytest.mark.asyncio(loop_scope="session")
async def test_an_emergency_ticket_exists_before_the_name_and_address_are_known(
    client: AsyncClient,
):
    """"I have smoke coming from the unit" + "I don't know the address here"
    used to produce no ticket and no alert: the tool demanded all four
    details. The ticket must exist first; the details are backfilled when
    the caller gives them."""
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "Emergency First")
    call_id = f"call_emerg_{uuid.uuid4().hex[:8]}"

    provider.queue_tool_round(
        [
            (
                "create_service_request",
                {
                    **_EMERGENCY,
                    "customer_name": "",
                    "customer_phone": "",
                    "service_address": "",
                },
            )
        ]
    )
    provider.queue_reply(
        default_reply(
            message_to_customer="Your emergency is logged. What is the address?",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        )
    )
    first = await _turn(
        client, call_id=call_id, assistant_id=assistant_id, utterance="Smoke from my furnace!"
    )
    assert first.status_code == 200, first.text

    created = provider.results[0].content
    assert created["success"] is True, created
    assert created["service_request_type"] == "emergency_ticket"
    assert created["callback_number_source"] == "caller_id"
    assert sorted(created["still_needed"]) == ["customer_name", "service_address"]

    conversation_id = await _conversation_for_call(call_id)
    ticket = await _ticket(conversation_id)
    assert ticket is not None
    assert ticket.customer_phone == _CALLER_ID

    # The caller gives the details on the next turn; they reach the ticket.
    provider.queue_tool_round([("create_service_request", _EMERGENCY)])
    provider.queue_reply(
        default_reply(
            message_to_customer="Thank you, I've added that.",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        )
    )
    second = await _turn(
        client, call_id=call_id, assistant_id=assistant_id, utterance="Dana, 12 Elm Street, Lisle"
    )
    assert second.status_code == 200, second.text
    ticket = await _ticket(conversation_id)
    assert ticket.customer_name == "Dana"
    assert ticket.customer_address == "12 Elm Street, Lisle"
    assert ticket.customer_phone == "6305550184"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_standard_request_still_requires_every_detail(client: AsyncClient):
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "Standard Strict")
    call_id = f"call_std_{uuid.uuid4().hex[:8]}"
    provider.queue_tool_round(
        [
            (
                "create_service_request",
                {**_EMERGENCY, "classification": "non_emergency", "service_address": ""},
            )
        ]
    )
    provider.queue_reply(default_reply(message_to_customer="What's the address?"))
    response = await _turn(client, call_id=call_id, assistant_id=assistant_id, utterance="...")
    assert response.status_code == 200
    refused = provider.results[0].content
    assert refused["success"] is False
    assert refused["missing_fields"] == ["service_address"]


# --- A later failure must not erase an earlier action --------------------------


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("stream", [False, True])
async def test_a_failed_turn_write_keeps_the_ticket_the_caller_was_told_about(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, stream: bool
):
    """The ticket is created in the tool round; persisting the turn then hits
    a real database error. Before, the request rolled back and the ticket —
    which the caller had been told was logged, and a dispatcher may already
    have been paged about — disappeared. Now the caller hears the fallback
    and the ticket is committed."""
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, f"Persist Fails {stream}")
    call_id = f"call_persist_{uuid.uuid4().hex[:8]}"

    provider.queue_tool_round([("create_service_request", _EMERGENCY)])
    provider.queue_reply(
        default_reply(
            message_to_customer="Your emergency has been logged.",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        )
    )

    original = SqlAlchemyConversationRepository.add_message

    async def failing_add_message(self, conversation_id, *, role, content):
        await _abort_the_transaction(self._session)
        return await original(self, conversation_id, role=role, content=content)

    monkeypatch.setattr(SqlAlchemyConversationRepository, "add_message", failing_add_message)

    response = await _turn(
        client,
        call_id=call_id,
        assistant_id=assistant_id,
        utterance="Smoke from my furnace!",
        stream=stream,
    )
    monkeypatch.undo()

    assert response.status_code == 200, response.text
    assert provider.results[0].content["success"] is True
    assert "trouble connecting" in _spoken_text(response)

    conversation_id = await _conversation_for_call(call_id)
    ticket = await _ticket(conversation_id)
    assert ticket is not None, "the ticket the caller was told about was rolled back"
    # The turn's own writes were undone together — never a half turn.
    assert await _message_count(conversation_id) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_a_tool_that_hits_a_database_error_does_not_take_earlier_tools_with_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Round 1 opens the ticket; round 2's tool hits a real database error.
    The tool returns INTERNAL_ERROR, and — the part that used to fail — the
    rest of the turn still works: it persists, and the ticket survives."""
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "Tool Fails")
    call_id = f"call_toolfail_{uuid.uuid4().hex[:8]}"

    from app.application.services.voice_tool_executor import VoiceToolExecutor

    original = VoiceToolExecutor._check_availability

    async def failing_check(self, organization_id, conversation_id, arguments, turn_index):
        await _abort_the_transaction(self._offered_slots._session)
        return await original(self, organization_id, conversation_id, arguments, turn_index)

    monkeypatch.setattr(VoiceToolExecutor, "_check_availability", failing_check)

    provider.queue_tool_round([("create_service_request", _EMERGENCY)])
    provider.queue_tool_round([("check_availability", {"service_name": None})])
    provider.queue_reply(
        default_reply(
            message_to_customer="Your emergency has been logged.",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        )
    )
    response = await _turn(
        client, call_id=call_id, assistant_id=assistant_id, utterance="Smoke!", stream=True
    )
    monkeypatch.undo()

    assert response.status_code == 200, response.text
    assert provider.results[0].content["success"] is True
    assert provider.results[1].content == {"success": False, "error": "INTERNAL_ERROR"}
    assert "Your emergency has been logged." in _spoken_text(response)

    conversation_id = await _conversation_for_call(call_id)
    assert await _ticket(conversation_id) is not None
    assert await _message_count(conversation_id) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_post_turn_sync_keeps_the_turn_and_the_ticket(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """The caller-ID association is the last write of the turn, after the
    caller has heard everything. It already "swallowed" its errors — but a
    caught database error does not un-abort a Postgres transaction, so the
    commit failed and the whole turn went with it."""
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "Sync Fails")
    call_id = f"call_syncfail_{uuid.uuid4().hex[:8]}"

    async def failing_associate(self, organization_id, *, customer_id, caller_number):
        await _abort_the_transaction(self._session)

    monkeypatch.setattr(SqlAlchemyCallerIdentityRepository, "associate", failing_associate)

    provider.queue_tool_round([("create_service_request", _EMERGENCY)])
    provider.queue_reply(
        default_reply(
            message_to_customer="Your emergency has been logged.",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
            customer_phone="6305550184",
            is_conversation_complete=True,
        )
    )
    response = await _turn(
        client, call_id=call_id, assistant_id=assistant_id, utterance="Smoke!", stream=True
    )
    monkeypatch.undo()

    assert response.status_code == 200, response.text
    assert "endCall" in response.text
    conversation_id = await _conversation_for_call(call_id)
    assert await _ticket(conversation_id) is not None
    assert await _message_count(conversation_id) == 2


class _ToolThenUnparseable(AIProvider):
    """Runs one real tool round, then fails the way a truncated or malformed
    model document now fails: as `AIProviderUnavailableError`."""

    def __init__(self, invocation: ToolInvocation) -> None:
        self._invocation = invocation

    async def generate_reply(self, request: AIRequest) -> AIReply:
        assert request.tool_executor is not None
        await request.tool_executor.execute(self._invocation)
        raise AIProviderUnavailableError(
            "The AI Brain returned a response that could not be understood."
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unparseable_model_reply_after_a_tool_round_keeps_the_ticket(
    client: AsyncClient,
):
    _Holder.provider = _ToolThenUnparseable(
        ToolInvocation(id="call_x", name="create_service_request", arguments=_EMERGENCY)
    )
    _, _, assistant_id = await _organization(client, "Unparseable")
    call_id = f"call_unparse_{uuid.uuid4().hex[:8]}"

    response = await _turn(
        client, call_id=call_id, assistant_id=assistant_id, utterance="Smoke!", stream=True
    )
    assert response.status_code == 200, response.text
    assert "trouble connecting" in _spoken_text(response)
    conversation_id = await _conversation_for_call(call_id)
    assert await _ticket(conversation_id) is not None


# --- Vapi-supplied values ------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_sip_caller_id_longer_than_a_phone_column_does_not_kill_the_call(
    client: AsyncClient,
):
    """A caller ID is Vapi's value, not ours: over a SIP trunk it is a URI.
    Written verbatim into varchar(32) it failed the conversation insert on
    the first turn — and on every turn after it, so the call never worked."""
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "Sip Caller")
    call_id = f"call_sip_{uuid.uuid4().hex[:8]}"
    provider.queue_reply(default_reply(message_to_customer="How can I help?"))

    sip = "sip:+15551230000@pstn.twilio-trunk.example.com"
    response = await _turn(
        client, call_id=call_id, assistant_id=assistant_id, utterance="Hi", caller_number=sip
    )
    assert response.status_code == 200, response.text
    assert _spoken_text(response) == "How can I help?"

    async with AsyncSessionLocal() as session:
        voice_call = (
            await session.execute(select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == call_id))
        ).scalar_one()
    # Reduced to its digits: still a stable lookup key for the next call from
    # the same trunk, and now one that fits.
    assert voice_call.caller_number == "15551230000"


@pytest.mark.asyncio(loop_scope="session")
async def test_the_turn_limit_ends_the_call_instead_of_looping(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """At `AI_MAX_CONVERSATION_TURNS` every further utterance used to get the
    generic "trouble connecting" line with no endCall — an unending loop."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "AI_MAX_CONVERSATION_TURNS", 1)
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "Turn Limit")
    call_id = f"call_limit_{uuid.uuid4().hex[:8]}"
    provider.queue_reply(default_reply(message_to_customer="How can I help?"))

    first = await _turn(client, call_id=call_id, assistant_id=assistant_id, utterance="Hi")
    assert first.status_code == 200
    for stream in (False, True):
        again = await _turn(
            client, call_id=call_id, assistant_id=assistant_id, utterance="Still there?", stream=stream
        )
        assert again.status_code == 200
        assert "not able to continue this call" in _spoken_text(again)
        assert "endCall" in again.text


@pytest.mark.asyncio(loop_scope="session")
async def test_an_end_of_call_report_with_unexpected_shapes_is_recorded(client: AsyncClient):
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "End Report")
    call_id = f"call_eoc_{uuid.uuid4().hex[:8]}"
    provider.queue_reply(default_reply(message_to_customer="How can I help?"))
    assert (await _turn(client, call_id=call_id, assistant_id=assistant_id, utterance="Hi")).status_code == 200

    report = await client.post(
        "/api/v1/voice/vapi/events",
        json={
            "message": {
                "type": "end-of-call-report",
                "call": {"id": call_id},
                "endedReason": "call.in-progress.error-providerfault-" + "x" * 80,
                "durationSeconds": "42.7",
                "recordingUrl": "https://example.com/" + "r" * 2000,
            }
        },
        headers={"x-vapi-secret": _SECRET},
    )
    assert report.status_code == 200, report.text

    async with AsyncSessionLocal() as session:
        voice_call = (
            await session.execute(select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == call_id))
        ).scalar_one()
        conversation = await session.get(ConversationModel, voice_call.conversation_id)
    assert voice_call.ended_at is not None
    assert len(voice_call.ended_reason) == 64
    assert voice_call.duration_seconds == 42
    assert voice_call.recording_url is None
    assert conversation.status.value == "completed"

    malformed = await client.post(
        "/api/v1/voice/vapi/events",
        json={"message": "not-an-object"},
        headers={"x-vapi-secret": _SECRET},
    )
    assert malformed.status_code == 200


@pytest.mark.asyncio(loop_scope="session")
async def test_an_emergency_opened_without_the_tool_still_gets_a_callback_number(
    client: AsyncClient,
):
    """Found by the real-model matrix: on "I have smoke coming from the unit"
    the model classified the emergency without calling the tool, so the
    ticket was opened by the outcome sync — which had no caller-ID fallback.
    The caller then refused both number and address, and the ticket a
    dispatcher would work from had neither. The caller ID fills the blank;
    a number the caller states later replaces it; a later turn that omits
    the number never puts the caller ID back over a stated one."""
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "Sync Caller Id")
    call_id = f"call_syncid_{uuid.uuid4().hex[:8]}"

    def emergency_reply(phone: str | None) -> AIReply:
        return default_reply(
            message_to_customer="I'm logging this as an emergency.",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
            customer_phone=phone,
        )

    provider.queue_reply(emergency_reply(None))
    assert (
        await _turn(client, call_id=call_id, assistant_id=assistant_id, utterance="Smoke!")
    ).status_code == 200
    conversation_id = await _conversation_for_call(call_id)
    ticket = await _ticket(conversation_id)
    assert ticket is not None
    assert ticket.customer_phone == _CALLER_ID

    provider.queue_reply(emergency_reply("6305550184"))
    await _turn(client, call_id=call_id, assistant_id=assistant_id, utterance="630 555 0184")
    assert (await _ticket(conversation_id)).customer_phone == "6305550184"

    provider.queue_reply(emergency_reply(None))
    await _turn(client, call_id=call_id, assistant_id=assistant_id, utterance="Please hurry.")
    assert (await _ticket(conversation_id)).customer_phone == "6305550184"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_turn_after_the_call_completed_ends_the_call_instead_of_looping(
    client: AsyncClient,
):
    """Normally impossible — the closing turn carries endCall — but if Vapi
    sends another utterance anyway, every one of them used to get "trouble
    connecting" with no endCall until the line timed out on silence."""
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    _, _, assistant_id = await _organization(client, "After Completion")
    call_id = f"call_after_{uuid.uuid4().hex[:8]}"
    provider.queue_reply(
        default_reply(message_to_customer="Thanks for calling. Goodbye.", is_conversation_complete=True)
    )
    first = await _turn(client, call_id=call_id, assistant_id=assistant_id, utterance="That's all")
    assert "endCall" in first.text

    for stream in (False, True):
        again = await _turn(
            client, call_id=call_id, assistant_id=assistant_id, utterance="Wait, one more thing", stream=stream
        )
        assert again.status_code == 200
        assert "already been completed" in _spoken_text(again)
        assert "trouble connecting" not in _spoken_text(again)
        assert "endCall" in again.text


class _BreaksMidStream(AIProvider):
    """Speaks, then fails with something that is not a domain error — the
    shape of a lost database connection or a plain defect."""

    async def generate_reply(self, request: AIRequest) -> AIReply:  # pragma: no cover
        raise NotImplementedError

    async def stream_reply(self, request: AIRequest):
        from app.domain.ai.provider import AITextDelta

        yield AITextDelta("Let me look into that. ")
        raise KeyError("an unexpected defect")


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unexpected_failure_mid_stream_still_ends_the_stream_speakably(
    client: AsyncClient,
):
    """Before, the SSE body ended without [DONE] and the caller heard dead air
    until Vapi hung up. Now the stream finishes with the spoken fallback."""
    _Holder.provider = _BreaksMidStream()
    _, _, assistant_id = await _organization(client, "Mid Stream")
    response = await _turn(
        client,
        call_id=f"call_mid_{uuid.uuid4().hex[:8]}",
        assistant_id=assistant_id,
        utterance="Hello?",
        stream=True,
    )
    assert response.status_code == 200
    assert response.text.rstrip().endswith("data: [DONE]")
    assert "trouble connecting" in _spoken_text(response)
