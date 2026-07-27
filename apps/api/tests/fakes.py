"""Test doubles shared across unit and integration tests. Kept in one place
so the AI Brain's dependencies (the LLM call, in particular) never have to
hit a real, paid, non-deterministic API in CI. Also holds the in-memory
repository fakes used to build a real `AIBrainService` for tests (both its
own unit tests and `VoiceService`'s, which wraps it) without a database."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from app.domain.ai.provider import AIProvider, AIReply, AIRequest
from app.domain.entities.analytics import BucketCount, DailyCount, DailyRevenue
from app.domain.entities.appointment import Appointment, AppointmentStatus
from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.domain.entities.business_profile import BusinessProfile
from app.domain.entities.conversation import Conversation, ConversationStatus
from app.domain.entities.conversation_message import ConversationMessage, MessageRole
from app.domain.entities.conversation_outcome import (
    CallClassification,
    ConversationOutcome,
    RecommendedAction,
)
from app.domain.entities.customer import Customer
from app.domain.entities.emergency_keyword import EmergencyKeyword
from app.domain.entities.emergency_ticket import EmergencyTicket, TicketStatus
from app.domain.entities.faq_entry import FAQEntry
from app.domain.entities.organization import Organization
from app.domain.entities.role import Role
from app.domain.entities.service import Service
from app.domain.entities.service_area import ServiceArea
from app.domain.entities.technician_profile import TechnicianProfile
from app.domain.entities.user import User
from app.domain.exceptions import EntityAlreadyExistsError
from app.domain.repositories.appointment_repository import AppointmentRepository
from app.domain.repositories.business_hours_repository import BusinessHoursRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.customer_repository import CustomerRepository
from app.domain.repositories.emergency_keyword_repository import EmergencyKeywordRepository
from app.domain.repositories.emergency_ticket_repository import EmergencyTicketRepository
from app.domain.repositories.faq_repository import FAQRepository
from app.domain.repositories.organization_repository import OrganizationRepository
from app.domain.repositories.role_repository import RoleRepository
from app.domain.repositories.service_area_repository import ServiceAreaRepository
from app.domain.repositories.service_repository import ServiceRepository
from app.domain.repositories.technician_profile_repository import TechnicianProfileRepository
from app.domain.repositories.user_repository import UserRepository


def _in_range(value: datetime, start: datetime | None, end: datetime) -> bool:
    if value >= end:
        return False
    return start is None or value >= start


class FakeConversationRepository(ConversationRepository):
    def __init__(self) -> None:
        self._conversations: dict[uuid.UUID, Conversation] = {}
        self._messages: dict[uuid.UUID, list[ConversationMessage]] = {}

    async def create(self, *, organization_id, channel, caller_phone_number):
        now = datetime.now(timezone.utc)
        conversation = Conversation(
            id=uuid.uuid4(),
            organization_id=organization_id,
            channel=channel,
            status=ConversationStatus.ACTIVE,
            caller_phone_number=caller_phone_number,
            started_at=now,
            ended_at=None,
            created_at=now,
            updated_at=now,
        )
        self._conversations[conversation.id] = conversation
        self._messages[conversation.id] = []
        return conversation

    async def get_by_id(self, organization_id, conversation_id):
        conversation = self._conversations.get(conversation_id)
        if conversation is None or conversation.organization_id != organization_id:
            return None
        return conversation

    async def list_for_organization(self, organization_id, *, limit, offset):
        matches = [c for c in self._conversations.values() if c.organization_id == organization_id]
        matches.sort(key=lambda c: c.started_at, reverse=True)
        return matches[offset : offset + limit]

    async def add_message(self, conversation_id, *, role, content):
        message = ConversationMessage(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            role=role,
            content=content,
            created_at=datetime.now(timezone.utc),
        )
        self._messages[conversation_id].append(message)
        return message

    async def list_messages(self, conversation_id):
        return list(self._messages.get(conversation_id, []))

    async def complete(self, conversation_id):
        conversation = self._conversations[conversation_id]
        updated = replace(
            conversation,
            status=ConversationStatus.COMPLETED,
            ended_at=datetime.now(timezone.utc),
        )
        self._conversations[conversation_id] = updated
        return updated

    # --- Analytics (Milestone 8) ---

    def _in_range(self, organization_id, *, start, end):
        return [
            c
            for c in self._conversations.values()
            if c.organization_id == organization_id and _in_range(c.started_at, start, end)
        ]

    async def count_in_range(self, organization_id, *, start, end):
        return len(self._in_range(organization_id, start=start, end=end))

    async def count_by_day(self, organization_id, *, start, end):
        buckets: dict = defaultdict(int)
        for c in self._in_range(organization_id, start=start, end=end):
            buckets[c.started_at.date()] += 1
        return [DailyCount(day=day, count=count) for day, count in sorted(buckets.items())]

    async def count_by_channel_in_range(self, organization_id, *, start, end):
        buckets: dict = defaultdict(int)
        for c in self._in_range(organization_id, start=start, end=end):
            buckets[c.channel.value] += 1
        return [BucketCount(label=label, count=count) for label, count in buckets.items()]


class FakeConversationOutcomeRepository(ConversationOutcomeRepository):
    def __init__(self, conversation_repository: "FakeConversationRepository | None" = None) -> None:
        self._outcomes: dict[uuid.UUID, ConversationOutcome] = {}
        self._conversations = conversation_repository

    async def upsert(
        self,
        conversation_id,
        *,
        classification,
        confidence,
        recommended_action,
        matched_service_id,
        customer_name,
        customer_phone,
        customer_address,
        summary,
    ):
        existing = self._outcomes.get(conversation_id)
        outcome = ConversationOutcome(
            id=existing.id if existing else uuid.uuid4(),
            conversation_id=conversation_id,
            classification=classification,
            confidence=confidence,
            recommended_action=recommended_action,
            matched_service_id=matched_service_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_address=customer_address,
            summary=summary,
            updated_at=datetime.now(timezone.utc),
        )
        self._outcomes[conversation_id] = outcome
        return outcome

    async def get_by_conversation_id(self, conversation_id):
        return self._outcomes.get(conversation_id)

    # --- Analytics (Milestone 8) ---
    #
    # Mirrors the real repository's join to `conversations` for org/date
    # scoping, since `ConversationOutcome` carries neither itself — see
    # `conversation_outcome_repository_impl.py`.

    def _outcomes_in_range(self, organization_id, *, start, end):
        assert self._conversations is not None, (
            "FakeConversationOutcomeRepository needs a conversation_repository "
            "to answer analytics queries."
        )
        matches = []
        for outcome in self._outcomes.values():
            conversation = self._conversations._conversations.get(outcome.conversation_id)
            if conversation is None or conversation.organization_id != organization_id:
                continue
            if _in_range(conversation.started_at, start, end):
                matches.append(outcome)
        return matches

    async def classification_breakdown(self, organization_id, *, start, end):
        buckets: dict = defaultdict(int)
        for outcome in self._outcomes_in_range(organization_id, start=start, end=end):
            buckets[outcome.classification.value] += 1
        return [BucketCount(label=label, count=count) for label, count in buckets.items()]

    async def recommended_action_breakdown(self, organization_id, *, start, end):
        buckets: dict = defaultdict(int)
        for outcome in self._outcomes_in_range(organization_id, start=start, end=end):
            buckets[outcome.recommended_action.value] += 1
        return [BucketCount(label=label, count=count) for label, count in buckets.items()]


class FakeBusinessProfileRepository(BusinessProfileRepository):
    def __init__(self, profile: BusinessProfile | None = None) -> None:
        self._profile = profile

    async def get_by_organization_id(self, organization_id) -> BusinessProfile | None:
        return self._profile

    async def upsert(self, **kwargs):
        raise NotImplementedError


class FakeBusinessHoursRepository(BusinessHoursRepository):
    def __init__(
        self,
        weekly: list[WeeklyHours] | None = None,
        exceptions: list[HoursException] | None = None,
    ) -> None:
        self._weekly = weekly or []
        self._exceptions = exceptions or []

    async def get_weekly(self, organization_id) -> list[WeeklyHours]:
        return self._weekly

    async def replace_weekly(self, organization_id, entries):
        raise NotImplementedError

    async def list_exceptions(self, organization_id) -> list[HoursException]:
        return self._exceptions

    async def add_exception(self, **kwargs):
        raise NotImplementedError

    async def delete_exception(self, organization_id, exception_id):
        raise NotImplementedError


class FakeServiceRepository(ServiceRepository):
    def __init__(self, services: list[Service] | None = None) -> None:
        self._services = services or []

    async def list(self, organization_id):
        return self._services

    async def get_by_id(self, organization_id, service_id):
        return next(
            (
                s
                for s in self._services
                if s.id == service_id and s.organization_id == organization_id
            ),
            None,
        )

    async def create(self, **kwargs):
        raise NotImplementedError

    async def update(self, *args, **kwargs):
        raise NotImplementedError

    async def delete(self, *args, **kwargs):
        raise NotImplementedError


class FakeServiceAreaRepository(ServiceAreaRepository):
    async def list(self, organization_id) -> list[ServiceArea]:
        return []

    async def create(self, **kwargs):
        raise NotImplementedError

    async def delete(self, *args, **kwargs):
        raise NotImplementedError


class FakeFAQRepository(FAQRepository):
    async def list(self, organization_id) -> list[FAQEntry]:
        return []

    async def create(self, **kwargs):
        raise NotImplementedError

    async def update(self, *args, **kwargs):
        raise NotImplementedError

    async def delete(self, *args, **kwargs):
        raise NotImplementedError


class FakeEmergencyKeywordRepository(EmergencyKeywordRepository):
    def __init__(self, keywords: list[EmergencyKeyword] | None = None) -> None:
        self._keywords = keywords or []

    async def list(self, organization_id):
        return self._keywords

    async def create(self, **kwargs):
        raise NotImplementedError

    async def delete(self, *args, **kwargs):
        raise NotImplementedError


class FakeEmergencyTicketRepository(EmergencyTicketRepository):
    def __init__(self) -> None:
        self._tickets: dict[uuid.UUID, EmergencyTicket] = {}

    async def create(
        self,
        *,
        organization_id,
        conversation_id,
        matched_service_id,
        customer_name,
        customer_phone,
        customer_address,
        summary,
    ):
        existing = await self.get_by_conversation_id(conversation_id)
        if existing is not None:
            return existing
        now = datetime.now(timezone.utc)
        ticket = EmergencyTicket(
            id=uuid.uuid4(),
            organization_id=organization_id,
            conversation_id=conversation_id,
            matched_service_id=matched_service_id,
            status=TicketStatus.NEW,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_address=customer_address,
            summary=summary,
            assigned_technician_user_id=None,
            assigned_at=None,
            closed_at=None,
            created_at=now,
            updated_at=now,
        )
        self._tickets[ticket.id] = ticket
        return ticket

    async def get_by_id(self, organization_id, ticket_id):
        ticket = self._tickets.get(ticket_id)
        if ticket is None or ticket.organization_id != organization_id:
            return None
        return ticket

    async def get_by_conversation_id(self, conversation_id):
        return next(
            (t for t in self._tickets.values() if t.conversation_id == conversation_id), None
        )

    async def list_for_organization(self, organization_id, *, status=None, limit, offset):
        matches = [t for t in self._tickets.values() if t.organization_id == organization_id]
        if status is not None:
            matches = [t for t in matches if t.status == status]
        matches.sort(key=lambda t: t.created_at, reverse=True)
        return matches[offset : offset + limit]

    async def assign(self, ticket_id, *, technician_user_id, assigned_at):
        ticket = self._tickets[ticket_id]
        updated = replace(
            ticket,
            assigned_technician_user_id=technician_user_id,
            assigned_at=assigned_at,
            status=TicketStatus.ASSIGNED,
        )
        self._tickets[ticket_id] = updated
        return updated

    async def update_status(
        self, organization_id, ticket_id, *, status, closed_at=None, actual_value=None
    ):
        ticket = self._tickets[ticket_id]
        updated = replace(
            ticket,
            status=status,
            closed_at=closed_at if closed_at is not None else ticket.closed_at,
            actual_value=actual_value if actual_value is not None else ticket.actual_value,
        )
        self._tickets[ticket_id] = updated
        return updated

    async def set_customer(self, organization_id, ticket_id, *, customer_id):
        ticket = self._tickets[ticket_id]
        updated = replace(ticket, customer_id=customer_id)
        self._tickets[ticket_id] = updated
        return updated

    async def list_by_customer_id(self, organization_id, customer_id):
        matches = [
            t
            for t in self._tickets.values()
            if t.organization_id == organization_id and t.customer_id == customer_id
        ]
        matches.sort(key=lambda t: t.created_at, reverse=True)
        return matches

    # --- Analytics (Milestone 8) ---

    async def count_created_in_range(self, organization_id, *, start, end):
        return len(
            [
                t
                for t in self._tickets.values()
                if t.organization_id == organization_id and _in_range(t.created_at, start, end)
            ]
        )

    def _closed_in_range(self, organization_id, *, status, start, end):
        return [
            t
            for t in self._tickets.values()
            if t.organization_id == organization_id
            and t.status == status
            and t.closed_at is not None
            and _in_range(t.closed_at, start, end)
        ]

    async def count_closed_in_range(self, organization_id, *, status, start, end):
        return len(self._closed_in_range(organization_id, status=status, start=start, end=end))

    async def sum_actual_value_in_range(self, organization_id, *, status, start, end):
        matches = self._closed_in_range(organization_id, status=status, start=start, end=end)
        return sum((t.actual_value or Decimal("0") for t in matches), Decimal("0"))

    async def revenue_by_day(self, organization_id, *, status, start, end):
        matches = self._closed_in_range(organization_id, status=status, start=start, end=end)
        buckets: dict = defaultdict(lambda: Decimal("0"))
        for t in matches:
            if t.actual_value is not None:
                buckets[t.closed_at.date()] += t.actual_value
        return [DailyRevenue(day=day, amount=amount) for day, amount in sorted(buckets.items())]

    async def average_resolution_minutes(self, organization_id, *, start, end):
        matches = self._closed_in_range(
            organization_id, status=TicketStatus.RESOLVED, start=start, end=end
        )
        if not matches:
            return None
        total_minutes = sum(
            (t.closed_at - t.created_at).total_seconds() / 60 for t in matches
        )
        return total_minutes / len(matches)


class FakeAppointmentRepository(AppointmentRepository):
    def __init__(self) -> None:
        self._appointments: dict[uuid.UUID, Appointment] = {}

    async def create(
        self,
        *,
        organization_id,
        conversation_id,
        matched_service_id,
        customer_name,
        customer_phone,
        customer_address,
        summary,
        duration_minutes,
    ):
        existing = await self.get_by_conversation_id(conversation_id)
        if existing is not None:
            return existing
        now = datetime.now(timezone.utc)
        appointment = Appointment(
            id=uuid.uuid4(),
            organization_id=organization_id,
            conversation_id=conversation_id,
            matched_service_id=matched_service_id,
            status=AppointmentStatus.REQUESTED,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_address=customer_address,
            summary=summary,
            scheduled_start_at=None,
            duration_minutes=duration_minutes,
            assigned_technician_user_id=None,
            assigned_at=None,
            closed_at=None,
            created_at=now,
            updated_at=now,
        )
        self._appointments[appointment.id] = appointment
        return appointment

    async def get_by_id(self, organization_id, appointment_id):
        appointment = self._appointments.get(appointment_id)
        if appointment is None or appointment.organization_id != organization_id:
            return None
        return appointment

    async def get_by_conversation_id(self, conversation_id):
        return next(
            (a for a in self._appointments.values() if a.conversation_id == conversation_id),
            None,
        )

    async def list_for_organization(self, organization_id, *, status=None, limit, offset):
        matches = [
            a for a in self._appointments.values() if a.organization_id == organization_id
        ]
        if status is not None:
            matches = [a for a in matches if a.status == status]
        matches.sort(
            key=lambda a: (a.scheduled_start_at is None, a.scheduled_start_at, a.created_at),
        )
        return matches[offset : offset + limit]

    async def schedule(
        self,
        organization_id,
        appointment_id,
        *,
        scheduled_start_at,
        duration_minutes,
        technician_user_id,
        assigned_at,
    ):
        appointment = self._appointments[appointment_id]
        updated = replace(
            appointment,
            scheduled_start_at=scheduled_start_at,
            duration_minutes=duration_minutes,
            assigned_technician_user_id=technician_user_id,
            assigned_at=assigned_at,
            status=AppointmentStatus.SCHEDULED,
        )
        self._appointments[appointment_id] = updated
        return updated

    async def update_status(
        self, organization_id, appointment_id, *, status, closed_at=None, actual_value=None
    ):
        appointment = self._appointments[appointment_id]
        updated = replace(
            appointment,
            status=status,
            closed_at=closed_at if closed_at is not None else appointment.closed_at,
            actual_value=actual_value if actual_value is not None else appointment.actual_value,
        )
        self._appointments[appointment_id] = updated
        return updated

    async def set_customer(self, organization_id, appointment_id, *, customer_id):
        appointment = self._appointments[appointment_id]
        updated = replace(appointment, customer_id=customer_id)
        self._appointments[appointment_id] = updated
        return updated

    async def list_by_customer_id(self, organization_id, customer_id):
        matches = [
            a
            for a in self._appointments.values()
            if a.organization_id == organization_id and a.customer_id == customer_id
        ]
        matches.sort(key=lambda a: a.created_at, reverse=True)
        return matches

    # --- Analytics (Milestone 8) ---

    async def count_created_in_range(self, organization_id, *, start, end):
        return len(
            [
                a
                for a in self._appointments.values()
                if a.organization_id == organization_id and _in_range(a.created_at, start, end)
            ]
        )

    def _closed_in_range(self, organization_id, *, status, start, end):
        return [
            a
            for a in self._appointments.values()
            if a.organization_id == organization_id
            and a.status == status
            and a.closed_at is not None
            and _in_range(a.closed_at, start, end)
        ]

    async def count_closed_in_range(self, organization_id, *, status, start, end):
        return len(self._closed_in_range(organization_id, status=status, start=start, end=end))

    async def sum_actual_value_in_range(self, organization_id, *, status, start, end):
        matches = self._closed_in_range(organization_id, status=status, start=start, end=end)
        return sum((a.actual_value or Decimal("0") for a in matches), Decimal("0"))

    async def revenue_by_day(self, organization_id, *, status, start, end):
        matches = self._closed_in_range(organization_id, status=status, start=start, end=end)
        buckets: dict = defaultdict(lambda: Decimal("0"))
        for a in matches:
            if a.actual_value is not None:
                buckets[a.closed_at.date()] += a.actual_value
        return [DailyRevenue(day=day, amount=amount) for day, amount in sorted(buckets.items())]

    async def status_breakdown_in_range(self, organization_id, *, start, end):
        buckets: dict = defaultdict(int)
        for a in self._appointments.values():
            if a.organization_id == organization_id and _in_range(a.created_at, start, end):
                buckets[a.status.value] += 1
        return [BucketCount(label=label, count=count) for label, count in buckets.items()]


class FakeTechnicianProfileRepository(TechnicianProfileRepository):
    def __init__(self) -> None:
        self._profiles: dict[uuid.UUID, TechnicianProfile] = {}

    async def create(self, *, organization_id, user_id, phone_number, is_on_call=True, notes=None):
        now = datetime.now(timezone.utc)
        profile = TechnicianProfile(
            id=uuid.uuid4(),
            organization_id=organization_id,
            user_id=user_id,
            phone_number=phone_number,
            is_on_call=is_on_call,
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        self._profiles[user_id] = profile
        return profile

    async def get_by_user_id(self, user_id):
        return self._profiles.get(user_id)

    async def list_for_organization(self, organization_id, *, on_call_only=False):
        matches = [p for p in self._profiles.values() if p.organization_id == organization_id]
        if on_call_only:
            matches = [p for p in matches if p.is_on_call]
        return matches

    async def set_on_call(self, organization_id, user_id, is_on_call):
        profile = self._profiles[user_id]
        updated = replace(profile, is_on_call=is_on_call)
        self._profiles[user_id] = updated
        return updated


class FakeCustomerRepository(CustomerRepository):
    def __init__(self) -> None:
        self._customers: dict[uuid.UUID, Customer] = {}

    async def create(
        self, *, organization_id, full_name, phone_number, email=None, address=None, notes=None
    ):
        if await self.get_by_phone_number(organization_id, phone_number) is not None:
            raise EntityAlreadyExistsError("Customer", "phone_number", phone_number)
        now = datetime.now(timezone.utc)
        customer = Customer(
            id=uuid.uuid4(),
            organization_id=organization_id,
            full_name=full_name,
            phone_number=phone_number,
            email=email,
            address=address,
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        self._customers[customer.id] = customer
        return customer

    async def get_by_id(self, organization_id, customer_id):
        customer = self._customers.get(customer_id)
        if customer is None or customer.organization_id != organization_id:
            return None
        return customer

    async def get_by_phone_number(self, organization_id, phone_number):
        return next(
            (
                c
                for c in self._customers.values()
                if c.organization_id == organization_id and c.phone_number == phone_number
            ),
            None,
        )

    async def list_for_organization(self, organization_id, *, search=None, limit, offset):
        matches = [c for c in self._customers.values() if c.organization_id == organization_id]
        if search:
            needle = search.lower()
            matches = [
                c
                for c in matches
                if needle in (c.full_name or "").lower() or needle in c.phone_number.lower()
            ]
        matches.sort(key=lambda c: c.created_at, reverse=True)
        return matches[offset : offset + limit]

    async def update(
        self, organization_id, customer_id, *, full_name, phone_number, email, address, notes
    ):
        customer = self._customers[customer_id]
        colliding = await self.get_by_phone_number(customer.organization_id, phone_number)
        if colliding is not None and colliding.id != customer_id:
            raise EntityAlreadyExistsError("Customer", "phone_number", phone_number)
        updated = replace(
            customer,
            full_name=full_name,
            phone_number=phone_number,
            email=email,
            address=address,
            notes=notes,
        )
        self._customers[customer_id] = updated
        return updated

    # --- Analytics (Milestone 8) ---

    async def count_new_in_range(self, organization_id, *, start, end):
        return len(
            [
                c
                for c in self._customers.values()
                if c.organization_id == organization_id and _in_range(c.created_at, start, end)
            ]
        )

    async def count_total(self, organization_id):
        return len([c for c in self._customers.values() if c.organization_id == organization_id])


class FakeUserRepository(UserRepository):
    """Covers what `DispatchService.create_technician` and `TeamService`
    (Milestone 9) need — same "only what's actually called" convention as
    every other fake in this module. `role_repository`, when provided,
    lets `set_roles` resolve role ids to real `Role` objects, mirroring how
    `FakeConversationOutcomeRepository` shares state with a conversation
    repository for its analytics methods."""

    def __init__(self, role_repository: "FakeRoleRepository | None" = None) -> None:
        self._users: dict[uuid.UUID, User] = {}
        self._roles = role_repository

    async def get_by_id(self, user_id):
        return self._users.get(user_id)

    async def get_by_email(self, email):
        return next((u for u in self._users.values() if u.email == email), None)

    async def create(
        self, *, organization_id, email, hashed_password, full_name, role_ids, is_superuser=False
    ):
        now = datetime.now(timezone.utc)
        roles = self._resolve_roles(role_ids)
        user = User(
            id=uuid.uuid4(),
            organization_id=organization_id,
            email=email,
            hashed_password=hashed_password,
            full_name=full_name,
            is_active=True,
            is_superuser=is_superuser,
            created_at=now,
            updated_at=now,
            last_login_at=None,
            roles=roles,
        )
        self._users[user.id] = user
        return user

    async def record_login(self, user_id):
        raise NotImplementedError

    async def list_by_organization_id(self, organization_id):
        matches = [u for u in self._users.values() if u.organization_id == organization_id]
        matches.sort(key=lambda u: u.created_at)
        return matches

    async def set_active(self, user_id, *, is_active):
        user = self._users[user_id]
        updated = replace(user, is_active=is_active)
        self._users[user_id] = updated
        return updated

    async def set_roles(self, user_id, *, role_ids):
        user = self._users[user_id]
        updated = replace(user, roles=self._resolve_roles(role_ids))
        self._users[user_id] = updated
        return updated

    def _resolve_roles(self, role_ids: list[uuid.UUID]) -> tuple[Role, ...]:
        if not role_ids or self._roles is None:
            return ()
        return tuple(role for role in self._roles._roles.values() if role.id in role_ids)


class FakeRoleRepository(RoleRepository):
    """Only `get_or_create_by_name` is implemented — `DispatchService` never
    calls `seed_default_roles`/`get_by_ids` (those are Auth's concern)."""

    def __init__(self) -> None:
        self._roles: dict[tuple[uuid.UUID, str], Role] = {}

    async def seed_default_roles(self, organization_id):
        raise NotImplementedError

    async def get_by_ids(self, role_ids):
        raise NotImplementedError

    async def get_or_create_by_name(self, organization_id, name, permission_codes):
        key = (organization_id, name)
        existing = self._roles.get(key)
        if existing is not None:
            return existing
        role = Role(
            id=uuid.uuid4(),
            organization_id=organization_id,
            name=name,
            description=f"{name} role",
            is_system_role=True,
            permission_codes=frozenset(permission_codes),
        )
        self._roles[key] = role
        return role


class FakeOrganizationRepository(OrganizationRepository):
    def __init__(self) -> None:
        self._organizations: dict[uuid.UUID, Organization] = {}

    def seed(self, organization: Organization) -> None:
        self._organizations[organization.id] = organization

    async def get_by_id(self, organization_id):
        return self._organizations.get(organization_id)

    async def get_by_slug(self, slug):
        return next((o for o in self._organizations.values() if o.slug == slug), None)

    async def create(self, *, name, slug):
        now = datetime.now(timezone.utc)
        organization = Organization(
            id=uuid.uuid4(), name=name, slug=slug, is_active=True, created_at=now, updated_at=now
        )
        self._organizations[organization.id] = organization
        return organization

    async def update(self, organization_id, *, name=None, is_active=None):
        organization = self._organizations[organization_id]
        updated = replace(
            organization,
            name=name if name is not None else organization.name,
            is_active=is_active if is_active is not None else organization.is_active,
        )
        self._organizations[organization_id] = updated
        return updated


def default_reply(**overrides) -> AIReply:
    kwargs = dict(
        message_to_customer="Thanks for calling — how else can I help?",
        classification=CallClassification.NON_EMERGENCY,
        confidence=0.9,
        recommended_action=RecommendedAction.ANSWER_FAQ,
        matched_service_name=None,
        customer_name=None,
        customer_phone=None,
        customer_address=None,
        is_conversation_complete=False,
        summary="Routine inquiry.",
    )
    kwargs.update(overrides)
    return AIReply(**kwargs)


class FakeAIProvider(AIProvider):
    """Replies are scripted via `queue_reply`; if the queue is empty a
    benign non-emergency reply is returned so tests that don't care about
    the AI's specific output don't have to script every turn."""

    def __init__(self) -> None:
        self._queue: list[AIReply] = []
        self.requests: list[AIRequest] = []

    def queue_reply(self, reply: AIReply) -> None:
        self._queue.append(reply)

    async def generate_reply(self, request: AIRequest) -> AIReply:
        self.requests.append(request)
        if self._queue:
            return self._queue.pop(0)
        return default_reply()
