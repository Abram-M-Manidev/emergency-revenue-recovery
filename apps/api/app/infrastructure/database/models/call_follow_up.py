"""Calls that ended without their request being recorded — see
`app/domain/follow_ups.py`."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.follow_ups import FollowUpReason
from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class CallFollowUpModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "call_follow_ups"
    __table_args__ = (
        # The dashboard's question: "which calls still need a callback?"
        Index("ix_call_follow_ups_organization_resolved", "organization_id", "resolved_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        # One per call: a retried turn and a repeated end-of-call report
        # must land on the same row, not ask for two callbacks.
        unique=True,
        nullable=False,
    )
    reason: Mapped[FollowUpReason] = mapped_column(
        SAEnum(FollowUpReason, name="call_follow_up_reason", native_enum=False, length=40),
        nullable=False,
    )
    possible_emergency: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    emergency_ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("emergency_tickets.id", ondelete="SET NULL"),
        nullable=True,
    )
    caller_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
