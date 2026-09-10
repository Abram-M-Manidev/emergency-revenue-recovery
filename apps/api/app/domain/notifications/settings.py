"""Where one organization's emergency alerts go, and what may be said about
it afterwards.

Two ideas that must not be collapsed
------------------------------------
`NotificationSettings` is what an operator is allowed to *see*: the channel,
whether alerting is on, and a masked hint of the destination. It deliberately
does not carry the destination itself, because a Slack or Teams
incoming-webhook URL is the entire credential — anyone holding it can post as
that integration — and this object is what gets serialised into an HTTP
response.

The raw destination is read only by `EmergencyNotificationService`, through
`NotificationSettingsRepository.get_destination`, on its way to the provider.
It never enters a response body, a log line, an LLM prompt, or a tool result.

Why a destination has to be validated at all
---------------------------------------------
The server makes this request, not the browser. An unvalidated destination is
a server-side request forgery primitive: an organization admin could point it
at a cloud metadata endpoint (169.254.169.254), at the deployment's own
Postgres, or at anything else reachable from inside the network but not from
the internet — and the response body would never even need to come back to
them, because merely issuing the request is often enough. `validate_webhook_
destination` is therefore a security control, not input tidying.
"""

from __future__ import annotations

import ipaddress
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from app.domain.notifications.emergency import NotificationChannel

_MAX_DESTINATION_LENGTH = 2048


class InvalidNotificationDestinationError(ValueError):
    """A destination that must not be stored or called.

    A `ValueError` subclass rather than a `DomainError`, because this is
    input validation on an authenticated admin request — the API layer turns
    it into a 422 alongside every other field error, rather than into one of
    the domain's operational failure codes."""


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    """The safe, operator-visible view of a tenant's alerting configuration.

    `destination_hint` shows enough to recognise which endpoint is
    configured — the host, and the last few characters — without being usable
    by anyone who reads it. That is the point: an operator needs to answer
    "is this pointing at the right Slack channel?" and must not be handed a
    working credential to answer it."""

    organization_id: uuid.UUID
    channel: NotificationChannel
    destination_hint: str
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


def mask_destination(destination: str) -> str:
    """A recognisable, unusable summary of a destination URL.

    Shows scheme and host in full (an operator's real question is almost
    always "is this the right service?") and only the last four characters of
    the path. Slack's webhook secret lives entirely in the path, so the host
    alone is not sensitive while the path is."""
    try:
        parsed = urlparse(destination)
    except ValueError:
        return "(unreadable)"
    host = parsed.hostname or "(unknown host)"
    tail = destination[-4:] if len(destination) >= 4 else ""
    return f"{parsed.scheme}://{host}/…{tail}"


def validate_webhook_destination(destination: str, *, allow_insecure: bool = False) -> str:
    """Checks a webhook URL is safe for the server to call, and returns it
    normalised.

    `allow_insecure` exists only for development and tests, where a local
    listener on plain HTTP is the only practical way to exercise the real
    provider. Production configuration never sets it — see the caller in
    `NotificationSettingsService`, which passes the environment's own answer
    rather than letting a request choose.

    The rules, and why each one is here:

    - **https only.** The payload contains the caller's name, callback
      number and address. Sending that over plain http would put a real
      emergency's PII on the wire in clear text.
    - **no credentials in the URL.** `https://user:pass@host/` would put a
      second secret in a field we mask for display and never intend to hold.
    - **no loopback, private, link-local, or reserved addresses.** This is
      the SSRF control. `169.254.169.254` is the cloud metadata service on
      AWS, GCP and Azure alike, and reaching it from inside the deployment
      can disclose instance credentials; `10/8`, `172.16/12`, `192.168/16`
      and `127/8` reach the deployment's own database, cache and admin
      surfaces.

    DNS is resolved here so a hostname cannot be used to smuggle a private
    address past a textual check. That resolution is best-effort: a name that
    does not resolve right now is allowed through, because a transient DNS
    failure must not permanently reject a legitimate endpoint, and the
    provider itself will simply fail to connect if the name is wrong. What it
    cannot do is *prove* safety at call time — DNS can change between this
    check and the request (a rebinding attack) — so this narrows the attack
    surface rather than closing it, and the deployment guidance is to run the
    API without access to internal admin networks it does not need.
    """
    if not destination or not destination.strip():
        raise InvalidNotificationDestinationError("A destination URL is required.")

    destination = destination.strip()
    if len(destination) > _MAX_DESTINATION_LENGTH:
        raise InvalidNotificationDestinationError(
            f"Destination URL must be at most {_MAX_DESTINATION_LENGTH} characters."
        )

    try:
        parsed = urlparse(destination)
    except ValueError as exc:
        raise InvalidNotificationDestinationError("Destination must be a valid URL.") from exc

    allowed_schemes = {"https", "http"} if allow_insecure else {"https"}
    if parsed.scheme not in allowed_schemes:
        raise InvalidNotificationDestinationError(
            "Destination must be an https:// URL — the alert contains the "
            "caller's name, phone number and address."
        )

    if parsed.username or parsed.password:
        raise InvalidNotificationDestinationError(
            "Destination must not contain a username or password."
        )

    host = parsed.hostname
    if not host:
        raise InvalidNotificationDestinationError("Destination must include a hostname.")

    for address in _resolve(host):
        if not address.is_global or address.is_multicast:
            raise InvalidNotificationDestinationError(
                "Destination must be a public address. Loopback, private, "
                "link-local and reserved addresses are not allowed."
            )

    return destination


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address `host` currently resolves to, or the literal if it is
    already an IP.

    Returns an empty list when the name does not resolve — see the note in
    `validate_webhook_destination` about why that is allowed through rather
    than rejected."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        raw = info[4][0]
        try:
            addresses.append(ipaddress.ip_address(raw))
        except ValueError:
            continue
    return addresses
