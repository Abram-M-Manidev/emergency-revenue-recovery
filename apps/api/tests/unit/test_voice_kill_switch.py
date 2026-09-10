"""The per-tenant voice kill switch.

An operator needs to stop one business's phone assistant without stopping
everyone's. Before this, the only levers were `AI_TOOLS_ENABLED` — a
process-wide setting that would have disabled tools for every tenant in the
deployment — and `organizations.is_active`, which disables the whole account
including the dashboard the operator would use to investigate.

What is under test
------------------
The switch is enforced in `VoiceService._resolve_voice_line`, the one
function both the streaming and non-streaming transports already share. That
placement is the property most worth pinning: a safety control that is
honoured on one transport and forgotten on the other is worse than none,
because it will read as working right up until the call that matters.

The deliberate fail-open
------------------------
With no organization repository wired in, calls proceed. That is an
asymmetry chosen on purpose and asserted below: this control exists to stop a
misbehaving assistant, not to become a second way for a deployment mistake to
take a business's phone line down.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.application.services.voice_service import (
    TranscriptSupersession,
    VoiceService,
    VoiceTextDelta,
)
from app.domain.entities.organization import Organization
from app.domain.entities.voice_line import VoiceLine, VoiceProvider
from app.domain.exceptions import VoiceAssistantDisabledError
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
    FakeOrganizationRepository,
    FakeServiceAreaRepository,
    FakeServiceRepository,
    fake_settings,
)

# Defined alongside `VoiceService`'s own tests rather than in `fakes.py`,
# and reused here the same way `test_completion_gate.py` does.
from tests.unit.test_voice_service import (
    FakeVoiceCallRepository,
    FakeVoiceLineRepository,
)

_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()
_ASSISTANT_ID = "asst_kill_switch"
_OTHER_ASSISTANT_ID = "asst_other_tenant"


def _organization(organization_id: uuid.UUID, *, voice_enabled: bool) -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(
        id=organization_id,
        name="Northside Heating & Cooling",
        slug=f"org-{organization_id.hex[:8]}",
        is_active=True,
        voice_assistant_enabled=voice_enabled,
        created_at=now,
        updated_at=now,
    )


def _voice_line(organization_id: uuid.UUID, assistant_id: str) -> VoiceLine:
    now = datetime.now(timezone.utc)
    return VoiceLine(
        id=uuid.uuid4(),
        organization_id=organization_id,
        provider=VoiceProvider.VAPI,
        vapi_assistant_id=assistant_id,
        vapi_phone_number_id=None,
        phone_number="+15005550006",
        is_active=True,
        created_at=now,
        updated_at=now,
    )


class _Harness:
    """A real `VoiceService` over in-memory repositories, with two tenants on
    one deployment — which is what makes the isolation assertions real rather
    than vacuous."""

    def __init__(
        self,
        *,
        voice_enabled: bool = True,
        other_voice_enabled: bool = True,
        wire_organizations: bool = True,
    ) -> None:
        from app.application.services.ai_brain_service import AIBrainService

        settings = fake_settings(AI_TOOLS_ENABLED=False)
        self.conversations = FakeConversationRepository()
        self.organizations = FakeOrganizationRepository()
        self.organizations.seed(_organization(_ORG_ID, voice_enabled=voice_enabled))
        self.organizations.seed(
            _organization(_OTHER_ORG_ID, voice_enabled=other_voice_enabled)
        )
        # No scripted queue: `FakeAIProvider` falls back to a benign
        # non-emergency reply, which is all these tests need — the subject
        # is whether the call reaches the model at all.
        self.provider = FakeAIProvider()

        ai_brain = AIBrainService(
            conversation_repository=self.conversations,
            conversation_outcome_repository=FakeConversationOutcomeRepository(
                self.conversations
            ),
            ai_provider=self.provider,
            business_profile_repository=FakeBusinessProfileRepository(None),
            business_hours_repository=FakeBusinessHoursRepository([]),
            service_repository=FakeServiceRepository([]),
            service_area_repository=FakeServiceAreaRepository(),
            faq_repository=FakeFAQRepository(),
            emergency_keyword_repository=FakeEmergencyKeywordRepository(),
            settings=settings,
            caller_identity_repository=FakeCallerIdentityRepository(
                FakeCustomerRepository()
            ),
        )
        self.voice = VoiceService(
            voice_line_repository=FakeVoiceLineRepository(
                [
                    _voice_line(_ORG_ID, _ASSISTANT_ID),
                    _voice_line(_OTHER_ORG_ID, _OTHER_ASSISTANT_ID),
                ]
            ),
            voice_call_repository=FakeVoiceCallRepository(),
            conversation_repository=self.conversations,
            ai_brain_service=ai_brain,
            supersession=TranscriptSupersession(),
            organization_repository=self.organizations if wire_organizations else None,
        )

    async def call(self, assistant_id: str = _ASSISTANT_ID):
        return await self.voice.handle_chat_completion(
            vapi_call_id=f"call_{uuid.uuid4().hex[:8]}",
            assistant_id=assistant_id,
            phone_number_id=None,
            customer_number="+15551230000",
            customer_utterance="My furnace has stopped working.",
        )

    async def stream(self, assistant_id: str = _ASSISTANT_ID) -> list[str]:
        spoken: list[str] = []
        async for event in self.voice.handle_chat_completion_stream(
            vapi_call_id=f"call_{uuid.uuid4().hex[:8]}",
            assistant_id=assistant_id,
            phone_number_id=None,
            customer_number="+15551230000",
            customer_utterance="My furnace has stopped working.",
        ):
            if isinstance(event, VoiceTextDelta):
                spoken.append(event.text)
        return spoken


# --- enabled: nothing changes ------------------------------------------------


@pytest.mark.asyncio
async def test_an_enabled_tenant_is_answered_normally():
    harness = _Harness(voice_enabled=True)

    result = await harness.call()

    assert result.reply_text
    assert result.organization_id == _ORG_ID


@pytest.mark.asyncio
async def test_an_enabled_tenant_is_answered_on_the_streaming_transport_too():
    harness = _Harness(voice_enabled=True)

    spoken = await harness.stream()

    assert "".join(spoken)


# --- disabled: refused, on both transports -----------------------------------


@pytest.mark.asyncio
async def test_a_disabled_tenant_is_refused():
    harness = _Harness(voice_enabled=False)

    with pytest.raises(VoiceAssistantDisabledError):
        await harness.call()


@pytest.mark.asyncio
async def test_a_disabled_tenant_is_refused_on_the_streaming_transport_too():
    """The transports share `_resolve_voice_line` precisely so this cannot
    diverge. Asserted rather than assumed, because a control honoured on one
    path and not the other reads as working until the call that matters."""
    harness = _Harness(voice_enabled=False)

    with pytest.raises(VoiceAssistantDisabledError):
        await harness.stream()


@pytest.mark.asyncio
async def test_a_disabled_tenant_never_reaches_the_model():
    """Enforced before the AI Brain is called, so a switched-off tenant
    accrues no LLM spend from calls it has stopped."""
    harness = _Harness(voice_enabled=False)

    with pytest.raises(VoiceAssistantDisabledError):
        await harness.call()

    assert harness.provider.requests == []


@pytest.mark.asyncio
async def test_a_disabled_tenant_creates_no_conversation():
    """Enforced before any row is written, so a disabled tenant accumulates
    no conversations, no outcomes, and nothing to clean up afterwards."""
    harness = _Harness(voice_enabled=False)

    with pytest.raises(VoiceAssistantDisabledError):
        await harness.call()

    listed = await harness.conversations.list_for_organization(_ORG_ID, limit=50, offset=0)
    assert listed == []


# --- isolation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabling_one_tenant_leaves_another_answering():
    """The property that makes this a per-tenant control rather than a global
    one. Both organizations live in the same deployment and the same
    repositories here, so a switch that leaked would be caught."""
    harness = _Harness(voice_enabled=False, other_voice_enabled=True)

    with pytest.raises(VoiceAssistantDisabledError):
        await harness.call(_ASSISTANT_ID)

    other = await harness.call(_OTHER_ASSISTANT_ID)
    assert other.organization_id == _OTHER_ORG_ID
    assert other.reply_text


@pytest.mark.asyncio
async def test_disabling_one_tenant_does_not_disable_every_tenant():
    harness = _Harness(voice_enabled=True, other_voice_enabled=False)

    result = await harness.call(_ASSISTANT_ID)
    assert result.organization_id == _ORG_ID

    with pytest.raises(VoiceAssistantDisabledError):
        await harness.call(_OTHER_ASSISTANT_ID)


# --- the deliberate fail-open ------------------------------------------------


@pytest.mark.asyncio
async def test_calls_proceed_when_no_organization_repository_is_wired():
    """A wiring mistake must restore the pre-switch behaviour, not silently
    take every tenant's phone line down. This asymmetry is the point: the
    control exists to stop a misbehaving assistant, and failing closed would
    make it a new way to cause an outage."""
    harness = _Harness(voice_enabled=False, wire_organizations=False)

    result = await harness.call()

    assert result.reply_text


@pytest.mark.asyncio
async def test_a_lookup_failure_does_not_drop_a_live_call():
    """An unknown switch state is not a reason to hang up on someone who may
    be reporting an emergency."""
    harness = _Harness(voice_enabled=True)

    async def _explode(organization_id):
        raise RuntimeError("database is having a bad day")

    harness.organizations.get_by_id = _explode  # type: ignore[method-assign]

    result = await harness.call()

    assert result.reply_text


# --- the switch is read fresh, not cached ------------------------------------


@pytest.mark.asyncio
async def test_flipping_the_switch_takes_effect_on_the_next_call():
    """An operator switching the assistant off during an incident needs it to
    stop now, not after a process restart — so the flag is read per call
    rather than cached anywhere."""
    harness = _Harness(voice_enabled=True)
    assert (await harness.call()).reply_text

    harness.organizations.seed(_organization(_ORG_ID, voice_enabled=False))

    with pytest.raises(VoiceAssistantDisabledError):
        await harness.call()

    harness.organizations.seed(_organization(_ORG_ID, voice_enabled=True))
    assert (await harness.call()).reply_text
