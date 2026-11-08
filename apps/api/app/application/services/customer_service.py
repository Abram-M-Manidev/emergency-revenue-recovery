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
from app.domain.exceptions import (
    EntityAlreadyExistsError,
    EntityNotFoundError,
    InvalidPhoneNumberError,
)
from app.domain.repositories.appointment_repository import AppointmentRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.caller_identity_repository import CallerIdentityRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.customer_repository import CustomerRepository
from app.domain.repositories.emergency_ticket_repository import EmergencyTicketRepository
from app.domain.transactions import NullSavepoints, Savepoints
from app.shared.utils.phone import (
    check_phone_number,
    legacy_phone_keys,
    region_for_country,
    valid_phone_number,
)

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
        # Makes the best-effort association genuinely best-effort on
        # Postgres; see `_associate_caller_number`.
        savepoints: Savepoints | None = None,
        # Tells a customer record created during this call (which a caller
        # correction may update) from one that existed before it (which it
        # may not). Optional; absent, every record is treated as pre-existing,
        # which is the previous, strictly additive behaviour.
        conversation_repository: ConversationRepository | None = None,
        # The business's country, for reading a national-format number staff
        # type in. Optional; absent, staff must enter international form.
        business_profile_repository: BusinessProfileRepository | None = None,
    ) -> None:
        self._customers = customer_repository
        self._outcomes = conversation_outcome_repository
        self._tickets = emergency_ticket_repository
        self._appointments = appointment_repository
        self._caller_identities = caller_identity_repository
        self._savepoints = savepoints or NullSavepoints()
        self._conversations = conversation_repository
        self._profiles = business_profile_repository

    # --- Automatic customer sync (the AI Brain -> Customers seam) ---

    async def sync_customer_from_outcome(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        caller_number: str | None = None,
    ) -> Customer | None:
        outcome = await self._outcomes.get_by_conversation_id(conversation_id)
        if outcome is None:
            return None

        # A valid E.164 number is the deduplication key, and nothing else is.
        # A number captured from speech arrives in whatever shape the
        # transcript took — "1 2 3 4 5 6 7 8 9" one turn, "123456789" the
        # next (2026-08-22) — and an incomplete one is not a key at all: on
        # 2026-10-31 a nine-digit number created a second record for a
        # caller who already had one. The outcome only ever holds numbers
        # validated on the way in, in E.164; the business's region is still
        # applied so an outcome written before validation existed resolves to
        # the same key. A value that is not a valid number is refused.
        phone_number = valid_phone_number(
            outcome.customer_phone, region=await self._region(organization_id)
        )
        if phone_number is None:
            # No usable phone number to dedupe on — a Customer record is
            # meaningless without a way to match repeat callers.
            return None

        customer = await self._find_by_phone(organization_id, phone_number)
        if customer is None:
            try:
                customer = await self._customers.create(
                    organization_id=organization_id,
                    full_name=outcome.customer_name,
                    phone_number=phone_number,
                    address=outcome.customer_address,
                )
            except EntityAlreadyExistsError:
                # Concurrent retry of the same AI Brain turn / webhook
                # raced us to create it first — fetch instead of failing.
                customer = await self._customers.get_by_phone_number(
                    organization_id, phone_number
                )
                if customer is None:
                    raise
        else:
            customer = await self._backfill_contact_details(
                organization_id,
                customer,
                outcome,
                created_this_call=await self._created_during(conversation_id, customer),
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

        Best-effort, like every other optional lookup on the call path. This
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
            # The savepoint is what makes the `except` below true. Catching
            # a database error does not un-abort a Postgres transaction, so
            # before it a failure here was "swallowed" and then failed the
            # very next statement — or the commit, after the caller had
            # already heard the whole turn. It sits inside the `try` so the
            # failed block is rolled back before the exception is caught.
            async with self._savepoints.isolate():
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

    async def _region(self, organization_id: uuid.UUID) -> str | None:
        """The business's own country as a phone region, or None when no
        profile repository is wired or no country is configured."""
        if self._profiles is None:
            return None
        profile = await self._profiles.get_by_organization_id(organization_id)
        return region_for_country(profile.country if profile else None)

    async def _staff_phone_number(self, organization_id: uuid.UUID, raw: str) -> str:
        """A number typed in by staff, validated and in E.164 — the same key
        the AI sync matches on, so a staff-created customer and a caller are
        one record, not two."""
        check = check_phone_number(raw, region=await self._region(organization_id))
        if check.e164 is None:
            raise InvalidPhoneNumberError(check.problem or "invalid")
        return check.e164

    async def _find_by_phone(
        self, organization_id: uuid.UUID, phone_number: str
    ) -> Customer | None:
        """The customer keyed on this E.164 number — or, failing that, on one
        of the forms the same number was stored in before numbers were
        validated (`legacy_phone_keys`). A returning caller whose record
        predates E.164 is matched rather than given a second record. Nothing
        is rewritten or merged: the old record keeps its old key."""
        customer = await self._customers.get_by_phone_number(organization_id, phone_number)
        if customer is not None:
            return customer
        for key in legacy_phone_keys(phone_number):
            customer = await self._customers.get_by_phone_number(organization_id, key)
            if customer is not None:
                return customer
        return None

    async def _created_during(self, conversation_id: uuid.UUID, customer: Customer) -> bool:
        """Whether this customer record came into existence during this
        call, so a correction the caller makes on the call belongs on it.

        A record that existed before the call is left alone except for
        blanks: the caller ID that matched it can be spoofed, so a stranger
        must not be able to rename someone else's customer by calling in."""
        if self._conversations is None:
            return False
        outcome_conversation = await self._conversations.get_by_id(
            customer.organization_id, conversation_id
        )
        if outcome_conversation is None:
            return False
        return customer.created_at >= outcome_conversation.created_at

    async def _backfill_contact_details(
        self,
        organization_id: uuid.UUID,
        customer: Customer,
        outcome: ConversationOutcome,
        *,
        created_this_call: bool = False,
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
            if not _is_blank(getattr(outcome, source))
            and (
                _is_blank(getattr(customer, field))
                # A record made on this call follows the caller's own
                # corrections; one that predates the call does not.
                or (created_this_call and getattr(customer, field) != getattr(outcome, source))
            )
        }
        if not updates:
            return customer

        overwritten = sorted(
            field for field in updates if not _is_blank(getattr(customer, field))
        )
        if overwritten:
            # Field names only: the values are the caller's PII.
            logger.info(
                "customer_contact_details_corrected",
                organization_id=str(organization_id),
                customer_id=str(customer.id),
                fields=overwritten,
            )
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
        phone_number = await self._staff_phone_number(organization_id, phone_number)
        if await self._find_by_phone(organization_id, phone_number) is not None:
            # Including a record keyed in a pre-E.164 form, which the unique
            # constraint alone would not see as the same number.
            raise EntityAlreadyExistsError("Customer", "phone_number", phone_number)
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
        current = await self.get_customer(organization_id, customer_id)
        if phone_number != current.phone_number:
            # Re-saving a record with its existing (possibly pre-E.164) key
            # is left alone; a changed number must be a valid one.
            phone_number = await self._staff_phone_number(organization_id, phone_number)
            other = await self._find_by_phone(organization_id, phone_number)
            if other is not None and other.id != customer_id:
                raise EntityAlreadyExistsError("Customer", "phone_number", phone_number)
        return await self._customers.update(
            organization_id,
            customer_id,
            full_name=full_name,
            phone_number=phone_number,
            email=email,
            address=address,
            notes=notes,
        )
