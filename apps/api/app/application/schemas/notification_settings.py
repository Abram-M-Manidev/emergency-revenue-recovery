"""Request/response shapes for emergency-notification configuration.

The response type carries `destination_hint` and never `destination`. That
is the whole design: a Slack or Teams incoming-webhook URL is the entire
credential, so the only way to guarantee it is never returned is for the
object the serialiser is handed not to have it. See
`app/domain/notifications/settings.py`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.notifications.emergency import NotificationChannel


class NotificationSettingsResponse(BaseModel):
    """What an operator may see about their own alerting configuration."""

    model_config = ConfigDict(from_attributes=True)

    channel: NotificationChannel
    #: Scheme, host, and the last four characters — enough to recognise the
    #: endpoint, not enough to post to it.
    destination_hint: str
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class ConfigureNotificationsRequest(BaseModel):
    """Set (or replace) where this organization's emergency alerts go."""

    channel: NotificationChannel = NotificationChannel.WEBHOOK
    #: Validated in `NotificationSettingsService`, not here: whether plain
    #: http is acceptable depends on the deployment environment, which a
    #: Pydantic validator cannot see. Length is capped here only to reject
    #: obvious nonsense before it reaches the service.
    destination: str = Field(min_length=8, max_length=2048)
    is_enabled: bool = True


class SetNotificationsEnabledRequest(BaseModel):
    """Pause or resume alerting without re-submitting the destination."""

    is_enabled: bool
