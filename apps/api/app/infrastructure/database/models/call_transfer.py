"""Human-fallback tables: where a tenant's calls may be transferred, and every
attempt to do so."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.call_transfer.attempt import DestinationKind, TransferReason, TransferStatus
from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class OrganizationCallTransferSettingsModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organization_call_transfer_settings"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        # One configuration per tenant: "where does this call go?" must have
        # exactly one answer at the moment a caller asks for a person.
        unique=True,
        nullable=False,
        index=True,
    )
    business_hours_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    after_hours_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    transfer_emergencies: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CallTransferAttemptModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "call_transfer_attempts"
    __table_args__ = (
        Index("ix_call_transfer_attempts_conversation_status", "conversation_id", "status"),
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
    )
    status: Mapped[TransferStatus] = mapped_column(
        SAEnum(TransferStatus, name="call_transfer_status", native_enum=False, length=30),
        nullable=False,
    )
    reason: Mapped[TransferReason] = mapped_column(
        SAEnum(TransferReason, name="call_transfer_reason", native_enum=False, length=30),
        nullable=False,
    )
    is_emergency: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    destination_kind: Mapped[DestinationKind | None] = mapped_column(
        SAEnum(DestinationKind, name="call_transfer_destination_kind", native_enum=False, length=20),
        nullable=True,
    )
    destination_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
