"""`is_valid_vapi_secret` is the single comparison behind both the webhook
route guard (`deps.verify_vapi_secret`) and the rate-limit exemption
(`RateLimitMiddleware`). The last test pins the property that matters
most: the two call sites must agree on every input, or the exemption and
the guard would disagree about who is authenticated."""

from __future__ import annotations

import pytest

from app.api.deps import verify_vapi_secret
from app.core.config import get_settings
from app.domain.exceptions import InvalidTokenError
from app.infrastructure.security.vapi_secret import is_valid_vapi_secret

_SECRET = "a-real-looking-webhook-secret-value"


def test_matching_secret_is_valid():
    assert is_valid_vapi_secret(_SECRET, _SECRET) is True


def test_wrong_secret_is_rejected():
    assert is_valid_vapi_secret("wrong", _SECRET) is False


def test_missing_header_is_rejected():
    assert is_valid_vapi_secret(None, _SECRET) is False
    assert is_valid_vapi_secret("", _SECRET) is False


def test_unconfigured_expected_secret_fails_closed():
    """A deployment that never set VAPI_SERVER_SECRET must be unreachable,
    not universally reachable."""
    assert is_valid_vapi_secret(_SECRET, None) is False
    assert is_valid_vapi_secret(_SECRET, "") is False
    assert is_valid_vapi_secret(None, None) is False


def test_non_ascii_header_is_rejected_rather_than_raising():
    """`hmac.compare_digest` raises TypeError on non-ASCII str, and the
    header is attacker-controlled — inside middleware that would surface
    as a 500 instead of a rate-limit decision."""
    assert is_valid_vapi_secret("secret-with-emoji-\U0001f600", _SECRET) is False


@pytest.mark.parametrize(
    "provided",
    [_SECRET, "wrong", "", None, "secret-with-emoji-\U0001f600"],
)
def test_route_guard_and_predicate_agree_on_every_input(provided):
    settings = get_settings().model_copy(update={"VAPI_SERVER_SECRET": _SECRET})

    predicate_says_valid = is_valid_vapi_secret(provided, settings.VAPI_SERVER_SECRET)

    try:
        verify_vapi_secret(x_vapi_secret=provided, settings=settings)
        guard_says_valid = True
    except InvalidTokenError:
        guard_says_valid = False

    assert predicate_says_valid is guard_says_valid
