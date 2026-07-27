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

from app.domain.entities.appointment import Appointment
from app.domain.entities.customer import Customer
from app.domain.entities.emergency_ticket import EmergencyTicket
from app.domain.exceptions import EntityAlreadyExistsError, EntityNotFoundError
from app.domain.repositories.appointment_repository import AppointmentRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.customer_repository import CustomerRepository
from app.domain.repositories.emergency_ticket_repository import EmergencyTicketRepository


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
    ) -> None:
        self._customers = customer_repository
        self._outcomes = conversation_outcome_repository
        self._tickets = emergency_ticket_repository
        self._appointments = appointment_repository

    # --- Automatic customer sync (the AI Brain -> Customers seam) ---

    async def sync_customer_from_outcome(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
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
        # Existing customer: intentionally left untouched. Later AI Brain
        # turns must not overwrite a record that may already be
        # staff-edited — same reasoning DispatchService/AppointmentService
        # use for not re-copying stale fields onto an existing ticket/
        # appointment.

        await self._link_existing_activity(organization_id, conversation_id, customer.id)
        return customer

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
