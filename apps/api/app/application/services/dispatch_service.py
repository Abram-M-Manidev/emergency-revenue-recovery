"""Orchestrates Emergency Dispatch: turns an AI Brain `ConversationOutcome`
(Milestone 3) into a real `EmergencyTicket`, and lets a dispatcher build a
technician roster and track a ticket through to resolution.

This service never talks to the AI Brain or Voice modules — it only reads
`ConversationOutcomeRepository`/`ConversationRepository`, both already owned
by those modules, exactly the seam `conversation_outcome.py`'s docstring
describes ("Emergency Dispatch... reads conversation_outcomes directly to
find work to act on"). Neither `AIBrainService` nor `VoiceService` is aware
this module exists — the automatic-ticket-creation trigger lives in the API
layer (see `api/v1/endpoints/ai_conversations.py` and `vapi_webhooks.py`),
one call to `sync_ticket_from_outcome` right after each of those already
calls into the AI Brain."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

import structlog

from app.application.services.emergency_notification_service import (
    EmergencyNotificationService,
)
from app.domain.entities.conversation_outcome import (
    CallClassification,
    ConversationOutcome,
    RecommendedAction,
)
from app.domain.entities.emergency_ticket import EmergencyTicket, TicketStatus
from app.domain.entities.rbac import DEFAULT_ROLES, TECHNICIAN_ROLE_NAME, Permissions
from app.domain.entities.technician_profile import TechnicianProfile
from app.domain.entities.user import User
from app.domain.exceptions import (
    AuthorizationError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    InvalidTicketStatusTransitionError,
)
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.emergency_ticket_repository import EmergencyTicketRepository
from app.domain.repositories.role_repository import RoleRepository
from app.domain.repositories.technician_profile_repository import TechnicianProfileRepository
from app.domain.repositories.user_repository import UserRepository
from app.infrastructure.security.password import hash_password
from app.shared.utils.phone import storable_phone_number

logger = structlog.get_logger("app.dispatch")

# Legal status transitions: NEW -> ASSIGNED -> EN_ROUTE -> RESOLVED, and any
# open state can be CANCELED. RESOLVED/CANCELED are terminal (no outgoing
# edges).
_ALLOWED_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.NEW: frozenset({TicketStatus.ASSIGNED, TicketStatus.CANCELED}),
    TicketStatus.ASSIGNED: frozenset({TicketStatus.EN_ROUTE, TicketStatus.CANCELED}),
    TicketStatus.EN_ROUTE: frozenset({TicketStatus.RESOLVED, TicketStatus.CANCELED}),
    TicketStatus.RESOLVED: frozenset(),
    TicketStatus.CANCELED: frozenset(),
}
_CLOSED_STATUSES = frozenset({TicketStatus.RESOLVED, TicketStatus.CANCELED})


def _is_blank(value: str | None) -> bool:
    """Treats `None`, `""`, and whitespace-only alike. The AI can emit an
    empty string for a detail it hasn't heard yet, which is the same thing
    as not knowing it — the second live web call produced exactly that,
    leaving a ticket whose contact columns were empty strings rather than
    NULL."""
    return value is None or not value.strip()


@dataclass(frozen=True, slots=True)
class TechnicianRosterEntry:
    """`TechnicianProfile` alone is just a phone number and an on-call flag
    — a dispatcher choosing who to assign a ticket to needs a name too. The
    name/email live on `User`, not `TechnicianProfile` (no duplication of
    data Auth already owns), so this composes the two read-side for the
    roster listing only; nothing else in this module needs it."""

    profile: TechnicianProfile
    full_name: str
    email: str


class DispatchService:
    def __init__(
        self,
        *,
        emergency_ticket_repository: EmergencyTicketRepository,
        technician_profile_repository: TechnicianProfileRepository,
        conversation_outcome_repository: ConversationOutcomeRepository,
        conversation_repository: ConversationRepository,
        user_repository: UserRepository,
        role_repository: RoleRepository,
        # Queues the emergency alert in the SAME transaction as the ticket it
        # is about (the outbox). Optional so existing construction sites keep
        # working; without it a ticket is created with no alert queued, which
        # the assistant then reports as "could not be confirmed".
        emergency_notifications: EmergencyNotificationService | None = None,
    ) -> None:
        self._tickets = emergency_ticket_repository
        self._technicians = technician_profile_repository
        self._outcomes = conversation_outcome_repository
        self._conversations = conversation_repository
        self._users = user_repository
        self._roles = role_repository
        self._notifications = emergency_notifications

    # --- Automatic ticket creation (the AI Brain -> Dispatch seam) ---

    async def sync_ticket_from_outcome(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> EmergencyTicket | None:
        outcome = await self._outcomes.get_by_conversation_id(conversation_id)
        if outcome is None:
            return None
        if (
            outcome.classification is not CallClassification.EMERGENCY
            or outcome.recommended_action is not RecommendedAction.CREATE_EMERGENCY_TICKET
        ):
            return None

        existing = await self._tickets.get_by_conversation_id(conversation_id)
        outcome = await self._with_caller_id_fallback(
            organization_id, conversation_id, outcome, existing
        )
        if existing is not None:
            # Already ticketed on an earlier turn. Later turns must not
            # re-copy (possibly stale) AI fields onto a ticket that may
            # already carry real dispatch progress — but a ticket opened
            # before the caller gave their name, number, or address would
            # otherwise keep those blanks forever, leaving the dispatcher
            # with nobody to call back. Fill in only what is still missing.
            return await self._backfill_contact_details(organization_id, existing, outcome)

        ticket = await self._tickets.create(
            organization_id=organization_id,
            conversation_id=conversation_id,
            matched_service_id=outcome.matched_service_id,
            customer_name=outcome.customer_name,
            customer_phone=outcome.customer_phone,
            customer_address=outcome.customer_address,
            summary=outcome.summary,
        )
        if self._notifications is not None:
            # Same transaction as the ticket: both commit, or neither does.
            # Idempotent by ticket, so the race path in `create` (which
            # returns the ticket another writer just created) is harmless.
            await self._notifications.enqueue(ticket)
        return ticket

    async def _with_caller_id_fallback(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        outcome: ConversationOutcome,
        existing: EmergencyTicket | None,
    ) -> ConversationOutcome:
        """The outcome, with the call's own caller ID standing in for a
        callback number nobody has stated yet.

        An emergency ticket without a callback number is one a dispatcher
        cannot act on. The tool path already falls back to the caller ID, but
        a ticket is just as often opened by this outcome sync — when the model
        classifies the emergency without calling the tool — and it had no
        fallback. On a real-model run a caller reporting smoke who then
        refused to give a number or an address left a ticket with neither,
        while the number they were calling from sat unused on the call.

        Only ever fills a blank: a number the caller stated always wins, and
        this never overwrites one already on the ticket."""
        if not _is_blank(outcome.customer_phone):
            return outcome
        if existing is not None and not _is_blank(existing.customer_phone):
            return outcome
        conversation = await self._conversations.get_by_id(organization_id, conversation_id)
        caller_id = storable_phone_number(
            conversation.caller_phone_number if conversation is not None else None
        )
        if caller_id is None:
            return outcome
        return replace(outcome, customer_phone=caller_id)

    async def _backfill_contact_details(
        self,
        organization_id: uuid.UUID,
        ticket: EmergencyTicket,
        outcome: ConversationOutcome,
    ) -> EmergencyTicket:
        """Brings the ticket's contact details up to date with what the AI
        currently understands. The exact mirror of
        `AppointmentService._backfill_contact_details`, whose docstring
        carries the full reasoning and the live evidence.

        A blank outcome value never overwrites a recorded one, so a later
        turn that omits a field the model reported earlier cannot erase it.
        A different non-blank value does overwrite, because a caller
        correcting their address mid-call is the normal case and this used
        to keep their first attempt forever — on an emergency ticket, that
        is the address someone is dispatched to.

        When nothing has changed this performs no write at all."""
        updates = {
            field: getattr(outcome, field)
            for field in ("customer_name", "customer_phone", "customer_address")
            if not _is_blank(getattr(outcome, field))
            and getattr(outcome, field) != getattr(ticket, field)
        }
        if not updates:
            return ticket

        # Field NAMES only — never their values, which are caller PII.
        # `overwritten` separates filling a blank from replacing a value the
        # AI reported earlier: the second is how a caller's correction reaches
        # the record, and also the only way a model that *degrades* a detail
        # could reach it. Silent either way until this line existed.
        logger.info(
            "ticket_contact_details_synced",
            organization_id=str(organization_id),
            fields=sorted(updates),
            overwritten=sorted(
                field for field in updates if not _is_blank(getattr(ticket, field))
            ),
        )

        return await self._tickets.backfill_contact_details(
            organization_id, ticket.id, **updates
        )

    # --- Tickets ---

    async def list_tickets(
        self,
        organization_id: uuid.UUID,
        *,
        status: TicketStatus | None = None,
        limit: int,
        offset: int,
    ) -> list[EmergencyTicket]:
        return await self._tickets.list_for_organization(
            organization_id, status=status, limit=limit, offset=offset
        )

    async def get_ticket_for_conversation(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> EmergencyTicket | None:
        """The ticket this conversation produced, if any.

        Returns None rather than raising, because "no ticket" is the normal
        case for most conversations. Added for the `book_appointment` tool:
        an emergency call has a ticket and no appointment, and without this
        the tool could only report `NO_SERVICE_REQUEST` — which instructs the
        assistant to call `create_service_request` and try again, looping it
        against a call that must never be booked at all."""
        ticket = await self._tickets.get_by_conversation_id(conversation_id)
        if ticket is None or ticket.organization_id != organization_id:
            return None
        return ticket

    async def get_ticket(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> EmergencyTicket:
        ticket = await self._tickets.get_by_id(organization_id, ticket_id)
        if ticket is None:
            # Cross-tenant id: from the caller's point of view, another
            # org's ticket simply doesn't exist — same convention as
            # AIBrainService.get_conversation / VoiceService.get_voice_call.
            raise EntityNotFoundError("EmergencyTicket", str(ticket_id))
        return ticket

    async def assign_ticket(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID, technician_user_id: uuid.UUID
    ) -> EmergencyTicket:
        ticket = await self.get_ticket(organization_id, ticket_id)
        if ticket.status in _CLOSED_STATUSES:
            raise InvalidTicketStatusTransitionError(
                f"Cannot assign a ticket that is already {ticket.status.value}."
            )

        technician = await self._technicians.get_by_user_id(technician_user_id)
        if technician is None or technician.organization_id != organization_id:
            raise EntityNotFoundError("TechnicianProfile", str(technician_user_id))

        return await self._tickets.assign(
            ticket_id,
            technician_user_id=technician_user_id,
            assigned_at=datetime.now(timezone.utc),
        )

    async def update_ticket_status(
        self,
        organization_id: uuid.UUID,
        ticket_id: uuid.UUID,
        new_status: TicketStatus,
        *,
        acting_user: User,
        actual_value: Decimal | None = None,
    ) -> EmergencyTicket:
        ticket = await self.get_ticket(organization_id, ticket_id)

        if (
            not acting_user.has_permission(Permissions.DISPATCH_MANAGE)
            and ticket.assigned_technician_user_id != acting_user.id
        ):
            raise AuthorizationError(
                "You can only update the status of tickets assigned to you."
            )

        if new_status not in _ALLOWED_TRANSITIONS[ticket.status]:
            raise InvalidTicketStatusTransitionError(
                f"Cannot move a ticket from {ticket.status.value} to {new_status.value}."
            )

        closed_at = datetime.now(timezone.utc) if new_status in _CLOSED_STATUSES else None
        return await self._tickets.update_status(
            organization_id, ticket_id, status=new_status, closed_at=closed_at, actual_value=actual_value
        )

    # --- Technicians ---

    async def list_technicians(
        self, organization_id: uuid.UUID, *, on_call_only: bool = False
    ) -> list[TechnicianProfile]:
        return await self._technicians.list_for_organization(
            organization_id, on_call_only=on_call_only
        )

    async def list_technician_roster(
        self, organization_id: uuid.UUID, *, on_call_only: bool = False
    ) -> list[TechnicianRosterEntry]:
        profiles = await self.list_technicians(organization_id, on_call_only=on_call_only)
        entries: list[TechnicianRosterEntry] = []
        for profile in profiles:
            user = await self._users.get_by_id(profile.user_id)
            if user is None:
                # The technician's User row was deleted out from under its
                # profile — shouldn't happen (users.id cascades), but skip
                # rather than error a whole roster listing over one row.
                continue
            entries.append(
                TechnicianRosterEntry(profile=profile, full_name=user.full_name, email=user.email)
            )
        return entries

    async def set_technician_on_call(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, is_on_call: bool
    ) -> TechnicianProfile:
        technician = await self._technicians.get_by_user_id(user_id)
        if technician is None or technician.organization_id != organization_id:
            raise EntityNotFoundError("TechnicianProfile", str(user_id))
        return await self._technicians.set_on_call(organization_id, user_id, is_on_call)

    async def create_technician(
        self,
        organization_id: uuid.UUID,
        *,
        full_name: str,
        email: str,
        phone_number: str,
        temporary_password: str,
    ) -> TechnicianProfile:
        if await self._users.get_by_email(email) is not None:
            raise EntityAlreadyExistsError("User", "email", email)

        technician_role = await self._roles.get_or_create_by_name(
            organization_id, TECHNICIAN_ROLE_NAME, DEFAULT_ROLES[TECHNICIAN_ROLE_NAME]
        )

        user = await self._users.create(
            organization_id=organization_id,
            email=email,
            hashed_password=hash_password(temporary_password),
            full_name=full_name,
            role_ids=[technician_role.id],
        )

        return await self._technicians.create(
            organization_id=organization_id,
            user_id=user.id,
            phone_number=phone_number,
        )
