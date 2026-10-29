"""SQLAlchemy implementations of the usage metering repositories."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ai.usage import TokenUsageTotal
from app.domain.repositories.usage_repository import CallUsageRepository, LLMUsageRepository
from app.domain.usage import BillingDecision, CallUsageFacts, CallUsageRecord
from app.infrastructure.database.models.usage import (
    CallUsageRecordModel,
    ConversationLLMUsageModel,
)


def _breakdown_to_json(breakdown: dict[str, Decimal] | None) -> dict[str, str] | None:
    # Decimal strings, not JSON numbers: a float round-trip would quietly
    # change the provider's figure.
    return None if breakdown is None else {key: str(value) for key, value in breakdown.items()}


def _breakdown_from_json(stored: dict[str, Any] | None) -> dict[str, Decimal] | None:
    return None if stored is None else {key: Decimal(str(value)) for key, value in stored.items()}


def _columns(record: CallUsageRecord) -> dict[str, Any]:
    facts, decision = record.facts, record.decision
    return {
        "organization_id": record.organization_id,
        "conversation_id": record.conversation_id,
        "vapi_call_id": facts.vapi_call_id,
        "attribution": record.attribution,
        "vapi_assistant_id": facts.vapi_assistant_id,
        "vapi_phone_number_id": facts.vapi_phone_number_id,
        "started_at": facts.started_at,
        "ended_at": facts.ended_at,
        "reported_duration_seconds": facts.reported_duration_seconds,
        "ended_reason": facts.ended_reason,
        "billing_period": decision.billing_period,
        "period_basis": decision.period_basis,
        "duration_source": decision.duration_source,
        "billable_seconds": decision.billable_seconds,
        "billable_minutes": decision.billable_minutes,
        "is_billable": decision.is_billable,
        "exclusion_reason": decision.exclusion_reason,
        "rules_version": decision.rules_version,
        "vapi_reported_cost": facts.vapi_reported_cost,
        "vapi_reported_cost_breakdown": _breakdown_to_json(facts.vapi_reported_cost_breakdown),
        "report_count": record.report_count,
        "first_reported_at": record.first_reported_at,
        "last_reported_at": record.last_reported_at,
    }


def _to_entity(model: CallUsageRecordModel) -> CallUsageRecord:
    return CallUsageRecord(
        id=model.id,
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        attribution=model.attribution,
        facts=CallUsageFacts(
            vapi_call_id=model.vapi_call_id,
            vapi_assistant_id=model.vapi_assistant_id,
            vapi_phone_number_id=model.vapi_phone_number_id,
            started_at=model.started_at,
            ended_at=model.ended_at,
            reported_duration_seconds=model.reported_duration_seconds,
            ended_reason=model.ended_reason,
            vapi_reported_cost=model.vapi_reported_cost,
            vapi_reported_cost_breakdown=_breakdown_from_json(model.vapi_reported_cost_breakdown),
        ),
        decision=BillingDecision(
            billing_period=model.billing_period,
            period_basis=model.period_basis,
            duration_source=model.duration_source,
            billable_seconds=model.billable_seconds,
            billable_minutes=model.billable_minutes,
            is_billable=model.is_billable,
            exclusion_reason=model.exclusion_reason,
            rules_version=model.rules_version,
        ),
        report_count=model.report_count,
        first_reported_at=model.first_reported_at,
        last_reported_at=model.last_reported_at,
    )


class SqlAlchemyCallUsageRepository(CallUsageRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_update(self, vapi_call_id: str) -> CallUsageRecord | None:
        # `populate_existing`: the row may have been written by a core INSERT
        # the identity map never saw, or changed by a concurrent delivery
        # this lock just waited for.
        result = await self._session.execute(
            select(CallUsageRecordModel)
            .where(CallUsageRecordModel.vapi_call_id == vapi_call_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model is not None else None

    async def insert_if_absent(self, record: CallUsageRecord) -> bool:
        # ON CONFLICT DO NOTHING on the unique call id: a unique violation
        # would poison the request's transaction.
        result = await self._session.execute(
            pg_insert(CallUsageRecordModel)
            .values(id=record.id, **_columns(record))
            .on_conflict_do_nothing(index_elements=["vapi_call_id"])
            .returning(CallUsageRecordModel.id)
        )
        return result.scalar_one_or_none() is not None

    async def update(self, record: CallUsageRecord) -> CallUsageRecord:
        model = await self._session.get(CallUsageRecordModel, record.id)
        assert model is not None  # locked by `get_for_update` in this transaction
        for name, value in _columns(record).items():
            setattr(model, name, value)
        model.updated_at = datetime.now(timezone.utc)
        await self._session.flush()
        return _to_entity(model)


class SqlAlchemyLLMUsageRepository(LLMUsageRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        channel: str,
        total: TokenUsageTotal,
    ) -> None:
        table = ConversationLLMUsageModel.__table__
        statement = pg_insert(ConversationLLMUsageModel).values(
            id=uuid.uuid4(),
            organization_id=organization_id,
            conversation_id=conversation_id,
            channel=channel,
            model=total.model,
            requests=total.requests,
            requests_without_usage=total.requests_without_usage,
            prompt_tokens=total.prompt_tokens,
            cached_prompt_tokens=total.cached_prompt_tokens,
            completion_tokens=total.completion_tokens,
        )
        # Atomic increment: two workers recording turns of one call can
        # never lose each other's counts.
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_conversation_llm_usage_model",
                set_={
                    "requests": table.c.requests + statement.excluded.requests,
                    "requests_without_usage": table.c.requests_without_usage
                    + statement.excluded.requests_without_usage,
                    "prompt_tokens": table.c.prompt_tokens + statement.excluded.prompt_tokens,
                    "cached_prompt_tokens": table.c.cached_prompt_tokens
                    + statement.excluded.cached_prompt_tokens,
                    "completion_tokens": table.c.completion_tokens
                    + statement.excluded.completion_tokens,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
