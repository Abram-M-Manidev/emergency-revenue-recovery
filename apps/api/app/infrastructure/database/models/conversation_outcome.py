from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.infrastructure.database.models.mixins import UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base

if TYPE_CHECKING:
    from app.infrastructure.database.models.conversation import ConversationModel


class ConversationOutcomeModel(UUIDPrimaryKeyMixin, Base):
    """One row per conversation (`conversation_id` is unique) — created on
    the first turn, updated in place on every subsequent one via
    `ConversationOutcomeRepository.upsert`."""

    __tablename__ = "conversation_outcomes"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    classification: Mapped[CallClassification] = mapped_column(
        SAEnum(CallClassification, name="call_classification", native_enum=False, length=20),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    recommended_action: Mapped[RecommendedAction] = mapped_column(
        SAEnum(RecommendedAction, name="recommended_action", native_enum=False, length=30),
        nullable=False,
    )
    matched_service_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    customer_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    conversation: Mapped[ConversationModel] = relationship(back_populates="outcome")
