"""Validation of emergency-notification destinations.

This is a security control, not input tidying, and it guards two different
things at once:

1. **Confidentiality of the payload.** The alert carries the caller's name,
   callback number and address. https is therefore not a preference.
2. **Server-side request forgery.** The *server* makes this request, from
   inside the deployment. An unvalidated destination is a primitive for
   reaching things the internet cannot: the cloud metadata endpoint
   (169.254.169.254, identical on AWS, GCP and Azure), the deployment's own
   Postgres on 10/8, an admin surface on localhost. The attacker here is an
   authenticated organization admin — a real threat model for a
   multi-tenant product, because signing up is not a high bar.

The masking tests matter for a third reason: a Slack or Teams
incoming-webhook URL is the entire credential, so anything that renders one
back to a human is a leak.
"""

from __future__ import annotations

import pytest

from app.domain.notifications.settings import (
    InvalidNotificationDestinationError,
    mask_destination,
    validate_webhook_destination,
)

# --- accepted ----------------------------------------------------------------


@pytest.mark.parametrize(
    "destination",
    [
        "https://hooks.example.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
        "https://example.com/webhooks/emergency",
        "https://events.pagerduty.com/v2/enqueue",
        "https://teams.example.com/webhook/abc123/IncomingWebhook/def456",
    ],
)
def test_public_https_endpoints_are_accepted(destination: str):
    assert validate_webhook_destination(destination) == destination


def test_surrounding_whitespace_is_trimmed():
    """Operators paste these out of another tool; a trailing newline should
    not be the reason a business's emergency alerting is misconfigured."""
    assert (
        validate_webhook_destination("  https://example.com/hook  ")
        == "https://example.com/hook"
    )


# --- transport ---------------------------------------------------------------


def test_plain_http_is_refused_by_default():
    with pytest.raises(InvalidNotificationDestinationError, match="https"):
        validate_webhook_destination("http://example.com/hook")


def test_plain_http_is_allowed_only_when_explicitly_permitted():
    """Development and tests need a local listener over http to exercise the
    real provider at all. Production never passes this flag — see
    `NotificationSettingsService.configure`, which derives it from the
    environment rather than from the request."""
    assert (
        validate_webhook_destination("http://example.com/hook", allow_insecure=True)
        == "http://example.com/hook"
    )


@pytest.mark.parametrize(
    "destination",
    ["ftp://example.com/hook", "file:///etc/passwd", "gopher://example.com/", "javascript:alert(1)"],
)
def test_non_http_schemes_are_refused(destination: str):
    with pytest.raises(InvalidNotificationDestinationError):
        validate_webhook_destination(destination)


def test_embedded_credentials_are_refused():
    """A second secret in a field we mask for display and never intend to
    hold."""
    with pytest.raises(InvalidNotificationDestinationError, match="username or password"):
        validate_webhook_destination("https://user:hunter2@example.com/hook")


# --- SSRF --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("destination", "why"),
    [
        ("https://169.254.169.254/latest/meta-data/", "cloud metadata service"),
        ("https://127.0.0.1/hook", "loopback"),
        ("https://localhost/hook", "loopback by name"),
        ("https://10.0.0.5/hook", "private 10/8"),
        ("https://192.168.1.10/hook", "private 192.168/16"),
        ("https://172.16.4.4/hook", "private 172.16/12"),
        ("https://[::1]/hook", "IPv6 loopback"),
        ("https://0.0.0.0/hook", "unspecified"),
    ],
)
def test_internal_addresses_are_refused(destination: str, why: str):
    with pytest.raises(InvalidNotificationDestinationError, match="public address"):
        validate_webhook_destination(destination), why


def test_internal_addresses_are_refused_even_when_http_is_permitted():
    """The insecure-transport escape hatch must not double as an SSRF escape
    hatch — otherwise enabling local development would open the metadata
    endpoint."""
    with pytest.raises(InvalidNotificationDestinationError, match="public address"):
        validate_webhook_destination(
            "http://169.254.169.254/latest/meta-data/", allow_insecure=True
        )


# --- shape -------------------------------------------------------------------


@pytest.mark.parametrize("destination", ["", "   ", "\n"])
def test_an_empty_destination_is_refused(destination: str):
    with pytest.raises(InvalidNotificationDestinationError, match="required"):
        validate_webhook_destination(destination)


def test_a_destination_without_a_host_is_refused():
    with pytest.raises(InvalidNotificationDestinationError):
        validate_webhook_destination("https:///hook")


def test_an_absurdly_long_destination_is_refused():
    with pytest.raises(InvalidNotificationDestinationError, match="at most"):
        validate_webhook_destination("https://example.com/" + "a" * 3000)


# --- masking -----------------------------------------------------------------


def test_masking_keeps_the_host_and_hides_the_secret():
    """Slack's webhook secret lives entirely in the path, so the host is safe
    to show and the path is not. An operator's real question is "is this the
    right service?", which the host answers."""
    hidden = mask_destination(
        "https://hooks.example.com/services/T00000000/B00000000/SUPERSECRETTOKEN"
    )

    assert "hooks.example.com" in hidden
    assert "SUPERSECRETTOKEN" not in hidden
    assert "T00000000" not in hidden
    assert "B00000000" not in hidden


def test_masking_leaves_enough_to_tell_two_endpoints_apart():
    first = mask_destination("https://example.com/hooks/aaaa1111")
    second = mask_destination("https://example.com/hooks/bbbb2222")

    assert first != second


def test_masking_never_raises_on_junk():
    """This runs on the way out of the database into an HTTP response. A
    malformed stored value must degrade to an unhelpful string, not to a 500
    on the settings page."""
    assert mask_destination("not a url at all")
    assert mask_destination("")
