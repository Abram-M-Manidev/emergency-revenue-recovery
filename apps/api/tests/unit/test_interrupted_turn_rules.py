"""`VoiceService.mark_turn_interrupted`'s rules, and the Vapi-transcript
divergence measurement, against in-memory fakes.

The end-to-end shape (a real dropped stream on real Postgres) is pinned in
`tests/integration/test_interrupted_turns.py`; this covers the edges."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.vapi_webhooks import _last_heard_reply
from app.application.schemas.voice import VapiChatCompletionRequest
from app.application.services.voice_service import (
    TurnReceipt,
    VoiceService,
    _check_heard_history,
    heard_similarity,
)
from app.domain.entities.conversation import ConversationChannel, ConversationStatus
from app.domain.entities.conversation_message import MessageRole
from app.domain.entities.offered_slot import OfferedSlot
from tests.fakes import FakeConversationRepository, FakeOfferedSlotRepository
from tests.log_capture import capture_events, names
from tests.unit.test_voice_service import FakeVoiceCallRepository

_ORG = uuid.uuid4()
_NOW = datetime(2026, 10, 30, 18, 17, tzinfo=timezone.utc)


class _World:
    def __init__(self) -> None:
        self.conversations = FakeConversationRepository()
        self.voice_calls = FakeVoiceCallRepository()
        self.offers = FakeOfferedSlotRepository()
        self.service = VoiceService(
            voice_line_repository=None,  # type: ignore[arg-type]
            voice_call_repository=self.voice_calls,
            conversation_repository=self.conversations,
            ai_brain_service=None,  # type: ignore[arg-type]
            offered_slot_repository=self.offers,
        )

    async def call(self):
        conversation = await self.conversations.create(
            organization_id=_ORG, channel=ConversationChannel.VOICE, caller_phone_number=None
        )
        voice_call = await self.voice_calls.create(
            organization_id=_ORG, conversation_id=conversation.id,
            vapi_call_id=f"call_{uuid.uuid4().hex[:6]}", caller_number=None,
        )
        customer = await self.conversations.add_message(
            conversation.id, role=MessageRole.CUSTOMER, content="Hello."
        )
        reply = await self.conversations.add_message(
            conversation.id, role=MessageRole.ASSISTANT, content="Hi there."
        )
        await self.voice_calls.mark_disclosure(voice_call.id, sent_at=_NOW, ai=True, recording=True)
        await self.conversations.complete(conversation.id)
        return conversation, voice_call, (customer.id, reply.id)

    def offer(self, conversation_id, start_hour: int, turn_index: int, *, selected=False):
        start = _NOW.replace(hour=start_hour) + timedelta(days=1)
        self.offers.offers[(_ORG, conversation_id, start)] = OfferedSlot(
            organization_id=_ORG, conversation_id=conversation_id, start_at=start,
            duration_minutes=60, offered_turn_index=turn_index,
            selected_at=_NOW if selected else None,
            selected_turn_index=turn_index + 2 if selected else None,
        )


def _receipt(conversation, voice_call, ids, **overrides) -> TurnReceipt:
    values = dict(organization_id=_ORG, conversation_id=conversation.id,
                  voice_call_id=voice_call.id, message_ids=ids, turn_index=2,
                  disclosure_given=True, completed_conversation=True)
    values.update(overrides)
    return TurnReceipt(**values)


@pytest.mark.asyncio
async def test_an_interrupted_turn_undoes_only_what_needed_hearing():
    world = _World()
    conversation, voice_call, ids = await world.call()
    world.offer(conversation.id, 9, turn_index=2)
    world.offer(conversation.id, 11, turn_index=2, selected=True)
    world.offer(conversation.id, 14, turn_index=0)

    with capture_events() as events:
        await world.service.mark_turn_interrupted(_receipt(conversation, voice_call, ids))

    messages = await world.conversations.list_messages(conversation.id)
    assert all(m.interrupted_at is not None for m in messages)
    stored = await world.voice_calls.get_by_conversation_id(conversation.id)
    assert stored is not None and stored.disclosure_sent_at is None
    reopened = await world.conversations.get_by_id(_ORG, conversation.id)
    assert reopened is not None and reopened.status is ConversationStatus.ACTIVE
    # Only the unselected offer from the unheard turn is withdrawn, and it is
    # kept for the record, linked to the reply that was not delivered.
    assert sorted(o.start_at.hour for o in world.offers.offers.values()) == [11, 14]
    [(withdrawn, message_id)] = world.offers.withdrawn
    assert withdrawn.start_at.hour == 9 and withdrawn.withdrawn_at is not None
    assert message_id == ids[1]
    event = next(e for e in events if e["event"] == "voice_turn_interrupted")
    assert event["offers_withdrawn"] == 1 and event["completion_reopened"] is True


@pytest.mark.asyncio
async def test_a_call_that_has_ended_is_not_reopened():
    world = _World()
    conversation, voice_call, ids = await world.call()
    await world.voice_calls.mark_ended(voice_call.vapi_call_id, ended_reason="customer-ended-call",
                                       duration_seconds=40, recording_url=None)

    await world.service.mark_turn_interrupted(_receipt(conversation, voice_call, ids))

    stored = await world.conversations.get_by_id(_ORG, conversation.id)
    assert stored is not None and stored.status is ConversationStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_turn_that_gave_no_notice_leaves_the_recorded_notice_alone():
    world = _World()
    conversation, voice_call, ids = await world.call()

    await world.service.mark_turn_interrupted(
        _receipt(conversation, voice_call, ids, disclosure_given=False, completed_conversation=False)
    )

    stored = await world.voice_calls.get_by_conversation_id(conversation.id)
    assert stored is not None and stored.disclosure_sent_at == _NOW
    unchanged = await world.conversations.get_by_id(_ORG, conversation.id)
    assert unchanged is not None and unchanged.status is ConversationStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_replay_is_settled_by_delivery_not_by_interruption():
    world = _World()
    conversation, voice_call, ids = await world.call()
    await world.conversations.mark_messages_interrupted(list(ids), at=_NOW)
    world.offer(conversation.id, 9, turn_index=2)
    replay = _receipt(conversation, voice_call, ids, replay=True, completed_conversation=False)

    # Dropped again: still unheard, and the earlier turn's offers are not
    # this replay's to withdraw.
    await world.service.mark_turn_interrupted(replay)
    assert all(m.interrupted_at == _NOW for m in await world.conversations.list_messages(conversation.id))
    assert len(world.offers.offers) == 1

    # Heard this time.
    await world.service.mark_turn_redelivered(replay)
    assert all(m.interrupted_at is None for m in await world.conversations.list_messages(conversation.id))


@pytest.mark.asyncio
async def test_a_failure_while_settling_is_logged_and_swallowed():
    world = _World()
    conversation, voice_call, ids = await world.call()

    async def broken(*args, **kwargs):
        raise RuntimeError("database is down")

    world.conversations.mark_messages_interrupted = broken  # type: ignore[method-assign]
    with capture_events() as events:
        await world.service.mark_turn_interrupted(_receipt(conversation, voice_call, ids))
    assert "voice_turn_interrupt_record_failed" in names(events)


# --- divergence measurement -----------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "heard", "matches"),
    [
        ("Your emergency is logged.", "Your emergency is logged.", True),
        # Vapi's copy also carries the notice ERRS speaks but does not store.
        ("What seems to be the problem?",
         "You're speaking with an automated assistant for us. What seems to be the problem?", True),
        ("Okay, I'll book that.", "okay  I'll book that", True),
        ("I understand this is a test call. Goodbye.", "Goodbye.", False),
        ("Your emergency is logged.", "Thanks for calling our service line.", False),
    ],
)
def test_heard_similarity(stored: str, heard: str, matches: bool):
    assert (heard_similarity(stored, heard) >= 0.9) is matches


@pytest.mark.asyncio
async def test_divergence_is_logged_with_lengths_only():
    conversations = FakeConversationRepository()
    conversation = await conversations.create(
        organization_id=_ORG, channel=ConversationChannel.VOICE, caller_phone_number=None
    )
    await conversations.add_message(conversation.id, role=MessageRole.CUSTOMER, content="Hi.")
    await conversations.add_message(conversation.id, role=MessageRole.ASSISTANT,
                                    content="Your private details are logged.")
    history = await conversations.list_messages(conversation.id)

    with capture_events() as events:
        _check_heard_history(history, "Something else entirely.")
        _check_heard_history(history, "Your private details are logged.")
        _check_heard_history(history, None)

    divergences = [e for e in events if e["event"] == "voice_history_divergence"]
    assert len(divergences) == 1
    assert "private" not in repr(divergences[0])
    assert set(divergences[0]) >= {"similarity", "stored_chars", "heard_chars"}


@pytest.mark.asyncio
async def test_an_interrupted_reply_is_not_what_divergence_compares_against():
    conversations = FakeConversationRepository()
    conversation = await conversations.create(
        organization_id=_ORG, channel=ConversationChannel.VOICE, caller_phone_number=None
    )
    await conversations.add_message(conversation.id, role=MessageRole.ASSISTANT, content="Heard reply.")
    unheard = await conversations.add_message(conversation.id, role=MessageRole.ASSISTANT,
                                              content="Never heard.")
    await conversations.mark_messages_interrupted([unheard.id], at=_NOW)

    with capture_events() as events:
        _check_heard_history(await conversations.list_messages(conversation.id), "Heard reply.")
    assert "voice_history_divergence" not in names(events)


def test_the_last_heard_reply_is_vapis_last_assistant_words_before_the_caller():
    payload = VapiChatCompletionRequest.model_validate({
        "call": {"id": "c1"},
        "messages": [
            {"role": "system", "content": "You are an assistant."},
            {"role": "assistant", "content": "Thanks for calling."},
            {"role": "user", "content": "Hi."},
            {"role": "assistant", "content": "How can I help?"},
            {"role": "tool", "content": "Success."},
            {"role": "user", "content": "My AC broke."},
        ],
    })
    assert _last_heard_reply(payload) == "How can I help?"
    first_turn = VapiChatCompletionRequest.model_validate({
        "call": {"id": "c1"},
        "messages": [{"role": "user", "content": "Hi."}],
    })
    assert _last_heard_reply(first_turn) is None


# --- model history: caller words kept, re-sends counted once ----------------------

from app.domain.turn_delivery import model_history, repeats  # noqa: E402

# Exactly as Vapi sent them on 2026-10-30 (see tests/integration/test_interrupted_turns.py).
_T1 = "Hello. I'm making a test call. You know, not really emergency."
_T2 = _T1 + " So my air conditioner stopped cooling. headliner and with service appointment."
_T3 = "This is test call. I assist outgoing today. You know, on air test, and I don't want them."
_T4 = _T3 + " Actually, technician dispatched."


async def _stored(*turns: tuple[str, str, bool]):
    """(caller words, reply, interrupted) per turn, stored the way ERRS does."""
    conversations = FakeConversationRepository()
    conversation = await conversations.create(
        organization_id=_ORG, channel=ConversationChannel.VOICE, caller_phone_number=None
    )
    for words, reply, interrupted in turns:
        pair = [
            await conversations.add_message(conversation.id, role=MessageRole.CUSTOMER, content=words),
            await conversations.add_message(conversation.id, role=MessageRole.ASSISTANT, content=reply),
        ]
        if interrupted:
            await conversations.mark_messages_interrupted([m.id for m in pair], at=_NOW)
    return await conversations.list_messages(conversation.id)


def _shape(messages):
    return [(m.role.value, m.content) for m in messages]


@pytest.mark.asyncio
async def test_the_real_test_call_sequence_reaches_the_model_once_and_without_unheard_replies():
    after_turn_1 = await _stored((_T1, "R1", True))
    assert model_history(after_turn_1, _T2) == []

    after_turn_3 = await _stored((_T1, "R1", True), (_T2, "R2", False), (_T3, "R3", True))
    assert _shape(model_history(after_turn_3, _T4)) == [("customer", _T2), ("assistant", "R2")]


@pytest.mark.asyncio
async def test_unanswered_words_that_are_not_resent_stay():
    history = await _stored(("My furnace is banging.", "R1", True))
    assert _shape(model_history(history, "Also the thermostat is blank.")) == [
        ("customer", "My furnace is banging.")]


@pytest.mark.asyncio
async def test_an_answered_message_is_never_dropped_even_if_repeated():
    """Only an interrupted message can be a re-send. A caller who repeats
    themselves after hearing a reply really did say it twice."""
    history = await _stored(("My AC is broken.", "Sorry to hear that.", False))
    assert _shape(model_history(history, "My AC is broken. It is really hot.")) == [
        ("customer", "My AC is broken."), ("assistant", "Sorry to hear that.")]


@pytest.mark.parametrize(
    ("earlier", "later", "expected"),
    [
        (_T1, _T2, True),
        (_T3, _T4, True),
        # The transcriber revised a word as the utterance grew.
        ("My air conditioner stop cooling", "My air conditioner stopped cooling and it is hot", True),
        ("My furnace is banging.", "Also the thermostat is blank.", False),
        ("Hello, I need a plumber for my kitchen sink.", "Hello.", False),
        ("", "anything", False),
    ],
)
def test_repeats(earlier: str, later: str, expected: bool):
    assert repeats(earlier, later) is expected


@pytest.mark.asyncio
async def test_a_once_only_instruction_stays_owed_until_a_reply_gets_through():
    from app.domain.life_safety import Hazard, detect_hazards
    from app.domain.turn_delivery import delivered_once_owed

    dropped = await _stored(("I smell gas.", "R1", True))
    assert delivered_once_owed(dropped, detect_hazards) == frozenset()

    heard_later = await _stored(("I smell gas.", "R1", True), ("Can someone come?", "R2", False))
    assert delivered_once_owed(heard_later, detect_hazards) == {Hazard.GAS}

    heard_at_once = await _stored(("I smell gas.", "R1", False))
    assert delivered_once_owed(heard_at_once, detect_hazards) == {Hazard.GAS}
