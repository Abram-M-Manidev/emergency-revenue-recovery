"""Letting a business tell ESSR where its emergency alerts should go.

The on-ramp Phase 1 deliberately left out. The notification port, delivery
state, idempotency and truthfulness gate all shipped, but nothing in the
product could set a destination — so every emergency call correctly, and
uselessly, told the caller the alert could not be confirmed.

Narrow on purpose
-----------------
This is not an organization-settings service that happens to include
notifications. It does one thing, and it is the only place that decides
whether a destination is acceptable — because the destination is both a
credential and a URL this server will make requests to, and both of those
properties want a single chokepoint rather than validation scattered across
an endpoint, a schema and a repository.

What it never does
------------------
Return the destination. `NotificationSettings` carries a masked hint and
nothing more, so no response this service produces can leak the credential
back out — not to the admin who set it, not to a lower-privileged user, and
not into a log. Reading the real value is `EmergencyNotificationService`'s
alone, on its way to the provider.
"""

from __future__ import annotations

import uuid

import structlog

from app.core.config import Settings
from app.domain.notifications.emergency import NotificationChannel
from app.domain.notifications.settings import (
    NotificationSettings,
    validate_webhook_destination,
)
from app.domain.repositories.notification_repository import NotificationSettingsRepository

logger = structlog.get_logger("app.notifications")


class NotificationSettingsService:
    def __init__(
        self,
        *,
        settings_repository: NotificationSettingsRepository,
        settings: Settings,
    ) -> None:
        self._repository = settings_repository
        self._settings = settings

    async def get(self, organization_id: uuid.UUID) -> NotificationSettings | None:
        """This organization's configuration, or None if it has never set one.

        Scoped by the organization the caller's JWT resolved to — there is no
        argument by which one tenant can read another's."""
        return await self._repository.get_settings(organization_id)

    async def configure(
        self,
        organization_id: uuid.UUID,
        *,
        channel: NotificationChannel,
        destination: str,
        is_enabled: bool = True,
    ) -> NotificationSettings:
        """Validates and stores where this organization's alerts go.

        Validation happens here rather than in the schema because it depends
        on the deployment, not only on the input: `allow_insecure` follows
        the environment, so a developer can point a webhook at a local
        listener over plain http while production refuses anything but
        https. A Pydantic validator has no access to that and would have to
        either forbid local testing or permit cleartext PII in production.
        """
        validated = validate_webhook_destination(
            destination,
            # Production never relaxes this. The alert payload carries the
            # caller's name, callback number and address, and http would put
            # a real emergency's PII on the wire in clear text.
            allow_insecure=not self._settings.is_production,
        )
        stored = await self._repository.upsert_settings(
            organization_id,
            channel=channel,
            destination=validated,
            is_enabled=is_enabled,
        )
        # The destination is a credential, so only the masked hint is logged
        # — enough for an operator to confirm *which* endpoint changed
        # without the log becoming a place secrets accumulate.
        logger.info(
            "notification_settings_configured",
            organization_id=str(organization_id),
            channel=channel.value,
            is_enabled=is_enabled,
            destination_hint=stored.destination_hint,
        )
        return stored

    async def set_enabled(
        self, organization_id: uuid.UUID, *, is_enabled: bool
    ) -> NotificationSettings | None:
        """Switches alerting on or off without touching the destination.

        Separate from `configure` so an operator pausing alerts during
        maintenance does not have to re-enter — and therefore re-handle — the
        webhook URL. Returns None when nothing is configured, which the API
        turns into a 404: enabling an alerting configuration that does not
        exist is a mistake worth surfacing, not a silent no-op."""
        stored = await self._repository.set_enabled(
            organization_id, is_enabled=is_enabled
        )
        if stored is None:
            return None
        logger.info(
            "notification_settings_toggled",
            organization_id=str(organization_id),
            is_enabled=is_enabled,
        )
        return stored

    async def remove(self, organization_id: uuid.UUID) -> bool:
        """Deletes the configuration outright, returning whether there was
        one.

        Distinct from disabling: this is what an operator does when the
        endpoint was wrong or the webhook has been rotated, and it must leave
        no copy of the old credential behind."""
        removed = await self._repository.delete_settings(organization_id)
        if removed:
            logger.info(
                "notification_settings_removed", organization_id=str(organization_id)
            )
        return removed
