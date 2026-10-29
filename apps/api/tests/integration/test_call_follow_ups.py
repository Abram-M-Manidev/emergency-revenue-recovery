"""Calls that end, or break, before the caller's request was recorded.

Real Vapi webhooks (`/chat/completions` and `/events`), real services, real
Postgres. Only the model (scripted, or deliberately broken) and the
call-control provider (a fake `CallTransferPort`) are replaced.

What this file pins down, each one a gap found by the 2026-10-28 acceptance
audit:

- B1: a switched-off assistant used to say "Please hold the line for our
  team" and then hang up. A caller told to hold is a caller who stops
  looking for help.
- B2: an AI outage used to say "try calling back in a few minutes" and do
  nothing else: no ticket, no alert, no person, even for a gas leak.
- B3: a caller who hung up before `create_service_request` ran left only a
  transcript. Nobody was told, and an emergency described in that call
  reached no one.
- B8: the owner could not see, from the dashboard, whether a transfer was
  attempted or what happened to it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.api.deps as deps_module
from app.api.deps import get_ai_provider, get_notification_provider, verify_vapi_secret
from app.domain.ai.provider import AIProvider, AIReply, AIRequest
from app.domain.call_transfer.attempt import TransferReason, TransferStatus
from app.domain.call_transfer.port import CallTransferPort, TransferInitiation
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_line import VoiceProvider
from app.domain.exceptions import AIProviderUnavailableError, InvalidTokenError
from app.domain.follow_ups import FollowUpReason
from app.domain.notifications.emergency import DeliveryStatus, NotificationChannel
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.call_follow_up import CallFollowUpModel
from app.infrastructure.database.models.call_transfer import (
    CallTransferAttemptModel,
    OrganizationCallTransferSettingsModel,
)
from app.infrastructure.database.models.emergency_ticket import EmergencyTicketModel
from app.infrastructure.database.models.notification import (
    EmergencyNotificationDeliveryModel,
    OrganizationNotificationSettingsModel,
)
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app, fastapi_app
from tests.fakes import FakeNotificationProvider, ScriptedToolAIProvider, default_reply

_SECRET = "test-vapi-secret-follow-ups"
_COMPLETIONS = "/api/v1/voice/vapi/chat/completions"
_EVENTS = "/api/v1/voice/vapi/events"
_CONTROL = "https://phone-call-websocket.aws-us-west-2-backend-production1.vapi.ai/f9q/control"
# 555-01xx: reserved for fiction, so no real subscriber can be implied.
_CALLER = "+16305550177"
_OFFICE = "+15550103000"
_ON_CALL = "+15550104000"
_AI_LINE = "+15550198000"


class _Holder:
    provider: AIProvider = ScriptedToolAIProvider()


class _Down(AIProvider):
    """The AI provider during an outage: every request fails the way the
    OpenAI adapter reports a timeout or a 5xx."""

    async def generate_reply(self, request: AIRequest) -> AIReply:
        raise AIProviderUnavailableError("The AI provider is unavailable.")


class RecordingPort(CallTransferPort):
    """Stands in for Vapi Live Call Control."""

    outcome = TransferInitiation(True)
    calls: list[dict[str, str]] = []

    async def transfer(self, *, call_control, destination_number, announcement):
        RecordingPort.calls.append(
            {"call_control": call_control, "number": destination_number, "announcement": announcement}
        )
        return RecordingPort.outcome


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
async def client(database_ready, monkeypatch):
    _Holder.provider = ScriptedToolAIProvider()
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: _Holder.provider
    fastapi_app.dependency_overrides[verify_vapi_secret] = _secret_override
    RecordingPort.outcome = TransferInitiation(True)
    RecordingPort.calls = []
    monkeypatch.setattr(deps_module, "VapiCallControlTransfer", RecordingPort)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    fastapi_app.dependency_overrides.pop(get_ai_provider, None)
    fastapi_app.dependency_overrides.pop(verify_vapi_secret, None)


def _scripted() -> ScriptedToolAIProvider:
    provider = ScriptedToolAIProvider()
    _Holder.provider = provider
    return provider


async def _tenant(
    *,
    organization_id: uuid.UUID | None = None,
    transfer: bool = False,
    enabled: bool = True,
) -> str:
    """A voice line (and optionally human transfer) for a new or existing
    organization. Returns the Vapi assistant id that routes to it."""
    assistant = f"asst_fu_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        if organization_id is None:
            organization_id = uuid.uuid4()
            session.add(OrganizationModel(
                id=organization_id, name=f"FU {organization_id.hex[:6]}",
                slug=f"fu-{organization_id.hex[:10]}", voice_assistant_enabled=enabled))
            await session.flush()
        session.add(VoiceLineModel(organization_id=organization_id, provider=VoiceProvider.VAPI,
                                   vapi_assistant_id=assistant, phone_number=_AI_LINE, is_active=True))
        if transfer:
            session.add(OrganizationCallTransferSettingsModel(
                organization_id=organization_id, business_hours_number=_OFFICE,
                after_hours_number=_ON_CALL, transfer_emergencies=False, is_enabled=True))
        await session.commit()
    return assistant


def _body(call_id: str, assistant: str, utterance: str, *, stream: bool = True,
          control: str | None = _CONTROL) -> dict:
    call: dict = {"id": call_id, "assistantId": assistant, "customer": {"number": _CALLER}}
    if control is not None:
        call["monitor"] = {"controlUrl": control}
    return {"call": call, "messages": [{"role": "user", "content": utterance}], "stream": stream}


def _spoken(response_text: str) -> str:
    """Every word the caller would hear, across SSE frames or a JSON body."""
    import json

    if not response_text.lstrip().startswith("data:"):
        return json.loads(response_text)["choices"][0]["message"]["content"]
    words = []
    for line in response_text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        delta = json.loads(line[len("data: "):])["choices"][0]["delta"]
        words.append(delta.get("content") or "")
    return "".join(words)


async def _turn(client: AsyncClient, body: dict) -> str:
    response = await client.post(_COMPLETIONS, json=body, headers={"x-vapi-secret": _SECRET})
    assert response.status_code == 200, response.text
    return response.text


async def _hang_up(client: AsyncClient, call_id: str, *, caller_lines: list[str] | None = None,
                   ended_reason: str = "customer-ended-call") -> None:
    message: dict = {"type": "end-of-call-report", "call": {"id": call_id},
                     "endedReason": ended_reason, "durationSeconds": 21}
    if caller_lines is not None:
        message["artifact"] = {"messages": [{"role": "bot", "message": "Thanks for calling."}]
                               + [{"role": "user", "message": line} for line in caller_lines]}
    response = await client.post(_EVENTS, json={"message": message},
                                 headers={"x-vapi-secret": _SECRET})
    assert response.status_code == 200, response.text


async def _records(call_id: str):
    async with AsyncSessionLocal() as session:
        voice_call = (await session.execute(
            select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == call_id))).scalar_one()
        conversation_id = voice_call.conversation_id
        follow_ups = (await session.execute(select(CallFollowUpModel).where(
            CallFollowUpModel.conversation_id == conversation_id))).scalars().all()
        tickets = (await session.execute(select(EmergencyTicketModel).where(
            EmergencyTicketModel.conversation_id == conversation_id))).scalars().all()
        deliveries = []
        if tickets:
            deliveries = (await session.execute(select(EmergencyNotificationDeliveryModel).where(
                EmergencyNotificationDeliveryModel.emergency_ticket_id == tickets[0].id))).scalars().all()
        attempts = (await session.execute(select(CallTransferAttemptModel).where(
            CallTransferAttemptModel.conversation_id == conversation_id))).scalars().all()
    return conversation_id, list(follow_ups), list(tickets), list(deliveries), list(attempts)


async def _register(client: AsyncClient, name: str) -> tuple[str, uuid.UUID]:
    response = await client.post("/api/v1/auth/register", json={
        "organization_name": name, "full_name": "Test Owner",
        "email": f"owner-{uuid.uuid4().hex[:10]}@example.com", "password": "super-secret-123"})
    assert response.status_code == 201, response.text
    body = response.json()
    return body["tokens"]["access_token"], uuid.UUID(body["user"]["organization_id"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- B1: the switched-off assistant -------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("stream", [True, False])
async def test_a_disabled_assistant_never_says_hold_the_line_while_it_hangs_up(client, stream):
    assistant = await _tenant(enabled=False)
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"

    response = await _turn(client, _body(call_id, assistant, "Hello?", stream=stream))

    spoken = _spoken(response)
    assert "endCall" in response, "a switched-off assistant still ends the call"
    assert "hold" not in spoken.lower(), "the caller must not be told to wait on a call being ended"
    assert "automated assistant is unavailable" in spoken
    assert "emergency" in spoken.lower(), "the caller must still be told where to go if in danger"


# --- B2: the AI provider is down ----------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_an_ai_outage_hands_the_caller_to_a_person_when_it_can(client):
    _Holder.provider = _Down()
    assistant = await _tenant(transfer=True)
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"

    response = await _turn(client, _body(call_id, assistant, "My heater stopped working."))

    assert "endCall" not in response, "hanging up would cut the transfer off"
    assert "try calling back" not in _spoken(response)
    assert [c["number"] for c in RecordingPort.calls] == [_OFFICE]
    _, follow_ups, tickets, _, attempts = await _records(call_id)
    assert [(a.status, a.reason) for a in attempts] == [
        (TransferStatus.INITIATED, TransferReason.SYSTEM_UNAVAILABLE)]
    assert [f.reason for f in follow_ups] == [FollowUpReason.AI_UNAVAILABLE]
    assert tickets == [], "nothing described an emergency"


@pytest.mark.asyncio(loop_scope="session")
async def test_an_ai_outage_with_nobody_to_transfer_to_points_at_help_and_flags_a_callback(client):
    _Holder.provider = _Down()
    assistant = await _tenant(transfer=False)
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"

    response = await _turn(client, _body(call_id, assistant, "I need my boiler serviced."))

    spoken = _spoken(response)
    assert "trouble connecting" in spoken
    assert "in danger" in spoken and "emergency number" in spoken
    assert RecordingPort.calls == []
    _, follow_ups, _, _, attempts = await _records(call_id)
    assert [a.status for a in attempts] == [TransferStatus.UNAVAILABLE]
    assert len(follow_ups) == 1
    follow_up = follow_ups[0]
    assert follow_up.reason is FollowUpReason.AI_UNAVAILABLE
    assert follow_up.caller_number == _CALLER
    assert follow_up.possible_emergency is False
    assert follow_up.resolved_at is None


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("stream", [True, False])
async def test_an_ai_outage_during_an_emergency_opens_a_ticket_and_queues_its_alert(client, stream):
    _Holder.provider = _Down()
    assistant = await _tenant(transfer=False)
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"

    response = await _turn(client, _body(call_id, assistant, "I smell gas in my kitchen!",
                                         stream=stream))

    spoken = _spoken(response)
    assert "leave the building" in spoken, "the fixed safety instruction must survive the outage"
    # Deliberately reversed: this asserted "flagged" — a claim made with no
    # alert destination configured, so nobody could have been alerted.
    assert "possible emergency" in spoken
    assert _UNCONFIRMED_ALERT in spoken
    assert "flagged" not in spoken and "being sent" not in spoken
    _, follow_ups, tickets, deliveries, _ = await _records(call_id)
    assert len(tickets) == 1
    assert tickets[0].customer_phone is not None, "the dispatcher needs someone to call back"
    assert "gas" in tickets[0].summary.lower()
    assert len(deliveries) == 1, "the alert is queued with the ticket"
    assert deliveries[0].status is DeliveryStatus.NOT_CONFIGURED
    assert follow_ups[0].possible_emergency is True
    assert follow_ups[0].emergency_ticket_id == tickets[0].id


# --- What the outage reply may say about the alert ---------------------------
#
# On the outage turn the alert is at most QUEUED: it is sent after the turn
# commits. The reply used to claim the call was "flagged to the team" in
# every state. Each state below gets only the sentence its evidence allows,
# and the ticket, the alert and the follow-up are created exactly as before.

_UNCONFIRMED_ALERT = (
    "I've logged your call as a possible emergency, but I can't confirm that anyone "
    "on the team has been alerted."
)
_ALERT_BEING_SENT = (
    "I've logged your call as a possible emergency, and an alert to the team is being sent now."
)
_ALERT_SENT = "I've sent your call to the team as a possible emergency."


async def _alert_destination(assistant: str) -> uuid.UUID:
    """Gives the assistant's organization an emergency alert destination.
    The send itself goes to the fake provider the test installs."""
    async with AsyncSessionLocal() as session:
        organization_id = (await session.execute(
            select(VoiceLineModel.organization_id)
            .where(VoiceLineModel.vapi_assistant_id == assistant))).scalar_one()
        session.add(OrganizationNotificationSettingsModel(
            organization_id=organization_id, channel=NotificationChannel.WEBHOOK,
            destination="https://alerts.example.com/hook", is_enabled=True))
        await session.commit()
    return organization_id


@pytest.fixture
def fake_alerts():
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    fastapi_app.dependency_overrides[get_notification_provider] = lambda: provider
    yield provider
    fastapi_app.dependency_overrides.pop(get_notification_provider, None)


async def _seed_earlier_emergency(call_id: str, *, status: DeliveryStatus,
                                  next_attempt_at: datetime | None, attempts: int) -> uuid.UUID:
    """An emergency ticket recorded earlier in this call, with its alert in
    `status` — what the outage turn finds and reuses."""
    async with AsyncSessionLocal() as session:
        voice_call = (await session.execute(
            select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == call_id))).scalar_one()
        ticket = EmergencyTicketModel(
            organization_id=voice_call.organization_id, conversation_id=voice_call.conversation_id,
            customer_phone=_CALLER, summary="Gas smell reported earlier in the call.")
        session.add(ticket)
        await session.flush()
        session.add(EmergencyNotificationDeliveryModel(
            organization_id=voice_call.organization_id, emergency_ticket_id=ticket.id,
            channel=NotificationChannel.WEBHOOK, provider="fake", status=status,
            attempts=attempts, next_attempt_at=next_attempt_at,
            delivered_at=datetime.now(timezone.utc) if status is DeliveryStatus.DELIVERED else None))
        await session.commit()
        return ticket.id


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("stream", [True, False])
async def test_an_outage_with_an_alert_queued_says_it_is_being_sent_not_sent(
    client, fake_alerts, stream
):
    _Holder.provider = _Down()
    assistant = await _tenant()
    await _alert_destination(assistant)
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"

    spoken = _spoken(await _turn(client, _body(call_id, assistant, "I smell gas in my kitchen!",
                                               stream=stream)))

    assert _ALERT_BEING_SENT in spoken
    assert _ALERT_SENT not in spoken and "flagged" not in spoken
    assert "leave the building" in spoken
    _, follow_ups, tickets, deliveries, _ = await _records(call_id)
    assert len(tickets) == 1 and len(deliveries) == 1, "ticket and alert exactly as before"
    assert follow_ups[0].possible_emergency is True


@pytest.mark.asyncio(loop_scope="session")
async def test_an_outage_after_an_alert_was_delivered_may_say_sent(client, fake_alerts):
    scripted = _scripted()
    scripted.queue_reply(default_reply(message_to_customer="What's going on?"))
    assistant = await _tenant()
    await _alert_destination(assistant)
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    await _turn(client, _body(call_id, assistant, "Hi, I need some help."))
    ticket_id = await _seed_earlier_emergency(call_id, status=DeliveryStatus.DELIVERED,
                                              next_attempt_at=None, attempts=1)

    _Holder.provider = _Down()
    spoken = _spoken(await _turn(client, _body(call_id, assistant, "I smell gas in here.")))

    assert _ALERT_SENT in spoken
    assert "dispatcher" not in spoken.lower() and "on the way" not in spoken.lower()
    _, follow_ups, tickets, _, _ = await _records(call_id)
    assert [t.id for t in tickets] == [ticket_id], "the earlier ticket is reused, not duplicated"
    assert follow_ups[0].emergency_ticket_id == ticket_id


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("label", "next_attempt_in", "attempts"),
    [("retries_spent", None, 8), ("failed_retry_pending", timedelta(minutes=5), 2)],
)
async def test_an_outage_after_an_alert_failed_claims_nothing(
    client, fake_alerts, label, next_attempt_in, attempts
):
    scripted = _scripted()
    scripted.queue_reply(default_reply(message_to_customer="What's going on?"))
    assistant = await _tenant()
    await _alert_destination(assistant)
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    await _turn(client, _body(call_id, assistant, "Hi, I need some help."))
    await _seed_earlier_emergency(
        call_id, status=DeliveryStatus.FAILED, attempts=attempts,
        next_attempt_at=(datetime.now(timezone.utc) + next_attempt_in) if next_attempt_in else None)

    _Holder.provider = _Down()
    spoken = _spoken(await _turn(client, _body(call_id, assistant, "I smell gas in here.")))

    assert _UNCONFIRMED_ALERT in spoken, label
    assert "being sent" not in spoken and _ALERT_SENT not in spoken, label
    assert "If anyone is in danger, please hang up and call" in spoken, label
    _, follow_ups, tickets, _, _ = await _records(call_id)
    assert len(tickets) == 1 and follow_ups[0].possible_emergency is True, label


# --- B3: the caller hangs up before the request is recorded ------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_hang_up_before_the_request_was_recorded_leaves_a_callback(client):
    scripted = _scripted()
    assistant = await _tenant()
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    # Still collecting details: no recommendation yet. (A BOOK_APPOINTMENT
    # outcome would already have opened an appointment request — a recorded
    # request, visible under Appointments, and so no follow-up.)
    scripted.queue_reply(default_reply(message_to_customer="Sure. What's your name?",
                                       classification=CallClassification.UNKNOWN,
                                       recommended_action=RecommendedAction.NONE))
    await _turn(client, _body(call_id, assistant, "I'd like someone to look at my AC."))

    await _hang_up(client, call_id)

    _, follow_ups, tickets, _, _ = await _records(call_id)
    assert [(f.reason, f.possible_emergency) for f in follow_ups] == [
        (FollowUpReason.ENDED_BEFORE_REQUEST_RECORDED, False)]
    assert follow_ups[0].caller_number == _CALLER
    assert tickets == []


@pytest.mark.asyncio(loop_scope="session")
async def test_an_emergency_described_before_the_hang_up_is_ticketed_and_alerted(client):
    scripted = _scripted()
    assistant = await _tenant()
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    # The model never called the tool and never classified it: exactly the
    # call that used to reach nobody.
    scripted.queue_reply(default_reply(message_to_customer="I'm sorry to hear that. Your name?",
                                       classification=CallClassification.UNKNOWN,
                                       recommended_action=RecommendedAction.NONE))
    await _turn(client, _body(call_id, assistant, "There's smoke coming out of my furnace"))

    await _hang_up(client, call_id)

    _, follow_ups, tickets, deliveries, _ = await _records(call_id)
    assert len(tickets) == 1 and len(deliveries) == 1
    assert "smoke" in tickets[0].summary.lower()
    assert follow_ups[0].possible_emergency is True
    assert follow_ups[0].emergency_ticket_id == tickets[0].id


@pytest.mark.asyncio(loop_scope="session")
async def test_words_only_vapi_heard_are_read_from_the_end_of_call_report(client):
    """The caller spoke again and hung up before that utterance ever reached
    ERRS. Vapi's own transcript is then the only record of it."""
    scripted = _scripted()
    assistant = await _tenant()
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    scripted.queue_reply(default_reply(message_to_customer="How can I help?"))
    await _turn(client, _body(call_id, assistant, "Hi there."))

    await _hang_up(client, call_id, caller_lines=["Hi there.", "My carbon monoxide alarm is going off"])

    _, follow_ups, tickets, _, _ = await _records(call_id)
    assert len(tickets) == 1
    assert follow_ups[0].possible_emergency is True


