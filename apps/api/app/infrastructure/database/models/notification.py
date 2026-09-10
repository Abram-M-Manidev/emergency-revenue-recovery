"""Emergency-notification tables: where a tenant wants alerts sent, and what
happened when we sent them.

Both enums use `native_enum=False`, matching every other enum column in this
schema (see `emergency_ticket.py`). That stores a VARCHAR with a CHECK
constraint rather than a PostgreSQL ENUM type, which keeps `ALTER TYPE`
migrations and case-sensitivity mismatches between the Python member name and
the database label out of the picture — a trap this project has already been
caught by twice.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.notifications.emergency import DeliveryStatus, NotificationChannel
from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class OrganizationNotificationSettingsModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per organization that has configured emergency alerting.

    Deliberately its own table rather than columns on `business_profiles`.
    That profile is Business Knowledge and is assembled into the LLM system
    prompt on every turn; `destination` is a credential (a Slack or Teams
    incoming-webhook URL is the entire secret). Keeping them in separate
    tables makes "the webhook URL ended up in a prompt" unavailable rather
    than merely unlikely.
    """

    __tablename__ = "organization_notification_settings"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        # One configuration per tenant. A second row would make "where do
        # this organization's alerts go?" ambiguous at the exact moment it
        # matters most.
        unique=True,
        nullable=False,
        index=True,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        SAEnum(NotificationChannel, name="notification_channel", native_enum=False, length=20),
        nullable=False,
    )
    # Text, not String(n): a Slack webhook URL is already ~120 characters and
    # PagerDuty's Events API v2 endpoints carry long routing keys. A length
    # cap here would fail at configuration time for no storage benefit.
    destination: Mapped[str] = mapped_column(Text, nullable=False)
    # An explicit off switch, so an operator can stop alerting during
    # maintenance without deleting the destination and having to find it
    # again. Off is treated exactly like unconfigured: the assistant is not
    # allowed to claim an alert either way.
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class EmergencyNotificationDeliveryModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per emergency ticket we have tried to alert someone about.

    The unique index on `emergency_ticket_id` is the idempotency mechanism,
    not merely a constraint: `claim()` inserts against it with ON CONFLICT DO
    NOTHING, so exactly one caller across all four uvicorn workers wins the
    right to send. Without it, a ticket created by the tool loop and then
    again by the webhook's outcome sync — on a transcript Vapi re-sent — would
    page the on-call engineer twice for one gas leak.
    """

    __tablename__ = "emergency_notification_deliveries"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    emergency_ticket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("emergency_tickets.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # Nullable because a delivery row exists even when nothing is configured:
    # "we tried and there was nowhere to send" is a fact worth keeping, and it
    # is the row an operator looks at to discover the gap.
    channel: Mapped[NotificationChannel | None] = mapped_column(
        SAEnum(NotificationChannel, name="notification_channel", native_enum=False, length=20),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        SAEnum(DeliveryStatus, name="notification_delivery_status", native_enum=False, length=20),
        nullable=False,
        default=DeliveryStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # A short token — "timeout", "http_500", "transport_error" — never a
    # provider response body, which can echo the destination URL back and so
    # would turn this column into a place secrets accumulate.
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
