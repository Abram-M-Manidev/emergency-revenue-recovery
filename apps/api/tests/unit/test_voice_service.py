"""Unit tests for VoiceService using in-memory fakes — no database, no real
LLM or Vapi call. `VoiceService` is built on top of a *real* `AIBrainService`
(wired with the same fakes `test_ai_brain_service.py` uses) rather than a
fake AI Brain, so these tests exercise the real hand-off between the two
services, not a mocked stand-in for it."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.application.services.ai_brain_service import AIBrainService
from app.application.services.voice_service import TranscriptSupersession, VoiceService
from app.domain.entities.conversation import ConversationChannel, ConversationStatus
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_call import VoiceCall
from app.domain.entities.voice_line import VoiceLine, VoiceProvider
from app.domain.exceptions import EntityNotFoundError, VoiceLineNotFoundError
from app.domain.repositories.voice_call_repository import VoiceCallRepository
from app.domain.repositories.voice_line_repository import VoiceLineRepository
from tests.fakes import (
    BlockingAIProvider,
    FakeAIProvider,
    FakeBusinessHoursRepository,
    FakeBusinessProfileRepository,
    FakeCallLock,
    FakeConversationOutcomeRepository,
    FakeConversationRepository,
    FakeEmergencyKeywordRepository,
    FakeFAQRepository,
    FakeServiceAreaRepository,
    FakeServiceRepository,
    default_reply,
)
from tests.log_capture import capture_events, names

_ORG_ID = uuid.uuid4()
_ASSISTANT_ID = "asst_test_1"
_PHONE_NUMBER_ID = "pn_test_1"


class FakeVoiceLineRepository(VoiceLineRepository):
    def __init__(self, lines: list[VoiceLine] | None = None) -> None:
        self._lines: dict[uuid.UUID, VoiceLine] = {line.id: line for line in (lines or [])}

    async def get_by_organization_id(self, organization_id):
        return next(
            (line for line in self._lines.values() if line.organization_id == organization_id),
            None,
        )

    async def get_by_vapi_assistant_id(self, assistant_id):
        return next(
            (line for line in self._lines.values() if line.vapi_assistant_id == assistant_id), None
        )

    async def get_by_vapi_phone_number_id(self, phone_number_id):
        return next(
            (line for line in self._lines.values() if line.vapi_phone_number_id == phone_number_id),
            None,
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
    *,
    ai_provider: FakeAIProvider | None = None,
    voice_lines: list[VoiceLine] | None = None,
    call_lock: FakeCallLock | None = None,
    supersession: TranscriptSupersession | None = None,
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
        # Fresh per test: the production default is a module-level registry,
        # which would leak sequence state between tests and make them
        # order-dependent.
        call_lock=call_lock or FakeCallLock(),
        supersession=supersession or TranscriptSupersession(),
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


# --- concurrent / interim transcript suppression ---------------------------
#
# Vapi issues a Custom-LLM request every time its transcription grows. A live
# call produced eight requests for three spoken sentences, five of them for a
# single sentence, with three overlapping an in-flight predecessor. Crucially
# the utterances were strict prefixes of one another and never identical, so
# equality-based dedupe caught none of them.
#
# `BlockingAIProvider` gates inside the provider so the interleaving is
# forced rather than timing-dependent.


async def _start(service, *, call_id: str, utterance: str):
    """Schedules a webhook turn and yields control so it reaches the lock."""
    task = asyncio.create_task(
        service.handle_chat_completion(
            vapi_call_id=call_id,
            assistant_id=_ASSISTANT_ID,
            phone_number_id=None,
            customer_number=None,
            customer_utterance=utterance,
        )
    )
    await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
async def test_two_concurrent_identical_transcripts_make_one_ai_call():
    """A. The plain retry case, but genuinely concurrent — the sequential
    dedupe alone could not see the first turn's messages because they were
    not committed yet."""
    provider = BlockingAIProvider()
    service, _, _, _, _ = _make_voice_service(
        ai_provider=provider, voice_lines=[_voice_line()]
    )

    first = await _start(service, call_id="call_c", utterance="My furnace died.")
    second = await _start(service, call_id="call_c", utterance="My furnace died.")
    await provider.entered.wait()
    provider.release()
    results = await asyncio.gather(first, second)

    assert len(provider.requests) == 1
    assert results[0].reply_text == results[1].reply_text


@pytest.mark.asyncio
async def test_five_concurrent_growing_transcripts_collapse_to_the_latest():
    """B. The exact shape of the observed failure: five overlapping requests
    whose transcripts grow.

    The reachable guarantee is "everything overtaken while queued is
    dropped", not "only one request ever generates". A request that is still
    the newest when it passes the check has no way to know a longer
    transcript is coming, so it must be answered — the future is not
    knowable. What the burst must collapse to is the already-in-flight
    request plus the final transcript, never the five in between."""
    provider = BlockingAIProvider()
    service, _, _, _, _ = _make_voice_service(
        ai_provider=provider, voice_lines=[_voice_line()]
    )
    # Seed one completed turn so a cached reply exists, matching a real call
    # where the burst follows the greeting and an earlier answer.
    provider.release()
    await service.handle_chat_completion(
        vapi_call_id="call_c",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number=None,
        customer_utterance="Hello?",
    )
    calls_after_seed = len(provider.requests)
    provider.gate.clear()
    provider.entered.clear()

    growing = [
        "My name is Lucky.",
        "My name is Lucky. My number is one,",
        "My name is Lucky. My number is one, two, three.",
        "My name is Lucky. My number is one, two, three. My address",
        "My name is Lucky. My number is one, two, three. My address is 16 Street.",
    ]
    tasks = [await _start(service, call_id="call_c", utterance=u) for u in growing]
    await provider.entered.wait()
    provider.release()
    await asyncio.gather(*tasks)

    new_requests = provider.requests[calls_after_seed:]
    # Five overlapping requests collapse to two: the one already generating
    # when the burst began, and the winner carrying the complete transcript.
    # The three in between never reach the model.
    assert len(new_requests) == 2, f"expected 2 AI calls for the burst, got {len(new_requests)}"
    assert new_requests[0].latest_customer_message == growing[0]
    assert new_requests[-1].latest_customer_message == growing[-1], (
        "the final answer must come from the complete transcript"
    )


@pytest.mark.asyncio
async def test_legitimate_second_turn_is_not_suppressed():
    """C. Supersession must never outlive the burst that caused it."""
    provider = FakeAIProvider()
    service, _, _, _, _ = _make_voice_service(
        ai_provider=provider, voice_lines=[_voice_line()]
    )

    for utterance in ("My furnace died.", "It is making a loud noise.", "Please hurry."):
        await service.handle_chat_completion(
            vapi_call_id="call_c",
            assistant_id=_ASSISTANT_ID,
            phone_number_id=None,
            customer_number=None,
            customer_utterance=utterance,
        )

    assert len(provider.requests) == 3
    assert [r.latest_customer_message for r in provider.requests] == [
        "My furnace died.",
        "It is making a loud noise.",
        "Please hurry.",
    ]


@pytest.mark.asyncio
async def test_failed_turn_releases_the_lock_for_the_next_request():
    """D. A provider failure must not wedge the rest of a live call."""
    provider = BlockingAIProvider()
    provider.fail_with = RuntimeError("provider exploded")
    service, _, _, _, _ = _make_voice_service(
        ai_provider=provider, voice_lines=[_voice_line()]
    )

    provider.release()
    with pytest.raises(RuntimeError):
        await service.handle_chat_completion(
            vapi_call_id="call_c",
            assistant_id=_ASSISTANT_ID,
            phone_number_id=None,
            customer_number=None,
            customer_utterance="My furnace died.",
        )

    provider.fail_with = None
    result = await service.handle_chat_completion(
        vapi_call_id="call_c",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number=None,
        customer_utterance="Are you there?",
    )
    assert result.reply_text


@pytest.mark.asyncio
async def test_cancellation_releases_the_lock():
    """E. Vapi hanging up mid-turn cancels the request task."""
    provider = BlockingAIProvider()
    lock = FakeCallLock()
    service, _, _, _, _ = _make_voice_service(
        ai_provider=provider, voice_lines=[_voice_line()], call_lock=lock
    )

    doomed = await _start(service, call_id="call_c", utterance="My furnace died.")
    await provider.entered.wait()
    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed

    provider.gate.set()
    result = await asyncio.wait_for(
        service.handle_chat_completion(
            vapi_call_id="call_c",
            assistant_id=_ASSISTANT_ID,
            phone_number_id=None,
            customer_number=None,
            customer_utterance="Hello?",
        ),
        timeout=5,
    )
    assert result.reply_text


@pytest.mark.asyncio
async def test_concurrent_requests_never_overlap_and_never_deadlock():
    """F. The property the production advisory lock exists to guarantee."""
    provider = FakeAIProvider()
    lock = FakeCallLock()
    service, _, _, _, _ = _make_voice_service(
        ai_provider=provider, voice_lines=[_voice_line()], call_lock=lock
    )

    tasks = [
        await _start(service, call_id="call_c", utterance=f"utterance {i}") for i in range(6)
    ]
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

    assert lock.max_concurrent == 1
    assert lock.acquisitions == 6


@pytest.mark.asyncio
async def test_different_calls_are_not_serialised_against_each_other():
    """G. One caller must never be blocked behind another caller's turn."""
    provider = BlockingAIProvider()
    lock = FakeCallLock()
    service, _, _, _, _ = _make_voice_service(
        ai_provider=provider, voice_lines=[_voice_line()], call_lock=lock
    )

    a = await _start(service, call_id="call_a", utterance="Caller A emergency.")
    b = await _start(service, call_id="call_b", utterance="Caller B emergency.")
    await provider.entered.wait()
    provider.release()
    await asyncio.wait_for(asyncio.gather(a, b), timeout=5)

    # Both reached the model: neither was treated as the other's duplicate.
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_supersession_does_not_leak_across_tenants():
    """H. Two organisations, two calls — resolution stays per voice line."""
    other_org = uuid.uuid4()
    lines = [
        _voice_line(vapi_assistant_id="asst_org_a"),
        _voice_line(
            id=uuid.uuid4(),
            organization_id=other_org,
            vapi_assistant_id="asst_org_b",
            vapi_phone_number_id="pn_org_b",
        ),
    ]
    provider = FakeAIProvider()
    service, _, _, voice_call_repo, _ = _make_voice_service(
        ai_provider=provider, voice_lines=lines
    )

    a = await service.handle_chat_completion(
        vapi_call_id="call_org_a",
        assistant_id="asst_org_a",
        phone_number_id=None,
        customer_number=None,
        customer_utterance="Org A emergency.",
    )
    b = await service.handle_chat_completion(
        vapi_call_id="call_org_b",
        assistant_id="asst_org_b",
        phone_number_id=None,
        customer_number=None,
        customer_utterance="Org B emergency.",
    )

    assert a.organization_id == _ORG_ID
    assert b.organization_id == other_org
    assert a.conversation_id != b.conversation_id
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_sequential_exact_repeat_still_returns_cached_reply():
    """I. The pre-existing retry protection must survive unchanged."""
    provider = FakeAIProvider()
    service, _, _, _, _ = _make_voice_service(
        ai_provider=provider, voice_lines=[_voice_line()]
    )

    first = await service.handle_chat_completion(
        vapi_call_id="call_c",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number=None,
        customer_utterance="My furnace died.",
    )
    second = await service.handle_chat_completion(
        vapi_call_id="call_c",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number=None,
        customer_utterance="My furnace died.",
    )

    assert len(provider.requests) == 1
    assert second.reply_text == first.reply_text


