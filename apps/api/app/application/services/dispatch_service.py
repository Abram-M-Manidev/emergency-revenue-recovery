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
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
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
    ) -> None:
        self._tickets = emergency_ticket_repository
        self._technicians = technician_profile_repository
        self._outcomes = conversation_outcome_repository
        self._conversations = conversation_repository
        self._users = user_repository
        self._roles = role_repository

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
        if existing is not None:
            # Already ticketed on an earlier turn — later turns of the same
            # conversation must not re-copy (possibly stale) AI fields onto
            # a ticket that may already have real dispatch progress on it.
            return existing

        return await self._tickets.create(
            organization_id=organization_id,
            conversation_id=conversation_id,
            matched_service_id=outcome.matched_service_id,
            customer_name=outcome.customer_name,
            customer_phone=outcome.customer_phone,
            customer_address=outcome.customer_address,
            summary=outcome.summary,
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
