"""Audit B5: a reported life-safety hazard floors the classification.

The model decides urgency, but it may only raise it. When the caller's own
words describe gas, carbon monoxide, smoke, sparking or flooding (fixed
patterns, `app/domain/life_safety.py`), whatever the model says, the call is
recorded as an emergency — in the tool and in the turn's outcome."""

from __future__ import annotations

import pytest

from app.domain.ai.tools import CREATE_SERVICE_REQUEST, ToolInvocation
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from tests.fakes import FakeAIProvider, default_reply
from tests.unit.test_contact_integrity import _CALLER_ID, _ORG_ID, _Harness, _request


@pytest.mark.asyncio
async def test_the_tool_records_a_hazard_call_as_an_emergency_whatever_the_model_says():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    executor = harness.factory.bind(_ORG_ID, conversation, 0, hazard_reported=True)

    result = await executor.execute(
        ToolInvocation(
            id="t",
            name=CREATE_SERVICE_REQUEST.name,
            arguments=_request(classification="non_emergency"),
        )
    )

    assert result.content["success"] is True
    assert result.content["service_request_type"] == "emergency_ticket"
    assert "safety hazard" in result.content["classification_overridden"]
    assert await harness.tickets.get_by_conversation_id(conversation) is not None
    assert await harness.appointments.get_by_conversation_id(conversation) is None


@pytest.mark.asyncio
async def test_without_a_hazard_the_models_classification_stands():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    executor = harness.factory.bind(_ORG_ID, conversation, 0)

    result = await executor.execute(
        ToolInvocation(id="t", name=CREATE_SERVICE_REQUEST.name, arguments=_request())
    )

    assert result.content["service_request_type"] == "appointment"
    assert "classification_overridden" not in result.content


@pytest.mark.asyncio
async def test_the_turn_outcome_is_floored_even_when_no_tool_ran():
    """The webhook's outcome sync then opens the ticket and its alert."""
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    provider = FakeAIProvider()
    provider.queue_reply(
        default_reply(
            classification=CallClassification.NON_EMERGENCY,
            recommended_action=RecommendedAction.BOOK_APPOINTMENT,
        )
    )

    await harness.brain(provider).send_message(
        _ORG_ID, conversation, "I smell gas near the furnace."
    )

    outcome = await harness.outcomes.get_by_conversation_id(conversation)
    assert outcome is not None
    assert outcome.classification is CallClassification.EMERGENCY
    assert outcome.recommended_action is RecommendedAction.CREATE_EMERGENCY_TICKET
    # The ticket a dispatcher reads says why, not the model's "Routine inquiry."
    assert outcome.summary.startswith("Caller reported a possible gas leak.")


@pytest.mark.asyncio
async def test_a_hazard_from_an_earlier_turn_still_floors_later_turns():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    provider = FakeAIProvider()
    brain = harness.brain(provider)
    provider.queue_reply(default_reply(classification=CallClassification.EMERGENCY))
    await brain.send_message(_ORG_ID, conversation, "There's smoke coming from the unit.")
    provider.queue_reply(default_reply(classification=CallClassification.NON_EMERGENCY))

    await brain.send_message(_ORG_ID, conversation, "My name is Dana.")

    outcome = await harness.outcomes.get_by_conversation_id(conversation)
    assert outcome is not None and outcome.classification is CallClassification.EMERGENCY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "words",
    ["My AC isn't cooling.", "The smoke detector battery is chirping."],
)
async def test_ordinary_calls_are_not_floored(words):
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    provider = FakeAIProvider()
    provider.queue_reply(default_reply(classification=CallClassification.NON_EMERGENCY))

    await harness.brain(provider).send_message(_ORG_ID, conversation, words)

    outcome = await harness.outcomes.get_by_conversation_id(conversation)
    assert outcome is not None and outcome.classification is CallClassification.NON_EMERGENCY


# --- Asserted vs. merely mentioned (pre-push audit, 2026-11-08) -------------
#
# The floor forces a ticket and an alert to a real person, so it needs the
# caller to report the hazard as happening. Mentions that are denied, past,
# hypothetical or asked about still get the spoken safety instruction and a
# confirming question — never silence — but not a forced emergency.


@pytest.mark.parametrize(
    ("words", "asserted"),
    [
        ("I smell gas.", {"gas"}),
        ("There is a carbon monoxide alarm.", {"carbon_monoxide"}),
        ("There is no gas leak.", set()),
        ("I previously had a gas leak.", set()),
        ("What should I do if I smell gas?", set()),
        # Someone else's hazard is still a danger to the caller: escalated.
        ("My neighbour reported smoke.", {"fire_smoke"}),
        ("There is water flooding my kitchen.", {"flooding"}),
        # Genuine reports that a naive negation/question rule would lose.
        ("I smell gas, what should I do?", {"gas"}),
        ("There is no smoke but I smell gas.", {"gas"}),
        ("I can't breathe there is smoke", {"fire_smoke"}),
        ("My neighbour's house is on fire and it is spreading to mine", {"fire_smoke"}),
        ("No idea why, water is pouring everywhere", {"flooding"}),
        ("My CO alarm is going off!", {"carbon_monoxide"}),
        # Qualified mentions.
        ("We had a gas leak last year.", set()),
        ("Could it be carbon monoxide?", set()),
        ("I don't smell gas anymore", set()),
        ("There isn't any smoke now", set()),
    ],
)
def test_only_hazards_reported_as_happening_are_asserted(words, asserted):
    from app.domain.life_safety import asserted_hazards, detect_hazards

    found = {h.value for h in asserted_hazards(words)}
    assert found == asserted
    # Never more than what was mentioned; the spoken instruction still uses
    # every mention.
    assert found <= {h.value for h in detect_hazards(words)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "words",
    ["There is no gas leak.", "I previously had a gas leak.", "What should I do if I smell gas?"],
)
async def test_a_qualified_mention_is_confirmed_not_forced(words):
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    provider = FakeAIProvider()
    provider.queue_reply(default_reply(classification=CallClassification.NON_EMERGENCY))

    result = await harness.brain(provider).send_message(_ORG_ID, conversation, words)

    outcome = await harness.outcomes.get_by_conversation_id(conversation)
    assert outcome is not None and outcome.classification is CallClassification.NON_EMERGENCY
    # Still told how to stay safe, and the model must confirm with a question.
    assert "Your safety comes first." in result.reply_message.content
    prompt = provider.requests[-1].system_prompt
    assert "may not mean it is happening now" in prompt
    assert "ask ONE short question to find out whether it is happening now" in prompt


@pytest.mark.asyncio
async def test_confirming_the_hazard_on_a_later_turn_escalates():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    provider = FakeAIProvider()
    brain = harness.brain(provider)
    provider.queue_reply(default_reply(classification=CallClassification.NON_EMERGENCY))
    await brain.send_message(_ORG_ID, conversation, "There's no gas leak, the heater is noisy.")
    provider.queue_reply(default_reply(classification=CallClassification.NON_EMERGENCY))

    await brain.send_message(_ORG_ID, conversation, "Actually now I smell gas.")

    outcome = await harness.outcomes.get_by_conversation_id(conversation)
    assert outcome is not None and outcome.classification is CallClassification.EMERGENCY
