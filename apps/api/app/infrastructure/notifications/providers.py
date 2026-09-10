"""Concrete `NotificationPort` adapters.

Three of them, and the difference between them is the difference between a
true and a false sentence on a live emergency call:

- `NullNotificationProvider` — nothing is configured. Reports
  `NOT_CONFIGURED`, never `DELIVERED`. This is the default, so a deployment
  that has not set alerting up degrades to telling callers the truth rather
  than to claiming an alert it never sent.
- `LoggingNotificationProvider` — development only. Reports `DELIVERED`
  while telling nobody, which is precisely the lie this milestone exists to
  remove; `Settings` refuses to boot with it when `ENVIRONMENT=production`.
- `WebhookNotificationProvider` — a real HTTP POST to an endpoint the
  organization already watches (Slack, Teams, PagerDuty, or anything else
  that accepts JSON). Chosen as the first real channel because it needs no
  vendor account, no per-message billing and no phone-number provisioning,
  so a pilot can switch it on the day it starts.

None of them raises. A provider that raised would abort the turn mid-sentence
— a silent hang-up on someone reporting a gas leak.
"""

from __future__ import annotations

import httpx
import structlog

from app.domain.notifications.emergency import (
    DeliveryStatus,
    EmergencyAlert,
    NotificationReceipt,
)
from app.domain.notifications.provider import NotificationPort

logger = structlog.get_logger("app.notifications")


class NullNotificationProvider(NotificationPort):
    """The honest default: no channel configured, so nothing was sent.

    Deliberately not a silent no-op returning success. The whole failure this
    module addresses was a system that behaved as though an alert had gone
    out when none had; a no-op that reported `DELIVERED` would reproduce it
    exactly, one layer down."""

    @property
    def name(self) -> str:
        return "null"

    async def send(
        self, alert: EmergencyAlert, destination: str | None
    ) -> NotificationReceipt:
        return NotificationReceipt(
            status=DeliveryStatus.NOT_CONFIGURED,
            provider=self.name,
            error_code="no_provider_configured",
        )


class LoggingNotificationProvider(NotificationPort):
    """Writes the alert to the application log and calls that delivered.

    For local development and tests only, and `Settings._validate_production_
    safety` rejects it outright when `ENVIRONMENT=production` — because in
    production its `DELIVERED` would license the assistant to tell a caller a
    dispatcher was alerted when the only thing that happened was a log line.

    The log line carries the ticket and conversation ids and nothing else.
    Name, phone and address are all on `alert` and all deliberately omitted:
    this codebase keeps caller details out of logs everywhere else, and an
    emergency is not the place to start making exceptions."""

    @property
    def name(self) -> str:
        return "logging"

    async def send(
        self, alert: EmergencyAlert, destination: str | None
    ) -> NotificationReceipt:
        logger.warning(
            "emergency_alert_logged_not_sent",
            organization_id=str(alert.organization_id),
            ticket_id=str(alert.ticket_id),
            conversation_id=str(alert.conversation_id),
            idempotency_key=alert.idempotency_key,
            note="development provider — no human was actually notified",
        )
        return NotificationReceipt(status=DeliveryStatus.DELIVERED, provider=self.name)


class WebhookNotificationProvider(NotificationPort):
    """POSTs the alert as JSON to an endpoint the organization already
    watches.

    The destination URL is supplied per call by
    `EmergencyNotificationService`, from `NotificationSettingsRepository` —
    never held on this object, because one provider instance serves every
    tenant in the process and a cached URL would be a cross-tenant leak
    waiting for a refactor.

    Timeouts are the caller's, passed in, because the budget belongs to the
    voice turn: every second spent here is silence a caller hears.
    """

    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "webhook"

    async def send(
        self, alert: EmergencyAlert, destination: str | None
    ) -> NotificationReceipt:
        if not destination:
            # Configured to use webhooks but with nowhere to send: an
            # onboarding gap, not a transport failure, and reported as the
            # former so it shows up as setup to finish rather than as noise
            # in an error rate.
            return NotificationReceipt(
                status=DeliveryStatus.NOT_CONFIGURED,
                provider=self.name,
                error_code="no_destination_configured",
            )

        payload = _alert_payload(alert)
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(destination, json=payload)
        except httpx.TimeoutException:
            return NotificationReceipt(
                status=DeliveryStatus.FAILED, provider=self.name, error_code="timeout"
            )
        except httpx.HTTPError:
            # DNS failure, TLS failure, connection refused. Deliberately not
            # logged with the exception text: httpx puts the request URL in
            # its messages, and for Slack and Teams that URL is the secret.
            return NotificationReceipt(
                status=DeliveryStatus.FAILED,
                provider=self.name,
                error_code="transport_error",
            )

        if 200 <= response.status_code < 300:
            # Any 2xx counts: the endpoint took responsibility for the
            # message. Slack answers 200 with a plain "ok" body, PagerDuty
            # answers 202 with a dedup key, and a generic collector may answer
            # 204 with nothing — reading the body to distinguish them would
            # couple us to each vendor for no gain.
            return NotificationReceipt(status=DeliveryStatus.DELIVERED, provider=self.name)

        return NotificationReceipt(
            status=DeliveryStatus.FAILED,
            provider=self.name,
            # Status class only. The body can echo the destination back.
            error_code=f"http_{response.status_code}",
        )


def _alert_payload(alert: EmergencyAlert) -> dict[str, object]:
    """The JSON body. Flat, stable keys plus a pre-rendered `text`.

    `text` exists because the two most likely destinations render it for
    free: Slack and Teams both display a top-level `text` field, so an
    organization can paste an incoming-webhook URL in and get a readable
    message without writing any transformation. The structured fields carry
    the same information for anything that parses rather than displays.
    """
    lines = [f"EMERGENCY: {alert.summary}"]
    if alert.customer_name:
        lines.append(f"Caller: {alert.customer_name}")
    if alert.customer_phone:
        lines.append(f"Callback: {alert.customer_phone}")
    if alert.customer_address:
        lines.append(f"Address: {alert.customer_address}")
    lines.append(f"Ticket: {alert.ticket_id}")

    return {
        "text": "\n".join(lines),
        "event": "emergency_ticket_created",
        "idempotency_key": alert.idempotency_key,
        "ticket_id": str(alert.ticket_id),
        "conversation_id": str(alert.conversation_id),
        "organization_id": str(alert.organization_id),
        "summary": alert.summary,
        "customer_name": alert.customer_name,
        "customer_phone": alert.customer_phone,
        "customer_address": alert.customer_address,
        "created_at": alert.created_at.isoformat(),
    }
