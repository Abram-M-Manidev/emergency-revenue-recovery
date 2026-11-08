"""Launch tooling (L1–L4, L7): the preflight's settings rules, the
acceptance checker's Vapi timeline, and the transfer-not-connected signal."""

from __future__ import annotations

import pytest

from app.cli.call_check import vapi_timeline
from app.cli.preflight import FAIL, WARN, settings_checks
from app.domain.follow_ups import transfer_not_connected
from tests.fakes import fake_settings


def _codes(checks, level):
    return {c.code for c in checks if c.level == level}


def test_production_with_alerts_off_fails_preflight():
    settings = fake_settings(NOTIFICATION_PROVIDER="none")
    object.__setattr__(settings, "ENVIRONMENT", "production")
    assert "alerts_provider_off" in _codes(settings_checks(settings), FAIL)


def test_outside_production_alerts_off_is_only_a_warning():
    checks = settings_checks(fake_settings(NOTIFICATION_PROVIDER="none"))
    assert "alerts_provider_off" in _codes(checks, WARN)
    assert "alerts_provider_off" not in _codes(checks, FAIL)


def test_paging_without_an_ack_link_base_fails():
    settings = fake_settings(PAGING_PROVIDER="logging", PAGING_ACK_BASE_URL=None)
    assert "paging_ack_url_missing" in _codes(settings_checks(settings), FAIL)


def test_the_preflight_never_prints_secret_values():
    settings = fake_settings(VAPI_API_KEY="vapi-secret-value-123", OPENAI_API_KEY="sk-test-abc")
    rendered = " ".join(f"{c.code} {c.detail}" for c in settings_checks(settings))
    assert "vapi-secret-value-123" not in rendered
    assert "sk-test-abc" not in rendered


_CALL = {
    "status": "ended",
    "endedReason": "customer-ended-call",
    "startedAt": "2026-10-31T03:02:28.375Z",
    "artifact": {
        "recordingUrl": "https://example.invalid/r.wav",
        "messages": [
            {"role": "system", "message": "prompt", "secondsFromStart": 0},
            {
                "role": "bot",
                "message": "Thanks for calling. You're speaking with an automated assistant "
                "for Example Co, and this call is recorded. How can I help?",
                "secondsFromStart": 2.02,
            },
            {"role": "user", "message": "My name is Dana and my number is 6305550184",
             "secondsFromStart": 14.69},
        ],
    },
}


def test_the_timeline_reports_notice_order_without_any_transcript_text():
    lines = "\n".join(vapi_timeline(_CALL))

    assert "recording      present" in lines
    assert "at 2.02s  ai_notice=True  recording_notice=True" in lines
    assert "notice before caller spoke: True" in lines
    for private in ("Dana", "6305550184", "Example Co", "How can I help"):
        assert private not in lines


def test_a_caller_speaking_before_the_notice_is_reported():
    call = {
        "artifact": {
            "messages": [
                {"role": "user", "message": "hello?", "secondsFromStart": 0.8},
                {"role": "bot", "message": "Hi there.", "secondsFromStart": 3.1},
            ]
        }
    }
    lines = "\n".join(vapi_timeline(call))
    assert "ai_notice=False" in lines
    assert "notice before caller spoke: False" in lines


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("call.forwarding.no-answer", True),
        ("call.forwarding.operator-busy", True),
        ("call.in-progress.error-transfer-failed", True),
        ("customer-ended-call", False),
        ("assistant-forwarded-call", False),
        (None, False),
    ],
)
def test_transfer_not_connected_reasons(reason, expected):
    assert transfer_not_connected(reason) is expected
