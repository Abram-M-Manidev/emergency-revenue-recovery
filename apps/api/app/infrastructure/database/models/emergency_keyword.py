from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class EmergencyKeywordModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "emergency_keywords"
    __table_args__ = (
        UniqueConstraint("organization_id", "phrase", name="uq_emergency_keyword_org_phrase"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    phrase: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
