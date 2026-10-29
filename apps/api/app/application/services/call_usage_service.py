"""Records one call's usage from its end-of-call report — see
`app/domain/usage.py` for the billing rules.

Runs after everything the end-of-call report does for the caller (marking
the call ended, completing the conversation, the follow-up and any ticket it
opens), in its own savepoint, and swallows every failure: metering is
bookkeeping, and must never cost a caller anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone

import structlog

from app.domain.repositories.usage_repository import CallUsageRepository
from app.domain.repositories.voice_call_repository import VoiceCallRepository
from app.domain.repositories.voice_line_repository import VoiceLineRepository
from app.domain.transactions import NullSavepoints, Savepoints
from app.domain.usage import (
    CallUsageFacts,
    CallUsageRecord,
    UsageAttribution,
    decide_billing,
    merge_facts,
)

logger = structlog.get_logger("app.usage")


class CallUsageService:
    def __init__(
        self,
        *,
        usage_repository: CallUsageRepository,
        voice_call_repository: VoiceCallRepository,
        voice_line_repository: VoiceLineRepository,
        savepoints: Savepoints | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._usage = usage_repository
        self._voice_calls = voice_call_repository
        self._voice_lines = voice_line_repository
        self._savepoints = savepoints or NullSavepoints()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def record_end_of_call(self, facts: CallUsageFacts) -> CallUsageRecord | None:
        """Returns the stored record, or None when recording failed."""
        try:
            # Savepoint inside the `try`: catching a database error does not
            # un-abort a Postgres transaction.
            async with self._savepoints.isolate():
                record = await self._record(facts)
        except Exception as exc:
            # Type only: a driver error quotes the offending parameters.
            logger.error(
                "call_usage_record_failed",
                vapi_call_id=facts.vapi_call_id,
                error=type(exc).__name__,
            )
            return None
        logger.info(
            "call_usage_recorded",
            vapi_call_id=record.vapi_call_id,
            organization_id=str(record.organization_id) if record.organization_id else None,
            attribution=record.attribution.value,
            billing_period=record.decision.billing_period.isoformat(),
            billable_minutes=record.decision.billable_minutes,
            exclusion_reason=(
                record.decision.exclusion_reason.value if record.decision.exclusion_reason else None
            ),
            duration_source=record.decision.duration_source.value,
            has_vapi_cost=record.facts.vapi_reported_cost is not None,
            report_count=record.report_count,
        )
        return record

    async def _record(self, incoming: CallUsageFacts) -> CallUsageRecord:
        received_at = self._clock()
        existing = await self._usage.get_for_update(incoming.vapi_call_id)
        if existing is None:
            organization_id, conversation_id, attribution = await self._attribute(incoming)
            record = CallUsageRecord(
                id=uuid.uuid4(),
                organization_id=organization_id,
                conversation_id=conversation_id,
                attribution=attribution,
                facts=incoming,
                decision=decide_billing(incoming, received_at=received_at),
                report_count=1,
                first_reported_at=received_at,
                last_reported_at=received_at,
            )
            if await self._usage.insert_if_absent(record):
                return record
            # A concurrent delivery of the same report won the insert.
            existing = await self._usage.get_for_update(incoming.vapi_call_id)
            assert existing is not None

        facts = merge_facts(existing.facts, incoming)
        organization_id, conversation_id, attribution = (
            existing.organization_id,
            existing.conversation_id,
            existing.attribution,
        )
        if attribution is UsageAttribution.UNATTRIBUTED:
            # A line provisioned since the first report may now match. An
            # attributed record is never re-attributed.
            organization_id, conversation_id, attribution = await self._attribute(facts)
        return await self._usage.update(
            CallUsageRecord(
                id=existing.id,
                organization_id=organization_id,
                conversation_id=conversation_id,
                attribution=attribution,
                facts=facts,
                # Re-decided from the merged facts, period anchored on the
                # FIRST delivery, so a duplicate can never move a call
                # between months on its own.
                decision=decide_billing(facts, received_at=existing.first_reported_at),
                report_count=existing.report_count + 1,
                first_reported_at=existing.first_reported_at,
                last_reported_at=received_at,
            )
        )

    async def _attribute(
        self, facts: CallUsageFacts
    ) -> tuple[uuid.UUID | None, uuid.UUID | None, UsageAttribution]:
        voice_call = await self._voice_calls.get_by_vapi_call_id(facts.vapi_call_id)
        if voice_call is not None:
            return voice_call.organization_id, voice_call.conversation_id, UsageAttribution.VOICE_CALL

        # Same routing keys, same precedence, as the call path's
        # `VoiceService._resolve_voice_line`: an assistant id we do not know
        # is never resolved through the phone number instead — that would
        # bill one tenant for an assistant nobody provisioned to it. Unlike
        # the call path, an inactive line still attributes: its tenant
        # incurred the cost all the same.
        voice_line = None
        if facts.vapi_assistant_id:
            voice_line = await self._voice_lines.get_by_vapi_assistant_id(facts.vapi_assistant_id)
        elif facts.vapi_phone_number_id:
            voice_line = await self._voice_lines.get_by_vapi_phone_number_id(
                facts.vapi_phone_number_id
            )
        if voice_line is not None:
            return voice_line.organization_id, None, UsageAttribution.VOICE_LINE

        logger.warning(
            "call_usage_unattributed",
            vapi_call_id=facts.vapi_call_id,
            has_assistant_id=facts.vapi_assistant_id is not None,
            has_phone_number_id=facts.vapi_phone_number_id is not None,
        )
        return None, None, UsageAttribution.UNATTRIBUTED
