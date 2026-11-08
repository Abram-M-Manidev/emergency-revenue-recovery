"""See `app/domain/repositories/lead_repository.py`. Personal data of
prospective customers; retention is set in docs/legal/PRIVACY_POLICY_DRAFT.md
and carried out by the operator, not automatically."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.models.mixins import UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class MarketingLeadModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "marketing_leads"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    business_name: Mapped[str] = mapped_column(String(160), nullable=False)
    # Stored lower-cased; unique, so a repeat request refreshes one row.
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    business_type: Mapped[str] = mapped_column(String(32), nullable=False)
    consent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
