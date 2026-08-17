"""P5: known-caller grounding.

Caller ID is a lookup hint, never proof of identity — it is trivially
spoofable over SIP. Most of these tests are therefore about what the model
is *not* told: an ambiguous number grounds nothing, and a stored address is
never placed in the prompt at all, so no prompt-building mistake can recite
it to whoever spoofed the number.

The unknown-caller prompt is asserted byte-identical to the pre-P5 one, so
P5 cannot silently change behaviour for callers it does not recognise.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.application.services.ai_brain_service import AIBrainService
from app.domain.entities.conversation import ConversationChannel
from app.domain.entities.known_caller import KnownCaller
from tests.fakes import (
    FakeAIProvider,
    FakeBusinessHoursRepository,
    FakeBusinessProfileRepository,
    FakeCallerIdentityRepository,
    FakeConversationOutcomeRepository,
    FakeConversationRepository,
    FakeCustomerRepository,
    FakeEmergencyKeywordRepository,
    FakeFAQRepository,
    FakeServiceAreaRepository,
    FakeServiceRepository,
    default_reply,
)

_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()
_CALLER = "+919999999999"


def _make_brain():
    conversations = FakeConversationRepository()
    customers = FakeCustomerRepository()
    identities = FakeCallerIdentityRepository(customers)
    provider = FakeAIProvider()
    brain = AIBrainService(
        conversation_repository=conversations,
        conversation_outcome_repository=FakeConversationOutcomeRepository(),
        ai_provider=provider,
        business_profile_repository=FakeBusinessProfileRepository(),
        business_hours_repository=FakeBusinessHoursRepository(),
        service_repository=FakeServiceRepository(),
        service_area_repository=FakeServiceAreaRepository(),
        faq_repository=FakeFAQRepository(),
        emergency_keyword_repository=FakeEmergencyKeywordRepository(),
        settings=SimpleNamespace(AI_MAX_CONVERSATION_TURNS=20),
        caller_identity_repository=identities,
    )
    return brain, conversations, customers, identities, provider


async def _customer(customers, *, org=_ORG_ID, name="Lucky", phone="123456789", address=None):
    return await customers.create(
        organization_id=org, full_name=name, phone_number=phone, address=address
    )


async def _prompt_for(brain, conversations, provider, *, caller_number, org=_ORG_ID):
    conversation = await conversations.create(
        organization_id=org,
        channel=ConversationChannel.VOICE,
        caller_phone_number=caller_number,
    )
    provider.queue_reply(default_reply(message_to_customer="Understood."))
    await brain.send_message(org, conversation.id, "There is no heat.")
    return provider.requests[-1].system_prompt


# --- recognition ---


@pytest.mark.asyncio
async def test_known_caller_with_name_is_grounded():
    brain, conversations, customers, identities, provider = _make_brain()
    customer = await _customer(customers, address="16 Street, California")
    await identities.associate(_ORG_ID, customer_id=customer.id, caller_number=_CALLER)

    prompt = await _prompt_for(brain, conversations, provider, caller_number=_CALLER)

    assert "Caller records" in prompt
    assert "Lucky" in prompt
    assert "NOT proof of identity" in prompt


@pytest.mark.asyncio
async def test_unknown_caller_prompt_is_byte_identical_to_ungrounded():
    """P5 must be invisible to callers it does not recognise."""
    brain, conversations, customers, identities, provider = _make_brain()
    baseline = await _prompt_for(brain, conversations, provider, caller_number=None)
    unknown = await _prompt_for(brain, conversations, provider, caller_number="+15550001111")

    assert unknown == baseline
    assert "Caller records" not in unknown


@pytest.mark.asyncio
async def test_text_conversation_never_triggers_a_lookup():
    brain, conversations, customers, identities, provider = _make_brain()
    identities.fail_with = AssertionError("lookup must not happen without a caller ID")

    conversation = await conversations.create(
        organization_id=_ORG_ID, channel=ConversationChannel.TEXT, caller_phone_number=None
    )
    provider.queue_reply(default_reply(message_to_customer="Hello."))
    await brain.send_message(_ORG_ID, conversation.id, "What are your hours?")

    assert "Caller records" not in provider.requests[-1].system_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "not-a-number", "+", "\t"])
async def test_malformed_or_blank_caller_id_falls_back_safely(bad):
    brain, conversations, customers, identities, provider = _make_brain()
    customer = await _customer(customers)
    await identities.associate(_ORG_ID, customer_id=customer.id, caller_number=_CALLER)

    prompt = await _prompt_for(brain, conversations, provider, caller_number=bad)

    assert "Caller records" not in prompt


@pytest.mark.asyncio
async def test_lookup_failure_degrades_to_unknown_caller():
    """A storage failure must never cost the caller their emergency."""
    brain, conversations, customers, identities, provider = _make_brain()
    identities.fail_with = RuntimeError("database unavailable")

    prompt = await _prompt_for(brain, conversations, provider, caller_number=_CALLER)

    assert "Caller records" not in prompt


# --- tenant isolation ---


@pytest.mark.asyncio
async def test_caller_id_cannot_resolve_another_tenants_customer():
    brain, conversations, customers, identities, provider = _make_brain()
    other = await _customer(customers, org=_OTHER_ORG_ID, name="Other Org Customer")
    await identities.associate(_OTHER_ORG_ID, customer_id=other.id, caller_number=_CALLER)

    prompt = await _prompt_for(brain, conversations, provider, caller_number=_CALLER)

    assert "Caller records" not in prompt
    assert "Other Org Customer" not in prompt


# --- cardinality ---


@pytest.mark.asyncio
async def test_one_customer_may_have_several_caller_ids():
    brain, conversations, customers, identities, provider = _make_brain()
    customer = await _customer(customers)
    for number in ("+919999999999", "+919000000001"):
        await identities.associate(_ORG_ID, customer_id=customer.id, caller_number=number)

    for number in ("+919999999999", "+919000000001"):
        prompt = await _prompt_for(brain, conversations, provider, caller_number=number)
        assert "Lucky" in prompt


@pytest.mark.asyncio
async def test_shared_caller_id_grounds_nothing_and_leaks_no_name():
    """A household or office line belongs to whoever picked up. Guessing
    would greet the wrong person, so nothing is grounded."""
    brain, conversations, customers, identities, provider = _make_brain()
    first = await _customer(customers, name="Alice", phone="111")
    second = await _customer(customers, name="Bob", phone="222")
    for customer in (first, second):
        await identities.associate(_ORG_ID, customer_id=customer.id, caller_number=_CALLER)

    prompt = await _prompt_for(brain, conversations, provider, caller_number=_CALLER)

    assert "Caller records" not in prompt
    assert "Alice" not in prompt
    assert "Bob" not in prompt


# --- what the model is allowed to know ---


@pytest.mark.asyncio
async def test_stored_address_is_never_placed_in_the_prompt():
    """The security decision: a caller ID match must not hand a spoofer a
    service address. The model is told an address exists, never its value."""
    brain, conversations, customers, identities, provider = _make_brain()
    customer = await _customer(customers, address="77 Nowhere Lane, Springfield")
    await identities.associate(_ORG_ID, customer_id=customer.id, caller_number=_CALLER)

    prompt = await _prompt_for(brain, conversations, provider, caller_number=_CALLER)

    assert "77 Nowhere Lane" not in prompt
    assert "Springfield" not in prompt
    assert "address on file" in prompt
    assert "must NOT guess or state it" in prompt


@pytest.mark.asyncio
async def test_prompt_never_exposes_internal_identifiers_or_contact_fields():
    brain, conversations, customers, identities, provider = _make_brain()
    customer = await customers.create(
        organization_id=_ORG_ID,
        full_name="Lucky",
        phone_number="123456789",
        email="lucky@example.com",
        address="16 Street",
        notes="VIP - internal only",
    )
    await identities.associate(_ORG_ID, customer_id=customer.id, caller_number=_CALLER)

    prompt = await _prompt_for(brain, conversations, provider, caller_number=_CALLER)

    for forbidden in (
        str(customer.id),
        str(_ORG_ID),
        "123456789",
        "lucky@example.com",
        "VIP - internal only",
        "16 Street",
    ):
        assert forbidden not in prompt, f"{forbidden!r} leaked into the prompt"


@pytest.mark.asyncio
async def test_known_caller_with_no_usable_fields_grounds_nothing():
    brain, conversations, customers, identities, provider = _make_brain()
    customer = await _customer(customers, name=None, address=None)
    await identities.associate(_ORG_ID, customer_id=customer.id, caller_number=_CALLER)

    baseline = await _prompt_for(brain, conversations, provider, caller_number=None)
    prompt = await _prompt_for(brain, conversations, provider, caller_number=_CALLER)

    assert prompt == baseline


@pytest.mark.asyncio
async def test_name_only_customer_grounds_name_without_address_language():
    brain, conversations, customers, identities, provider = _make_brain()
    customer = await _customer(customers, name="Lucky", address=None)
    await identities.associate(_ORG_ID, customer_id=customer.id, caller_number=_CALLER)

    prompt = await _prompt_for(brain, conversations, provider, caller_number=_CALLER)

    assert "Lucky" in prompt
    assert "address on file" not in prompt


@pytest.mark.asyncio
async def test_address_only_customer_grounds_confirmation_without_a_name():
    brain, conversations, customers, identities, provider = _make_brain()
    customer = await _customer(customers, name=None, address="16 Street, California")
    await identities.associate(_ORG_ID, customer_id=customer.id, caller_number=_CALLER)

    prompt = await _prompt_for(brain, conversations, provider, caller_number=_CALLER)

    assert "address on file" in prompt
    assert "shows the name" not in prompt
    assert "16 Street" not in prompt


@pytest.mark.asyncio
async def test_prompt_states_the_caller_overrides_stored_records():
    brain, conversations, customers, identities, provider = _make_brain()
    customer = await _customer(customers, address="16 Street, California")
    await identities.associate(_ORG_ID, customer_id=customer.id, caller_number=_CALLER)

    prompt = await _prompt_for(brain, conversations, provider, caller_number=_CALLER)

    assert "takes precedence over the records" in prompt
    assert "Never argue with them" in prompt


# --- P5 is read-only ---


@pytest.mark.asyncio
async def test_grounding_never_writes_customer_fields():
    brain, conversations, customers, identities, provider = _make_brain()
    customer = await _customer(customers, name="Lucky", address="16 Street, California")
    await identities.associate(_ORG_ID, customer_id=customer.id, caller_number=_CALLER)

    await _prompt_for(brain, conversations, provider, caller_number=_CALLER)

    after = await customers.get_by_id(_ORG_ID, customer.id)
    assert after == customer, "P5 must not mutate the customer record"


# --- the value object itself ---


def test_known_caller_treats_blank_fields_as_absent():
    assert KnownCaller(name="  ", address="").is_empty
    assert not KnownCaller(name="Lucky", address=None).is_empty
    assert KnownCaller(name=None, address=" 1 High St ").address_on_file
    assert not KnownCaller(name=None, address="   ").address_on_file