@pytest.mark.asyncio
async def test_superseded_burst_preserves_emergency_outcome():
    """J. Suppression must not cost the emergency classification that the
    winning transcript produced."""
    provider = BlockingAIProvider()
    service, _, conversation_repo, _, _ = _make_voice_service(
        ai_provider=provider, voice_lines=[_voice_line()]
    )
    provider.release()
    await service.handle_chat_completion(
        vapi_call_id="call_c",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number=None,
        customer_utterance="Hello?",
    )
    provider.gate.clear()
    provider.entered.clear()
    # Both the in-flight request and the winner reach the model (see test B),
    # so script an emergency verdict for each — the winner's is the one that
    # must survive as the persisted outcome.
    for _ in range(2):
        provider.queue_reply(
            default_reply(
                message_to_customer="Help is on the way.",
                classification=CallClassification.EMERGENCY,
                recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
            )
        )

    tasks = [
        await _start(service, call_id="call_c", utterance=u)
        for u in ("My basement", "My basement is flooding!")
    ]
    await provider.entered.wait()
    provider.release()
    results = await asyncio.gather(*tasks)

    winner = results[-1]
    outcome = await service._ai_brain.get_outcome(_ORG_ID, winner.conversation_id)
    assert outcome is not None
    assert outcome.classification is CallClassification.EMERGENCY
    assert outcome.recommended_action is RecommendedAction.CREATE_EMERGENCY_TICKET


