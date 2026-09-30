"""Emergency paging: the pieces that need no database.

Configuration validation, the reduction of a page to the one fact the
assistant may act on, the signed acknowledgement link, the provider adapters
(against `httpx.MockTransport` — no request ever leaves the process), the
production configuration guards, and the exact sentences the assistant is
given for each paging state.

Every number is in the 555-01xx block reserved for fictional use.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from app.application.services.prompt_builder import build_system_prompt
from app.application.services.voice_tool_executor import (
    _PAGING_STEPS,
    _paging_progress_line,
    _with_paging_step,
)
from app.core.config import Settings
from app.domain.paging.page import (
    CallerPagingState,
    EmergencyPage,
    PageMessage,
    PageNotification,
    PageNotificationStatus,
    PageStatus,
    PagingOutcome,
    RecipientRole,
    caller_paging_state,
    every_attempt_failed,
)
from app.domain.paging.settings import (
    InvalidPagingSettingsError,
    PagingChannel,
    mask_paging_number,
    validate_paging_settings,
)
from app.infrastructure.paging.ack_links import HmacAckLinkSigner
from app.infrastructure.paging.providers import (
    LoggingPagingProvider,
    NullPagingProvider,
    TwilioPagingProvider,
    build_paging_provider,
)
from tests.fakes import fake_settings

_PRIMARY = "+16305550101"
_BACKUP = "+16305550102"
_AI_LINE = "+16305550199"
_FROM = "+16305550100"
_NOW = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)


# --- Configuration ------------------------------------------------------------


def _validate(**overrides):
    values = dict(
        organization_id=uuid.uuid4(),
        is_enabled=True,
        primary_number=_PRIMARY,
        backup_number=_BACKUP,
        sms_enabled=True,
        voice_enabled=False,
        ack_timeout_seconds=300,
        forbidden_numbers=frozenset({_AI_LINE}),
    )
    values.update(overrides)
    return validate_paging_settings(**values)


def test_a_valid_configuration_is_normalised():
    settings = _validate(primary_number="+1 (630) 555-0101", voice_enabled=True)
    assert settings.primary_number == _PRIMARY
    assert settings.channels == (PagingChannel.SMS, PagingChannel.VOICE)
    assert settings.is_operational


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"primary_number": "555-0101"}, "E.164"),
        ({"primary_number": "+0630555010"}, "E.164"),
        ({"primary_number": "call me"}, "E.164"),
        ({"backup_number": "+1630555010212345678"}, "E.164"),
        ({"primary_number": _AI_LINE}, "AI voice line"),
        ({"backup_number": _AI_LINE}, "AI voice line"),
        ({"backup_number": _PRIMARY}, "different number"),
        ({"primary_number": None, "backup_number": None}, "primary on-call number"),
        ({"primary_number": None, "is_enabled": False}, "needs a primary"),
        ({"sms_enabled": False, "voice_enabled": False}, "at least one paging channel"),
        ({"ack_timeout_seconds": 59}, "between 60 and 3600"),
        ({"ack_timeout_seconds": 3601}, "between 60 and 3600"),
    ],
)
def test_malformed_configurations_are_refused_without_echoing_the_number(overrides, rule):
    with pytest.raises(InvalidPagingSettingsError, match=rule) as caught:
        _validate(**overrides)
    for number in (_PRIMARY, _BACKUP, _AI_LINE, "5550101"):
        assert number not in str(caught.value)


def test_paging_may_be_switched_off_without_fixing_the_numbers():
    settings = _validate(
        is_enabled=False, primary_number=None, backup_number=None,
        sms_enabled=False, voice_enabled=False,
    )
    assert not settings.is_operational


def test_a_missing_backup_is_allowed():
    assert _validate(backup_number=None).backup_number is None


def test_masking_keeps_only_the_country_code_and_last_four():
    assert mask_paging_number(_PRIMARY) == "+1••••••0101"
    assert mask_paging_number(None) is None


# --- What the assistant may act on -------------------------------------------------


def _page(status: PageStatus = PageStatus.PAGING_PRIMARY) -> EmergencyPage:
    return EmergencyPage(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), ticket_id=uuid.uuid4(), status=status,
        escalate_at=_NOW, ack_timeout_seconds=300, escalated_at=None, acknowledged_at=None,
        acknowledged_by_role=None, acknowledged_via=None, acknowledged_by_user_id=None,
        unresolved_at=None, unresolved_reason=None, created_at=_NOW, updated_at=_NOW,
    )


def _notification(
    status: PageNotificationStatus, role: RecipientRole = RecipientRole.PRIMARY
) -> PageNotification:
    return PageNotification(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), page_id=uuid.uuid4(), role=role,
        channel=PagingChannel.SMS, destination=_PRIMARY, status=status, attempts=1,
        next_attempt_at=None, lease_expires_at=None, provider="fake", provider_message_id=None,
        error_code=None, sent_at=None, created_at=_NOW, updated_at=_NOW,
    )


S = PageNotificationStatus


@pytest.mark.parametrize(
    ("page_status", "notifications", "expected"),
    [
        (None, [], CallerPagingState.OFF),
        (PageStatus.PAGING_PRIMARY, [], CallerPagingState.QUEUED),
        (PageStatus.PAGING_PRIMARY, [S.QUEUED], CallerPagingState.QUEUED),
        (PageStatus.PAGING_PRIMARY, [S.SENDING], CallerPagingState.QUEUED),
        (PageStatus.PAGING_PRIMARY, [S.RETRYING, S.FAILED], CallerPagingState.QUEUED),
        (PageStatus.PAGING_PRIMARY, [S.SENT, S.FAILED], CallerPagingState.SENT),
        (PageStatus.PAGING_BACKUP, [S.SENT, S.QUEUED], CallerPagingState.SENT),
        # Sent is never acknowledged, however long it has been.
        (PageStatus.UNRESOLVED, [S.SENT], CallerPagingState.SENT),
        (PageStatus.UNRESOLVED, [S.FAILED, S.FAILED], CallerPagingState.FAILED),
        (PageStatus.ACKNOWLEDGED, [S.FAILED, S.CANCELED], CallerPagingState.ACKNOWLEDGED),
        (PageStatus.ACKNOWLEDGED, [], CallerPagingState.ACKNOWLEDGED),
    ],
)
def test_caller_paging_state(page_status, notifications, expected):
    page = _page(page_status) if page_status else None
    assert caller_paging_state(page, [_notification(s) for s in notifications]) is expected


def test_unreachable_means_every_notification_to_that_recipient_failed():
    primary_failed = [_notification(S.FAILED), _notification(S.FAILED)]
    assert every_attempt_failed(primary_failed, RecipientRole.PRIMARY)
    assert not every_attempt_failed(primary_failed, RecipientRole.BACKUP)
    assert not every_attempt_failed(
        [_notification(S.FAILED), _notification(S.SENT)], RecipientRole.PRIMARY
    )
    assert not every_attempt_failed(
        [_notification(S.FAILED), _notification(S.RETRYING)], RecipientRole.PRIMARY
    )


def test_the_idempotency_key_is_stable_per_notification():
    notification = _notification(S.QUEUED)
    assert notification.idempotency_key == (
        f"emergency_page:{notification.page_id}:primary:sms"
    )


# --- The assistant's sentences ---------------------------------------------------------


def test_every_paging_state_forbids_claiming_anyone_is_on_the_way():
    for state, step in _PAGING_STEPS.items():
        assert "on the way" in step and "Do NOT" in step, state


def test_only_acknowledged_licenses_acknowledged_and_only_sent_licenses_paged():
    assert "may say the on-call technician has acknowledged" in _PAGING_STEPS[
        CallerPagingState.ACKNOWLEDGED
    ]
    for state in (CallerPagingState.QUEUED, CallerPagingState.SENT, CallerPagingState.FAILED):
        assert "may say the on-call technician has acknowledged" not in _PAGING_STEPS[state]
    assert "may say the on-call technician has been paged" in _PAGING_STEPS[CallerPagingState.SENT]
    assert "has been paged" not in _PAGING_STEPS[CallerPagingState.QUEUED].split("Do NOT")[0]


def test_paging_off_adds_nothing_to_the_existing_emergency_sentence():
    assert _with_paging_step("Existing.", CallerPagingState.OFF) == "Existing."
    assert _paging_progress_line(CallerPagingState.OFF) is None
    assert _with_paging_step("Existing.", CallerPagingState.SENT).startswith("Existing. ")


def test_the_prompt_binds_paging_claims_to_the_tool_result():
    prompt = build_system_prompt(
        profile=None, weekly_hours=[], hours_exceptions=[], services=[], service_areas=[],
        faqs=[], emergency_keywords=[], today=_NOW.date(), emergency_keyword_hint=False,
        tools_enabled=True,
    )
    assert '"on_call_paging"' in prompt
    assert "never tell the caller a technician is on the way" in prompt


# --- Signed acknowledgement links ------------------------------------------------------------


def test_a_link_round_trips_to_its_page_and_role():
    signer = HmacAckLinkSigner("unit-test-secret-value-long-enough")
    page_id = uuid.uuid4()
    for role in RecipientRole:
        token = signer.issue(page_id, role)
        assert len(token) == 44 and "=" not in token
        assert signer.verify(token) == (page_id, role)


def test_links_cannot_be_forged_swapped_or_altered():
    signer = HmacAckLinkSigner("unit-test-secret-value-long-enough")
    page_id = uuid.uuid4()
    token = signer.issue(page_id, RecipientRole.PRIMARY)

    assert HmacAckLinkSigner("another-secret-value-long-enough").verify(token) is None
    # Every single-character change is rejected.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    for index in range(len(token)):
        replacement = next(c for c in alphabet if c != token[index])
        altered = token[:index] + replacement + token[index + 1 :]
        assert signer.verify(altered) is None, index
    # The primary's link cannot be turned into the backup's.
    backup = signer.issue(page_id, RecipientRole.BACKUP)
    assert signer.verify(backup) == (page_id, RecipientRole.BACKUP)
    assert backup != token
    for garbage in ("", "x" * 65, "!!!!", "not base64 at all", token[:-4]):
        assert signer.verify(garbage) is None


# --- Providers (no network) --------------------------------------------------------------------


def _message(channel: PagingChannel, body: str = "EMERGENCY for Acme: smoke & <sparks>") -> PageMessage:
    return PageMessage(
        organization_id=uuid.uuid4(), ticket_id=uuid.uuid4(), channel=channel, to=_PRIMARY,
        body=body, idempotency_key="emergency_page:x:primary:sms",
    )


def _twilio(handler) -> TwilioPagingProvider:
    return TwilioPagingProvider(
        account_sid="AC_test_not_real", auth_token="not-a-real-token", from_number=_FROM,
        timeout_seconds=1.0, transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_twilio_sms_is_a_form_post_to_messages_and_accepted_is_only_accepted():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"sid": "SMabc123", "status": "queued"})

    receipt = await _twilio(handler).send(_message(PagingChannel.SMS))
    assert receipt.outcome is PagingOutcome.ACCEPTED
    assert receipt.provider_message_id == "SMabc123"
    [request] = seen
    assert str(request.url) == (
        "https://api.twilio.com/2010-04-01/Accounts/AC_test_not_real/Messages.json"
    )
    form = parse_qs(request.content.decode())
    assert form["To"] == [_PRIMARY] and form["From"] == [_FROM]
    assert form["Body"] == ["EMERGENCY for Acme: smoke & <sparks>"]
    assert request.headers["authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_twilio_voice_speaks_the_message_as_escaped_twiml():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"sid": "CAabc123"})

    receipt = await _twilio(handler).send(_message(PagingChannel.VOICE))
    assert receipt.outcome is PagingOutcome.ACCEPTED
    assert str(seen[0].url).endswith("/Calls.json")
    twiml = parse_qs(seen[0].content.decode())["Twiml"][0]
    assert twiml.startswith("<Response><Say>") and twiml.count("<Say>") == 2
    assert "smoke &amp; &lt;sparks&gt;" in twiml and "<sparks>" not in twiml


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "outcome", "error_code"),
    [
        (400, {"code": 21211, "message": "The 'To' number +16305550101 is not valid"},
         PagingOutcome.REJECTED, "twilio_21211"),
        (401, {"code": 20003}, PagingOutcome.REJECTED, "twilio_20003"),
        (429, {}, PagingOutcome.FAILED, "http_429"),
        (503, None, PagingOutcome.FAILED, "http_503"),
    ],
)
async def test_twilio_refusals_map_to_permanent_or_transient(status, body, outcome, error_code):
    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, text="Service Unavailable")
        return httpx.Response(status, json=body)

    receipt = await _twilio(handler).send(_message(PagingChannel.SMS))
    assert receipt.outcome is outcome
    assert receipt.error_code == error_code
    assert _PRIMARY not in (receipt.error_code or "")


@pytest.mark.asyncio
async def test_twilio_timeouts_and_network_errors_are_transient_and_never_raise():
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert (await _twilio(timeout).send(_message(PagingChannel.SMS))).error_code == "timeout"
    receipt = await _twilio(refused).send(_message(PagingChannel.SMS))
    assert (receipt.outcome, receipt.error_code) == (PagingOutcome.FAILED, "transport_error")


@pytest.mark.asyncio
async def test_twilio_without_credentials_reports_not_configured_and_sends_nothing():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("a request was made without credentials")

    provider = TwilioPagingProvider(
        account_sid="", auth_token=None, from_number=_FROM, timeout_seconds=1.0,
        transport=httpx.MockTransport(handler),
    )
    receipt = await provider.send(_message(PagingChannel.SMS))
    assert (receipt.outcome, receipt.error_code) == (
        PagingOutcome.NOT_CONFIGURED, "credentials_missing"
    )


@pytest.mark.asyncio
async def test_the_null_provider_never_claims_a_page():
    receipt = await NullPagingProvider().send(_message(PagingChannel.SMS))
    assert receipt.outcome is PagingOutcome.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_the_logging_provider_logs_no_number_or_body():
    from tests.log_capture import capture_events

    with capture_events() as events:
        receipt = await LoggingPagingProvider().send(_message(PagingChannel.SMS))
    assert receipt.outcome is PagingOutcome.ACCEPTED
    dumped = str(events)
    assert _PRIMARY not in dumped and "sparks" not in dumped


def test_the_provider_defaults_to_none():
    assert build_paging_provider(fake_settings()).name == "none"
    assert build_paging_provider(fake_settings(PAGING_PROVIDER="logging")).name == "logging"
    assert build_paging_provider(fake_settings(PAGING_PROVIDER="twilio")).name == "twilio"


# --- Production configuration guards --------------------------------------------------------------


def _production(**overrides) -> Settings:
    values = {
        "ENVIRONMENT": "production",
        "DEBUG": False,
        "JWT_SECRET_KEY": "a-generated-production-secret-that-is-long-enough-1234567890",
        "CORS_ORIGINS": ["https://app.example.com"],
        "VAPI_SERVER_SECRET": "a-real-webhook-shared-secret",
        "OPENAI_API_KEY": "sk-not-a-real-key-for-tests-only",
        "TWILIO_ACCOUNT_SID": "",
        "TWILIO_AUTH_TOKEN": "",
        "TWILIO_PHONE_NUMBER": "",
    }
    values.update(overrides)
    return Settings(**values)


def test_production_refuses_the_logging_paging_provider():
    with pytest.raises(ValueError, match="PAGING_PROVIDER='logging'"):
        _production(PAGING_PROVIDER="logging")


def test_production_twilio_paging_needs_credentials():
    with pytest.raises(ValueError, match="TWILIO_AUTH_TOKEN"):
        _production(
            PAGING_PROVIDER="twilio", TWILIO_ACCOUNT_SID="AC_not_real",
            TWILIO_PHONE_NUMBER=_FROM,
        )
    assert _production(
        PAGING_PROVIDER="twilio", TWILIO_ACCOUNT_SID="AC_not_real",
        TWILIO_AUTH_TOKEN="not-a-real-token", TWILIO_PHONE_NUMBER=_FROM,
    ).PAGING_PROVIDER == "twilio"


def test_production_ack_links_must_be_https():
    with pytest.raises(ValueError, match="PAGING_ACK_BASE_URL"):
        _production(PAGING_ACK_BASE_URL="http://essr.example.com")
    assert _production(PAGING_ACK_BASE_URL="https://essr.example.com/").PAGING_ACK_BASE_URL == (
        "https://essr.example.com"
    )


def test_paging_defaults_to_off_in_production():
    assert _production().PAGING_PROVIDER == "none"