@pytest.mark.asyncio(loop_scope="session")
async def test_the_assistants_own_words_never_make_a_call_an_emergency(client):
    """Vapi's transcript carries both sides. The assistant's safety sentence
    names smoke and gas; only the caller's lines may count."""
    scripted = _scripted()
    assistant = await _tenant()
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    scripted.queue_reply(default_reply(message_to_customer="What can I do for you?"))
    await _turn(client, _body(call_id, assistant, "Question about my bill."))

    response = await client.post(_EVENTS, headers={"x-vapi-secret": _SECRET}, json={"message": {
        "type": "end-of-call-report", "call": {"id": call_id}, "endedReason": "customer-ended-call",
        "artifact": {"transcript": "AI: If you smell gas, leave now.\nUser: Question about my bill."},
    }})
    assert response.status_code == 200

    _, follow_ups, tickets, _, _ = await _records(call_id)
    assert tickets == []
    assert follow_ups[0].possible_emergency is False


@pytest.mark.asyncio(loop_scope="session")
async def test_a_recorded_request_leaves_no_callback(client):
    scripted = _scripted()
    assistant = await _tenant()
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round([("create_service_request", {
        "customer_name": "Ari", "customer_phone": "6305550177",
        "service_address": "9 Elm Street, Lisle", "problem_description": "AC not cooling.",
        "classification": "non_emergency", "service_name": None})])
    scripted.queue_reply(default_reply(message_to_customer="Got it, I've logged that.",
                                       recommended_action=RecommendedAction.BOOK_APPOINTMENT))
    await _turn(client, _body(call_id, assistant, "AC not cooling. I'm Ari at 9 Elm Street."))

    await _hang_up(client, call_id)

    _, follow_ups, _, _, _ = await _records(call_id)
    assert follow_ups == []


