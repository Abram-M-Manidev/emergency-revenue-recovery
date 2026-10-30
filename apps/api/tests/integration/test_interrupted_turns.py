"""Turns the caller never heard (P1, from the 2026-10-30 test call).

On that call Vapi dropped two of ERRS's streamed replies because the caller
was still talking. ERRS finished both turns anyway, which is right for
tickets but treated the replies as heard:
- the caller notice was recorded as given, so it was never spoken;
- the dropped replies went into the model's history;
- a dropped "you don't want anything, goodbye" completed the conversation,
  so the caller's next words got "this call has already been completed".

Reproduced through the real ASGI app with a genuine `http.disconnect` after
the first spoken words (the way uvicorn delivers Vapi dropping a request),
against real Postgres. Only the model is scripted.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_ai_provider, verify_vapi_secret
from app.domain.entities.conversation import ConversationStatus
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_line import VoiceProvider
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.conversation import ConversationModel
from app.infrastructure.database.models.conversation_message import ConversationMessageModel
from app.infrastructure.database.models.emergency_ticket import EmergencyTicketModel
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app, fastapi_app
from tests.fakes import ScriptedToolAIProvider, default_reply

_SECRET = "test-vapi-secret-interrupted"
_PATH = "/api/v1/voice/vapi/chat/completions"
_CALLER = "+16305550133"  # 555-01xx: reserved for fiction.
_FIRST_MESSAGE = "Thanks for calling our service line. Tell me what's happening."
_NOTICE = "automated assistant"


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
    assistant = f"asst_int_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(OrganizationModel(id=org_id, name=f"Interrupt {org_id.hex[:6]}",
                                      slug=f"it-{org_id.hex[:10]}"))
        await session.flush()
        session.add(VoiceLineModel(organization_id=org_id, provider=VoiceProvider.VAPI,
                                   vapi_assistant_id=assistant, is_active=True))
        await session.commit()
    return assistant


def _body(call_id: str, assistant: str, messages: list[dict]) -> bytes:
    return json.dumps({
        "call": {"id": call_id, "assistantId": assistant, "customer": {"number": _CALLER}},
        "messages": messages,
        "stream": True,
    }).encode()


def _transcript(*turns: tuple[str, str]) -> list[dict]:
    """Vapi's own transcript: its first message, then (role, text) pairs."""
    return [{"role": "assistant", "content": _FIRST_MESSAGE}] + [
        {"role": role, "content": text} for role, text in turns
    ]


async def _dropped(call_id: str, assistant: str, messages: list[dict]) -> None:
    """One streamed turn that Vapi abandons as soon as the first spoken words
    have been sent, as it does when the caller keeps talking."""
    body = _body(call_id, assistant, messages)
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
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": _PATH, "raw_path": _PATH.encode(), "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"x-vapi-secret", _SECRET.encode()),
                    (b"content-length", str(len(body)).encode())],
        "client": ("127.0.0.1", 5555), "server": ("test", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), 30)
    # The drained turn commits after the disconnect; let it land.
    await asyncio.sleep(0.5)


