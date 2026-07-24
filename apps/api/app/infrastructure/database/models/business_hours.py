"""Two related tables, always read/edited together as one 'Hours' section:
weekly recurring hours and one-off calendar overrides (holidays/closures).
See `app.domain.repositories.business_hours_repository` for why they share
a single repository interface."""

from __future__ import annotations

import uuid
from datetime import date as py_date
from datetime import time as py_time

from sqlalchemy import Boolean, Date, ForeignKey, SmallInteger, String, Time, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class WeeklyHoursModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "business_hours"
    __table_args__ = (
        UniqueConstraint("organization_id", "day_of_week", name="uq_business_hours_org_day"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 0=Mon .. 6=Sun
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    open_time: Mapped[py_time | None] = mapped_column(Time, nullable=True)
    close_time: Mapped[py_time | None] = mapped_column(Time, nullable=True)


class HoursExceptionModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "business_hours_exceptions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "exception_date", name="uq_business_hours_exceptions_org_date"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    exception_date: Mapped[py_date] = mapped_column(Date, nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    open_time: Mapped[py_time | None] = mapped_column(Time, nullable=True)
    close_time: Mapped[py_time | None] = mapped_column(Time, nullable=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
