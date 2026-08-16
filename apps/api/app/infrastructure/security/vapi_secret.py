"""Shared-secret comparison for Vapi's server-to-server webhooks.

Extracted so the two places that need it — `deps.verify_vapi_secret`,
which rejects the request, and `RateLimitMiddleware`, which decides
whether the request may skip the IP-keyed bucket — can never drift apart.
A divergence would be silent and bad in both directions: a middleware
that accepted a secret the dependency rejects would hand rate-limit
exemption to unauthenticated traffic, and one that rejected a secret the
dependency accepts would throttle real calls.

Deliberately a predicate rather than a guard: the middleware needs a
boolean and must not raise, since an exception from `add_middleware()`
never reaches the specific exception handlers (see `core/middleware.py`).
"""

from __future__ import annotations

import hmac


def is_valid_vapi_secret(provided: str | None, expected: str | None) -> bool:
    """Constant-time comparison of the `x-vapi-secret` header.

    Fails closed on a missing/empty configured secret, so a deployment
    that never set `VAPI_SERVER_SECRET` cannot be talked to by anyone
    rather than by everyone.

    `hmac.compare_digest` raises `TypeError` on non-ASCII `str` input, and
    the header is attacker-controlled — so a crafted header would
    otherwise surface as a 500 from whichever layer called this. Treated
    as "not valid", which is what a header that cannot even be compared
    is.
    """
    if not expected or not provided:
        return False
    try:
        return hmac.compare_digest(provided, expected)
    except TypeError:
        return False
