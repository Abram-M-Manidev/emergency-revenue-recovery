"""Business-specific profile data for a tenant: display name, timezone,
address, and business type. Kept separate from OrganizationModel (the
auth/billing tenant record) because this is business-domain knowledge, not
auth state — see ARCHITECTURE.md's separate 'Business Knowledge' module."""

from __future__ import annotations

import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.entities.business_profile import BusinessType
from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class BusinessProfileModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "business_profiles"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # native_enum=False stores this as a plain VARCHAR rather than a Postgres
    # native ENUM type, so adding a new business type later is a data change,
    # not a schema migration that has to ALTER TYPE.
    business_type: Mapped[BusinessType] = mapped_column(
        SAEnum(BusinessType, name="business_type", native_enum=False, length=20), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(60), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="US")
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