@pytest.mark.asyncio(loop_scope="session")
async def test_a_call_the_assistant_finished_without_a_request_leaves_no_callback(client):
    """An hours question, answered, and the assistant ended the call: there
    is nothing to call back about."""
    scripted = _scripted()
    assistant = await _tenant()
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    scripted.queue_reply(default_reply(message_to_customer="We open at 8. Thank you for calling.",
                                       is_conversation_complete=True))
    await _turn(client, _body(call_id, assistant, "What time do you open?"))

    await _hang_up(client, call_id, ended_reason="assistant-ended-call")

    _, follow_ups, _, _, _ = await _records(call_id)
    assert follow_ups == []


@pytest.mark.asyncio(loop_scope="session")
async def test_a_transferred_call_with_no_recorded_request_is_flagged_for_confirmation(client):
    """Nothing confirms that anyone answered a transfer. If no request was
    recorded either, the business must check the caller was helped."""
    scripted = _scripted()
    assistant = await _tenant(transfer=True)
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round([("transfer_to_human", {"reason": "caller_requested",
                                                      "is_emergency": False})])
    scripted.queue_reply(default_reply(message_to_customer="",
                                       recommended_action=RecommendedAction.ESCALATE_TO_HUMAN))
    await _turn(client, _body(call_id, assistant, "Let me talk to a person."))

    await _hang_up(client, call_id, ended_reason="assistant-forwarded-call")

    _, follow_ups, _, _, _ = await _records(call_id)
    assert [f.reason for f in follow_ups] == [FollowUpReason.TRANSFERRED_BEFORE_REQUEST_RECORDED]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_repeated_end_of_call_report_is_idempotent(client):
    scripted = _scripted()
    assistant = await _tenant()
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    scripted.queue_reply(default_reply(message_to_customer="Oh no. What's the address?",
                                       classification=CallClassification.UNKNOWN))
    await _turn(client, _body(call_id, assistant, "There's water pouring through my ceiling"))

    await _hang_up(client, call_id)
    await _hang_up(client, call_id)

    _, follow_ups, tickets, deliveries, _ = await _records(call_id)
    assert len(follow_ups) == 1 and len(tickets) == 1 and len(deliveries) == 1


