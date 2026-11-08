"""See `app/domain/repositories/contact_correction_repository.py`.

A handful of rows per call at most, cascade-deleted with the conversation.
The proposed value is caller PII and is retained exactly as long as the
conversation it belongs to."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.entities.conversation_outcome import CUSTOMER_ADDRESS_MAX_LENGTH
from app.infrastructure.database.models.mixins import UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class ContactCorrectionRequestModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "contact_correction_requests"
    __table_args__ = (
        # At most one pending proposal per (conversation, field): proposing
        # again supersedes the previous one in the same transaction.
        Index(
            "uq_contact_correction_one_pending",
            "conversation_id",
            "field",
            unique=True,
            postgresql_where=text("confirmed_at IS NULL AND superseded_at IS NULL"),
        ),
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
        nullable=False,
        index=True,
    )
    field: Mapped[str] = mapped_column(String(32), nullable=False)
    proposed_value: Mapped[str] = mapped_column(
        String(CUSTOMER_ADDRESS_MAX_LENGTH), nullable=False
    )
    proposed_turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
