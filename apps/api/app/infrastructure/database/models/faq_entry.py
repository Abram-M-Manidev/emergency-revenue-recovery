from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class FAQEntryModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "faq_entries"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
