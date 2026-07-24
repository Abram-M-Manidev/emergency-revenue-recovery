"""Organization = tenant. Every user, role, and (future) business record
belongs to exactly one organization, which is the seam multi-tenancy
(Milestone 9) will scope queries on."""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class OrganizationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    users: Mapped[list[UserModel]] = relationship(  # noqa: F821
        back_populates="organization", cascade="all, delete-orphan"
    )
    roles: Mapped[list[RoleModel]] = relationship(  # noqa: F821
        back_populates="organization", cascade="all, delete-orphan"
    )
