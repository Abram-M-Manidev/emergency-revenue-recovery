"""Opaque refresh token generation.

The client-visible token is `{token_id}.{secret}` — the id gives O(1) DB
lookup, the secret is what's hashed and compared, so the database never
stores (or leaks, via a dump) anything that alone authenticates a session.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IssuedRefreshToken:
    token_id: uuid.UUID
    raw_value: str
    token_hash: str


def hash_token_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def issue_refresh_token() -> IssuedRefreshToken:
    token_id = uuid.uuid4()
    secret = secrets.token_urlsafe(48)
    raw_value = f"{token_id}.{secret}"
    return IssuedRefreshToken(token_id=token_id, raw_value=raw_value, token_hash=hash_token_secret(secret))


def parse_refresh_token(raw_value: str) -> tuple[uuid.UUID, str] | None:
    """Split a client-supplied refresh token into (token_id, secret).

    Returns None if the token is malformed rather than raising, since a
    malformed token is an expected input from untrusted clients, not a bug.
    """
    try:
        id_part, secret = raw_value.split(".", 1)
        return uuid.UUID(id_part), secret
    except ValueError:
        return None
