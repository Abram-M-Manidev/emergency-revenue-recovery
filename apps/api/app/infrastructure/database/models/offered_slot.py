"""See `app/domain/repositories/offered_slot_repository.py` for why this
table exists rather than an in-process cache.

One row per (conversation, start time) the caller was actually read, plus
whether they went on to choose it. Small and short-lived by nature — a
handful of rows per call — and cascade-deleted with its conversation, so it
needs no retention policy of its own."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.models.mixins import UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class OfferedSlotModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "conversation_offered_slots"
    __table_args__ = (
        # One row per offered start time, so re-offering the same slot — which
        # happens whenever `check_availability` runs twice in a conversation —
        # updates rather than duplicates.
        UniqueConstraint(
            "conversation_id", "slot_start_at", name="uq_offered_slot_conversation_start"
        ),
        # At most one live selection per conversation, enforced by the
        # database rather than by the read-modify-write in
        # `mark_selected`. Two transcription-driven turns on one call can
        # overlap, and without this both could clear-then-set and leave two
        # selected rows — from which `get_active_selection` would return an
        # arbitrary one, quietly authorising a time the caller did not pick.
        Index(
            "uq_offered_slot_one_active_selection",
            "conversation_id",
            unique=True,
            postgresql_where=text("selected_at IS NOT NULL"),
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
    slot_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    offered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # Persisted conversation-message count at the start of the turn that read
    # this slot out. The caller cannot have responded to an offer during the
    # very turn it was made, so a selection is only credible from a strictly
    # higher index — see `app/domain/entities/offered_slot.py`.
    offered_turn_index: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    selected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    selected_turn_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
