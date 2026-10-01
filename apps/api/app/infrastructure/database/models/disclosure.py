"""One row per organization that has chosen its caller-disclosure policy. No
row means the default policy (both notices on) — see
`app/domain/disclosure.py`."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class OrganizationDisclosureSettingsModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organization_disclosure_settings"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    ai_disclosure_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    recording_notice_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