# --- B8: what the owner can see ----------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_owner_sees_transfer_attempts_and_the_callback_on_the_call(client):
    token, organization_id = await _register(client, "Follow-up Owner View")
    assistant = await _tenant(organization_id=organization_id, transfer=True)
    RecordingPort.outcome = TransferInitiation(False, "PROVIDER_REJECTED")
    scripted = _scripted()
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round([("transfer_to_human", {"reason": "caller_requested",
                                                      "is_emergency": False})])
    scripted.queue_reply(default_reply(message_to_customer="I can't connect you right now."))
    await _turn(client, _body(call_id, assistant, "Can I talk to a human?"))
    await _hang_up(client, call_id)
    conversation_id, _, _, _, _ = await _records(call_id)

    response = await client.get(f"/api/v1/voice/calls/{conversation_id}", headers=_auth(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(a["status"], a["reason"], a["error_code"]) for a in body["transfer_attempts"]] == [
        ("failed", "caller_requested", "PROVIDER_REJECTED")]
    attempt = body["transfer_attempts"][0]
    assert attempt["destination_kind"] in {"business_hours", "after_hours"}
    assert _OFFICE not in response.text and _ON_CALL not in response.text, "numbers are masked"
    assert body["follow_up"]["reason"] == "ended_before_request_recorded"
    assert body["follow_up"]["resolved_at"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_callbacks_are_listed_resolved_and_kept_per_tenant(client):
    token, organization_id = await _register(client, "Follow-up List A")
    other_token, other_id = await _register(client, "Follow-up List B")
    assistant = await _tenant(organization_id=organization_id)
    other_assistant = await _tenant(organization_id=other_id)
    _Holder.provider = _Down()
    call_id = f"call_fu_{uuid.uuid4().hex[:8]}"
    await _turn(client, _body(call_id, assistant, "Is anyone there?", control=None))
    await _turn(client, _body(f"call_fu_{uuid.uuid4().hex[:8]}", other_assistant, "Hello?",
                              control=None))

    listed = await client.get("/api/v1/voice/follow-ups", headers=_auth(token))
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
    item = listed.json()[0]
    assert item["reason"] == "ai_unavailable" and item["caller_number"] == _CALLER

    assert (await client.post(f"/api/v1/voice/follow-ups/{item['id']}/resolve",
                              headers=_auth(other_token))).status_code == 404
    resolved = await client.post(f"/api/v1/voice/follow-ups/{item['id']}/resolve",
                                 headers=_auth(token))
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolved_at"] is not None

    assert (await client.get("/api/v1/voice/follow-ups", headers=_auth(token))).json() == []
    everything = await client.get("/api/v1/voice/follow-ups?include_resolved=true",
                                  headers=_auth(token))
    assert [i["id"] for i in everything.json()] == [item["id"]]
    assert (await client.get("/api/v1/voice/follow-ups")).status_code == 401
