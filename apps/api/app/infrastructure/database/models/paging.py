"""Emergency-paging tables: who a tenant pages, each emergency's page, and
every notification sent for it.

Enums use `native_enum=False`, like every enum in this schema, which stores
the Python member NAME (e.g. 'PAGING_PRIMARY') in a VARCHAR — raw SQL against
these columns must use the upper-case names.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.paging.page import (
    AcknowledgementMethod,
    PageNotificationStatus,
    PageStatus,
    RecipientRole,
)
from app.domain.paging.settings import (
    MAX_ACK_TIMEOUT_SECONDS,
    MIN_ACK_TIMEOUT_SECONDS,
    PagingChannel,
)
from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base

_ROLE = SAEnum(RecipientRole, name="paging_recipient_role", native_enum=False, length=20)


class OrganizationPagingSettingsModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per tenant. Its own table, never on `business_profiles`: that
    profile is assembled into the LLM prompt, and these are personal
    mobiles."""

    __tablename__ = "organization_paging_settings"
    __table_args__ = (
        CheckConstraint(
            f"ack_timeout_seconds BETWEEN {MIN_ACK_TIMEOUT_SECONDS} AND {MAX_ACK_TIMEOUT_SECONDS}",
            name="ck_organization_paging_settings_ack_timeout",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    primary_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    backup_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sms_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    voice_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ack_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)


class EmergencyPageModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One per emergency ticket. The unique index on the ticket is the
    idempotency mechanism: `create` inserts against it with ON CONFLICT DO
    NOTHING, so a ticket synced twice is paged once."""

    __tablename__ = "emergency_pages"
    __table_args__ = (
        # The escalation poller's scan: only pages still waiting on someone.
        Index(
            "ix_emergency_pages_escalate_at",
            "escalate_at",
            postgresql_where=text("escalate_at IS NOT NULL"),
        ),
    )

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
    status: Mapped[PageStatus] = mapped_column(
        SAEnum(PageStatus, name="emergency_page_status", native_enum=False, length=20),
        nullable=False,
    )
    ack_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Non-null exactly while the page is escalating (paging_primary /
    #: paging_backup): when the current recipient's window closes.
    escalate_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_by_role: Mapped[RecipientRole | None] = mapped_column(_ROLE, nullable=True)
    acknowledged_via: Mapped[AcknowledgementMethod | None] = mapped_column(
        SAEnum(AcknowledgementMethod, name="paging_ack_method", native_enum=False, length=20),
        nullable=True,
    )
    acknowledged_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    unresolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unresolved_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)


class EmergencyPageNotificationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One per (page, recipient role, channel): the send outbox.

    `next_attempt_at` is the single "due at" column: when it is first due,
    when a retry is due, and — while `sending` — when the claiming worker's
    lease runs out and the row becomes due again. NULL once terminal."""

    __tablename__ = "emergency_page_notifications"
    __table_args__ = (
        UniqueConstraint(
            "page_id", "role", "channel", name="uq_emergency_page_notifications_page_role_channel"
        ),
        Index(
            "ix_emergency_page_notifications_due",
            "next_attempt_at",
            postgresql_where=text("next_attempt_at IS NOT NULL"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("emergency_pages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[RecipientRole] = mapped_column(_ROLE, nullable=False)
    channel: Mapped[PagingChannel] = mapped_column(
        SAEnum(PagingChannel, name="paging_channel", native_enum=False, length=20),
        nullable=False,
    )
    destination: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[PageNotificationStatus] = mapped_column(
        SAEnum(
            PageNotificationStatus,
            name="emergency_page_notification_status",
            native_enum=False,
            length=20,
        ),
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
