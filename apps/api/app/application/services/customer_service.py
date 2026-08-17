"""Orchestrates the internal Customer/CRM module (Milestone 7): a unified
customer record per distinct caller, populated automatically from AI Brain
`ConversationOutcome`s (Milestone 3) and linked to whichever
`EmergencyTicket` (Milestone 5) or `Appointment` (Milestone 6) the same
conversation produced.

This service never talks to the AI Brain, Dispatch, or Appointment
services directly — it only reads `ConversationOutcomeRepository` (already
owned by AI Brain) and `EmergencyTicketRepository`/`AppointmentRepository`
(already owned by Dispatch/Appointments), exactly the seam
`conversation_outcome.py`'s docstring describes. None of those three
modules is aware Customers exists — the automatic-sync trigger lives in
the API layer (see `api/v1/endpoints/ai_conversations.py` and
`vapi_webhooks.py`), one call to `sync_customer_from_outcome` right after
the existing dispatch/appointment sync calls, mirroring exactly how those
two are wired in for their own modules.

Unlike Dispatch/Appointments, syncing is not gated on
`recommended_action` — any conversation outcome that captured a phone
number is worth a Customer record, not just ones that produced a ticket
or appointment."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog

from app.domain.entities.appointment import Appointment
from app.domain.entities.conversation_outcome import ConversationOutcome
from app.domain.entities.customer import Customer
from app.domain.entities.emergency_ticket import EmergencyTicket
from app.domain.exceptions import EntityAlreadyExistsError, EntityNotFoundError
from app.domain.repositories.appointment_repository import AppointmentRepository
from app.domain.repositories.caller_identity_repository import CallerIdentityRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.customer_repository import CustomerRepository
from app.domain.repositories.emergency_ticket_repository import EmergencyTicketRepository

logger = structlog.get_logger("app.customers")


def _is_blank(value: str | None) -> bool:
    """Treats `None`, `""`, and whitespace-only alike. The AI can emit an
    empty string for a detail it hasn't heard yet, which is the same thing
    as not knowing it.

    Deliberately a private copy of `DispatchService`'s identical helper
    rather than a shared import: the two services are peers that know
    nothing about each other (see this module's docstring), and coupling
    them through a utility would be a wider change than the behaviour it
    supports."""
    return value is None or not value.strip()


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    """A customer's activity across the two modules that can produce
    work from a conversation — composed read-side only, same pattern as
    `DispatchService.TechnicianRosterEntry`."""

    customer: Customer
    tickets: list[EmergencyTicket]
    appointments: list[Appointment]


class CustomerService:
    def __init__(
        self,
        *,
        customer_repository: CustomerRepository,
        conversation_outcome_repository: ConversationOutcomeRepository,
        emergency_ticket_repository: EmergencyTicketRepository,
        appointment_repository: AppointmentRepository,
        # Optional: without it, P5 association capture is skipped and
        # C1 behaves exactly as before.
        caller_identity_repository: CallerIdentityRepository | None = None,
    ) -> None:
        self._customers = customer_repository
        self._outcomes = conversation_outcome_repository
        self._tickets = emergency_ticket_repository
        self._appointments = appointment_repository
        self._caller_identities = caller_identity_repository

    # --- Automatic customer sync (the AI Brain -> Customers seam) ---

    async def sync_customer_from_outcome(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        caller_number: str | None = None,
    ) -> Customer | None:
        outcome = await self._outcomes.get_by_conversation_id(conversation_id)
        if outcome is None or outcome.customer_phone is None:
            # No phone number to dedupe on — a Customer record is
            # meaningless without a way to match repeat callers.
            return None

        customer = await self._customers.get_by_phone_number(
            organization_id, outcome.customer_phone
        )
        if customer is None:
            try:
                customer = await self._customers.create(
                    organization_id=organization_id,
                    full_name=outcome.customer_name,
                    phone_number=outcome.customer_phone,
                    address=outcome.customer_address,
                )
            except EntityAlreadyExistsError:
                # Concurrent retry of the same AI Brain turn / webhook
                # raced us to create it first — fetch instead of failing.
                customer = await self._customers.get_by_phone_number(
                    organization_id, outcome.customer_phone
                )
                if customer is None:
                    raise
        else:
            customer = await self._backfill_contact_details(
                organization_id, customer, outcome
            )

        await self._link_existing_activity(organization_id, conversation_id, customer.id)
        await self._associate_caller_number(organization_id, customer.id, caller_number)
        return customer

    async def _associate_caller_number(
        self, organization_id: uuid.UUID, customer_id: uuid.UUID, caller_number: str | None
    ) -> None:
        """Records that this telephony line has been used by this customer
        (P5), so a later call from the same number can be recognised.

        Strictly additive bookkeeping, and strictly separate from C1: it
        writes only the association row and never touches `phone_number`,
        `full_name`, `address`, `email`, or `notes`. In particular it does
        NOT make the caller ID a second deduplication key — customers are
        still matched solely by the callback number the caller stated.

        Silent when there is no caller ID (the whole text path) or no
        repository wired. Idempotent, because it runs on every turn of a
        call, not once.

        Best-effort, mirroring `AIBrainService._resolve_known_caller`. This
        is the *last* statement of the outcome sync, after C1 and after the
        ticket/appointment links have already succeeded, so an exception
        escaping here would turn a fully successful turn into a broken one:
        `_run_outcome_syncs` only catches `DomainError`, so a storage error
        would propagate out of the streaming generator and the turn would
        never emit `[DONE]` or the `endCall` tool call — on a live
        emergency call, a hang-up that never happens and a transaction that
        may take the new ticket down with it. Recognising a repeat caller
        is a convenience; none of that is worth losing for it.

        `Exception`, never `BaseException`: `GeneratorExit` and
        `CancelledError` must still propagate so P1's lock and the request
        transaction unwind on a disconnect exactly as H3 established."""
        if self._caller_identities is None or not caller_number:
            return
        try:
            await self._caller_identities.associate(
                organization_id, customer_id=customer_id, caller_number=caller_number
            )
        except Exception:
            # `caller_number` is deliberately absent from the log — it is a
            # phone number. The internal ids are enough to reconcile.
            logger.warning(
                "caller_identity_association_failed",
                organization_id=str(organization_id),
                customer_id=str(customer_id),
                exc_info=True,
            )

    async def _backfill_contact_details(
        self, organization_id: uuid.UUID, customer: Customer, outcome: ConversationOutcome
    ) -> Customer:
        """Copies caller details the AI has since learned onto a customer
        created without them.

        Strictly additive, mirroring `DispatchService._backfill_contact_details`:
        a field is written only when the customer's own value is blank AND
        the outcome has something to put there. A staff correction
        therefore always wins over the AI, and a later turn that *loses* a
        detail (the model omitting a field it reported earlier) can never
        blank out a value already on the record. When nothing is missing
        this performs no write at all.

        Only the two fields a `ConversationOutcome` can actually source are
        considered. `phone_number` is excluded because it is the key this
        customer was just matched on; `email`/`notes` are excluded because
        they are staff-owned and the outcome has no counterpart for either
        — the repository method cannot write any of the three."""
        updates = {
            field: getattr(outcome, source)
            for field, source in (("full_name", "customer_name"), ("address", "customer_address"))
            if _is_blank(getattr(customer, field)) and not _is_blank(getattr(outcome, source))
        }
        if not updates:
            return customer

        return await self._customers.backfill_contact_details(
            organization_id, customer.id, **updates
        )

    async def _link_existing_activity(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID, customer_id: uuid.UUID
    ) -> None:
        ticket = await self._tickets.get_by_conversation_id(conversation_id)
        if ticket is not None and ticket.customer_id is None:
            await self._tickets.set_customer(organization_id, ticket.id, customer_id=customer_id)

        appointment = await self._appointments.get_by_conversation_id(conversation_id)
        if appointment is not None and appointment.customer_id is None:
            await self._appointments.set_customer(
                organization_id, appointment.id, customer_id=customer_id
            )

    # --- Customers ---

    async def get_customer(self, organization_id: uuid.UUID, customer_id: uuid.UUID) -> Customer:
        customer = await self._customers.get_by_id(organization_id, customer_id)
        if customer is None:
            # Cross-tenant id: from the caller's point of view, another
            # org's customer simply doesn't exist — same convention as
            # DispatchService.get_ticket / AppointmentService.get_appointment.
            raise EntityNotFoundError("Customer", str(customer_id))
        return customer

    async def list_customers(
        self,
        organization_id: uuid.UUID,
        *,
        search: str | None = None,
        limit: int,
        offset: int,
    ) -> list[Customer]:
        return await self._customers.list_for_organization(
            organization_id, search=search, limit=limit, offset=offset
        )

    async def get_customer_history(
        self, organization_id: uuid.UUID, customer_id: uuid.UUID
    ) -> CustomerHistory:
        customer = await self.get_customer(organization_id, customer_id)
        tickets = await self._tickets.list_by_customer_id(organization_id, customer_id)
        appointments = await self._appointments.list_by_customer_id(organization_id, customer_id)
        return CustomerHistory(customer=customer, tickets=tickets, appointments=appointments)

    async def create_customer(
        self,
        organization_id: uuid.UUID,
        *,
        full_name: str | None,
        phone_number: str,
        email: str | None = None,
        address: str | None = None,
        notes: str | None = None,
    ) -> Customer:
        return await self._customers.create(
            organization_id=organization_id,
            full_name=full_name,
            phone_number=phone_number,
            email=email,
            address=address,
            notes=notes,
        )

    async def update_customer(
        self,
        organization_id: uuid.UUID,
        customer_id: uuid.UUID,
        *,
        full_name: str | None,
        phone_number: str,
        email: str | None,
        address: str | None,
        notes: str | None,
    ) -> Customer:
        await self.get_customer(organization_id, customer_id)
        return await self._customers.update(
            organization_id,
            customer_id,
            full_name=full_name,
            phone_number=phone_number,
            email=email,
            address=address,
            notes=notes,
        )
