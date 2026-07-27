"""A caller interaction (see `app/domain/entities/conversation.py`).
`messages` and `outcome` cascade-delete with their parent conversation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.entities.conversation import ConversationChannel, ConversationStatus
from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base

if TYPE_CHECKING:
    from app.infrastructure.database.models.conversation_message import ConversationMessageModel
    from app.infrastructure.database.models.conversation_outcome import ConversationOutcomeModel


class ConversationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # Matches Analytics' org+date-range aggregates (`count_in_range`,
        # `count_by_day`, `count_by_channel_in_range` in
        # `conversation_repository_impl.py`, all filtered on `started_at`,
        # not `created_at`) — kept in sync with migration `a2b3c4d5e6f7` so
        # autogenerate never sees a spurious index-drop diff.
        Index("ix_conversations_org_started_at", "organization_id", "started_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # native_enum=False: see business_profile.py's BusinessType column for why.
    channel: Mapped[ConversationChannel] = mapped_column(
        SAEnum(ConversationChannel, name="conversation_channel", native_enum=False, length=20),
        nullable=False,
    )
    status: Mapped[ConversationStatus] = mapped_column(
        SAEnum(ConversationStatus, name="conversation_status", native_enum=False, length=20),
        nullable=False,
        default=ConversationStatus.ACTIVE,
    )
    caller_phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[list[ConversationMessageModel]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessageModel.created_at",
    )
    outcome: Mapped[ConversationOutcomeModel | None] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", uselist=False
    )
