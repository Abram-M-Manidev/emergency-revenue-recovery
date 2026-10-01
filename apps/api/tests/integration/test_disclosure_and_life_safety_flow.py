"""Caller disclosure and life safety end to end: the real Vapi Custom-LLM
webhook, the real services, real PostgreSQL. Only the model (scripted), the
paging provider (fake) and Vapi call control (fake) are replaced.

What only this level shows: that the notice and the safety instruction are
the first words on the wire in both transports; that the notice is recorded
on the call row in the turn's transaction and so is not repeated; that a
hazard call still produces its ticket, its page and its transfer exactly as
before; and that the Owner settings are authorised and tenant-bound.

All numbers are in the 555-01xx block reserved for fictional use. Nothing
reaches a real provider or a real emergency service.
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.api.deps as deps_module
from app.api.deps import get_ai_provider, get_paging_worker, verify_vapi_secret
from app.domain.call_transfer.attempt import TransferStatus
from app.domain.call_transfer.port import CallTransferPort, TransferInitiation
from app.domain.entities.business_profile import BusinessType
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_line import VoiceProvider
from app.domain.paging.page import PageNotificationStatus
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.business_profile import BusinessProfileModel
from app.infrastructure.database.models.call_transfer import (
    CallTransferAttemptModel,
    OrganizationCallTransferSettingsModel,
)
from app.infrastructure.database.models.disclosure import OrganizationDisclosureSettingsModel
from app.infrastructure.database.models.emergency_ticket import EmergencyTicketModel
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.paging import (
    EmergencyPageNotificationModel,
    OrganizationPagingSettingsModel,
)
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app, fastapi_app
from tests.fakes import ScriptedToolAIProvider, default_reply
from tests.integration.test_emergency_paging_flow import FakePagingProvider, _worker
from tests.log_capture import capture_events

_SECRET = "test-vapi-secret-disclosure"
_PATH = "/api/v1/voice/vapi/chat/completions"
_CONTROL = "https://phone-call-websocket.aws-us-west-2-backend-production1.vapi.ai/d1/control"
_PRIMARY = "+16305550101"
_ON_CALL = "+16305550104"
_AI_LINE = "+16305550199"
_CALLER = "+16305550184"


class RecordingPort(CallTransferPort):
    calls: list[str] = []

    async def transfer(self, *, call_control, destination_number, announcement):
        RecordingPort.calls.append(destination_number)
        return TransferInitiation(True)


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
async def harness(database_ready, monkeypatch):
    scripted = ScriptedToolAIProvider()
    pager = FakePagingProvider()
    worker = _worker(pager)
    RecordingPort.calls = []
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: scripted
    fastapi_app.dependency_overrides[verify_vapi_secret] = _secret_override
    fastapi_app.dependency_overrides[get_paging_worker] = lambda: worker
    monkeypatch.setattr(deps_module, "VapiCallControlTransfer", RecordingPort)
    yield scripted, pager
    for dependency in (get_ai_provider, verify_vapi_secret, get_paging_worker):
        fastapi_app.dependency_overrides.pop(dependency, None)


async def _tenant(
    name: str,
    *,
    ai: bool | None = None,
    recording: bool | None = None,
    paging: bool = False,
    transfer: bool = False,
    country: str | None = None,
) -> tuple[uuid.UUID, str]:
    org_id = uuid.uuid4()
    assistant = f"asst_disc_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(OrganizationModel(id=org_id, name=name, slug=f"dc-{org_id.hex[:10]}"))
        await session.flush()
        session.add(VoiceLineModel(organization_id=org_id, provider=VoiceProvider.VAPI,
                                   vapi_assistant_id=assistant, phone_number=_AI_LINE,
                                   is_active=True))
        if country is not None:
            session.add(BusinessProfileModel(
                organization_id=org_id, business_type=BusinessType.HVAC, display_name=name,
                timezone="America/Chicago", country=country,
            ))
        if ai is not None:
            session.add(OrganizationDisclosureSettingsModel(
                organization_id=org_id, ai_disclosure_enabled=ai,
                recording_notice_enabled=bool(recording),
            ))
        if paging:
            session.add(OrganizationPagingSettingsModel(
                organization_id=org_id, is_enabled=True, primary_number=_PRIMARY,
                backup_number=None, sms_enabled=True, voice_enabled=False,
                ack_timeout_seconds=300,
            ))
        if transfer:
            session.add(OrganizationCallTransferSettingsModel(
                organization_id=org_id, business_hours_number=None, after_hours_number=_ON_CALL,
                transfer_emergencies=False, is_enabled=True,
            ))
        await session.commit()
    return org_id, assistant


def _body(call_id: str, assistant: str, utterance: str | None, *, stream: bool) -> dict:
    messages = [{"role": "system", "content": "You are a helpful assistant."}]
    if utterance is not None:
        messages.append({"role": "user", "content": utterance})
    return {
        "call": {"id": call_id, "assistantId": assistant, "customer": {"number": _CALLER},
                 "monitor": {"controlUrl": _CONTROL}},
        "messages": messages,
        "stream": stream,
    }


async def _post(body: dict) -> str:
    """The caller-facing text, whichever transport was asked for."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_PATH, json=body, headers={"x-vapi-secret": _SECRET})
    assert response.status_code == 200, response.text
    if not body["stream"]:
        return response.json()["choices"][0]["message"]["content"]
    spoken = []
    for line in response.text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            delta = json.loads(line[6:])["choices"][0]["delta"]
            spoken.append(delta.get("content") or "")
    return "".join(spoken)