async def _heard(call_id: str, assistant: str, messages: list[dict]) -> str:
    """One streamed turn Vapi takes in full. Returns the spoken text."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_PATH, content=_body(call_id, assistant, messages),
                                     headers={"x-vapi-secret": _SECRET,
                                              "content-type": "application/json"})
    assert response.status_code == 200, response.text
    spoken = []
    for line in response.text.splitlines():
        if line.startswith("data: {"):
            delta = json.loads(line[6:])["choices"][0]["delta"]
            spoken.append(delta.get("content") or "")
    return "".join(spoken)


async def _state(call_id: str):
    async with AsyncSessionLocal() as session:
        call = (await session.execute(
            select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == call_id))).scalar_one()
        conversation = await session.get(ConversationModel, call.conversation_id)
        messages = (await session.execute(
            select(ConversationMessageModel)
            .where(ConversationMessageModel.conversation_id == call.conversation_id)
            .order_by(ConversationMessageModel.created_at))).scalars().all()
        return call, conversation, list(messages)


# The caller's utterances exactly as Vapi sent them on 2026-10-30 (call
# 01a18e07…, read from the ngrok inspector), in the order ERRS received them.
# Vapi's two other requests ("Hello. I'm making a test call." and "This is
# test call. I assist outgoing today.") were cancelled mid-upload and never
# reached a turn.
_T1 = "Hello. I'm making a test call. You know, not really emergency."
_T2 = _T1 + " So my air conditioner stopped cooling. headliner and with service appointment."
_T3 = "This is test call. I assist outgoing today. You know, on air test, and I don't want them."
_T4 = _T3 + " Actually, technician dispatched."
_R1 = ("Hello! I understand this is a test call. If you have any questions or need "
       "assistance with HVAC services, feel free to ask.")
_R2 = "I can help you schedule a service appointment. What is your full name?"
_R3 = ("I understand this is a test call and you do not want assistance or a service "
       "appointment. If you need help in the future, please feel free to call back.")


def _history(provider: ScriptedToolAIProvider) -> list[tuple[str, str]]:
    return [(turn.role, turn.content) for turn in provider.requests[-1].history]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_2026_10_30_test_call_now_ends_the_way_the_caller_heard_it(scripted):
    assistant = await _line()
    call_id = f"call_int_{uuid.uuid4().hex[:8]}"

    # Turn 1: answered, but Vapi drops it while the caller keeps talking.
    scripted.queue_reply(default_reply(message_to_customer=_R1))
    await _dropped(call_id, assistant, _transcript(("user", _T1)))

    call, conversation, messages = await _state(call_id)
    assert call.disclosure_sent_at is None, "the notice was recorded although never heard"
    assert [m.interrupted_at is not None for m in messages] == [True, True]

    # Turn 2: Vapi re-sends the caller's words, grown. Heard in full. The
    # model sees neither the undelivered reply nor a second copy of the
    # caller's opening words: they are inside this utterance already.
    scripted.queue_reply(default_reply(message_to_customer=_R2))
    spoken = await _heard(call_id, assistant, _transcript(("user", _T2)))
    assert _NOTICE in spoken, "the notice must lead the first reply the caller hears"
    assert _history(scripted) == []
    assert scripted.requests[-1].latest_customer_message == _T2
    call, _, _ = await _state(call_id)
    assert call.disclosure_sent_at is not None

    # Turn 3: the model understands and completes the call; Vapi drops it.
    scripted.queue_reply(default_reply(message_to_customer=_R3, is_conversation_complete=True))
    await _dropped(call_id, assistant, _transcript(("user", _T2), ("assistant", _R2),
                                                   ("user", _T3)))
    _, conversation, messages = await _state(call_id)
    assert conversation.status == ConversationStatus.ACTIVE, "an unheard completion ended the call"
    assert conversation.ended_at is None
    assert [m.interrupted_at is not None for m in messages[-2:]] == [True, True]

    # Turn 4: the caller's later words reach the model instead of
    # "this call has already been completed", with the history the caller
    # actually lived: their turn-2 words and the reply they heard. Turn 3's
    # words come once, inside this utterance; its reply not at all.
    requests_before = len(scripted.requests)
    scripted.queue_reply(default_reply(message_to_customer="Of course, let me arrange that."))
    spoken = await _heard(call_id, assistant, _transcript(("user", _T2), ("assistant", _R2),
                                                          ("user", _T4)))
    assert "already been completed" not in spoken
    assert "Of course, let me arrange that." in spoken
    assert len(scripted.requests) == requests_before + 1
    assert _history(scripted) == [("customer", _T2), ("assistant", _R2)]
    assert scripted.requests[-1].latest_customer_message == _T4
    assert _NOTICE not in spoken, "the notice is given once, not again"

    # Storage keeps the full record: all four caller messages, all four
    # replies, the two undelivered ones marked.
    _, _, messages = await _state(call_id)
    assert [m.content for m in messages if m.role.value == "customer"] == [_T1, _T2, _T3, _T4]
    assert [m.interrupted_at is not None for m in messages] == [
        True, True, False, False, True, True, False, False]


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unanswered_caller_message_that_is_not_resent_stays_in_history(scripted):
    """When Vapi does NOT re-send the caller's words (they hung up and called
    nothing more, or Vapi sent the next utterance as a new message), those
    words must not vanish from the model's view: only the undelivered reply
    goes."""
    assistant = await _line()
    call_id = f"call_int_{uuid.uuid4().hex[:8]}"
    banging = "My furnace is making a loud banging noise."
    scripted.queue_reply(default_reply(message_to_customer="That sounds worth a look."))
    await _dropped(call_id, assistant, _transcript(("user", banging)))

    scripted.queue_reply(default_reply(message_to_customer="Thanks, noted."))
    await _heard(call_id, assistant, _transcript(("user", banging),
                                                 ("user", "Also the thermostat screen is blank.")))
    assert _history(scripted) == [("customer", banging)]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_retry_with_identical_words_is_not_stored_twice(scripted):
    """Vapi retrying the same transcript is answered from the stored turn:
    the model is not asked again and no second copy of the caller's words
    is written."""
    assistant = await _line()
    call_id = f"call_int_{uuid.uuid4().hex[:8]}"
    words = "My water heater is leaking."
    scripted.queue_reply(default_reply(message_to_customer="I can help with that leak."))
    await _heard(call_id, assistant, _transcript(("user", words)))
    requests_before = len(scripted.requests)

    spoken = await _heard(call_id, assistant, _transcript(("user", words)))

    assert "I can help with that leak." in spoken
    assert len(scripted.requests) == requests_before
    _, _, messages = await _state(call_id)
    assert [m.content for m in messages] == [words, "I can help with that leak."]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_completion_the_caller_heard_still_ends_the_call(scripted):
    assistant = await _line()
    call_id = f"call_int_{uuid.uuid4().hex[:8]}"
    scripted.queue_reply(default_reply(message_to_customer="Thanks for calling. Goodbye.",
                                       is_conversation_complete=True))
    await _heard(call_id, assistant, _transcript(("user", "That's all, thanks.")))

    _, conversation, messages = await _state(call_id)
    assert conversation.status == ConversationStatus.COMPLETED
    assert all(m.interrupted_at is None for m in messages)
    spoken = await _heard(call_id, assistant, _transcript(("user", "That's all, thanks. Bye.")))
    assert "already been completed" in spoken


@pytest.mark.asyncio(loop_scope="session")
async def test_an_interrupted_turn_keeps_its_emergency_ticket(scripted):
    assistant = await _line()
    call_id = f"call_int_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round(
        [("create_service_request", {
            "customer_phone": "6305550133", "problem_description": "Sparks from the furnace.",
            "classification": "emergency", "service_name": None})],
        speak="I'm logging this emergency right now.",
    )
    scripted.queue_reply(default_reply(message_to_customer="Your emergency is logged.",
                                       classification=CallClassification.EMERGENCY,
                                       recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET))
    await _dropped(call_id, assistant, _transcript(("user", "Sparks from my furnace!")))

    call, _, messages = await _state(call_id)
    assert [m.interrupted_at is not None for m in messages] == [True, True]
    async with AsyncSessionLocal() as session:
        tickets = (await session.execute(select(EmergencyTicketModel).where(
            EmergencyTicketModel.conversation_id == call.conversation_id))).scalars().all()
    assert len(tickets) == 1, "the ticket is a real event and must survive the dropped reply"


@pytest.mark.asyncio(loop_scope="session")
async def test_everything_the_model_said_across_tool_rounds_is_stored(scripted):
    assistant = await _line()
    call_id = f"call_int_{uuid.uuid4().hex[:8]}"
    scripted.queue_tool_round(
        [("create_service_request", {
            "customer_phone": "6305550133", "problem_description": "No heat at all.",
            "classification": "emergency", "service_name": None})],
        speak="Let me log that for you.",
    )
    scripted.queue_reply(default_reply(message_to_customer="It is logged for the team.",
                                       classification=CallClassification.EMERGENCY,
                                       recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET))
    await _heard(call_id, assistant, _transcript(("user", "My heating is out.")))

    _, _, messages = await _state(call_id)
    assert messages[-1].content == "Let me log that for you. It is logged for the team."


@pytest.mark.asyncio(loop_scope="session")
async def test_an_identical_retry_replays_the_unheard_reply_and_marks_it_heard(scripted):
    assistant = await _line()
    call_id = f"call_int_{uuid.uuid4().hex[:8]}"
    utterance = "I need someone to look at my boiler."
    scripted.queue_reply(default_reply(message_to_customer="I can help with the boiler."))
    await _dropped(call_id, assistant, _transcript(("user", utterance)))

    requests_before = len(scripted.requests)
    spoken = await _heard(call_id, assistant, _transcript(("user", utterance)))
    assert "I can help with the boiler." in spoken
    assert _NOTICE in spoken
    assert len(scripted.requests) == requests_before, "a replay must not run the model again"
    _, _, messages = await _state(call_id)
    assert all(m.interrupted_at is None for m in messages)


@pytest.mark.asyncio(loop_scope="session")
async def test_unheard_offers_are_kept_but_can_never_be_selected_or_booked(database_ready):
    from datetime import datetime, timedelta, timezone

    from app.domain.entities.availability import AvailabilitySlot
    from app.domain.entities.conversation import ConversationChannel
    from app.domain.entities.conversation_message import MessageRole
    from app.infrastructure.database.models.offered_slot import OfferedSlotModel
    from app.infrastructure.database.repositories.conversation_repository_impl import (
        SqlAlchemyConversationRepository,
    )
    from app.infrastructure.database.repositories.offered_slot_repository_impl import (
        SqlAlchemyOfferedSlotRepository,
    )

    org_id = uuid.uuid4()
    base = datetime(2030, 1, 7, 9, tzinfo=timezone.utc)
    nine, eleven, two = base, base + timedelta(hours=2), base + timedelta(hours=5)
    slot = lambda start: AvailabilitySlot(start_at=start, duration_minutes=60)  # noqa: E731
    async with AsyncSessionLocal() as session:
        session.add(OrganizationModel(id=org_id, name=f"Offers {org_id.hex[:6]}",
                                      slug=f"of-{org_id.hex[:10]}"))
        await session.flush()
        conversations = SqlAlchemyConversationRepository(session)
        conversation = await conversations.create(
            organization_id=org_id, channel=ConversationChannel.VOICE, caller_phone_number=None
        )
        reply = await conversations.add_message(conversation.id, role=MessageRole.ASSISTANT,
                                                content="I have 9 or 11 tomorrow.")
        offers = SqlAlchemyOfferedSlotRepository(session)
        await offers.record_offered(org_id, conversation.id, [slot(nine), slot(eleven)], turn_index=4)
        await offers.record_offered(org_id, conversation.id, [slot(two)], turn_index=2)
        await offers.mark_selected(org_id, conversation.id, eleven, turn_index=6)

        # Turn 4's reply was never delivered.
        assert await offers.withdraw_unheard_offers(
            org_id, conversation.id, 4, interrupted_message_id=reply.id) == 1
        # Idempotent: already withdrawn, nothing more to do.
        assert await offers.withdraw_unheard_offers(
            org_id, conversation.id, 4, interrupted_message_id=reply.id) == 0

        # Every enforcement read ignores it, so it cannot be selected or booked.
        assert await offers.list_offered_starts(org_id, conversation.id) == [eleven, two]
        assert await offers.get_offered(org_id, conversation.id, nine) is None
        assert await offers.offered_duration_minutes(org_id, conversation.id, nine) is None
        assert await offers.mark_selected(org_id, conversation.id, nine, turn_index=8) is None
        # A refused selection must not have cleared the real one.
        selection = await offers.get_active_selection(org_id, conversation.id)
        assert selection is not None and selection.start_at == eleven

        # ...but the row is still there, linked to the undelivered reply.
        audit = (await session.execute(select(OfferedSlotModel).where(
            OfferedSlotModel.conversation_id == conversation.id,
            OfferedSlotModel.slot_start_at == nine))).scalars().all()
        assert len(audit) == 1
        assert audit[0].withdrawn_at is not None and audit[0].withdrawn_message_id == reply.id

        # The same time read out again on a heard turn is a new, active offer
        # that can be chosen, and the withdrawn record stays beside it.
        await offers.record_offered(org_id, conversation.id, [slot(nine)], turn_index=8)
        offered = await offers.get_offered(org_id, conversation.id, nine)
        assert offered is not None and offered.offered_turn_index == 8
        assert await offers.mark_selected(org_id, conversation.id, nine, turn_index=10) is not None
        rows = (await session.execute(select(OfferedSlotModel).where(
            OfferedSlotModel.conversation_id == conversation.id,
            OfferedSlotModel.slot_start_at == nine))).scalars().all()
        assert sorted(r.withdrawn_at is None for r in rows) == [False, True]
        await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_a_safety_instruction_on_a_dropped_turn_is_given_on_the_next_heard_one(scripted):
    """The instruction is spoken once per call. On a dropped turn it was not
    heard, so it is still owed, even when the caller's next message does not
    repeat the hazard."""
    assistant = await _line()
    call_id = f"call_int_{uuid.uuid4().hex[:8]}"
    gas = "I can smell gas in my kitchen."
    scripted.queue_reply(default_reply(message_to_customer="I'm logging that now."))
    await _dropped(call_id, assistant, _transcript(("user", gas)))

    scripted.queue_reply(default_reply(message_to_customer="Someone will be in touch."))
    spoken = await _heard(call_id, assistant, _transcript(("user", gas),
                                                          ("user", "Can someone come today?")))
    assert "leave the building now" in spoken

    # Heard once: not repeated on the turn after.
    scripted.queue_reply(default_reply(message_to_customer="Anything else?"))
    spoken = await _heard(call_id, assistant, _transcript(("user", "Can someone come today?"),
                                                          ("assistant", "Someone will be in touch."),
                                                          ("user", "Thanks.")))
    assert "leave the building now" not in spoken
