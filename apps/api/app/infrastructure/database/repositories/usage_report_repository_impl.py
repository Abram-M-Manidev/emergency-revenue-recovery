from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.repositories.usage_report_repository import UsageReportRepository
from app.domain.usage import UsageAttribution
from app.domain.usage_reconciliation import ReconciliationOutcome, ReconciliationRunStatus
from app.domain.usage_report import ModelTokenUsage, UsageHealthFacts, UsageSummary
from app.infrastructure.database.models.conversation import ConversationModel
from app.infrastructure.database.models.usage import (
    CallUsageRecordModel,
    ConversationLLMUsageModel,
    UsageReconciliationRunModel,
    VapiCallReconciliationModel,
)
from app.infrastructure.database.models.voice_call import VoiceCallModel

# Outcomes needing no operator action. MISSING_LOCALLY (a webhook we never
# received), MISSING_AT_VAPI and DISCREPANT all do; NOT_ENDED is a call still
# in progress.
_CLEAN_OUTCOMES = (
    ReconciliationOutcome.MATCHED,
    ReconciliationOutcome.FILLED,
    ReconciliationOutcome.NOT_ENDED,
)


class SqlAlchemyUsageReportRepository(UsageReportRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def summary(self, organization_id: uuid.UUID, billing_period: date) -> UsageSummary:
        records = (
            CallUsageRecordModel.organization_id == organization_id,
            CallUsageRecordModel.billing_period == billing_period,
        )
        calls, billable_calls, billable_minutes = (
            await self._session.execute(
                select(
                    func.count(),
                    func.count().filter(CallUsageRecordModel.is_billable.is_(True)),
                    func.coalesce(func.sum(CallUsageRecordModel.billable_minutes), 0),
                ).where(*records)
            )
        ).one()

        excluded = (
            await self._session.execute(
                select(CallUsageRecordModel.exclusion_reason, func.count())
                .where(*records, CallUsageRecordModel.exclusion_reason.is_not(None))
                .group_by(CallUsageRecordModel.exclusion_reason)
            )
        ).all()

        reconciled = (
            await self._session.execute(
                select(VapiCallReconciliationModel.outcome, func.count())
                .join(
                    CallUsageRecordModel,
                    CallUsageRecordModel.id == VapiCallReconciliationModel.call_usage_record_id,
                )
                .where(*records)
                .group_by(VapiCallReconciliationModel.outcome)
            )
        ).all()
        reconciliation = {outcome.value: count for outcome, count in reconciled}

        versions = (
            await self._session.execute(
                select(CallUsageRecordModel.rules_version)
                .where(*records)
                .distinct()
                .order_by(CallUsageRecordModel.rules_version)
            )
        ).scalars().all()

        # LLM tokens for conversations that STARTED in the period, so they
        # line up with the calls above as closely as the data allows.
        period_start = datetime(billing_period.year, billing_period.month, 1, tzinfo=timezone.utc)
        period_end = (
            datetime(billing_period.year + 1, 1, 1, tzinfo=timezone.utc)
            if billing_period.month == 12
            else datetime(billing_period.year, billing_period.month + 1, 1, tzinfo=timezone.utc)
        )
        tokens = (
            await self._session.execute(
                select(
                    ConversationLLMUsageModel.model,
                    func.sum(ConversationLLMUsageModel.requests),
                    func.sum(ConversationLLMUsageModel.prompt_tokens),
                    func.sum(ConversationLLMUsageModel.completion_tokens),
                )
                .join(
                    ConversationModel,
                    ConversationModel.id == ConversationLLMUsageModel.conversation_id,
                )
                .where(
                    ConversationLLMUsageModel.organization_id == organization_id,
                    ConversationModel.started_at >= period_start,
                    ConversationModel.started_at < period_end,
                )
                .group_by(ConversationLLMUsageModel.model)
                .order_by(ConversationLLMUsageModel.model)
            )
        ).all()

        return UsageSummary(
            billing_period=billing_period,
            calls=int(calls),
            billable_calls=int(billable_calls),
            billable_minutes=int(billable_minutes),
            excluded_calls={reason.value: int(count) for reason, count in excluded},
            reconciliation=reconciliation,
            unreconciled_calls=int(calls) - sum(reconciliation.values()),
            rules_versions=list(versions),
            llm_usage=[
                ModelTokenUsage(
                    model=model,
                    requests=int(requests or 0),
                    prompt_tokens=int(prompt or 0),
                    completion_tokens=int(completion or 0),
                )
                for model, requests, prompt, completion in tokens
            ],
        )

    async def health_facts(
        self, *, now: datetime, lookback_start: datetime, grace_end: datetime
    ) -> UsageHealthFacts:
        last_run = (
            await self._session.execute(
                select(UsageReconciliationRunModel.started_at, UsageReconciliationRunModel.status)
                .order_by(UsageReconciliationRunModel.started_at.desc())
                .limit(1)
            )
        ).first()
        last_success = (
            await self._session.execute(
                select(func.max(UsageReconciliationRunModel.finished_at)).where(
                    UsageReconciliationRunModel.status == ReconciliationRunStatus.SUCCEEDED
                )
            )
        ).scalar_one()

        missing = (
            await self._session.execute(
                select(func.count())
                .select_from(VoiceCallModel)
                .outerjoin(
                    CallUsageRecordModel,
                    CallUsageRecordModel.vapi_call_id == VoiceCallModel.vapi_call_id,
                )
                .where(
                    VoiceCallModel.started_at >= lookback_start,
                    VoiceCallModel.started_at < grace_end,
                    CallUsageRecordModel.id.is_(None),
                )
            )
        ).scalar_one()

        unattributed = (
            await self._session.execute(
                select(func.count()).where(
                    CallUsageRecordModel.attribution == UsageAttribution.UNATTRIBUTED,
                    CallUsageRecordModel.first_reported_at >= lookback_start,
                )
            )
        ).scalar_one()

        discrepant = (
            await self._session.execute(
                select(func.count()).where(
                    VapiCallReconciliationModel.checked_at >= lookback_start,
                    VapiCallReconciliationModel.outcome.not_in(_CLEAN_OUTCOMES),
                )
            )
        ).scalar_one()

        return UsageHealthFacts(
            now=now,
            last_run_started_at=last_run[0] if last_run else None,
            last_run_status=last_run[1].value if last_run else None,
            last_success_finished_at=last_success,
            calls_missing_usage=int(missing),
            unattributed_records=int(unattributed),
            discrepant_calls=int(discrepant),
        )
