"""Handing a live call to a person — the decision, the record, the honest result.

Executed as the `transfer_to_human` tool. Every path ends in a tool result
the assistant can say truthfully:

- INITIATED: the provider accepted the transfer. The caller has already heard
  the handoff sentence (spoken by the provider as part of the transfer), so
  the model must add nothing — and must never claim someone answered.
- UNAVAILABLE / FAILED: no transfer happened. The model tells the caller so,
  and offers the fallback: take their details so the team calls back.

Emergency handling is untouched by this service. A transfer never creates,
replaces or suppresses an emergency ticket; the business policy
"transfer emergencies to a person" is only honoured AFTER the ticket exists,
so the ticket and its alert outbox remain the primary emergency path.

Persistence: the attempt row is flushed BEFORE the provider is called and
updated after, inside the tool's own savepoint. A transfer failure can
therefore never roll back anything else in the turn (an emergency ticket
created a moment earlier included), and the webhook's hang-up drain commits
the turn even when the provider moves the call and Vapi drops the stream.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from app.domain.call_transfer.attempt import (
    CallTransferAttempt,
    DestinationKind,
    TransferFailure,
    TransferReason,
    TransferStatus,
)
from app.domain.call_transfer.port import CallTransferPort
from app.domain.call_transfer.resolution import NoDestination, is_open_at, resolve_destination
from app.domain.call_transfer.settings import mask_number
from app.domain.repositories.business_hours_repository import BusinessHoursRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.call_transfer_repository import (
    CallTransferAttemptRepository,
    CallTransferSettingsRepository,
)
from app.domain.repositories.emergency_ticket_repository import EmergencyTicketRepository
from app.domain.repositories.voice_line_repository import VoiceLineRepository
from app.shared.utils.phone import normalize_phone_number

logger = structlog.get_logger("app.call_transfer")

# Spoken by the provider as part of an ACCEPTED transfer — never before.
# Worded as what is happening ("connecting"), never as an outcome
# ("someone is on the line"), because nothing here can confirm an answer.
ANNOUNCEMENTS: dict[DestinationKind, str] = {
    DestinationKind.BUSINESS_HOURS: "I'm connecting you with someone at the office now.",
    DestinationKind.AFTER_HOURS: "I'm connecting you with our on-call team now.",
}

_AFTER_INITIATED = (
    "The transfer has been accepted and the caller has ALREADY heard: \"{announcement}\". "
    "Set message_to_customer to an empty string — anything more may talk over the handoff. "
    "Set is_conversation_complete to false. Never say that anyone has answered, joined, or is "
    "on the line: that has not been confirmed."
)
_FALLBACK = (
    "No transfer happened. Tell the caller honestly, in one short sentence, that you can't "
    "connect them to a person right now, then offer the fallback: take their name, callback "
    "number and what they need so the team can call them back. Do not promise a specific "
    "callback time. Do not say anyone has been reached."
)
_NEXT_STEP: dict[str, str] = {
    TransferFailure.EMERGENCY_TICKET_REQUIRED: (
        "This business transfers emergencies only after the emergency request is recorded. "
        "Call create_service_request first (you need the caller's name, phone and address), "
        "then call transfer_to_human again with reason emergency_policy. If the caller simply "
        "asks for a person, use reason caller_requested instead — a caller who asks is never "
        "refused a human."
    ),
}
_EMERGENCY_NOTE = (
    " This is an emergency: if the emergency request was already recorded with "
    "create_service_request, it still stands — tell the caller it is recorded, exactly as that "
    "tool's result permits and no further."
)


class CallTransferService:
    def __init__(
        self,
        *,
        settings_repository: CallTransferSettingsRepository,
        attempt_repository: CallTransferAttemptRepository,
        business_profile_repository: BusinessProfileRepository,
        business_hours_repository: BusinessHoursRepository,
        voice_line_repository: VoiceLineRepository,
        emergency_ticket_repository: EmergencyTicketRepository,
        transfer_port: CallTransferPort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings_repository
        self._attempts = attempt_repository
        self._profiles = business_profile_repository
        self._hours = business_hours_repository
        self._voice_lines = voice_line_repository
        self._tickets = emergency_ticket_repository
        self._port = transfer_port
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def transfer(
        self,
        *,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        reason: TransferReason,
        is_emergency: bool,
        call_control: str | None,
    ) -> dict[str, Any]:
        # A call already being moved is never moved again: a second request
        # (a retried turn, a repetitive model) must not re-dial.
        existing = await self._attempts.get_initiated_for_conversation(conversation_id)
        if existing is not None:
            return {
                "success": True,
                "transfer_status": TransferStatus.INITIATED.value,
                "already_in_progress": True,
                "destination": existing.destination_kind.value if existing.destination_kind else None,
                "next_step": _AFTER_INITIATED.format(
                    announcement=ANNOUNCEMENTS.get(existing.destination_kind or DestinationKind.AFTER_HOURS, "")
                ),
            }

        settings = await self._settings.get(organization_id)
        attempt = await self._attempts.add(
            CallTransferAttempt(
                id=uuid.uuid4(),
                organization_id=organization_id,
                conversation_id=conversation_id,
                reason=reason,
                is_emergency=is_emergency or reason is TransferReason.EMERGENCY_POLICY,
            )
        )

        if reason is TransferReason.EMERGENCY_POLICY:
            if settings is None:
                return await self._unavailable(attempt, TransferFailure.NOT_CONFIGURED)
            if not settings.transfer_emergencies:
                return await self._unavailable(attempt, TransferFailure.DISABLED)
            ticket = await self._tickets.get_by_conversation_id(conversation_id)
            if ticket is None:
                return await self._unavailable(attempt, TransferFailure.EMERGENCY_TICKET_REQUIRED)

        destination = resolve_destination(
            settings,
            open_now=await self._open_now(organization_id),
            ai_line_numbers=await self._ai_line_numbers(organization_id),
        )
        if isinstance(destination, NoDestination):
            return await self._unavailable(attempt, destination.error_code)
        if call_control is None:
            # No live call to move: the dashboard/text channel, or a Vapi
            # assistant without monitorPlan.controlEnabled.
            return await self._unavailable(attempt, TransferFailure.CALL_CONTROL_UNAVAILABLE)

        attempt = await self._attempts.save(attempt.resolve(destination.kind, destination.number))
        announcement = ANNOUNCEMENTS[destination.kind]
        initiation = await self._port.transfer(
            call_control=call_control,
            destination_number=destination.number,
            announcement=announcement,
        )
        if not initiation.accepted:
            attempt = await self._attempts.save(
                attempt.failed(initiation.error_code or TransferFailure.PROVIDER_ERROR)
            )
            self._log(attempt)
            return self._failure_result(attempt)

        attempt = await self._attempts.save(attempt.initiated())
        self._log(attempt)
        return {
            "success": True,
            "transfer_status": TransferStatus.INITIATED.value,
            "destination": destination.kind.value,
            "next_step": _AFTER_INITIATED.format(announcement=announcement),
        }

    async def progress_note(
        self,
        *,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        has_emergency_ticket: bool,
    ) -> str | None:
        """A line for the per-turn progress section of the prompt, or None.

        The emergency-transfer policy is stated only once the emergency is
        recorded, which is what makes "ticket first, then a person" the order
        the model follows — the transfer can never stand in for the ticket."""
        if await self._attempts.get_initiated_for_conversation(conversation_id) is not None:
            return (
                "- This call has ALREADY been handed to a person (transfer accepted). Do NOT call "
                "transfer_to_human again. Set message_to_customer to an empty string."
            )
        if not has_emergency_ticket:
            return None
        settings = await self._settings.get(organization_id)
        if settings is not None and settings.is_enabled and settings.transfer_emergencies:
            return (
                "- This business hands recorded emergencies to a person. Unless the caller has "
                "said they do not want that, call transfer_to_human now with reason "
                "emergency_policy and is_emergency true."
            )
        return None

    # --- helpers ---

    async def _unavailable(self, attempt: CallTransferAttempt, error_code: str) -> dict[str, Any]:
        attempt = await self._attempts.save(attempt.unavailable(error_code))
        self._log(attempt)
        return self._failure_result(attempt)

    def _failure_result(self, attempt: CallTransferAttempt) -> dict[str, Any]:
        next_step = _NEXT_STEP.get(attempt.error_code or "", _FALLBACK)
        if attempt.is_emergency and attempt.error_code != TransferFailure.EMERGENCY_TICKET_REQUIRED:
            next_step += _EMERGENCY_NOTE
        return {
            "success": False,
            "error": attempt.error_code,
            "transfer_status": attempt.status.value,
            "next_step": next_step,
        }

    async def _open_now(self, organization_id: uuid.UUID) -> bool | None:
        weekly = await self._hours.get_weekly(organization_id)
        if not weekly:
            return None
        profile = await self._profiles.get_by_organization_id(organization_id)
        now = self._clock()
        local = now.astimezone(ZoneInfo(profile.timezone)) if profile is not None else now
        exceptions = await self._hours.list_exceptions(organization_id)
        return is_open_at(local, weekly, exceptions)

    async def _ai_line_numbers(self, organization_id: uuid.UUID) -> frozenset[str]:
        line = await self._voice_lines.get_by_organization_id(organization_id)
        number = normalize_phone_number(line.phone_number) if line is not None else None
        return frozenset({number}) if number else frozenset()

    @staticmethod
    def _log(attempt: CallTransferAttempt) -> None:
        logger.info(
            "call_transfer_attempted",
            status=attempt.status.value,
            reason=attempt.reason.value,
            is_emergency=attempt.is_emergency,
            destination_kind=attempt.destination_kind.value if attempt.destination_kind else None,
            destination=mask_number(attempt.destination_number),
            error_code=attempt.error_code,
            organization_id=str(attempt.organization_id),
            conversation_id=str(attempt.conversation_id),
        )
