"""Access token issuance and verification.

Access tokens are short-lived, stateless JWTs carrying the claims needed to
authorize a request (user id, organization id, permission codes) without a
database round-trip. Refresh tokens are handled separately (see
`app/infrastructure/security/tokens.py`) because they must be revocable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from jwt import PyJWTError

from app.core.config import Settings
from app.domain.exceptions import InvalidTokenError

ACCESS_TOKEN_TYPE = "access"


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    permissions: frozenset[str]
    is_superuser: bool


def create_access_token(
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    permissions: frozenset[str],
    is_superuser: bool,
    settings: Settings,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "org_id": str(organization_id),
        "permissions": sorted(permissions),
        "is_superuser": is_superuser,
        "type": ACCESS_TOKEN_TYPE,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str, *, settings: Settings) -> AccessTokenClaims:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except PyJWTError as exc:
        raise InvalidTokenError() from exc

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise InvalidTokenError("Token is not a valid access token.")

    try:
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            organization_id=uuid.UUID(payload["org_id"]),
            permissions=frozenset(payload.get("permissions", [])),
            is_superuser=bool(payload.get("is_superuser", False)),
        )
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError("Token payload is malformed.") from exc
