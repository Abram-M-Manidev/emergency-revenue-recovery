"""See `app/domain/entities/voice_call.py` for why this is a separate table
from `conversations` rather than extra columns bolted onto it."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class VoiceCallModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "voice_calls"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    vapi_call_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    caller_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recording_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # The caller notice ERRS gave on this call (see app/domain/disclosure.py).
    # Nullable: NULL means unknown — a call from before disclosure existed, or
    # one ERRS never spoke on — never "no notice".
    disclosure_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    disclosed_ai: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    disclosed_recording: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
