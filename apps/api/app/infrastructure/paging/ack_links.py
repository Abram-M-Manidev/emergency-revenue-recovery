"""Signed acknowledgement links.

A token is `base64url(page_id ‖ role ‖ HMAC-SHA256(key, page_id ‖ role)[:16])`
— 44 URL-safe characters, carrying a 128-bit MAC. Stateless: nothing is
stored, so the raw token never sits in the database, yet the worker can put
it in a text message long after the page was created.

The key is derived from `JWT_SECRET_KEY` with a fixed domain-separation
label, so a paging token can never be confused with (or used as) anything
else signed with that secret. Rotating `JWT_SECRET_KEY` therefore also
invalidates outstanding acknowledgement links; recipients can still
acknowledge from the dashboard.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import uuid

from app.domain.paging.page import RecipientRole
from app.domain.paging.port import AckLinkSigner

_LABEL = b"errs/emergency-page-ack/v1"
_MAC_BYTES = 16
_ROLE_BYTES = {RecipientRole.PRIMARY: b"P", RecipientRole.BACKUP: b"B"}
_ROLES_BY_BYTE = {value: role for role, value in _ROLE_BYTES.items()}
_TOKEN_BYTES = 16 + 1 + _MAC_BYTES


class HmacAckLinkSigner(AckLinkSigner):
    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("an acknowledgement-link secret is required")
        self._key = hmac.new(secret.encode("utf-8"), _LABEL, hashlib.sha256).digest()

    def _mac(self, payload: bytes) -> bytes:
        return hmac.new(self._key, payload, hashlib.sha256).digest()[:_MAC_BYTES]

    def issue(self, page_id: uuid.UUID, role: RecipientRole) -> str:
        payload = page_id.bytes + _ROLE_BYTES[role]
        return base64.urlsafe_b64encode(payload + self._mac(payload)).decode("ascii").rstrip("=")

    def verify(self, token: str) -> tuple[uuid.UUID, RecipientRole] | None:
        if not token or len(token) > 64:
            return None
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except (binascii.Error, ValueError):
            return None
        if len(raw) != _TOKEN_BYTES:
            return None
        payload, mac = raw[:17], raw[17:]
        if not hmac.compare_digest(mac, self._mac(payload)):
            return None
        role = _ROLES_BY_BYTE.get(payload[16:17])
        if role is None:
            return None
        return uuid.UUID(bytes=payload[:16]), role
