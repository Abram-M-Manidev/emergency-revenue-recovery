"""Usage metering tables — see `app/domain/usage.py` and
`app/domain/ai/usage.py`.

Both outlive the conversation they describe (`conversation_id` is SET NULL,
not CASCADE): they are billing evidence, and must survive whatever
retention policy later deletes transcripts."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.usage import (
    DurationSource,
    ExclusionReason,
    PeriodBasis,
    UsageAttribution,
    UsageSource,
)
from app.domain.usage_reconciliation import ReconciliationOutcome, ReconciliationRunStatus
from app.infrastructure.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.infrastructure.database.session import Base


class CallUsageRecordModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "call_usage_records"
    __table_args__ = (
        # The monthly report's question: "what did this tenant use in May?"
        Index("ix_call_usage_records_organization_period", "organization_id", "billing_period"),
    )

    # Nullable: an unattributed call has no tenant, and is kept anyway.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # One row per call: a re-delivered report merges into it.
    vapi_call_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    attribution: Mapped[UsageAttribution] = mapped_column(
        SAEnum(UsageAttribution, name="usage_attribution", native_enum=False, length=20),
        nullable=False,
    )
    vapi_assistant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vapi_phone_number_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reported_duration_seconds: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3), nullable=True
    )
    ended_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # The decision, as `decide_billing` made it under `rules_version`.
    billing_period: Mapped[date] = mapped_column(Date, nullable=False)
    period_basis: Mapped[PeriodBasis] = mapped_column(
        SAEnum(PeriodBasis, name="usage_period_basis", native_enum=False, length=20),
        nullable=False,
    )
    duration_source: Mapped[DurationSource] = mapped_column(
        SAEnum(DurationSource, name="usage_duration_source", native_enum=False, length=20),
        nullable=False,
    )
    billable_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    billable_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    is_billable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    exclusion_reason: Mapped[ExclusionReason | None] = mapped_column(
        SAEnum(ExclusionReason, name="usage_exclusion_reason", native_enum=False, length=30),
        nullable=True,
    )
    rules_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # Vapi's own figures, exactly as its report carried them. Unverified
    # schema: absent unless the report had them, never computed here.
    vapi_reported_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    # {component: decimal-string}, whitelisted keys only.
    vapi_reported_cost_breakdown: Mapped[dict[str, str] | None] = mapped_column(
        JSONB, nullable=True
    )

    report_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source: Mapped[UsageSource] = mapped_column(
        SAEnum(UsageSource, name="usage_source", native_enum=False, length=20),
        nullable=False,
        default=UsageSource.WEBHOOK,
        server_default="WEBHOOK",
    )
    first_reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UsageReconciliationRunModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One reconciliation run — see `app/domain/usage_reconciliation.py`.
    A dry run leaves no row: it is rolled back with everything else."""

    __tablename__ = "usage_reconciliation_runs"

    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ReconciliationRunStatus] = mapped_column(
        SAEnum(ReconciliationRunStatus, name="usage_reconciliation_run_status",
               native_enum=False, length=20),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counts: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True)
    # Exception type name only.
    error: Mapped[str | None] = mapped_column(String(64), nullable=True)


class VapiCallReconciliationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The latest reconciliation of one Vapi call: the outcome, and Vapi's
    own view of the call beside (never on top of) `call_usage_records`."""

    __tablename__ = "vapi_call_reconciliations"
    __table_args__ = (
        Index("ix_vapi_call_reconciliations_outcome", "outcome"),
    )

    vapi_call_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    call_usage_record_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("call_usage_records.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("usage_reconciliation_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    outcome: Mapped[ReconciliationOutcome] = mapped_column(
        SAEnum(ReconciliationOutcome, name="usage_reconciliation_outcome",
               native_enum=False, length=20),
        nullable=False,
    )
    # [Discrepancy value, ...] and [facts field name, ...].
    discrepancies: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    filled_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    check_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Vapi's view, as its call API reported it.
    vapi_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vapi_assistant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vapi_phone_number_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vapi_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vapi_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vapi_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vapi_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vapi_ended_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vapi_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    vapi_cost_breakdown: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    vapi_minutes_normal: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    vapi_minutes_overage: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    transport_minutes: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    vapi_usage_incomplete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class ConversationLLMUsageModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversation_llm_usage"
    __table_args__ = (
        UniqueConstraint("conversation_id", "model", name="uq_conversation_llm_usage_model"),
        Index("ix_conversation_llm_usage_organization_created", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # "voice" | "text": the dashboard simulator spends tokens too, but is not
    # call usage.
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requests_without_usage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cached_prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
