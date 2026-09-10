"""Organization = tenant. Every user, role, and (future) business record
belongs to exactly one organization, which is the seam multi-tenancy
(Milestone 9) will scope queries on."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base

if TYPE_CHECKING:
    from app.infrastructure.database.models.role import RoleModel
    from app.infrastructure.database.models.user import UserModel


class OrganizationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Per-tenant voice kill switch — see `Organization.voice_assistant_enabled`.
    # Lives on the tenant row rather than in its own table because it is a
    # single operational boolean that every inbound call must read: an extra
    # join on the hottest path of the system, to store one bit, would be the
    # wrong trade.
    voice_assistant_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    users: Mapped[list[UserModel]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    roles: Mapped[list[RoleModel]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