@pytest.mark.asyncio
async def test_supersession_and_turn_start_are_observable():
    """H2: P1's suppression decision has to be visible. Without it, the only
    evidence a request was dropped is the *absence* of a turn in the
    database — which is exactly the archaeology H2 exists to remove."""
    provider = BlockingAIProvider()
    service, _, _, _, _ = _make_voice_service(
        ai_provider=provider, voice_lines=[_voice_line()]
    )

    # A first, uncontended turn so a cached reply exists to fall back on —
    # the superseded branch requires one.
    provider.release()
    await service.handle_chat_completion(
        vapi_call_id="call_obs",
        assistant_id=_ASSISTANT_ID,
        phone_number_id=None,
        customer_number=None,
        customer_utterance="Hello?",
    )
    provider.gate.clear()
    provider.entered.clear()
    for _ in range(3):
        provider.queue_reply(default_reply(message_to_customer="Help is on the way."))

    with capture_events() as entries:
        # Three, not two. With two, the first is already inside the model
        # (its `is_current` check having passed) and the second is current
        # by the time it takes the lock — neither is ever superseded. The
        # third is what makes the middle request stale while it waits.
        tasks = [
            await _start(service, call_id="call_obs", utterance=utterance)
            for utterance in (
                "My basement",
                "My basement is",
                "My basement is flooding!",
            )
        ]
        await provider.entered.wait()
        provider.release()
        await asyncio.gather(*tasks)

    emitted = names(entries)
    assert "vapi_chat_completion_superseded" in emitted
    assert "voice_turn_started" in emitted
    assert "voice_line_resolved" in emitted

    superseded = [e for e in entries if e.get("event") == "vapi_chat_completion_superseded"]
    assert superseded[0]["vapi_call_id"] == "call_obs"
    # The sequence number is what makes "which request lost" answerable.
    assert superseded[0]["sequence"] >= 1

    resolved = [e for e in entries if e.get("event") == "voice_line_resolved"]
    assert resolved[0]["matched_on"] == "assistant_id"
