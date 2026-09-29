"""Human fallback: the transfer state machine, destination rules, the service's
decision flow (with in-memory fakes and a scriptable provider), and the Vapi
Live Call Control adapter over a mock HTTP transport.

Scenario numbers match the Phase B brief:
 1 valid destination        2 missing destination     3 provider rejects
 4 provider timeout         5 caller hangs up first   6 human before intake
 7 human during emergency   8 changes mind (see integration test)
 9 initiated successfully  10 unavailable after hours
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any

import httpx
import pytest

from app.application.services.call_transfer_service import ANNOUNCEMENTS, CallTransferService
from app.domain.call_transfer.attempt import (
    CallTransferAttempt,
    DestinationKind,
    InvalidTransferTransitionError,
    TransferFailure,
    TransferReason,
    TransferStatus,
)
from app.domain.call_transfer.port import CallTransferPort, TransferInitiation
from app.domain.call_transfer.resolution import (
    NoDestination,
    ResolvedDestination,
    is_open_at,
    resolve_destination,
)
from app.domain.call_transfer.settings import (
    CallTransferSettings,
    InvalidTransferNumberError,
    mask_number,
    validate_transfer_number,
)
from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.infrastructure.telephony.vapi_call_control import (
    VapiCallControlTransfer,
    is_vapi_control_url,
)

ORG = uuid.uuid4()
CONV = uuid.uuid4()
OFFICE = "+15550101000"
ON_CALL = "+15550102000"
AI_LINE = "+15550199000"
CONTROL = "https://phone-call-websocket.aws-us-west-2-backend-production1.vapi.ai/abc123/control"


def _settings(**overrides: Any) -> CallTransferSettings:
    base = dict(organization_id=ORG, business_hours_number=OFFICE, after_hours_number=ON_CALL,
                transfer_emergencies=False, is_enabled=True)
    base.update(overrides)
    return CallTransferSettings(**base)  # type: ignore[arg-type]


def _weekly(open_: time = time(8), close: time = time(17)) -> list[WeeklyHours]:
    return [WeeklyHours(id=uuid.uuid4(), organization_id=ORG, day_of_week=d, is_closed=d >= 5,
                        open_time=open_ if d < 5 else None, close_time=close if d < 5 else None)
            for d in range(7)]


# --- state machine -----------------------------------------------------------


def _attempt() -> CallTransferAttempt:
    return CallTransferAttempt(id=uuid.uuid4(), organization_id=ORG, conversation_id=CONV,
                               reason=TransferReason.CALLER_REQUESTED, is_emergency=False)


def test_state_machine_allows_only_the_documented_edges():
    a = _attempt()
    assert a.status is TransferStatus.REQUESTED
    resolved = a.resolve(DestinationKind.BUSINESS_HOURS, OFFICE)
    assert resolved.status is TransferStatus.DESTINATION_RESOLVED
    assert resolved.initiated().status is TransferStatus.INITIATED
    assert resolved.failed("X").status is TransferStatus.FAILED
    assert a.unavailable("Y").status is TransferStatus.UNAVAILABLE

    # No shortcut from "requested" straight to "initiated": a transfer that
    # never resolved a destination cannot claim to have started.
    with pytest.raises(InvalidTransferTransitionError):
        a.initiated()
    with pytest.raises(InvalidTransferTransitionError):
        resolved.unavailable("Z")
    for terminal in (resolved.initiated(), resolved.failed("X"), a.unavailable("Y")):
        assert terminal.is_terminal
        with pytest.raises(InvalidTransferTransitionError):
            terminal.failed("again")


def test_there_is_no_answered_state():
    # Nothing in the stack can confirm a person answered; the states must
    # not offer a way to claim it.
    assert {s.value for s in TransferStatus} == {
        "requested", "destination_resolved", "initiated", "unavailable", "failed"}


# --- settings validation ---------------------------------------------------------


def test_transfer_numbers_are_normalised_e164_and_never_the_ai_line():
    assert validate_transfer_number("+1 (555) 010-1000", forbidden=frozenset()) == OFFICE
    assert validate_transfer_number("", forbidden=frozenset()) is None
    assert validate_transfer_number(None, forbidden=frozenset()) is None
    for bad in ("5550101000", "+0123456789", "call the office", "+1555"):
        with pytest.raises(InvalidTransferNumberError):
            validate_transfer_number(bad, forbidden=frozenset())
    with pytest.raises(InvalidTransferNumberError, match="AI voice line"):
        validate_transfer_number("+1 555 019 9000", forbidden=frozenset({AI_LINE}))


def test_masked_number_hides_all_but_the_last_four():
    masked = mask_number(ON_CALL)
    assert masked is not None and masked.endswith("2000") and "010" not in masked


# --- hours + destination resolution ------------------------------------------------


def test_is_open_at_follows_weekly_hours_and_dated_exceptions():
    weekly = _weekly()
    tuesday = datetime(2026, 9, 29, 10, 0)
    assert is_open_at(tuesday, weekly, []) is True
    assert is_open_at(tuesday.replace(hour=17), weekly, []) is False  # [open, close)
    assert is_open_at(datetime(2026, 10, 3, 10), weekly, []) is False  # Saturday
    closed_day = HoursException(id=uuid.uuid4(), organization_id=ORG, date=date(2026, 9, 29),
                                is_closed=True, open_time=None, close_time=None, label="Holiday")
    assert is_open_at(tuesday, weekly, [closed_day]) is False
    assert is_open_at(tuesday, [], []) is None  # hours never configured


@pytest.mark.parametrize(
    ("settings", "open_now", "expected"),
    [
        (None, True, NoDestination(TransferFailure.NOT_CONFIGURED)),
        (_settings(is_enabled=False), True, NoDestination(TransferFailure.DISABLED)),
        (_settings(), True, ResolvedDestination(DestinationKind.BUSINESS_HOURS, OFFICE)),
        (_settings(business_hours_number=None), True,
         ResolvedDestination(DestinationKind.AFTER_HOURS, ON_CALL)),
        (_settings(), False, ResolvedDestination(DestinationKind.AFTER_HOURS, ON_CALL)),
        # 10: after hours with no on-call line is NOT sent to the closed office.
        (_settings(after_hours_number=None), False, NoDestination(TransferFailure.NO_DESTINATION_NOW)),
        (_settings(), None, ResolvedDestination(DestinationKind.BUSINESS_HOURS, OFFICE)),
        (_settings(business_hours_number=None, after_hours_number=None), True,
         NoDestination(TransferFailure.NO_DESTINATION_NOW)),
    ],
)
def test_destination_resolution(settings, open_now, expected):
    assert resolve_destination(settings, open_now=open_now, ai_line_numbers=frozenset()) == expected


def test_resolution_rechecks_the_ai_line_at_call_time():
    # A voice line mapped to the office number AFTER settings were saved.
    result = resolve_destination(_settings(), open_now=True, ai_line_numbers=frozenset({OFFICE}))
    assert result == NoDestination(TransferFailure.DESTINATION_IS_AI_LINE)


# --- the service, with fakes -------------------------------------------------------


class FakeSettingsRepo:
    def __init__(self, settings: CallTransferSettings | None) -> None:
        self.value = settings

    async def get(self, organization_id):
        return self.value


class FakeAttemptRepo:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, CallTransferAttempt] = {}
        self.history: list[tuple[uuid.UUID, TransferStatus]] = []

    async def add(self, attempt):
        self.rows[attempt.id] = attempt
        self.history.append((attempt.id, attempt.status))
        return attempt

    async def save(self, attempt):
        assert attempt.id in self.rows, "save() before add()"
        self.rows[attempt.id] = attempt
        self.history.append((attempt.id, attempt.status))
        return attempt

    async def get_initiated_for_conversation(self, conversation_id):
        return next((a for a in self.rows.values() if a.conversation_id == conversation_id
                     and a.status is TransferStatus.INITIATED), None)

    async def list_for_conversation(self, conversation_id):
        return [a for a in self.rows.values() if a.conversation_id == conversation_id]


@dataclass
class FakeProfile:
    timezone: str = "America/Chicago"


class FakeProfiles:
    async def get_by_organization_id(self, organization_id):
        return FakeProfile()


class FakeHours:
    def __init__(self, weekly: list[WeeklyHours]) -> None:
        self.weekly = weekly

    async def get_weekly(self, organization_id):
        return self.weekly

    async def list_exceptions(self, organization_id):
        return []


@dataclass
class FakeLine:
    phone_number: str | None


class FakeVoiceLines:
    def __init__(self, number: str | None = AI_LINE) -> None:
        self.number = number

    async def get_by_organization_id(self, organization_id):
        return FakeLine(self.number)


class FakeTickets:
    def __init__(self, has_ticket: bool = False) -> None:
        self.has_ticket = has_ticket

    async def get_by_conversation_id(self, conversation_id):
        return object() if self.has_ticket else None


@dataclass
class FakePort(CallTransferPort):
    outcome: TransferInitiation = field(default_factory=lambda: TransferInitiation(True))
    calls: list[dict[str, str]] = field(default_factory=list)

    async def transfer(self, *, call_control, destination_number, announcement):
        self.calls.append({"call_control": call_control, "number": destination_number,
                           "announcement": announcement})
        return self.outcome


# Tuesday 2026-09-29 15:00 UTC = 10:00 in Chicago (open); 04:00 UTC = 23:00 Monday (closed).
IN_HOURS = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
AFTER_HOURS = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)


def _service(*, settings=_settings(), port=None, tickets=False, clock=IN_HOURS, weekly=None):
    attempts = FakeAttemptRepo()
    port = port or FakePort()
    svc = CallTransferService(
        settings_repository=FakeSettingsRepo(settings),  # type: ignore[arg-type]
        attempt_repository=attempts,  # type: ignore[arg-type]
        business_profile_repository=FakeProfiles(),  # type: ignore[arg-type]
        business_hours_repository=FakeHours(_weekly() if weekly is None else weekly),  # type: ignore[arg-type]
        voice_line_repository=FakeVoiceLines(),  # type: ignore[arg-type]
        emergency_ticket_repository=FakeTickets(tickets),  # type: ignore[arg-type]
        transfer_port=port,
        clock=lambda: clock,
    )
    return svc, attempts, port


async def _transfer(svc, *, reason=TransferReason.CALLER_REQUESTED, is_emergency=False,
                    call_control=CONTROL):
    return await svc.transfer(organization_id=ORG, conversation_id=CONV, reason=reason,
                              is_emergency=is_emergency, call_control=call_control)


def _only(attempts: FakeAttemptRepo) -> CallTransferAttempt:
    [attempt] = attempts.rows.values()
    return attempt


@pytest.mark.asyncio
async def test_1_and_9_valid_destination_is_initiated_after_provider_acceptance():
    svc, attempts, port = _service()
    result = await _transfer(svc)
    assert result["success"] is True and result["transfer_status"] == "initiated"
    assert result["destination"] == "business_hours"
    assert port.calls == [{"call_control": CONTROL, "number": OFFICE,
                           "announcement": ANNOUNCEMENTS[DestinationKind.BUSINESS_HOURS]}]
    # Every state it passed through was recorded, in order, before and after the provider call.
    statuses = [s for _, s in attempts.history]
    assert statuses == [TransferStatus.REQUESTED, TransferStatus.DESTINATION_RESOLVED,
                        TransferStatus.INITIATED]
    # The model is told the caller already heard the handoff, and never to claim an answer.
    assert "ALREADY heard" in result["next_step"] and "Never say" in result["next_step"]
    # The number itself never reaches the model.
    assert OFFICE not in str(result)


@pytest.mark.asyncio
async def test_9_a_second_request_never_moves_a_call_that_is_already_moving():
    svc, attempts, port = _service()
    await _transfer(svc)
    again = await _transfer(svc)
    assert again["success"] is True and again["already_in_progress"] is True
    assert len(port.calls) == 1 and len(attempts.rows) == 1


@pytest.mark.asyncio
async def test_2_missing_destination_is_unavailable_and_nothing_is_dialled():
    svc, attempts, port = _service(settings=None)
    result = await _transfer(svc)
    assert result == {**result, "success": False, "error": TransferFailure.NOT_CONFIGURED,
                      "transfer_status": "unavailable"}
    assert "can't connect them to a person right now" in result["next_step"]
    assert port.calls == [] and _only(attempts).status is TransferStatus.UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [TransferFailure.PROVIDER_REJECTED, TransferFailure.PROVIDER_TIMEOUT,
                                  TransferFailure.PROVIDER_ERROR])
async def test_3_4_provider_rejection_or_timeout_fails_honestly(code):
    svc, attempts, _ = _service(port=FakePort(TransferInitiation(False, code)))
    result = await _transfer(svc)
    assert result["success"] is False and result["error"] == code
    assert result["transfer_status"] == "failed"
    assert "No transfer happened" in result["next_step"]
    assert _only(attempts).status is TransferStatus.FAILED


@pytest.mark.asyncio
async def test_5_caller_already_gone_is_a_failed_transfer_not_an_error():
    # When the caller hangs up first, Vapi refuses the control request.
    svc, attempts, _ = _service(port=FakePort(TransferInitiation(False, TransferFailure.PROVIDER_REJECTED)))
    result = await _transfer(svc)
    assert result["success"] is False and _only(attempts).status is TransferStatus.FAILED


@pytest.mark.asyncio
async def test_6_a_caller_asking_for_a_person_before_intake_is_not_refused():
    svc, _, port = _service(tickets=False)
    result = await _transfer(svc, reason=TransferReason.CALLER_REQUESTED)
    assert result["success"] is True and len(port.calls) == 1


@pytest.mark.asyncio
async def test_7_human_request_during_emergency_is_honoured_even_without_a_ticket():
    svc, attempts, port = _service(tickets=False)
    result = await _transfer(svc, reason=TransferReason.CALLER_REQUESTED, is_emergency=True)
    assert result["success"] is True and _only(attempts).is_emergency is True


@pytest.mark.asyncio
async def test_7_emergency_policy_transfer_requires_the_ticket_first():
    svc, attempts, port = _service(settings=_settings(transfer_emergencies=True), tickets=False)
    result = await _transfer(svc, reason=TransferReason.EMERGENCY_POLICY, is_emergency=True)
    assert result["error"] == TransferFailure.EMERGENCY_TICKET_REQUIRED
    assert "create_service_request first" in result["next_step"]
    assert port.calls == []

    svc, _, port = _service(settings=_settings(transfer_emergencies=True), tickets=True)
    assert (await _transfer(svc, reason=TransferReason.EMERGENCY_POLICY, is_emergency=True))["success"]
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_7_emergency_policy_is_refused_when_the_business_has_not_enabled_it():
    svc, _, port = _service(settings=_settings(transfer_emergencies=False), tickets=True)
    result = await _transfer(svc, reason=TransferReason.EMERGENCY_POLICY, is_emergency=True)
    assert result["error"] == TransferFailure.DISABLED and port.calls == []


@pytest.mark.asyncio
async def test_emergency_failure_tells_the_model_the_ticket_still_stands():
    svc, _, _ = _service(settings=None)
    result = await _transfer(svc, is_emergency=True)
    assert "emergency request was already recorded" in result["next_step"]


@pytest.mark.asyncio
async def test_10_after_hours_goes_to_on_call_or_is_honestly_unavailable():
    svc, _, port = _service(clock=AFTER_HOURS)
    result = await _transfer(svc)
    assert result["destination"] == "after_hours"
    assert port.calls[0]["number"] == ON_CALL
    assert port.calls[0]["announcement"] == ANNOUNCEMENTS[DestinationKind.AFTER_HOURS]

    svc, attempts, port = _service(settings=_settings(after_hours_number=None), clock=AFTER_HOURS)
    result = await _transfer(svc)
    assert result["error"] == TransferFailure.NO_DESTINATION_NOW and port.calls == []


@pytest.mark.asyncio
async def test_no_live_call_control_is_unavailable_never_a_pretend_transfer():
    svc, attempts, port = _service()
    result = await _transfer(svc, call_control=None)
    assert result["error"] == TransferFailure.CALL_CONTROL_UNAVAILABLE and port.calls == []


@pytest.mark.asyncio
async def test_announcements_never_claim_that_someone_answered():
    for sentence in ANNOUNCEMENTS.values():
        lowered = sentence.lower()
        assert "connecting" in lowered
        for claim in ("answered", "on the line", "has joined", "on the way", "is here"):
            assert claim not in lowered


@pytest.mark.asyncio
async def test_progress_note_orders_ticket_before_policy_transfer():
    svc, _, _ = _service(settings=_settings(transfer_emergencies=True))
    assert await svc.progress_note(organization_id=ORG, conversation_id=CONV,
                                   has_emergency_ticket=False) is None
    note = await svc.progress_note(organization_id=ORG, conversation_id=CONV, has_emergency_ticket=True)
    assert note is not None and "emergency_policy" in note
    await _transfer(svc)
    note = await svc.progress_note(organization_id=ORG, conversation_id=CONV, has_emergency_ticket=True)
    assert note is not None and "ALREADY been handed" in note


# --- the Vapi adapter -------------------------------------------------------------


def test_only_https_vapi_control_urls_are_used():
    assert is_vapi_control_url(CONTROL)
    for bad in (None, "", "http://x.vapi.ai/c", "https://vapi.ai.evil.com/c", "https://169.254.169.254/",
                "https://evilvapi.ai/c", "not a url"):
        assert not is_vapi_control_url(bad)


@pytest.mark.asyncio
async def test_adapter_sends_the_live_call_control_transfer_command():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    port = VapiCallControlTransfer(transport=httpx.MockTransport(handler))
    result = await port.transfer(call_control=CONTROL, destination_number=OFFICE, announcement="Hi")
    assert result == TransferInitiation(True)
    [request] = seen
    assert request.method == "POST" and str(request.url) == CONTROL
    import json
    assert json.loads(request.content) == {
        "type": "transfer", "destination": {"type": "number", "number": OFFICE}, "content": "Hi"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "code"), [
    (httpx.Response(400, json={"error": "call ended"}), TransferFailure.PROVIDER_REJECTED),
    (httpx.Response(500), TransferFailure.PROVIDER_REJECTED),
    (httpx.Response(302, headers={"location": "https://elsewhere.example"}), TransferFailure.PROVIDER_REJECTED),
])
async def test_adapter_reports_non_2xx_as_not_accepted(response, code):
    port = VapiCallControlTransfer(transport=httpx.MockTransport(lambda r: response))
    assert await port.transfer(call_control=CONTROL, destination_number=OFFICE,
                               announcement="x") == TransferInitiation(False, code)


@pytest.mark.asyncio
async def test_adapter_never_raises_on_timeout_or_transport_error(capsys):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def broken(request):
        raise httpx.ConnectError(f"could not reach {request.url}", request=request)

    for handler, code in ((timeout, TransferFailure.PROVIDER_TIMEOUT), (broken, TransferFailure.PROVIDER_ERROR)):
        port = VapiCallControlTransfer(transport=httpx.MockTransport(handler))
        assert await port.transfer(call_control=CONTROL, destination_number=OFFICE,
                                   announcement="x") == TransferInitiation(False, code)
    # The control URL is a capability over the live call: never logged.
    assert CONTROL not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_adapter_refuses_a_non_vapi_url_without_sending_anything():
    sent: list[httpx.Request] = []
    port = VapiCallControlTransfer(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    result = await port.transfer(call_control="https://169.254.169.254/latest", destination_number=OFFICE,
                                 announcement="x")
    assert result == TransferInitiation(False, TransferFailure.CALL_CONTROL_UNAVAILABLE) and sent == []
