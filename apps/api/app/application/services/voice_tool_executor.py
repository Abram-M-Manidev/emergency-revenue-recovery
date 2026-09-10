"""Executes the AI Brain's business tools against the real services.

This is the only place a tool name becomes a database write. It owns no
business rules of its own — every rule already lives in `DispatchService`,
`AppointmentService`, `CustomerService`, or the availability engine — so
what this module actually does is narrower than it looks: validate model
output, translate it into the vocabulary those services already speak, and
translate their answers (and their exceptions) back into flat JSON the model
can branch on.

Reuse of the existing seam
--------------------------
`create_service_request` deliberately does *not* insert a ticket or an
appointment itself. It writes the `ConversationOutcome` that
`sync_ticket_from_outcome` / `sync_appointment_from_outcome` /
`sync_customer_from_outcome` already read, then calls those three in the
same order the webhook does. So a tool-driven turn and a pre-tool turn
converge on identical records through identical code, and Dispatch,
Customers, and Analytics needed no changes at all.

Never raises
------------
Every public path returns a `ToolResult`. A tool that raised would abort the
turn mid-sentence, which on a live phone call is a silent hang-up; a
structured `{"success": false, "error": ...}` instead lets the assistant
apologise, ask for the missing detail, or offer another slot — which is the
entire point of giving it tools.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date as py_date
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.application.services.appointment_service import AppointmentService
from app.application.services.customer_service import CustomerService
from app.application.services.dispatch_service import DispatchService
from app.application.services.emergency_notification_service import (
    EmergencyNotificationService,
)
from app.core.config import Settings
from app.domain.ai.tools import (
    BOOK_APPOINTMENT,
    CHECK_AVAILABILITY,
    CREATE_SERVICE_REQUEST,
    SELECT_APPOINTMENT_SLOT,
    ToolErrors,
    ToolExecutor,
    ToolExecutorFactory,
    ToolInvocation,
    ToolResult,
)
from app.domain.entities.appointment import Appointment
from app.domain.entities.availability import AvailabilityQuery, AvailabilitySlot
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.offered_slot import SlotSelectionVerdict
from app.domain.entities.service import Service
from app.domain.exceptions import (
    AppointmentOutsideBusinessHoursError,
    AppointmentSlotInThePastError,
    AppointmentSlotUnavailableError,
    AvailabilityUnavailableError,
    DomainError,
    EntityNotFoundError,
    InvalidAppointmentStatusTransitionError,
    SlotNotOfferedError,
    SlotNotSelectedError,
)
from app.domain.notifications.emergency import DeliveryStatus, NotificationDelivery
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.offered_slot_repository import OfferedSlotRepository
from app.domain.repositories.service_repository import ServiceRepository
from app.shared.logging.timing import elapsed_ms, now
from app.shared.utils.phone import normalize_phone_number

logger = structlog.get_logger("app.voice.tools")

_DEFAULT_CONFIDENCE = 0.9

_CLASSIFICATIONS = {
    "emergency": CallClassification.EMERGENCY,
    "non_emergency": CallClassification.NON_EMERGENCY,
}


class VoiceToolExecutor(ToolExecutorFactory):
    """Unbound executor: holds the services, knows nothing about which call
    it is serving.

    Bound to a conversation via `bind()`, because the organization and
    conversation must come from the authenticated request context
    (`VoiceLine` resolution, or the JWT on the text path) and must never be
    taken from tool arguments. That separation is the tenant-isolation
    guarantee: there is no argument a model could emit — hallucinated or
    injected via the caller's speech — that could reach another
    organization's data, because no tool accepts an organization at all."""

    def __init__(
        self,
        *,
        appointment_service: AppointmentService,
        dispatch_service: DispatchService,
        customer_service: CustomerService,
        conversation_outcome_repository: ConversationOutcomeRepository,
        service_repository: ServiceRepository,
        business_profile_repository: BusinessProfileRepository,
        offered_slot_repository: OfferedSlotRepository,
        settings: Settings,
        # Optional so every pre-existing construction site keeps working.
        # Absent, an emergency ticket is still created and the assistant is
        # simply never permitted to claim a dispatcher was alerted — the
        # honest degradation, and the one the whole design fails towards.
        emergency_notification_service: EmergencyNotificationService | None = None,
    ) -> None:
        self._appointments = appointment_service
        self._dispatch = dispatch_service
        self._customers = customer_service
        self._outcomes = conversation_outcome_repository
        self._services = service_repository
        self._profiles = business_profile_repository
        self._offered_slots = offered_slot_repository
        self._settings = settings
        self._notifications = emergency_notification_service

    def bind(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        turn_index: int,
    ) -> ToolExecutor:
        return _BoundToolExecutor(self, organization_id, conversation_id, turn_index)

    async def describe_progress(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> str | None:
        """See `ToolExecutorFactory.describe_progress`. Reads the records
        this call has already produced and states them as instructions, so
        the model resumes rather than restarts."""
        ticket = await self._dispatch.get_ticket_for_conversation(
            organization_id, conversation_id
        )
        if ticket is not None:
            # Read back from the delivery record rather than assumed. This
            # previously asserted "dispatcher alerted" on every later turn of
            # an emergency call, which put the false sentence back into the
            # prompt even once the tool result itself had stopped claiming it.
            alerted = await self._dispatcher_was_alerted(organization_id, ticket.id)
            if alerted:
                reassurance = (
                    "- A dispatcher has been alerted and will contact them. "
                    "You may say so, and should reassure them someone will be "
                    "in touch shortly."
                )
            else:
                reassurance = (
                    "- The emergency IS recorded for the team, but the alert "
                    "to a dispatcher could NOT be confirmed. Do NOT tell the "
                    "caller a dispatcher has been alerted, notified, or is on "
                    "the way. Say their emergency is logged and the team will "
                    "see it, and tell them to call the business directly — or "
                    "the emergency services — if it is dangerous right now."
                )
            return (
                "Progress on this call (from the business's records):\n"
                "- An emergency ticket has already been created. Do NOT call "
                "create_service_request again, and do NOT offer or attempt an "
                "appointment.\n" + reassurance
            )

        appointment = await self._appointments.get_appointment_for_conversation(
            organization_id, conversation_id
        )
        if appointment is None:
            return None

        lines = [
            "Progress on this call (from the business's records, not from "
            "memory — trust these over your own recollection):",
            "- A service request has already been created for this caller. "
            "Do NOT call create_service_request again unless the caller "
            "corrects a detail.",
        ]

        if appointment.scheduled_start_at is not None:
            zone = await self._zone_for(organization_id)
            local = appointment.scheduled_start_at.astimezone(zone)
            lines.append(
                f'- The appointment is BOOKED for {_spoken(local)}. This is '
                "confirmed and you may say so, as a standard appointment — "
                "never as emergency service or an emergency dispatch. Only "
                "call book_appointment again if the caller asks to move it."
            )
        else:
            lines.append(
                "- No appointment time is booked yet. Nothing is scheduled "
                "until book_appointment has returned \"success\": true, so do "
                "not say otherwise and do not end the call before then."
            )
            # A choice the caller already made must survive into the next
            # turn. Without this the model sees "not booked", re-offers the
            # same list, and asks a caller who has already answered to answer
            # again — the same amnesia `_bookable_now_lines` exists to fix,
            # one rung further down the ladder.
            selection = await self._appointments.get_active_selection_for_conversation(
                organization_id, conversation_id
            )
            if selection is not None:
                zone = await self._zone_for(organization_id)
                chosen = selection.start_at.astimezone(zone)
                lines.append(
                    f"- The caller has ALREADY chosen {_spoken(chosen)}. Do "
                    "not offer times again and do not ask them to choose "
                    "again — call book_appointment for that time now."
                )
            else:
                lines.extend(await self._bookable_now_lines(organization_id, appointment))
        return "\n".join(lines)

    async def _bookable_now_lines(
        self, organization_id: uuid.UUID, appointment: Appointment
    ) -> list[str]:
        """Whether there is any availability worth telling the model about —
        deliberately not which times, and never a bookable argument.

        Exists because tool results are not in the transcript: on a new turn
        the model can see that it once discussed times but not whether any
        remain, and without this it would either re-run intake or tell the
        caller nothing is free. Saying "there is availability, go and fetch
        it" resolves that without handing over a shortcut.

        An earlier version listed the times *and* their `date=`/`start_time=`
        arguments. That fixed the amnesia and created a worse problem: on
        2026-08-23 the model took one of those arguments and booked a Monday
        morning the caller had never been read, with no availability check
        and no choice made. The times now come only from
        `check_availability`, whose results are recorded as offers, and
        `book_for_conversation` refuses anything not in that record.

        Best-effort: this is an enhancement to the prompt, not a precondition
        for answering the caller, so a failure here degrades to the model
        calling `check_availability` itself — which is what it should do
        anyway."""
        try:
            result = await self._appointments.find_availability(
                organization_id,
                AvailabilityQuery(
                    service_id=appointment.matched_service_id,
                    exclude_appointment_id=appointment.id,
                ),
            )
        except DomainError:
            logger.warning("tool_progress_availability_failed", exc_info=True)
            return []

        if not result.slots:
            return [
                "- There is no availability in the next few days. Do not "
                "invent a time; offer to take the caller's preferences for a "
                "callback."
            ]

        # Deliberately says only *that* times exist — never which ones, and
        # never a ready-made `date=`/`start_time=` pair. Listing them here
        # put a valid booking argument in front of the model on every turn,
        # and on 2026-08-23 it used one: the caller gave their details and
        # the model booked a Monday morning slot it had never read out, with
        # no availability check and no choice made. Naming the times is
        # `check_availability`'s job, and going through it is what records
        # the offer that `book_appointment` then requires.
        return [
            f"- There is availability in the next few days ({len(result.slots)} "
            "times found just now). You do NOT have those times here, and you "
            "must not guess or reuse one. Call check_availability to get the "
            "real slots, read them to the caller, and book only the one they "
            "choose — book_appointment will refuse any time this conversation "
            "was not offered."
        ]

    # --- Tool implementations ---

    async def _create_service_request(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        arguments: dict[str, Any],
        turn_index: int,
    ) -> dict[str, Any]:
        missing = [
            field
            for field in ("customer_name", "customer_phone", "service_address", "problem_description")
            if _is_blank(arguments.get(field))
        ]
        if missing:
            # Named rather than a generic failure so the assistant can ask
            # for precisely what it still needs instead of restarting the
            # whole intake.
            return {
                "success": False,
                "error": ToolErrors.MISSING_REQUIRED_FIELDS,
                "missing_fields": missing,
            }

        classification = _CLASSIFICATIONS.get(str(arguments.get("classification", "")).strip())
        if classification is None:
            return {
                "success": False,
                "error": ToolErrors.INVALID_ARGUMENTS,
                "detail": "classification must be 'emergency' or 'non_emergency'.",
            }

        matched_service = await self._match_service(organization_id, arguments.get("service_name"))
        recommended_action = (
            RecommendedAction.CREATE_EMERGENCY_TICKET
            if classification is CallClassification.EMERGENCY
            else RecommendedAction.BOOK_APPOINTMENT
        )

        existing = await self._outcomes.get_by_conversation_id(conversation_id)
        await self._outcomes.upsert(
            conversation_id,
            classification=classification,
            confidence=existing.confidence if existing is not None else _DEFAULT_CONFIDENCE,
            recommended_action=recommended_action,
            matched_service_id=matched_service.id if matched_service else None,
            customer_name=_clean(arguments["customer_name"]),
            # Canonicalised here too, so the snapshot copied onto the
            # ticket/appointment matches the customer key rather than
            # whatever spacing the transcript happened to use.
            customer_phone=normalize_phone_number(arguments["customer_phone"])
            or _clean(arguments["customer_phone"]),
            customer_address=_clean(arguments["service_address"]),
            summary=_clean(arguments["problem_description"]) or "Service request.",
        )

        # The existing AI-Brain -> module seam, called in the same order the
        # webhook already calls it, so a tool-driven turn produces exactly
        # the records a pre-tool turn did.
        ticket = await self._dispatch.sync_ticket_from_outcome(organization_id, conversation_id)
        appointment = await self._appointments.sync_appointment_from_outcome(
            organization_id, conversation_id
        )
        customer = await self._customers.sync_customer_from_outcome(
            organization_id, conversation_id
        )

        if ticket is not None:
            # The alert is attempted here, inside the tool call, rather than
            # after the turn — because the assistant is about to speak, and
            # what it is allowed to say depends on whether this succeeded.
            # Bounded and non-raising: a failure costs the caller a weaker
            # sentence, never the ticket and never the call.
            delivery = await self._notify_dispatcher(ticket)
            return {
                "success": True,
                "service_request_id": str(ticket.id),
                "service_request_type": "emergency_ticket",
                "status": ticket.status.value,
                "priority": "emergency",
                **_dispatcher_alert_fields(delivery),
                "customer_id": str(customer.id) if customer else None,
                "service_name": matched_service.name if matched_service else None,
                "bookable": False,
                "next_step": _dispatcher_next_step(delivery),
            }

        if appointment is not None:
            return {
                "success": True,
                "service_request_id": str(appointment.id),
                "service_request_type": "appointment",
                "status": appointment.status.value,
                "priority": "standard",
                "customer_id": str(customer.id) if customer else None,
                "service_name": matched_service.name if matched_service else None,
                "duration_minutes": appointment.duration_minutes,
                "bookable": True,
                "next_step": "Call check_availability next.",
            }

        # Neither sync produced a record. Reported honestly rather than as a
        # success with a null id — the assistant must not go on to promise a
        # booking against a request that does not exist.
        logger.error(
            "create_service_request_produced_no_record",
            organization_id=str(organization_id),
            conversation_id=str(conversation_id),
            classification=classification.value,
        )
        return {"success": False, "error": ToolErrors.INTERNAL_ERROR}

    async def _check_availability(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        arguments: dict[str, Any],
        turn_index: int,
    ) -> dict[str, Any]:
        matched_service = await self._match_service(organization_id, arguments.get("service_name"))

        preferred_date = _parse_date(arguments.get("preferred_date"))
        if preferred_date is None and not _is_blank(arguments.get("preferred_date")):
            return {
                "success": False,
                "error": ToolErrors.INVALID_ARGUMENTS,
                "detail": "preferred_date must be YYYY-MM-DD.",
            }

        # This conversation's own appointment must not count against it. It
        # already holds a slot only because the caller chose one, so counting
        # it as a conflict hides the very time they picked — see
        # `AvailabilityQuery.exclude_appointment_id`. None whenever the call
        # has not booked yet, which leaves the search unchanged.
        own_appointment = await self._appointments.get_appointment_for_conversation(
            organization_id, conversation_id
        )
        own_appointment_id = own_appointment.id if own_appointment else None

        query = AvailabilityQuery(
            service_id=matched_service.id if matched_service else None,
            preferred_date=preferred_date,
            earliest_time=_parse_time(arguments.get("earliest_time")),
            latest_time=_parse_time(arguments.get("latest_time")),
            days_to_search=_parse_int(arguments.get("days_to_search")),
            exclude_appointment_id=own_appointment_id,
        )
        result = await self._appointments.find_availability(organization_id, query)

        # A requested day with nothing on it is a dead end for the caller
        # unless something else is offered. A live call asked for "tomorrow",
        # which was a Sunday the business is closed on: the search returned
        # nothing, the assistant reported nothing, and the conversation
        # stalled — while three slots existed the following working day.
        # Widening once, and saying so, turns that into a real choice.
        widened = False
        if not result.slots and (preferred_date is not None or query.earliest_time is not None):
            # Every constraint is dropped, including `days_to_search`. A
            # caller asking about one specific day usually makes the model
            # narrow the window to that day, and inheriting that narrowing
            # would search the same empty ground again.
            result = await self._appointments.find_availability(
                organization_id,
                AvailabilityQuery(
                    service_id=query.service_id,
                    # Carried across: the widened search is the same
                    # conversation asking again, so it must not start
                    # counting that conversation's own slot as taken.
                    exclude_appointment_id=own_appointment_id,
                ),
            )
            widened = bool(result.slots)

        # Recorded before the result is handed back, so a slot can only be
        # booked after the caller has genuinely been offered it. This is the
        # half of the consent invariant that a prompt rule cannot provide:
        # `book_for_conversation` refuses any time absent from this record.
        if result.slots:
            await self._offered_slots.record_offered(
                organization_id, conversation_id, result.slots, turn_index
            )

        zone = _zone_or_utc(result.timezone)
        slots = [_render_slot(slot, zone) for slot in result.slots]
        payload: dict[str, Any] = {
            "success": True,
            "timezone": result.timezone,
            "duration_minutes": result.duration_minutes,
            "slots": slots,
        }
        if slots:
            # Spelled out because tool results are not part of the stored
            # transcript: on the next turn the model will have its own
            # spoken sentence and nothing else, so a `slot_id` it is holding
            # now is gone by then. Telling it here that date plus start_time
            # is equally valid is what lets a caller say "the first one" a
            # turn later and actually get booked — without this, the model
            # had no identifier it could still quote and simply re-offered
            # the same list indefinitely.
            payload["next_step"] = (
                "Offer these to the caller with their dates. When they choose "
                "one, call book_appointment right away — with slot_id if you "
                "still have it this turn, otherwise with the chosen slot's "
                "date and start_time."
            )
            if widened:
                # Said explicitly so the assistant does not present these as
                # though they were on the day the caller asked for.
                payload["widened_search"] = True
                payload["widened_from"] = (
                    preferred_date.isoformat() if preferred_date else None
                )
                payload["next_step"] = (
                    "Nothing was free in the window the caller asked for, so "
                    "this is the nearest availability instead. Tell them that "
                    "plainly, offer these times with their dates, and book "
                    "whichever they choose."
                )
        if not slots:
            # An empty list is a real answer, not a failure — but the
            # assistant needs to know *why* so it offers to widen the search
            # rather than telling the caller the business is closed forever.
            payload["reason"] = "NO_SLOTS_IN_RANGE"
            payload["next_step"] = (
                "No slots matched. Offer to look further ahead, or take the "
                "caller's preferred times and tell them the office will call back."
            )
        return payload

    async def _select_appointment_slot(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        arguments: dict[str, Any],
        turn_index: int,
    ) -> dict[str, Any]:
        """Records the caller's choice — the rung between offering a time and
        booking it.

        The division of labour is the point. Resolving "the first one", "the
        9:30", "yeah that works" or "the morning one" to an instant is
        language understanding, and the model does it; nothing here parses
        the caller's words, and there is deliberately no keyword table to go
        stale. What this enforces is narrower and not negotiable: that the
        instant it names was offered to *this* conversation, and that the
        caller has actually spoken since hearing it."""
        ticket = await self._dispatch.get_ticket_for_conversation(
            organization_id, conversation_id
        )
        if ticket is not None:
            # Same ordering as booking: an emergency call must not be walked
            # down the appointment path at all, and saying so here stops the
            # model looping through selection to reach a booking it can never
            # complete.
            return {
                "success": False,
                "error": ToolErrors.EMERGENCY_NOT_BOOKABLE,
                "next_step": (
                    "This call was classified as an emergency. Do not select "
                    "or book an appointment time."
                ),
            }

        resolved = await self._resolve_requested_slot(organization_id, arguments)
        if resolved is None:
            logger.info(
                "select_slot_unresolvable",
                had_slot_id=not _is_blank(arguments.get("slot_id")),
                had_date=not _is_blank(arguments.get("date")),
                had_start_time=not _is_blank(arguments.get("start_time")),
            )
            return {
                "success": False,
                "error": ToolErrors.INVALID_SLOT,
                "detail": (
                    "Could not identify a time. Supply the slot_id from a "
                    "check_availability result, or date as YYYY-MM-DD and "
                    "start_time as HH:MM in 24-hour form (9 AM is \"09:00\")."
                ),
            }
        start_at, _ = resolved

        verdict, offered = await self._appointments.select_slot_for_conversation(
            organization_id,
            conversation_id,
            start_at=start_at,
            turn_index=turn_index,
        )

        if verdict is SlotSelectionVerdict.NOT_OFFERED:
            logger.info(
                "select_slot_not_offered",
                organization_id=str(organization_id),
                conversation_id=str(conversation_id),
                turn_index=turn_index,
            )
            return {
                "success": False,
                "error": ToolErrors.SLOT_NOT_OFFERED,
                "selection_state": "none",
                "next_step": (
                    "That time was never offered to this caller. Call "
                    "check_availability, read them the times it returns, and "
                    "record whichever they choose."
                ),
            }

        if verdict is SlotSelectionVerdict.NOT_YET_HEARD:
            # The observed failure, refused at the point it is made rather
            # than at the write. Logged with both indices because "offered
            # and chosen in one breath" is the signature to watch for.
            logger.info(
                "select_slot_not_yet_heard",
                organization_id=str(organization_id),
                conversation_id=str(conversation_id),
                turn_index=turn_index,
                offered_turn_index=offered.offered_turn_index if offered else None,
            )
            return {
                "success": False,
                "error": ToolErrors.SLOT_NOT_YET_HEARD,
                "selection_state": "none",
                "next_step": (
                    "You have only just offered this time — the caller has "
                    "not answered yet. Read them the options, stop, and wait "
                    "for their reply before recording a choice or booking "
                    "anything."
                ),
            }

        assert offered is not None  # RECORDED always carries the row.
        zone = await self._zone_for(organization_id)
        local = offered.start_at.astimezone(zone)
        return {
            "success": True,
            "selection_state": "selected",
            "date": local.date().isoformat(),
            "start_time": local.strftime("%H:%M"),
            "timezone": str(zone),
            "spoken_time": _spoken(local),
            "duration_minutes": offered.duration_minutes,
            "next_step": (
                "The caller's choice is recorded. Call book_appointment for "
                "this same time now. Nothing is reserved until that returns "
                "\"success\": true, so do not tell them it is booked yet."
            ),
        }

    async def _book_appointment(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        arguments: dict[str, Any],
        turn_index: int,
    ) -> dict[str, Any]:
        # An emergency call produced a ticket and no appointment. Checked
        # before anything else because the alternative failure —
        # `NO_SERVICE_REQUEST` from the missing appointment — tells the
        # assistant to create a service request and retry, which would loop
        # it against a call that must never be booked.
        ticket = await self._dispatch.get_ticket_for_conversation(
            organization_id, conversation_id
        )
        if ticket is not None:
            return {
                "success": False,
                "error": ToolErrors.EMERGENCY_NOT_BOOKABLE,
                "next_step": (
                    "This call was classified as an emergency, so it is not "
                    "bookable. Do not book an appointment. Do not state "
                    "whether a dispatcher was alerted — that was already "
                    "settled by create_service_request's result, and "
                    "repeating it here would be a guess."
                ),
            }

        resolved = await self._resolve_requested_slot(organization_id, arguments)
        if resolved is None:
            # Which identifying fields the model supplied, never their
            # values — enough to tell "it sent nothing" from "it sent a
            # malformed date", which is exactly what a live INVALID_SLOT
            # failure needed and could not answer.
            logger.info(
                "book_appointment_slot_unresolvable",
                had_slot_id=not _is_blank(arguments.get("slot_id")),
                had_date=not _is_blank(arguments.get("date")),
                had_start_time=not _is_blank(arguments.get("start_time")),
            )
            return {
                "success": False,
                "error": ToolErrors.INVALID_SLOT,
                "detail": (
                    "Could not identify a time. Call check_availability now "
                    "and book with a slot_id from its result, in this same "
                    "turn. A slot_id from an earlier turn is no longer valid. "
                    "If you use date and start_time instead, they must be "
                    "YYYY-MM-DD and HH:MM in 24-hour form (9 AM is \"09:00\") "
                    "and must match a time this caller was offered."
                ),
            }
        start_at, duration_minutes = resolved

        try:
            appointment = await self._appointments.book_for_conversation(
                organization_id,
                conversation_id,
                start_at=start_at,
                duration_minutes=duration_minutes,
            )
        except SlotNotSelectedError:
            # Diagnostics only — re-raised untouched. This is the refusal that
            # makes the consent invariant real, so it must be visible in the
            # logs as its own event rather than as a generic booking failure.
            await self._log_slot_not_selected(
                organization_id, conversation_id, start_at, turn_index
            )
            raise
        except SlotNotOfferedError:
            # Diagnostics only — re-raised untouched, so the refusal and its
            # error code are exactly what they were. This exists because the
            # 2026-08-23 refusal recorded nothing but its own name: the
            # requested instant was unrecoverable afterwards, leaving a
            # 12/24-hour slip and a timezone slip indistinguishable.
            await self._log_slot_not_offered(
                organization_id, conversation_id, arguments, start_at
            )
            raise

        zone = await self._zone_for(organization_id)
        local_start = appointment.scheduled_start_at.astimezone(zone)  # type: ignore[union-attr]
        local_end = local_start + timedelta(minutes=appointment.duration_minutes or 0)
        return {
            "success": True,
            "appointment_id": str(appointment.id),
            "status": "confirmed",
            "date": local_start.date().isoformat(),
            "start_time": local_start.strftime("%H:%M"),
            "end_time": local_end.strftime("%H:%M"),
            "timezone": str(zone),
            "spoken_time": _spoken(local_start),
            "duration_minutes": appointment.duration_minutes,
            # Restated at the exact moment the assistant is about to
            # confirm, because that is where it went wrong on a live call:
            # a correctly-classified non-emergency AC fault was booked as a
            # standard appointment and then confirmed with "an emergency
            # technician will be dispatched". The prompt says the same
            # thing, but a rule adjacent to the result is the one the model
            # is reading when it composes the sentence.
            "next_step": (
                "Confirm this as a standard appointment: the date, the time, "
                "and that a technician will come then. Do NOT describe it as "
                "emergency service, an emergency technician, or a dispatch."
            ),
        }

    # --- Shared helpers ---

    async def _log_slot_not_selected(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        start_at: datetime,
        turn_index: int,
    ) -> None:
        """Records a consent refusal in derived fields only.

        Same discipline as `_log_slot_not_offered`: calendar instants,
        organization settings and counts, never the raw arguments. What this
        adds is the shape of the disagreement — what the caller actually
        chose, if anything, versus what the model tried to book — which is
        the difference between "the model booked the wrong one of three" and
        "the model booked with no choice on record at all".

        Best-effort: a diagnostic must never turn a clean refusal into a
        failed turn."""
        try:
            selection = await self._appointments.get_active_selection_for_conversation(
                organization_id, conversation_id
            )
            offered = await self._offered_slots.list_offered_starts(
                organization_id, conversation_id
            )
            logger.info(
                "book_appointment_slot_not_selected",
                organization_id=str(organization_id),
                conversation_id=str(conversation_id),
                requested_start_at=start_at.astimezone(timezone.utc).isoformat(),
                selected_start_at=(
                    selection.start_at.astimezone(timezone.utc).isoformat()
                    if selection
                    else None
                ),
                has_selection=selection is not None,
                selected_turn_index=selection.selected_turn_index if selection else None,
                turn_index=turn_index,
                offered_count=len(offered),
            )
        except Exception:
            logger.warning(
                "book_appointment_slot_not_selected_diagnostics_failed", exc_info=True
            )

    async def _log_slot_not_offered(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        arguments: dict[str, Any],
        start_at: datetime,
    ) -> None:
        """Records why a booking was refused, in derived fields only.

        Everything here is either a calendar instant, an organization
        setting, or a boolean about which argument shape the model chose.
        The raw `arguments` dict is deliberately never logged: the model can
        misfill a field, and an address landing in a log line is exactly the
        kind of leak the rest of this module's logging avoids.

        Best-effort — a diagnostic must never turn a clean refusal into a
        failed turn."""
        try:
            offered = await self._offered_slots.list_offered_starts(
                organization_id, conversation_id
            )
            nearest = (
                min(offered, key=lambda candidate: abs(candidate - start_at))
                if offered
                else None
            )
            zone = await self._zone_for(organization_id)
            logger.info(
                "book_appointment_slot_not_offered",
                requested_start_at=start_at.astimezone(timezone.utc).isoformat(),
                organization_timezone=str(zone),
                organization_id=str(organization_id),
                conversation_id=str(conversation_id),
                had_slot_id=not _is_blank(arguments.get("slot_id")),
                had_date=not _is_blank(arguments.get("date")),
                had_start_time=not _is_blank(arguments.get("start_time")),
                slot_id_parsed=(
                    AvailabilitySlot.parse_slot_id(str(arguments.get("slot_id") or "").strip())
                    is not None
                ),
                offered_count=len(offered),
                nearest_offered_start_at=(
                    nearest.astimezone(timezone.utc).isoformat() if nearest else None
                ),
            )
        except Exception:
            logger.warning("book_appointment_slot_not_offered_diagnostics_failed", exc_info=True)

    async def _resolve_requested_slot(
        self, organization_id: uuid.UUID, arguments: dict[str, Any]
    ) -> tuple[datetime, int | None] | None:
        """A `slot_id` is preferred because it carries the duration the
        availability search actually used. An explicit date+time is accepted
        as a fallback for the case where the model paraphrases instead of
        quoting — the duration then falls back to the appointment's own,
        which `book_for_conversation` resolves."""
        slot_id = arguments.get("slot_id")
        if not _is_blank(slot_id):
            parsed = AvailabilitySlot.parse_slot_id(str(slot_id).strip())
            if parsed is not None:
                return parsed[0], parsed[1]
            # A malformed slot_id is not fatal on its own — fall through to
            # date/time if the model supplied those too.

        day = _parse_date(arguments.get("date"))
        at = _parse_time(arguments.get("start_time"))
        if day is None or at is None:
            return None
        zone = await self._zone_for(organization_id)
        return datetime.combine(day, at).replace(tzinfo=zone), None

    async def _match_service(self, organization_id: uuid.UUID, name: Any) -> Service | None:
        """Resolves a service by the name the model quoted.

        Case- and whitespace-insensitive, and falls back to a containment
        match, because the model is transcribing a catalogue entry from a
        prompt into an argument and "AC Repair" for "Air Conditioning
        Repair" is a near-certainty on a voice call. An unmatched name is
        never an error — it only means the visit length falls back to the
        default."""
        if _is_blank(name):
            return None
        needle = str(name).strip().lower()
        services = [s for s in await self._services.list(organization_id) if s.is_active]
        for service in services:
            if service.name.lower() == needle:
                return service
        for service in services:
            if needle in service.name.lower() or service.name.lower() in needle:
                return service

        # Deliberately no fuzzy scoring and no abbreviation table. Picking the
        # "closest" catalogue entry for something like "AC repair" would have
        # to choose between three services whose names all contain "repair",
        # and a wrong choice silently applies a wrong visit length — which
        # then offers slots of the wrong size and can double-book a
        # technician. Falling back to the default duration is the safe miss.
        #
        # Logged because the miss is otherwise invisible: the caller still
        # gets an appointment, just a default-length one, and this is the
        # only signal that the prompt's service catalogue needs rewording.
        logger.info(
            "tool_service_name_unmatched",
            organization_id=str(organization_id),
            requested=needle,
            catalogue_size=len(services),
        )
        return None

    async def _zone_for(self, organization_id: uuid.UUID) -> ZoneInfo:
        profile = await self._profiles.get_by_organization_id(organization_id)
        return _zone_or_utc(profile.timezone if profile else "UTC")

    # --- Emergency alerting ---

    async def _notify_dispatcher(self, ticket: Any) -> NotificationDelivery | None:
        """Attempts the outbound alert for a newly-created emergency ticket.

        Returns None when no notification service is wired in at all, which
        `_dispatcher_alert_fields` treats identically to a failure: the
        assistant may not claim an alert either way. That equivalence is
        deliberate — a missing dependency is a deployment mistake, and the
        one thing it must never do is silently restore the old behaviour of
        claiming an alert that never happened.

        Never raises. The service is already bounded and non-raising, and
        this adds a second net because the cost of being wrong is a dropped
        call on someone reporting an emergency."""
        if self._notifications is None:
            logger.warning(
                "emergency_notification_service_unavailable",
                organization_id=str(ticket.organization_id),
                ticket_id=str(ticket.id),
            )
            return None
        try:
            return await self._notifications.notify_ticket(ticket)
        except Exception:
            logger.error(
                "emergency_notification_failed",
                organization_id=str(ticket.organization_id),
                ticket_id=str(ticket.id),
                exc_info=True,
            )
            return None

    async def _dispatcher_was_alerted(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> bool:
        """Whether a human was actually told, read from stored delivery state.

        Fails closed on any error: an unreadable delivery record means we
        cannot prove anyone was alerted, and the whole point of this milestone
        is that unproven means unsaid."""
        if self._notifications is None:
            return False
        try:
            delivery = await self._notifications.get_delivery(organization_id, ticket_id)
        except Exception:
            logger.warning("emergency_notification_lookup_failed", exc_info=True)
            return False
        return delivery is not None and delivery.alerted_a_human


class _BoundToolExecutor(ToolExecutor):
    """One conversation's view of the executor: the organization and
    conversation are fixed at construction, so no tool argument can redirect
    a write to another tenant.

    Also where the never-raise and timeout guarantees are enforced, in one
    place rather than in each tool."""

    _HANDLERS = {
        CREATE_SERVICE_REQUEST.name: "_create_service_request",
        CHECK_AVAILABILITY.name: "_check_availability",
        SELECT_APPOINTMENT_SLOT.name: "_select_appointment_slot",
        BOOK_APPOINTMENT.name: "_book_appointment",
    }

    def __init__(
        self,
        parent: VoiceToolExecutor,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        turn_index: int,
    ) -> None:
        self._parent = parent
        self._organization_id = organization_id
        self._conversation_id = conversation_id
        # Fixed for the life of this turn. Every tool call the model makes
        # while answering one caller utterance shares it, which is what makes
        # "offered and selected in the same turn" detectable at all.
        self._turn_index = turn_index

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started_at = now()
        content = await self._run(invocation)
        # One line per tool execution, carrying the correlation ids an
        # incident review needs. `vapi_call_id`/`turn_id` are already bound
        # into the log context by the webhook, so they appear here for free.
        # Deliberately no arguments and no caller details: names, phone
        # numbers, and addresses all pass through this method and none of
        # them belongs in a log.
        logger.info(
            "voice_tool_executed",
            tool_name=invocation.name,
            success=bool(content.get("success")),
            error_code=content.get("error"),
            elapsed_ms=elapsed_ms(started_at),
            organization_id=str(self._organization_id),
            conversation_id=str(self._conversation_id),
            service_request_id=content.get("service_request_id"),
            customer_id=content.get("customer_id"),
            appointment_id=content.get("appointment_id"),
            slots_offered=len(content.get("slots", [])) if "slots" in content else None,
            # Derived, never a caller instant in a log line the assistant did
            # not already produce: whether this turn holds a live selection,
            # and which turn the offer came from. Enough to reconstruct a
            # consent refusal without recording what the caller asked for.
            selection_state=content.get("selection_state"),
            turn_index=self._turn_index,
            # Whether this turn was permitted to tell the caller a human was
            # alerted. Present only on emergency results; the single most
            # important field on this line during an incident review, because
            # it is what the caller was or was not told.
            dispatcher_alerted=content.get("dispatcher_alerted"),
            notification_status=content.get("notification_status"),
        )
        return ToolResult(id=invocation.id, name=invocation.name, content=content)

    async def _run(self, invocation: ToolInvocation) -> dict[str, Any]:
        handler_name = self._HANDLERS.get(invocation.name)
        if handler_name is None:
            return {"success": False, "error": ToolErrors.UNKNOWN_TOOL, "tool_name": invocation.name}

        handler = getattr(self._parent, handler_name)
        try:
            return await asyncio.wait_for(
                handler(
                    self._organization_id,
                    self._conversation_id,
                    invocation.arguments,
                    self._turn_index,
                ),
                timeout=self._parent._settings.AI_TOOL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # `asyncio.TimeoutError` is an alias of the builtin from 3.11.
            logger.warning("voice_tool_timed_out", tool_name=invocation.name)
            return {"success": False, "error": ToolErrors.TIMEOUT}
        except DomainError as exc:
            return _domain_error_result(exc)
        except Exception:
            # Deliberately broad, and deliberately not re-raised: an
            # unexpected failure in a tool must degrade to something the
            # assistant can say, never to a dropped call. Logged with a
            # traceback so it is still visible as a defect.
            logger.error("voice_tool_failed", tool_name=invocation.name, exc_info=True)
            return {"success": False, "error": ToolErrors.INTERNAL_ERROR}


def _dispatcher_alert_fields(delivery: NotificationDelivery | None) -> dict[str, Any]:
    """The machine-readable half of emergency truthfulness.

    `dispatcher_alerted` is the only thing licensing the assistant to say a
    human was told, and it is `True` for exactly one reason: a configured
    provider accepted the message. Not because a ticket exists, not because
    the model believes it should have — the tool result carries the backend's
    evidence, and the prompt tells the assistant to read it.

    `notification_status` travels alongside so the assistant can tell "nobody
    is set up to be alerted here" from "we tried and it failed", which are
    different sentences to a caller with a gas leak."""
    if delivery is None:
        return {
            "dispatcher_alerted": False,
            "notification_status": DeliveryStatus.FAILED.value,
        }
    return {
        "dispatcher_alerted": delivery.alerted_a_human,
        "notification_status": delivery.status.value,
    }


def _dispatcher_next_step(delivery: NotificationDelivery | None) -> str:
    """What the assistant should say about the alert, in words, next to the
    flag it is derived from.

    Both because the prompt's general rule and a result-adjacent instruction
    reinforce each other, and because this codebase has already learned that
    the rule the model actually follows is the one sitting beside the result
    it is reading (see the standard-vs-emergency wording note in
    `_book_appointment`)."""
    if delivery is not None and delivery.alerted_a_human:
        return (
            "A dispatcher has been alerted and will contact the caller. You "
            "may say so. Do not offer an appointment time."
        )
    return (
        "The emergency IS recorded and the team will see it, but the alert to "
        "a dispatcher could NOT be confirmed. Do NOT say a dispatcher has "
        "been alerted, notified, or is on the way. Tell the caller their "
        "emergency has been logged for the team, and that if it is dangerous "
        "right now they should call the business directly or the emergency "
        "services. Do not offer an appointment time."
    )


def _domain_error_result(exc: DomainError) -> dict[str, Any]:
    """Maps the domain vocabulary onto the flat error codes the prompt names.

    Each mapping exists because the assistant's recovery differs: a full
    slot means "offer another time", a closed day means "offer another day",
    a missing service request means "call create_service_request first", and
    an emergency means "stop trying to book and reassure the caller"."""
    if isinstance(exc, SlotNotOfferedError):
        # Distinct from SLOT_UNAVAILABLE on purpose: that means "offered but
        # since taken, offer another"; this means "you never offered this at
        # all, go and get real options first".
        return {
            "success": False,
            "error": ToolErrors.SLOT_NOT_OFFERED,
            "next_step": (
                "You have not offered this time to the caller. Call "
                "check_availability, read the caller the slots it returns, "
                "and book only the one they choose."
            ),
        }
    if isinstance(exc, SlotNotSelectedError):
        # The consent refusal. Distinct from SLOT_NOT_OFFERED because the
        # times are real and already spoken — the assistant does not need to
        # go and fetch anything, it needs to stop and let the caller answer.
        return {
            "success": False,
            "error": ToolErrors.SLOT_NOT_SELECTED,
            "selection_state": "none",
            "next_step": (
                "The caller has not chosen this time. Do NOT tell them "
                "anything is booked. Ask which of the times you offered they "
                "would like, wait for their answer, then call "
                "select_appointment_slot with the time they name and book "
                "that."
            ),
        }
    if isinstance(exc, AppointmentSlotUnavailableError):
        return {
            "success": False,
            "error": ToolErrors.SLOT_UNAVAILABLE,
            # The chosen time was released when the verdict came back, so the
            # assistant must collect a fresh choice rather than retrying.
            "selection_state": "none",
            "next_step": (
                "That time was taken while you were talking. Apologise "
                "briefly, call check_availability, offer what comes back, and "
                "record the caller's new choice before booking."
            ),
        }
    if isinstance(exc, AppointmentSlotInThePastError):
        return {"success": False, "error": ToolErrors.SLOT_IN_THE_PAST}
    if isinstance(exc, AppointmentOutsideBusinessHoursError):
        return {"success": False, "error": ToolErrors.OUTSIDE_BUSINESS_HOURS}
    if isinstance(exc, EntityNotFoundError):
        return {"success": False, "error": ToolErrors.NO_SERVICE_REQUEST}
    if isinstance(exc, InvalidAppointmentStatusTransitionError):
        return {"success": False, "error": ToolErrors.EMERGENCY_NOT_BOOKABLE, "detail": exc.message}
    if isinstance(exc, AvailabilityUnavailableError):
        # A configuration fault, not a caller-recoverable one. Surfaced as
        # INTERNAL_ERROR so the assistant apologises and offers a callback
        # instead of inventing times.
        logger.error("availability_provider_not_configured")
        return {"success": False, "error": ToolErrors.INTERNAL_ERROR}
    return {"success": False, "error": ToolErrors.INTERNAL_ERROR, "detail": exc.message}


def _render_slot(slot: AvailabilitySlot, zone: ZoneInfo) -> dict[str, Any]:
    local_start = slot.start_at.astimezone(zone)
    local_end = slot.end_at.astimezone(zone)
    return {
        "slot_id": slot.slot_id,
        "date": local_start.date().isoformat(),
        "start_time": local_start.strftime("%H:%M"),
        "end_time": local_end.strftime("%H:%M"),
        # Pre-rendered so the model reads a time back rather than doing
        # 24h -> 12h arithmetic itself, which is a step it can get wrong and
        # the caller would never catch.
        "label": _spoken(local_start),
    }


def _spoken(value: datetime) -> str:
    hour = value.hour % 12 or 12
    meridiem = "AM" if value.hour < 12 else "PM"
    minute = f":{value.minute:02d}" if value.minute else ""
    return f"{value.strftime('%A, %B')} {value.day} at {hour}{minute} {meridiem}"


def _zone_or_utc(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _is_blank(value: Any) -> bool:
    return value is None or not str(value).strip()


def _clean(value: Any) -> str:
    return str(value).strip()


def _parse_date(value: Any) -> py_date | None:
    if _is_blank(value):
        return None
    try:
        return py_date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _parse_time(value: Any) -> time | None:
    if _is_blank(value):
        return None
    text = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
