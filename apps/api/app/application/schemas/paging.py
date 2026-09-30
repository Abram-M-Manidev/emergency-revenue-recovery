"""Request/response shapes for emergency paging.

Recipient numbers are returned in full only by the Owner-only settings
endpoint (`organization:manage`), who needs to see exactly which phone is
paged — the same rule as call-transfer numbers. Everywhere else (the
dispatch view of a page) they are masked, and they never reach prompts, tool
results or logs.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.application.services.emergency_paging_service import PageView
from app.domain.paging.page import (
    AcknowledgementMethod,
    PageNotificationStatus,
    PageStatus,
    RecipientRole,
)
from app.domain.paging.settings import DEFAULT_ACK_TIMEOUT_SECONDS, PagingChannel


class PagingSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    is_enabled: bool
    primary_number: str | None
    backup_number: str | None
    sms_enabled: bool
    voice_enabled: bool
    ack_timeout_seconds: int
    created_at: datetime | None
    updated_at: datetime | None


class ConfigurePagingRequest(BaseModel):
    """Set (or replace) this organization's paging. The rules (E.164, not
    the AI line, backup differs from primary, timeout bounds, a primary and a
    channel when enabled) are enforced in the domain; lengths are capped here
    only to reject obvious nonsense early."""

    is_enabled: bool = True
    primary_number: str | None = Field(default=None, max_length=40)
    backup_number: str | None = Field(default=None, max_length=40)
    sms_enabled: bool = True
    voice_enabled: bool = False
    ack_timeout_seconds: int = Field(default=DEFAULT_ACK_TIMEOUT_SECONDS, ge=1, le=86_400)


class PageNotificationResponse(BaseModel):
    role: RecipientRole
    channel: PagingChannel
    #: Masked, e.g. `+1•••••4567`.
    destination_hint: str
    status: PageNotificationStatus
    attempts: int
    error_code: str | None
    sent_at: datetime | None
    next_attempt_at: datetime | None
    created_at: datetime


class EmergencyPageResponse(BaseModel):
    id: uuid.UUID
    ticket_id: uuid.UUID
    status: PageStatus
    ack_timeout_seconds: int
    escalate_at: datetime | None
    escalated_at: datetime | None
    acknowledged_at: datetime | None
    acknowledged_by_role: RecipientRole | None
    acknowledged_via: AcknowledgementMethod | None
    acknowledged_by_user_id: uuid.UUID | None
    unresolved_at: datetime | None
    unresolved_reason: str | None
    created_at: datetime
    notifications: list[PageNotificationResponse]

    @classmethod
    def from_view(cls, view: PageView) -> EmergencyPageResponse:
        page = view.page
        return cls(
            id=page.id,
            ticket_id=page.ticket_id,
            status=page.status,
            ack_timeout_seconds=page.ack_timeout_seconds,
            escalate_at=page.escalate_at,
            escalated_at=page.escalated_at,
            acknowledged_at=page.acknowledged_at,
            acknowledged_by_role=page.acknowledged_by_role,
            acknowledged_via=page.acknowledged_via,
            acknowledged_by_user_id=page.acknowledged_by_user_id,
            unresolved_at=page.unresolved_at,
            unresolved_reason=page.unresolved_reason,
            created_at=page.created_at,
            notifications=[
                PageNotificationResponse(
                    role=n.role,
                    channel=n.channel,
                    destination_hint=n.destination_hint,
                    status=n.status,
                    attempts=n.attempts,
                    error_code=n.error_code,
                    sent_at=n.sent_at,
                    next_attempt_at=n.next_attempt_at,
                    created_at=n.created_at,
                )
                for n in view.notifications
            ],
        )


class AcknowledgeLinkRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)


class AcknowledgementResponse(BaseModel):
    status: PageStatus
    acknowledged_at: datetime | None
    #: True when this request found the page already acknowledged — by this
    #: link earlier, by the other recipient, or from the dashboard.
    already_acknowledged: bool
