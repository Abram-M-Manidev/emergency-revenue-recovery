from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.entities.emergency_ticket import TicketStatus
from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class EmergencyTicketModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "emergency_tickets"
    __table_args__ = (
        # Matches real query patterns in `emergency_ticket_repository_impl.py`:
        # `list_for_organization` filters org+status; `count_created_in_range`
        # filters org+created_at; `count_closed_in_range`/
        # `sum_actual_value_in_range`/`revenue_by_day`/
        # `average_resolution_minutes` all filter org+closed_at. Kept in sync
        # with migration `a2b3c4d5e6f7` so autogenerate never sees a spurious
        # index-drop diff.
        Index("ix_emergency_tickets_org_status", "organization_id", "status"),
        Index("ix_emergency_tickets_org_created_at", "organization_id", "created_at"),
        Index("ix_emergency_tickets_org_closed_at", "organization_id", "closed_at"),
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
        unique=True,
        nullable=False,
    )
    matched_service_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(TicketStatus, name="ticket_status", native_enum=False, length=20),
        nullable=False,
        default=TicketStatus.NEW,
    )
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    customer_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_technician_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    actual_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