async def _voice_call(call_id: str) -> VoiceCallModel:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == call_id))
        ).scalar_one()


# =============================================================================
# Disclosure
# =============================================================================


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("stream", [False, True])
async def test_the_first_reply_leads_with_the_notice_and_only_the_first(harness, stream):
    scripted, _ = harness
    _, assistant = await _tenant("Delta Heating")
    call_id = f"call_{uuid.uuid4().hex[:10]}"

    scripted.queue_reply(default_reply(message_to_customer="How can I help?"))
    first = await _post(_body(call_id, assistant, "hi there", stream=stream))
    assert first == (
        "You're speaking with an automated assistant for Delta Heating, and this call is "
        "recorded. How can I help?"
    )
    call = await _voice_call(call_id)
    assert call.disclosure_sent_at is not None
    assert (call.disclosed_ai, call.disclosed_recording) == (True, True)

    scripted.queue_reply(default_reply(message_to_customer="Sure."))
    second = await _post(_body(call_id, assistant, "my furnace is noisy", stream=stream))
    assert second == "Sure."


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("stream", [False, True])
async def test_the_opening_request_is_answered_by_errs_not_the_model(harness, stream):
    """Vapi set to let the model speak first: a request with no caller words.
    ERRS answers with the fixed opening — the model is never called — and
    the first caller turn after it carries no second notice."""
    scripted, _ = harness
    _, assistant = await _tenant("Echo Plumbing")
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    model_calls = len(scripted.requests)

    opening = await _post(_body(call_id, assistant, None, stream=stream))
    assert opening == (
        "Thanks for calling Echo Plumbing. You're speaking with an automated assistant for "
        "Echo Plumbing, and this call is recorded. How can I help you today?"
    )
    assert len(scripted.requests) == model_calls

    scripted.queue_reply(default_reply(message_to_customer="Let me help."))
    assert await _post(_body(call_id, assistant, "my sink is clogged", stream=stream)) == (
        "Let me help."
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_the_opening_for_a_switched_off_assistant_is_the_unavailable_message(harness):
    _, assistant = await _tenant("Foxtrot Electric")
    async with AsyncSessionLocal() as session:
        org = (
            await session.execute(select(OrganizationModel).where(OrganizationModel.name == "Foxtrot Electric"))
        ).scalar_one()
        org.voice_assistant_enabled = False
        await session.commit()
    spoken = await _post(_body(f"call_{uuid.uuid4().hex[:10]}", assistant, None, stream=False))
    assert "automated assistant is unavailable" in spoken


@pytest.mark.asyncio(loop_scope="session")
async def test_each_tenant_gets_its_own_notice(harness):
    scripted, _ = harness
    _, quiet = await _tenant("Golf Quiet Co", ai=False, recording=False)
    _, ai_only = await _tenant("Hotel AI Co", ai=True, recording=False)
    scripted.queue_reply(default_reply(message_to_customer="Hello."))
    scripted.queue_reply(default_reply(message_to_customer="Hello."))
    assert await _post(_body(f"call_{uuid.uuid4().hex[:10]}", quiet, "hi", stream=True)) == "Hello."
    assert await _post(_body(f"call_{uuid.uuid4().hex[:10]}", ai_only, "hi", stream=True)) == (
        "You're speaking with an automated assistant for Hotel AI Co. Hello."
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_a_recording_without_a_notice_is_surfaced_to_the_dashboard(harness):
    scripted, _ = harness
    org_id, assistant = await _tenant("India Recorded Co", ai=True, recording=False)
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    scripted.queue_reply(default_reply(message_to_customer="Hello."))
    await _post(_body(call_id, assistant, "hi", stream=False))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        report = await client.post(
            "/api/v1/voice/vapi/events",
            json={"message": {"type": "end-of-call-report", "call": {"id": call_id},
                              "endedReason": "customer-ended-call", "durationSeconds": 12,
                              "recordingUrl": "https://storage.example.invalid/r.wav"}},
            headers={"x-vapi-secret": _SECRET},
        )
    assert report.status_code == 200
    call = await _voice_call(call_id)
    assert call.recording_url is not None, "an existing recording is never deleted"
    assert call.disclosed_recording is False


# --- Owner configuration -----------------------------------------------------------


async def _register(client: AsyncClient, name: str) -> str:
    response = await client.post("/api/v1/auth/register", json={
        "organization_name": name, "full_name": "Owner Owner",
        "email": f"owner-{uuid.uuid4().hex[:10]}@example.com", "password": "super-secret-123",
    })
    assert response.status_code == 201, response.text
    return response.json()["tokens"]["access_token"]


async def _member(client: AsyncClient, owner: str) -> str:
    email = f"member-{uuid.uuid4().hex[:10]}@example.com"
    created = await client.post("/api/v1/team/members", headers=_auth(owner), json={
        "full_name": "Mem Ber", "email": email, "temporary_password": "member-secret-123",
        "role": "Member",
    })
    assert created.status_code in (200, 201), created.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": "member-secret-123"})
    return login.json()["tokens"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_configures_the_notice_members_cannot_and_tenants_are_separate(database_ready):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "Juliet Disclosure Co")
        other = await _register(client, "Kilo Disclosure Co")
        member = await _member(client, owner)
        path = "/api/v1/organizations/current/disclosure"

        default = (await client.get(path, headers=_auth(owner))).json()
        assert default["is_default"] is True
        assert default["ai_disclosure_enabled"] and default["recording_notice_enabled"]
        assert default["disclosure_sentence"] == (
            "You're speaking with an automated assistant for Juliet Disclosure Co, and this call "
            "is recorded."
        )

        assert (await client.get(path, headers=_auth(member))).status_code == 403
        assert (await client.put(path, headers=_auth(member), json={
            "ai_disclosure_enabled": False, "recording_notice_enabled": False,
        })).status_code == 403

        saved = await client.put(path, headers=_auth(owner), json={
            "ai_disclosure_enabled": True, "recording_notice_enabled": False,
        })
        assert saved.status_code == 200
        assert saved.json()["is_default"] is False
        assert saved.json()["disclosure_sentence"] == (
            "You're speaking with an automated assistant for Juliet Disclosure Co."
        )
        # The other tenant is untouched.
        assert (await client.get(path, headers=_auth(other))).json()["recording_notice_enabled"]

        assert (await client.delete(path, headers=_auth(owner))).status_code == 204
        assert (await client.get(path, headers=_auth(owner))).json()["is_default"] is True


# =============================================================================
# Life safety, end to end — and the rest of the emergency machinery intact
# =============================================================================

_GAS_TICKET = {
    "customer_name": None,
    "customer_phone": None,
    "service_address": None,
    "problem_description": "Caller smells gas in the basement.",
    "classification": "emergency",
    "service_name": None,
}


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("stream", [False, True])
async def test_a_gas_leak_call_gets_notice_then_safety_then_a_ticket_and_a_page(harness, stream):
    """11, 18, 21, 22, 23. Caller gives no name or address. They hear the
    notice, then the fixed safety instruction, then the model; the ticket is
    created anyway (from caller ID), the on-call technician is paged, and no
    result anywhere claims the emergency services were contacted."""
    scripted, pager = harness
    org_id, assistant = await _tenant("Lima Gas Co", paging=True, country="US")
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    sends_before = len(pager.sends)

    scripted.queue_tool_round([("create_service_request", _GAS_TICKET)])
    scripted.queue_reply(default_reply(
        message_to_customer="I've logged your emergency and we're paging the on-call technician.",
        classification=CallClassification.EMERGENCY,
        recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
    ))
    with capture_events() as events:
        spoken = await _post(_body(call_id, assistant, "I smell gas really strongly!", stream=stream))

    notice = "You're speaking with an automated assistant for Lima Gas Co, and this call is recorded."
    assert spoken.startswith(notice + " Your safety comes first. If you smell gas, please leave")
    assert "call 911 or your gas company" in spoken
    assert spoken.endswith("we're paging the on-call technician.")

    tool_result = scripted.results[-1].content
    assert tool_result["success"] is True
    assert tool_result["on_call_paging"] == "queued"
    for claim in ("911 has been", "emergency services have been", "fire department has"):
        assert claim not in json.dumps(tool_result).lower()
    prompt = scripted.requests[-1].system_prompt
    assert "LIFE-SAFETY" in prompt and "nobody has" in prompt

    async with AsyncSessionLocal() as session:
        tickets = (await session.execute(
            select(EmergencyTicketModel).where(EmergencyTicketModel.organization_id == org_id)
        )).scalars().all()
        pages = (await session.execute(
            select(EmergencyPageNotificationModel).where(
                EmergencyPageNotificationModel.organization_id == org_id
            )
        )).scalars().all()
    assert len(tickets) == 1
    assert tickets[0].customer_phone, "the caller ID should stand in for a number never given"
    assert [p.status for p in pages] == [PageNotificationStatus.SENT]
    assert len(pager.sends) == sends_before + 1

    hazard_logs = [e for e in events if e.get("event") == "life_safety_instruction_given"]
    assert len(hazard_logs) == 1 and hazard_logs[0]["hazards"] == ["gas"]
    assert "smell gas really strongly" not in json.dumps(events, default=str)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_caller_in_danger_who_asks_for_a_person_is_still_transferred(harness):
    """24. The safety instruction does not get in the way of the human exit,
    and the transfer does not get in the way of the ticket."""
    scripted, _ = harness
    org_id, assistant = await _tenant("Mike Electric", transfer=True)
    call_id = f"call_{uuid.uuid4().hex[:10]}"

    scripted.queue_tool_round([("create_service_request", {
        **_GAS_TICKET, "problem_description": "Panel sparking and smoking."})])
    scripted.queue_reply(default_reply(
        message_to_customer="I've logged it.",
        classification=CallClassification.EMERGENCY,
        recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
    ))
    first = await _post(_body(call_id, assistant, "my electrical panel is sparking and smoking", stream=True))
    assert "If there's smoke or fire, get everyone out" in first
    assert "stay well away from anything sparking" in first

    scripted.queue_tool_round([("transfer_to_human", {"reason": "caller_requested", "is_emergency": True})])
    scripted.queue_reply(default_reply(message_to_customer=""))
    second = await _post(_body(call_id, assistant, "put me through to a real person now", stream=True))
    assert "Your safety comes first" not in second, "instruction repeated on a later turn"
    assert RecordingPort.calls[-1] == _ON_CALL

    async with AsyncSessionLocal() as session:
        attempt = (await session.execute(
            select(CallTransferAttemptModel).where(CallTransferAttemptModel.organization_id == org_id)
        )).scalar_one()
        tickets = (await session.execute(
            select(EmergencyTicketModel).where(EmergencyTicketModel.organization_id == org_id)
        )).scalars().all()
    assert attempt.status is TransferStatus.INITIATED
    assert len(tickets) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_life_safety_state_never_crosses_tenants(harness):
    """25. Tenant A's caller reports smoke; tenant B's caller, at the same
    time, asks about a tune-up. B gets no safety script and no directive."""
    scripted, _ = harness
    _, a = await _tenant("November HVAC", ai=False, recording=False)
    _, b = await _tenant("Oscar HVAC", ai=False, recording=False)
    scripted.queue_reply(default_reply(message_to_customer="Logged."))
    a_text = await _post(_body(f"call_{uuid.uuid4().hex[:10]}", a, "there's smoke from the vents", stream=True))
    scripted.queue_reply(default_reply(message_to_customer="Sure, we do tune-ups."))
    b_text = await _post(_body(f"call_{uuid.uuid4().hex[:10]}", b, "do you do tune-ups?", stream=True))
    assert a_text.startswith("Your safety comes first.")
    # No business profile, so no known country: the caller is pointed at
    # "your local emergency number" rather than a guessed one.
    assert "call your local emergency number" in a_text
    assert b_text == "Sure, we do tune-ups."
    assert "LIFE-SAFETY" not in scripted.requests[-1].system_prompt
