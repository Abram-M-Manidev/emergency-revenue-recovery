"""End-to-end scheduling workflow against a real Postgres database.

This is the module that answers the question the 2026-08-22 call raised. It
drives the actual Vapi Custom-LLM webhook over HTTP — the same route a phone
call hits — with the real `deps.py` wiring, the real
`DatabaseAvailabilityProvider`, the real Postgres advisory booking lock, and
real SQL. Only the language model is a double, because the tool *arguments*
are what a model contributes and those are exactly what a test must control.

The invariant every test here exists to defend:

    a tool result of {"success": true} for book_appointment
        <=> the appointment row has a non-null `scheduled_start_at`

If that biconditional holds, the assistant cannot truthfully confirm a
booking that did not happen, because the only thing it is permitted to
confirm from is the tool result.

Dates are never hardcoded. Business hours are seeded wide enough that
availability exists whenever the suite runs, and every booking uses a slot
`check_availability` actually returned — which is also how the real flow
works: offer, choose, book.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_ai_provider, verify_vapi_secret
from app.domain.entities.voice_line import VoiceProvider
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.appointment import AppointmentModel
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app, fastapi_app
from tests.fakes import ScriptedToolAIProvider, default_reply

_TEST_VAPI_SECRET = "test-vapi-secret-scheduling"

_LUCKY = {
    "customer_name": "Lucky",
    "customer_phone": "123456789",
    "service_address": "16th Street, California",
    "problem_description": "AC is running but not cooling the house.",
    "classification": "non_emergency",
    "service_name": "Air Conditioning Repair",
}


def _verify_vapi_secret_override(x_vapi_secret: str | None = Header(default=None)) -> None:
    if x_vapi_secret != _TEST_VAPI_SECRET:
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


@pytest_asyncio.fixture(loop_scope="session")
async def provider():
    scripted = ScriptedToolAIProvider()
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: scripted
    fastapi_app.dependency_overrides[verify_vapi_secret] = _verify_vapi_secret_override
    yield scripted
    fastapi_app.dependency_overrides.pop(get_ai_provider, None)
    fastapi_app.dependency_overrides.pop(verify_vapi_secret, None)


@pytest_asyncio.fixture(loop_scope="session")
async def client(database_ready, provider):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _vapi() -> dict[str, str]:
    return {"x-vapi-secret": _TEST_VAPI_SECRET}


async def _register(client: AsyncClient, org_name: str, email: str) -> tuple[str, uuid.UUID]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": org_name,
            "full_name": "Test Owner",
            "email": email,
            "password": "super-secret-123",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["tokens"]["access_token"], uuid.UUID(body["user"]["organization_id"])


async def _seed_organization(client: AsyncClient, token: str, org_id: uuid.UUID, assistant_id: str):
    """Business profile, wide hours, one service, and a voice line.

    Hours are 00:00-23:59 every day so the suite is not sensitive to the
    weekday or hour it runs at — the point of these tests is the booking
    machinery, and `test_availability_provider.py` already covers hours
    arithmetic against a pinned clock."""
    profile = await client.put(
        "/api/v1/business-knowledge/profile",
        json={
            "business_type": "hvac",
            "display_name": "Northside Heating & Cooling",
            "phone_number": "+15005550006",
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

    service = await client.post(
        "/api/v1/business-knowledge/services",
        json={
            "name": "Air Conditioning Repair",
            "description": "Diagnose and repair a cooling fault.",
            "category": "cooling",
            "is_emergency_eligible": False,
            "is_active": True,
            "default_duration_minutes": 90,
            "default_price": 189.0,
        },
        headers=_auth(token),
    )
    assert service.status_code in (200, 201), service.text

    async with AsyncSessionLocal() as session:
        session.add(
            VoiceLineModel(
                organization_id=org_id,
                provider=VoiceProvider.VAPI,
                vapi_assistant_id=assistant_id,
                vapi_phone_number_id=None,
                phone_number="+15005550006",
                is_active=True,
            )
        )
        await session.commit()


async def _turn(client: AsyncClient, *, call_id: str, assistant_id: str, utterance: str):
    """One Vapi Custom-LLM turn, non-streamed (the JSON transport), which
    exercises the identical service path the SSE transport uses."""
    return await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json={
            "call": {
                "id": call_id,
                "assistantId": assistant_id,
                "customer": {"number": "+15551230000"},
            },
            "messages": [{"role": "user", "content": utterance}],
        },
        headers=_vapi(),
    )


async def _appointment_row(conversation_id: uuid.UUID) -> AppointmentModel | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AppointmentModel).where(AppointmentModel.conversation_id == conversation_id)
        )
        return result.scalar_one_or_none()


async def _only_conversation_id(client: AsyncClient, token: str) -> uuid.UUID:
    listing = await client.get("/api/v1/ai/conversations", headers=_auth(token))
    assert listing.status_code == 200
    return uuid.UUID(listing.json()[0]["id"])


async def _consent_conversation_id(client: AsyncClient, token: str) -> uuid.UUID:
    """The newest conversation on this organization.

    The consent test runs a throwaway reconnaissance call before the one it
    actually asserts on, so "the only conversation" is no longer true there.
    The listing is ordered newest-first."""
    listing = await client.get("/api/v1/ai/conversations", headers=_auth(token))
    assert listing.status_code == 200
    assert len(listing.json()) >= 2
    return uuid.UUID(listing.json()[0]["id"])


# --- The workflow ------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_full_workflow_creates_checks_and_books_against_postgres(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    assistant_id = "asst_sched_full"
    token, org_id = await _register(client, "Scheduling Org A", "sched-a@example.com")
    await _seed_organization(client, token, org_id, assistant_id)

    # Turn 1: the caller describes the problem and gives their details.
    provider.queue_tool_round([("create_service_request", _LUCKY)])
    provider.queue_tool_round([("check_availability", {"service_name": "Air Conditioning Repair"})])
    provider.queue_reply(default_reply(message_to_customer="I have some times available."))

    first = await _turn(
        client,
        call_id="call_sched_full",
        assistant_id=assistant_id,
        utterance="My AC is running but it's not cooling.",
    )
    assert first.status_code == 200, first.text

    creation = provider.results[0].content
    assert creation["success"] is True
    assert creation["service_request_type"] == "appointment"
    assert creation["bookable"] is True
    assert creation["duration_minutes"] == 90

    availability = provider.results[1].content
    assert availability["success"] is True
    assert availability["slots"], "no slots were offered against a fully-open week"
    offered = availability["slots"][0]

    # The appointment exists but holds no time yet — precisely the state the
    # 2026-08-22 call ended in, and the state a confirmation must not follow.
    conversation_id = await _only_conversation_id(client, token)
    before = await _appointment_row(conversation_id)
    assert before is not None
    assert before.scheduled_start_at is None
    assert before.status.value == "requested"

    # Turn 2: the caller picks the offered slot. Selection is its own round —
    # the model must have the choice on record before the booking is allowed,
    # and the booking is refused with SLOT_NOT_SELECTED without it.
    provider.queue_tool_round(
        [("select_appointment_slot", {"slot_id": offered["slot_id"]})]
    )
    provider.queue_tool_round([("book_appointment", {"slot_id": offered["slot_id"]})])
    provider.queue_reply(
        default_reply(
            message_to_customer=f"You're confirmed for {offered['label']}.",
            is_conversation_complete=True,
        )
    )
    second = await _turn(
        client,
        call_id="call_sched_full",
        assistant_id=assistant_id,
        utterance="That first time works for me.",
    )
    assert second.status_code == 200, second.text

    booking = provider.results[-1].content
    assert booking["success"] is True
    assert booking["status"] == "confirmed"
    assert booking["date"] == offered["date"]
    assert booking["start_time"] == offered["start_time"]

    # The database agrees with what the caller was told.
    after = await _appointment_row(conversation_id)
    assert after is not None
    assert str(after.id) == booking["appointment_id"]
    assert after.status.value == "scheduled"
    assert after.scheduled_start_at is not None
    assert after.duration_minutes == 90
    assert after.scheduled_start_at.astimezone(timezone.utc).strftime("%H:%M") == (
        booking["start_time"]
    )
    # And the contact details reached the record this time.
    assert after.customer_phone == "123456789"
    assert after.customer_address == "16th Street, California"
    assert after.customer_id is not None

    # The turn also emitted the endCall tool call, so the line hangs up.
    assert second.json()["choices"][0]["finish_reason"] == "tool_calls"

    # The dashboard sees the same thing the caller heard.
    listing = await client.get("/api/v1/appointments?status=scheduled", headers=_auth(token))
    assert listing.status_code == 200
    assert [a["id"] for a in listing.json()] == [str(after.id)]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_second_caller_cannot_take_a_slot_that_is_already_booked(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    """Double-booking prevention against real SQL and the real Postgres
    advisory lock."""
    assistant_id = "asst_sched_conflict"
    token, org_id = await _register(client, "Scheduling Org B", "sched-b@example.com")
    await _seed_organization(client, token, org_id, assistant_id)

    # BOTH callers are offered the slot before either takes it. That ordering
    # is the only way two conversations legitimately contend for one time,
    # and therefore the only way to reach the conflict check at all: a caller
    # who checks availability *after* the slot is gone is never offered it,
    # and would be refused as SLOT_NOT_OFFERED long before the double-booking
    # machinery ran.
    provider.queue_tool_round([("create_service_request", _LUCKY)])
    provider.queue_tool_round([("check_availability", {"service_name": "Air Conditioning Repair"})])
    provider.queue_reply(default_reply(message_to_customer="Here are some times."))
    await _turn(
        client,
        call_id="call_conflict_1",
        assistant_id=assistant_id,
        utterance="My AC is not cooling.",
    )
    taken = provider.results[-1].content["slots"][0]

    provider.queue_tool_round(
        [("create_service_request", {**_LUCKY, "customer_phone": "555000111"})]
    )
    provider.queue_tool_round(
        [("check_availability", {"service_name": "Air Conditioning Repair"})]
    )
    provider.queue_reply(default_reply(message_to_customer="Here are some times."))
    await _turn(
        client,
        call_id="call_conflict_2",
        assistant_id=assistant_id,
        utterance="My AC is broken too.",
    )
    assert taken["slot_id"] in {
        slot["slot_id"] for slot in provider.results[-1].content["slots"]
    }, "both callers must have been offered the contested slot"

    # Caller one takes it first.
    provider.queue_tool_round([("select_appointment_slot", {"slot_id": taken["slot_id"]})])
    provider.queue_tool_round([("book_appointment", {"slot_id": taken["slot_id"]})])
    provider.queue_reply(default_reply(message_to_customer="Confirmed."))
    await _turn(
        client,
        call_id="call_conflict_1",
        assistant_id=assistant_id,
        utterance="I'll take it.",
    )
    assert provider.results[-1].content["success"] is True

    provider.queue_tool_round([("select_appointment_slot", {"slot_id": taken["slot_id"]})])
    provider.queue_tool_round([("book_appointment", {"slot_id": taken["slot_id"]})])
    provider.queue_reply(
        default_reply(message_to_customer="I'm sorry, that time has just been taken.")
    )
    second = await _turn(
        client,
        call_id="call_conflict_2",
        assistant_id=assistant_id,
        utterance="My AC is broken too, I want that same slot.",
    )
    assert second.status_code == 200, second.text

    rejection = provider.results[-1].content
    assert rejection["success"] is False
    assert rejection["error"] == "SLOT_UNAVAILABLE"

    # The refusal wrote nothing: the second caller's appointment is still a
    # request with no time on it.
    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(AppointmentModel)
                    .where(AppointmentModel.organization_id == org_id)
                    .order_by(AppointmentModel.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    assert rows[0].status.value == "scheduled" and rows[0].scheduled_start_at is not None
    assert rows[1].status.value == "requested" and rows[1].scheduled_start_at is None


@pytest.mark.asyncio(loop_scope="session")
async def test_a_booked_slot_stops_being_offered_to_the_next_caller(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    """Capacity is one by default, so availability must shrink after a
    booking — otherwise the assistant keeps offering a slot that will then
    fail to book."""
    assistant_id = "asst_sched_shrink"
    token, org_id = await _register(client, "Scheduling Org C", "sched-c@example.com")
    await _seed_organization(client, token, org_id, assistant_id)

    provider.queue_tool_round([("create_service_request", _LUCKY)])
    provider.queue_tool_round([("check_availability", {"service_name": "Air Conditioning Repair"})])
    provider.queue_reply(default_reply(message_to_customer="Times available."))
    await _turn(
        client, call_id="call_shrink_1", assistant_id=assistant_id, utterance="AC not cooling."
    )
    first_offer = provider.results[-1].content["slots"][0]

    provider.queue_tool_round(
        [("select_appointment_slot", {"slot_id": first_offer["slot_id"]})]
    )
    provider.queue_tool_round([("book_appointment", {"slot_id": first_offer["slot_id"]})])
    provider.queue_reply(default_reply(message_to_customer="Confirmed."))
    await _turn(client, call_id="call_shrink_1", assistant_id=assistant_id, utterance="Yes.")
    assert provider.results[-1].content["success"] is True

    # A different caller checks availability afterwards.
    provider.queue_tool_round([("check_availability", {"service_name": "Air Conditioning Repair"})])
    provider.queue_reply(default_reply(message_to_customer="Times available."))
    await _turn(
        client, call_id="call_shrink_2", assistant_id=assistant_id, utterance="Any times today?"
    )

    second_offer_ids = [s["slot_id"] for s in provider.results[-1].content["slots"]]
    assert first_offer["slot_id"] not in second_offer_ids


@pytest.mark.asyncio(loop_scope="session")
async def test_an_emergency_call_produces_a_ticket_and_refuses_to_book(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    assistant_id = "asst_sched_emergency"
    token, org_id = await _register(client, "Scheduling Org D", "sched-d@example.com")
    await _seed_organization(client, token, org_id, assistant_id)

    provider.queue_tool_round(
        [("create_service_request", {**_LUCKY, "classification": "emergency"})]
    )
    provider.queue_tool_round([("book_appointment", {"date": "2026-12-01", "start_time": "10:00"})])
    provider.queue_reply(
        default_reply(message_to_customer="A dispatcher has been alerted and will call you.")
    )
    response = await _turn(
        client,
        call_id="call_emergency",
        assistant_id=assistant_id,
        utterance="I smell gas and my furnace is making a banging noise.",
    )
    assert response.status_code == 200, response.text

    creation = provider.results[-2].content
    assert creation["service_request_type"] == "emergency_ticket"
    assert creation["bookable"] is False

    refusal = provider.results[-1].content
    assert refusal["success"] is False
    assert refusal["error"] == "EMERGENCY_NOT_BOOKABLE"

    tickets = await client.get("/api/v1/dispatch/tickets", headers=_auth(token))
    assert len(tickets.json()) == 1
    appointments = await client.get("/api/v1/appointments", headers=_auth(token))
    assert appointments.json() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_booking_without_a_service_request_is_refused(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    assistant_id = "asst_sched_noreq"
    token, org_id = await _register(client, "Scheduling Org E", "sched-e@example.com")
    await _seed_organization(client, token, org_id, assistant_id)

    provider.queue_tool_round([("book_appointment", {"date": "2026-12-01", "start_time": "10:00"})])
    provider.queue_reply(default_reply(message_to_customer="Let me take your details first."))
    response = await _turn(
        client, call_id="call_noreq", assistant_id=assistant_id, utterance="Book me for Tuesday."
    )
    assert response.status_code == 200

    assert provider.results[-1].content["error"] == "NO_SERVICE_REQUEST"
    appointments = await client.get("/api/v1/appointments", headers=_auth(token))
    assert appointments.json() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_a_past_slot_is_refused_by_the_database_backed_engine(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    assistant_id = "asst_sched_past"
    token, org_id = await _register(client, "Scheduling Org F", "sched-f@example.com")
    await _seed_organization(client, token, org_id, assistant_id)

    provider.queue_tool_round([("create_service_request", _LUCKY)])
    provider.queue_tool_round([("book_appointment", {"date": "2020-01-06", "start_time": "10:00"})])
    provider.queue_reply(default_reply(message_to_customer="That date has passed."))
    await _turn(
        client, call_id="call_past", assistant_id=assistant_id, utterance="Come last January."
    )

    # Refused as never-offered: authorisation is checked before feasibility,
    # and `check_availability` can never return a date in 2020.
    assert provider.results[-1].content["error"] == "SLOT_NOT_OFFERED"
    conversation_id = await _only_conversation_id(client, token)
    row = await _appointment_row(conversation_id)
    assert row is not None and row.scheduled_start_at is None


@pytest.mark.asyncio(loop_scope="session")
async def test_tool_writes_never_cross_a_tenant_boundary(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    """The organization comes from the `VoiceLine` the call arrived on, never
    from anything the model emits."""
    assistant_id = "asst_sched_tenant"
    token_a, org_a = await _register(client, "Scheduling Org G", "sched-g@example.com")
    token_b, org_b = await _register(client, "Scheduling Org H", "sched-h@example.com")
    await _seed_organization(client, token_a, org_a, assistant_id)

    provider.queue_tool_round(
        [
            (
                "create_service_request",
                {**_LUCKY, "organization_id": str(org_b), "customer_phone": "999888777"},
            )
        ]
    )
    provider.queue_reply(default_reply(message_to_customer="Logged."))
    await _turn(
        client, call_id="call_tenant", assistant_id=assistant_id, utterance="AC broken."
    )

    assert provider.results[-1].content["success"] is True
    assert (await client.get("/api/v1/appointments", headers=_auth(token_a))).json() != []
    assert (await client.get("/api/v1/appointments", headers=_auth(token_b))).json() == []


async def _stream_turn(
    client: AsyncClient, *, call_id: str, assistant_id: str, utterance: str
) -> tuple[str, bool]:
    """One Vapi turn over SSE — the transport a real phone call uses.

    Returns the concatenated spoken text and whether an `endCall` tool call
    was emitted, both parsed out of the frames exactly as Vapi parses them."""
    response = await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json={
            "call": {
                "id": call_id,
                "assistantId": assistant_id,
                "customer": {"number": "+15551230000"},
            },
            "messages": [{"role": "user", "content": utterance}],
            "stream": True,
        },
        headers=_vapi(),
    )
    assert response.status_code == 200, response.text

    spoken: list[str] = []
    end_call = False
    for line in response.text.splitlines():
        if not line.startswith("data: ") or line.endswith("[DONE]"):
            continue
        frame = json.loads(line[len("data: ") :])
        delta = frame["choices"][0].get("delta") or {}
        if delta.get("content"):
            spoken.append(delta["content"])
        for call in delta.get("tool_calls") or []:
            if call.get("function", {}).get("name") == "endCall":
                end_call = True
    return "".join(spoken), end_call


@pytest.mark.asyncio(loop_scope="session")
async def test_the_streaming_transport_books_when_the_model_speaks_before_calling_tools(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    """The regression, end to end over SSE against Postgres.

    Every other test in this module uses the JSON transport, which was never
    affected — it inspects `message.tool_calls` before `message.content`. The
    streaming transport is the one a phone call uses, and the one where an
    announcement arriving before the tool-call deltas used to make the tool
    call disappear. That is what ended the 2026-08-22 call on
    `silence-timed-out`.
    """
    assistant_id = "asst_sched_streaming"
    token, org_id = await _register(client, "Scheduling Org J", "sched-j@example.com")
    await _seed_organization(client, token, org_id, assistant_id)

    # The model announces the work *and* requests the tools in the same
    # response — the shape that used to lose the tool call.
    provider.queue_tool_round(
        [("create_service_request", _LUCKY)],
        speak="Thanks. I'll get that logged for you now.",
    )
    provider.queue_tool_round(
        [("check_availability", {"service_name": "Air Conditioning Repair"})],
        speak="Let me look at the schedule.",
    )
    provider.queue_reply(default_reply(message_to_customer="Here are some times."))

    spoken, _ = await _stream_turn(
        client,
        call_id="call_streaming",
        assistant_id=assistant_id,
        utterance="My AC is running but it's not cooling.",
    )

    # The caller heard the model's own announcements, and never the system
    # holding phrase on top of them.
    assert "I'll get that logged for you now." in spoken
    assert "Let me check that for you, one moment." not in spoken

    creation = provider.results[0].content
    assert creation["success"] is True
    availability = provider.results[-1].content
    assert availability["success"] is True and availability["slots"]
    offered = availability["slots"][0]

    conversation_id = await _only_conversation_id(client, token)
    before = await _appointment_row(conversation_id)
    assert before is not None and before.scheduled_start_at is None

    # And the booking turn, also announced-then-called.
    provider.queue_tool_round(
        [
            ("select_appointment_slot", {"slot_id": offered["slot_id"]}),
            ("book_appointment", {"slot_id": offered["slot_id"]}),
        ],
        speak="Booking that in for you.",
    )
    provider.queue_reply(
        default_reply(
            message_to_customer=f"You're confirmed for {offered['label']}.",
            is_conversation_complete=True,
        )
    )
    spoken, end_call = await _stream_turn(
        client,
        call_id="call_streaming",
        assistant_id=assistant_id,
        utterance="That first time works.",
    )

    booking = provider.results[-1].content
    assert booking["success"] is True
    assert "confirmed" in spoken
    assert end_call is True

    after = await _appointment_row(conversation_id)
    assert after is not None
    assert str(after.id) == booking["appointment_id"]
    assert after.status.value == "scheduled"
    assert after.scheduled_start_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_the_booking_invariant_holds_across_every_outcome(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    """The whole point, checked two ways.

    First against this test's own call, correlating a `book_appointment`
    result directly to the row it claims to have created. Then against every
    appointment this module produced — including the refusals in the tests
    above, which persist because the schema fixture is module-scoped — as a
    database-level statement that needs no knowledge of which tool call made
    which row:

        scheduled_start_at IS NOT NULL  <=>  status = 'scheduled'

    and no two scheduled appointments in one organization overlap, which is
    what capacity of one means.
    """
    assistant_id = "asst_sched_invariant"
    token, org_id = await _register(client, "Scheduling Org I", "sched-i@example.com")
    await _seed_organization(client, token, org_id, assistant_id)

    provider.queue_tool_round([("create_service_request", _LUCKY)])
    provider.queue_tool_round([("check_availability", {"service_name": "Air Conditioning Repair"})])
    provider.queue_reply(default_reply(message_to_customer="Times available."))
    await _turn(
        client, call_id="call_invariant", assistant_id=assistant_id, utterance="AC not cooling."
    )
    offered = provider.results[-1].content["slots"][0]

    provider.queue_tool_round(
        [("select_appointment_slot", {"slot_id": offered["slot_id"]})]
    )
    provider.queue_tool_round([("book_appointment", {"slot_id": offered["slot_id"]})])
    provider.queue_reply(default_reply(message_to_customer="Confirmed."))
    await _turn(client, call_id="call_invariant", assistant_id=assistant_id, utterance="Yes.")

    booking = provider.results[-1].content
    assert booking["success"] is True
    booked_row = await _appointment_row(await _only_conversation_id(client, token))
    assert booked_row is not None
    assert str(booked_row.id) == booking["appointment_id"]
    assert booked_row.scheduled_start_at is not None

    # The database-wide statement, across every appointment this module made.
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(AppointmentModel))).scalars().all()

    assert len(rows) >= 5, "expected the earlier tests' appointments to still be present"
    for row in rows:
        if row.scheduled_start_at is None:
            assert row.status.value == "requested"
        else:
            assert row.status.value == "scheduled"
            assert row.scheduled_start_at > datetime(2020, 1, 1, tzinfo=timezone.utc)

    by_org: dict[uuid.UUID, list[AppointmentModel]] = {}
    for row in rows:
        if row.scheduled_start_at is not None:
            by_org.setdefault(row.organization_id, []).append(row)
    for booked in by_org.values():
        booked.sort(key=lambda r: r.scheduled_start_at)
        for earlier, later in zip(booked, booked[1:], strict=False):
            earlier_end = earlier.scheduled_start_at + timedelta(
                minutes=earlier.duration_minutes or 60
            )
            assert earlier_end <= later.scheduled_start_at, (
                "two scheduled appointments overlap in one organization, "
                "which capacity of one forbids"
            )


# --- Appointment consent, over the real webhook and real SQL -----------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_caller_must_choose_before_a_booking_is_written(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    """The observed failure, reproduced over HTTP against Postgres: the model
    offers times and books one without the caller ever answering.

    The unit suite proves the rule; this proves the wiring — that the turn
    index reaching the executor really is derived from the persisted message
    history, and that the refusal survives the whole request path rather than
    being an artefact of in-memory fakes.
    """
    assistant_id = "asst_sched_consent"
    token, org_id = await _register(client, "Scheduling Org L", "sched-l@example.com")
    await _seed_organization(client, token, org_id, assistant_id)

    # A throwaway call first, purely to learn what this organization's next
    # free slot is. The dates in this module are never hardcoded — they depend
    # on when the suite runs — and the attack below has to name a real offered
    # time or it would be refused for the wrong reason.
    provider.queue_tool_round([("create_service_request", _LUCKY)])
    provider.queue_tool_round([("check_availability", {"service_name": "Air Conditioning Repair"})])
    provider.queue_reply(default_reply(message_to_customer="I have some times."))
    await _turn(
        client,
        call_id="call_sched_consent_recon",
        assistant_id=assistant_id,
        utterance="AC not cooling.",
    )
    offered = provider.results[-1].content["slots"][0]

    # Now the real subject: ONE turn doing the whole flow — intake,
    # availability, and then, without the caller having said a word since the
    # times were computed, recording a "choice" and booking it. This is the
    # shape of the failure that motivated the invariant.
    provider.queue_tool_round([("create_service_request", _LUCKY)])
    provider.queue_tool_round([("check_availability", {"service_name": "Air Conditioning Repair"})])
    provider.queue_tool_round(
        [
            (
                "select_appointment_slot",
                {"date": offered["date"], "start_time": offered["start_time"]},
            )
        ]
    )
    provider.queue_tool_round(
        [("book_appointment", {"date": offered["date"], "start_time": offered["start_time"]})]
    )
    provider.queue_reply(default_reply(message_to_customer="Which time would you like?"))
    first = await _turn(
        client,
        call_id="call_sched_consent",
        assistant_id=assistant_id,
        utterance="My AC is running but it's not cooling.",
    )
    assert first.status_code == 200, first.text

    selection_attempt = provider.results[-2].content
    booking_attempt = provider.results[-1].content

    # The selection is refused on turn ordering: the offer and the "choice"
    # carry the same turn index, so the caller cannot have answered.
    assert selection_attempt["success"] is False
    assert selection_attempt["error"] == "SLOT_NOT_YET_HEARD"
    # And with no selection on record the booking has nothing to stand on.
    assert booking_attempt["success"] is False
    assert booking_attempt["error"] == "SLOT_NOT_SELECTED"

    conversation_id = await _consent_conversation_id(client, token)
    unbooked = await _appointment_row(conversation_id)
    assert unbooked is not None
    assert unbooked.scheduled_start_at is None
    assert unbooked.status.value == "requested"

    # Now the caller actually answers, on a later turn, and the same two calls
    # succeed — so the refusal above was about consent, not about the tools.
    provider.queue_tool_round(
        [("select_appointment_slot", {"slot_id": offered["slot_id"]})]
    )
    provider.queue_tool_round([("book_appointment", {"slot_id": offered["slot_id"]})])
    provider.queue_reply(
        default_reply(message_to_customer="Confirmed.", is_conversation_complete=True)
    )
    await _turn(
        client,
        call_id="call_sched_consent",
        assistant_id=assistant_id,
        utterance="The first one, please.",
    )

    booking = provider.results[-1].content
    assert booking["success"] is True, booking
    booked = await _appointment_row(conversation_id)
    assert booked is not None
    assert booked.status.value == "scheduled"
    assert booked.scheduled_start_at is not None

    # And the selection is on the row in Postgres, keyed to the turn it was
    # made in — the evidence a later audit would need.
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT slot_start_at, offered_turn_index, selected_at, "
                    "selected_turn_index FROM conversation_offered_slots "
                    "WHERE conversation_id = :cid ORDER BY slot_start_at"
                ),
                {"cid": conversation_id},
            )
        ).mappings().all()

    selected = [row for row in rows if row["selected_at"] is not None]
    assert len(selected) == 1, "exactly one live selection per conversation"
    assert selected[0]["selected_turn_index"] > selected[0]["offered_turn_index"]


@pytest.mark.asyncio(loop_scope="session")
async def test_postgres_refuses_a_second_live_selection_on_one_conversation(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    """The partial unique index, asserted against the real database.

    The in-memory fake mirrors "one live selection per conversation" by
    construction, which means the unit tests cannot fail if the real index is
    missing or misspelled. This can.
    """
    assistant_id = "asst_sched_index"
    token, org_id = await _register(client, "Scheduling Org K", "sched-k@example.com")
    await _seed_organization(client, token, org_id, assistant_id)

    provider.queue_tool_round([("create_service_request", _LUCKY)])
    provider.queue_tool_round([("check_availability", {"service_name": "Air Conditioning Repair"})])
    provider.queue_reply(default_reply(message_to_customer="I have some times."))
    await _turn(
        client,
        call_id="call_sched_index",
        assistant_id=assistant_id,
        utterance="AC not cooling.",
    )
    conversation_id = await _only_conversation_id(client, token)

    async with AsyncSessionLocal() as session:
        starts = (
            await session.execute(
                text(
                    "SELECT slot_start_at FROM conversation_offered_slots "
                    "WHERE conversation_id = :cid ORDER BY slot_start_at"
                ),
                {"cid": conversation_id},
            )
        ).scalars().all()
        assert len(starts) >= 2

        # Bypassing the repository on purpose: this is a test of the database
        # constraint, so it has to attempt exactly what a race could produce
        # if the clear-then-set pair were ever interleaved.
        await session.execute(
            text(
                "UPDATE conversation_offered_slots SET selected_at = now(), "
                "selected_turn_index = 4 WHERE conversation_id = :cid "
                "AND slot_start_at = :start"
            ),
            {"cid": conversation_id, "start": starts[0]},
        )
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "UPDATE conversation_offered_slots SET selected_at = now(), "
                    "selected_turn_index = 4 WHERE conversation_id = :cid "
                    "AND slot_start_at = :start"
                ),
                {"cid": conversation_id, "start": starts[1]},
            )
        await session.rollback()
