"""See `app/domain/entities/voice_line.py` for why this exists: the
inbound-call → organization routing table."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.entities.voice_line import VoiceProvider
from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class VoiceLineModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "voice_lines"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # native_enum=False: see business_profile.py's BusinessType column for why.
    provider: Mapped[VoiceProvider] = mapped_column(
        SAEnum(VoiceProvider, name="voice_provider", native_enum=False, length=20),
        nullable=False,
        default=VoiceProvider.VAPI,
    )
    vapi_assistant_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    vapi_phone_number_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )
    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
