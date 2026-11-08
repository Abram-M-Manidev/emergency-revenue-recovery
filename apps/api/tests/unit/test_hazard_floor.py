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
