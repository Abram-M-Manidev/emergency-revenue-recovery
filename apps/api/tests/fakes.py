"""Test doubles shared across unit and integration tests. Kept in one place
so the AI Brain's dependencies (the LLM call, in particular) never have to
hit a real, paid, non-deterministic API in CI. Also holds the in-memory
repository fakes used to build a real `AIBrainService` for tests (both its
own unit tests and `VoiceService`'s, which wraps it) without a database."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.config import Settings
from app.domain.ai.provider import (
    AIProvider,
    AIReply,
    AIReplyComplete,
    AIRequest,
    AIStreamEvent,
    AITextDelta,
    AIToolPhase,
)
from app.domain.ai.tools import BOOK_APPOINTMENT, ToolInvocation, ToolResult
from app.domain.entities.analytics import BucketCount, DailyCount, DailyRevenue
from app.domain.entities.appointment import Appointment, AppointmentStatus
from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.domain.entities.business_profile import BusinessProfile
from app.domain.entities.conversation import Conversation, ConversationStatus
from app.domain.entities.conversation_message import ConversationMessage
from app.domain.entities.conversation_outcome import (
    CallClassification,
    ConversationOutcome,
    RecommendedAction,
)
from app.domain.entities.customer import Customer
from app.domain.entities.emergency_keyword import EmergencyKeyword
from app.domain.entities.emergency_ticket import EmergencyTicket, TicketStatus
from app.domain.entities.faq_entry import FAQEntry
from app.domain.entities.offered_slot import OfferedSlot
from app.domain.entities.organization import Organization
from app.domain.entities.rbac import DEFAULT_ROLES
from app.domain.entities.role import Role
from app.domain.entities.service import Service
from app.domain.entities.service_area import ServiceArea
from app.domain.entities.technician_profile import TechnicianProfile
from app.domain.entities.user import User
from app.domain.exceptions import EntityAlreadyExistsError, EntityNotFoundError
from app.domain.locks import BookingLock, CallLock
from app.domain.notifications.emergency import (
    DeliveryStatus,
    EmergencyAlert,
    NotificationChannel,
    NotificationDelivery,
    NotificationReceipt,
)
from app.domain.notifications.provider import NotificationPort
from app.domain.notifications.settings import NotificationSettings, mask_destination
from app.domain.repositories.appointment_repository import AppointmentRepository
from app.domain.repositories.business_hours_repository import BusinessHoursRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.caller_identity_repository import CallerIdentityRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.customer_repository import CustomerRepository
from app.domain.repositories.emergency_keyword_repository import EmergencyKeywordRepository
from app.domain.repositories.emergency_ticket_repository import EmergencyTicketRepository
from app.domain.repositories.faq_repository import FAQRepository
from app.domain.repositories.notification_repository import (
    NotificationDeliveryRepository,
    NotificationSettingsRepository,
)
from app.domain.repositories.offered_slot_repository import OfferedSlotRepository
from app.domain.repositories.organization_repository import OrganizationRepository
from app.domain.repositories.role_repository import RoleRepository
from app.domain.repositories.service_area_repository import ServiceAreaRepository
from app.domain.repositories.service_repository import ServiceRepository
from app.domain.repositories.technician_profile_repository import TechnicianProfileRepository
from app.domain.repositories.user_repository import UserRepository

# Mirrors `appointment_repository_impl._FALLBACK_DURATION_MINUTES`, so the
# fake and the real overlap query agree on a SCHEDULED row with no duration.
_FALLBACK_DURATION_MINUTES = 60


def fake_settings(**overrides: object) -> Settings:
    """A real `Settings` object with test-friendly overrides.

    Deliberately the real class rather than a `SimpleNamespace` stub. A stub
    only carries the fields whoever wrote it remembered to add, so every
    setting introduced later silently breaks every test that predates it —
    which is exactly what happened when `AI_TOOLS_ENABLED` was added and
    eleven previously-passing unit tests started failing with
    `AttributeError`. Building from the real class means a test tracks the
    real settings surface automatically, and a service reading a setting the
    tests never heard of gets the production default instead of a crash.

    `conftest.py` has already forced `ENVIRONMENT=testing` and a
    `JWT_SECRET_KEY` into the environment by the time this is called, so
    construction always succeeds."""
    return Settings().model_copy(update=dict(overrides))  # type: ignore[call-arg]


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
    def __init__(self, conversation_repository: FakeConversationRepository | None = None) -> None:
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

    async def backfill_contact_details(
        self,
        organization_id,
        ticket_id,
        *,
        customer_name=None,
        customer_phone=None,
        customer_address=None,
    ):
        # Mirrors the real repository: the lookup is org-scoped, so a
        # mismatched tenant finds nothing and raises rather than writing.
        ticket = self._tickets.get(ticket_id)
        if ticket is None or ticket.organization_id != organization_id:
            raise EntityNotFoundError("EmergencyTicket", str(ticket_id))
        updates = {
            field: value
            for field, value in (
                ("customer_name", customer_name),
                ("customer_phone", customer_phone),
                ("customer_address", customer_address),
            )
            if value is not None
        }
        updated = replace(ticket, **updates)
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
        self.backfill_calls: list[tuple[uuid.UUID, dict[str, str | None]]] = []

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

    async def backfill_contact_details(
        self,
        organization_id,
        appointment_id,
        *,
        customer_name=None,
        customer_phone=None,
        customer_address=None,
    ):
        # `backfill_calls` lets a test assert that an appointment with
        # nothing missing produced *no write at all*, which an equality
        # check on the returned record cannot distinguish from a no-op
        # update. Same device as `FakeCustomerRepository`.
        self.backfill_calls.append(
            (
                appointment_id,
                {
                    "customer_name": customer_name,
                    "customer_phone": customer_phone,
                    "customer_address": customer_address,
                },
            )
        )
        # Mirrors the real repository: the lookup is org-scoped, so a
        # mismatched tenant finds nothing and raises rather than writing.
        appointment = self._appointments.get(appointment_id)
        if appointment is None or appointment.organization_id != organization_id:
            raise EntityNotFoundError("Appointment", str(appointment_id))
        updates = {
            field: value
            for field, value in (
                ("customer_name", customer_name),
                ("customer_phone", customer_phone),
                ("customer_address", customer_address),
            )
            if value is not None
        }
        updated = replace(appointment, **updates)
        self._appointments[appointment_id] = updated
        return updated

    async def list_scheduled_in_range(self, organization_id, *, start_at, end_at):
        matches = [
            a
            for a in self._appointments.values()
            if a.organization_id == organization_id
            and a.status is AppointmentStatus.SCHEDULED
            and a.scheduled_start_at is not None
            and a.scheduled_start_at < end_at
            and a.scheduled_start_at
            + timedelta(minutes=a.duration_minutes or _FALLBACK_DURATION_MINUTES)
            > start_at
        ]
        matches.sort(key=lambda a: a.scheduled_start_at)
        return matches

    async def count_overlapping(
        self, organization_id, *, start_at, end_at, exclude_appointment_id=None
    ):
        # Mirrors the SQL in `appointment_repository_impl.py` exactly: only
        # SCHEDULED rows hold time, the interval is half-open so
        # back-to-back appointments do not collide, and the excluded id is
        # what stops a reschedule conflicting with itself.
        count = 0
        for appointment in self._appointments.values():
            if appointment.organization_id != organization_id:
                continue
            if appointment.status is not AppointmentStatus.SCHEDULED:
                continue
            if appointment.scheduled_start_at is None:
                continue
            if exclude_appointment_id is not None and appointment.id == exclude_appointment_id:
                continue
            occupied_end = appointment.scheduled_start_at + timedelta(
                minutes=appointment.duration_minutes or _FALLBACK_DURATION_MINUTES
            )
            if appointment.scheduled_start_at < end_at and occupied_end > start_at:
                count += 1
        return count

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
        self.backfill_calls: list[tuple[uuid.UUID, dict[str, str | None]]] = []

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

    async def backfill_contact_details(
        self, organization_id, customer_id, *, full_name=None, address=None
    ):
        # `backfill_calls` lets a test assert that a customer with nothing
        # missing produced *no write at all*, which an equality check on
        # the returned record cannot distinguish from a no-op update.
        self.backfill_calls.append((customer_id, {"full_name": full_name, "address": address}))
        customer = self._customers.get(customer_id)
        if customer is None or customer.organization_id != organization_id:
            raise EntityNotFoundError("Customer", str(customer_id))
        changes = {}
        if full_name is not None:
            changes["full_name"] = full_name
        if address is not None:
            changes["address"] = address
        updated = replace(customer, **changes)
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

    def __init__(self, role_repository: FakeRoleRepository | None = None) -> None:
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
        # Implemented because `AuthService.login` calls it on every
        # successful sign-in, so any unit test of the auth flow trips over it
        # otherwise.
        user = self._users[user_id]
        updated = replace(user, last_login_at=datetime.now(timezone.utc))
        self._users[user_id] = updated
        return updated

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
    """Backs both `DispatchService` (which only ever calls
    `get_or_create_by_name`) and `AuthService.register`, which seeds the
    default role set for a new organization.

    `seed_default_roles` builds from `DEFAULT_ROLES` rather than a
    hand-written list, so a permission added to a role in the real catalogue
    appears here too instead of quietly diverging."""

    def __init__(self) -> None:
        self._roles: dict[tuple[uuid.UUID, str], Role] = {}
        self._by_id: dict[uuid.UUID, Role] = {}

    async def seed_default_roles(self, organization_id):
        seeded: dict[str, Role] = {}
        for name, permission_codes in DEFAULT_ROLES.items():
            role = await self.get_or_create_by_name(
                organization_id, name, permission_codes
            )
            seeded[name] = role
        return seeded

    async def get_by_ids(self, role_ids):
        return [self._by_id[role_id] for role_id in role_ids if role_id in self._by_id]

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
        self._by_id[role.id] = role
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

    async def update(
        self, organization_id, *, name=None, is_active=None, voice_assistant_enabled=None
    ):
        organization = self._organizations[organization_id]
        updated = replace(
            organization,
            name=name if name is not None else organization.name,
            is_active=is_active if is_active is not None else organization.is_active,
            voice_assistant_enabled=(
                voice_assistant_enabled
                if voice_assistant_enabled is not None
                else organization.voice_assistant_enabled
            ),
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


class BlockingAIProvider(FakeAIProvider):
    """`generate_reply` parks on an `asyncio.Event` until `release()` is
    called.

    This is what makes the concurrency tests deterministic: a race can be
    set up by starting several requests, waiting for `entered` to confirm
    one is genuinely inside the provider, then releasing — rather than
    sleeping and hoping the interleaving happens."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.fail_with: Exception | None = None

    def release(self) -> None:
        self.gate.set()

    async def generate_reply(self, request: AIRequest) -> AIReply:
        self.requests.append(request)
        self.entered.set()
        await self.gate.wait()
        if self.fail_with is not None:
            raise self.fail_with
        if self._queue:
            return self._queue.pop(0)
        return default_reply()


class FakeCallLock(CallLock):
    """Per-key `asyncio.Lock`, mirroring the mutual exclusion
    `PostgresAdvisoryCallLock` provides.

    A single-process test cannot exercise cross-worker behaviour, but it can
    verify the property the production lock exists to guarantee — that two
    requests for one call never overlap — which `max_concurrent` records."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._active = 0
        self.max_concurrent = 0
        self.acquisitions = 0

    def hold(self, key: str) -> AbstractAsyncContextManager[None]:
        return self._hold(key)

    @asynccontextmanager
    async def _hold(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            self.acquisitions += 1
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
            try:
                yield
            finally:
                self._active -= 1


class FakeOfferedSlotRepository(OfferedSlotRepository):
    """In-memory record of what each conversation was offered, and of which
    offer the caller then chose.

    Keyed by `(organization_id, conversation_id, start_at)` so the tests can
    prove the isolation properties the real queries enforce: an offer made to
    one conversation cannot authorise a booking in another, neither can one
    made to another tenant, and neither can a *selection* made in either.

    Mirrors the production constraint that at most one slot per conversation
    is selected at a time — `mark_selected` clears the others, exactly as the
    partial unique index forces the real repository to."""

    def __init__(self) -> None:
        self.offers: dict[tuple[uuid.UUID, uuid.UUID, datetime], OfferedSlot] = {}

    async def record_offered(self, organization_id, conversation_id, slots, turn_index):
        for slot in slots:
            key = (organization_id, conversation_id, slot.start_at)
            existing = self.offers.get(key)
            self.offers[key] = OfferedSlot(
                organization_id=organization_id,
                conversation_id=conversation_id,
                start_at=slot.start_at,
                duration_minutes=slot.duration_minutes,
                # A re-offer refreshes the duration but must not move the
                # turn index or disturb a selection — the ON CONFLICT DO
                # UPDATE set in the real repository is exactly this narrow.
                offered_turn_index=(
                    existing.offered_turn_index if existing else turn_index
                ),
                selected_at=existing.selected_at if existing else None,
                selected_turn_index=existing.selected_turn_index if existing else None,
            )

    async def list_offered_starts(self, organization_id, conversation_id):
        return sorted(
            start_at
            for (org, conv, start_at) in self.offers
            if org == organization_id and conv == conversation_id
        )

    async def offered_duration_minutes(self, organization_id, conversation_id, start_at):
        offered = self.offers.get((organization_id, conversation_id, start_at))
        return offered.duration_minutes if offered else None

    async def get_offered(self, organization_id, conversation_id, start_at):
        return self.offers.get((organization_id, conversation_id, start_at))

    async def mark_selected(self, organization_id, conversation_id, start_at, turn_index):
        key = (organization_id, conversation_id, start_at)
        if key not in self.offers:
            return None
        await self.clear_selection(organization_id, conversation_id)
        offered = self.offers[key]
        updated = replace(
            offered,
            selected_at=datetime.now(timezone.utc),
            selected_turn_index=turn_index,
        )
        self.offers[key] = updated
        return updated

    async def get_active_selection(self, organization_id, conversation_id):
        for (org, conv, _), offered in self.offers.items():
            if org == organization_id and conv == conversation_id and offered.is_selected:
                return offered
        return None

    async def clear_selection(self, organization_id, conversation_id):
        for key, offered in list(self.offers.items()):
            org, conv, _ = key
            if org == organization_id and conv == conversation_id and offered.is_selected:
                self.offers[key] = replace(
                    offered, selected_at=None, selected_turn_index=None
                )


class FakeBookingLock(BookingLock):
    """Per-key `asyncio.Lock`, mirroring what `PostgresAdvisoryBookingLock`
    provides across worker processes.

    `max_concurrent` is the point of it: the double-booking test asserts
    that two callers racing for one slot never execute the
    verify-then-write section at the same time. Without that assertion a
    passing conflict test proves only that the two requests happened not to
    interleave, not that they cannot."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._active = 0
        self.max_concurrent = 0
        self.acquisitions = 0

    def hold(self, key: str) -> AbstractAsyncContextManager[None]:
        return self._hold(key)

    @asynccontextmanager
    async def _hold(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            self.acquisitions += 1
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
            try:
                yield
            finally:
                self._active -= 1


@dataclass(frozen=True, slots=True)
class _SpokenToolRound:
    """One round in which the model both speaks and requests tools — the
    response shape `response_format=json_schema` + `tools` produces about a
    third of the time, and the one that used to have its tool calls
    discarded."""

    text: str | None
    calls: list[tuple[str, dict]]


class ScriptedToolAIProvider(AIProvider):
    """An `AIProvider` that really drives the tool loop, without OpenAI.

    Scripted as a list of *rounds*. Each round is either a list of
    `(tool_name, arguments)` pairs — which this provider executes through
    the real `request.tool_executor`, exactly as `OpenAIProvider` does — or
    an `AIReply`, which ends the turn.

    This exists to prove the half of the flow that OpenAI's wire format is
    irrelevant to: that a tool call reaches the real executor, the real
    application services, and the real database, and that the result comes
    back. `tests/unit/test_openai_tool_loop.py` covers the other half (chunk
    accumulation, message shaping, round limits) against a stubbed client.

    `results` records every `ToolResult` produced, so a test can assert on
    what the model would actually have seen — which is the only thing that
    can justify the assistant's final sentence."""

    def __init__(self, rounds: list[object] | None = None) -> None:
        self.rounds: list[object] = rounds or []
        self.requests: list[AIRequest] = []
        self.results: list[ToolResult] = []
        self.invocations: list[ToolInvocation] = []

    def queue_tool_round(self, calls: list[tuple[str, dict]], *, speak: str | None = None) -> None:
        """`speak` reproduces the response shape that broke the 2026-08-22
        call: the model announcing what it is about to do *and* requesting
        the tool in the same response. Without it a test can only exercise
        the tool-calls-only shape, which is exactly the blind spot that let
        the bug reach a real caller."""
        self.rounds.append(_SpokenToolRound(speak, calls) if speak else calls)

    def queue_reply(self, reply: AIReply) -> None:
        self.rounds.append(reply)

    def _booking_failed_unrecovered(self, since: int) -> bool:
        """Mirrors `OpenAIProvider._updated_booking_state`: a booking failure
        latches, and a later success clears it.

        `since` scopes it to the current turn. `results` accumulates for the
        whole conversation, but the real flag is a local in one
        `stream_reply`/`generate_reply` call — so without the offset a turn-1
        failure would leak into turn 2 and this double would gate calls the
        production loop lets through."""
        state = False
        for result in self.results[since:]:
            if result.name != BOOK_APPOINTMENT.name:
                continue
            state = not result.content.get("success")
        return state

    def _finalise(self, reply: AIReply, since: int) -> AIReply:
        return replace(
            reply, booking_failed_unrecovered=self._booking_failed_unrecovered(since)
        )

    async def generate_reply(self, request: AIRequest) -> AIReply:
        self.requests.append(request)
        turn_started_at = len(self.results)
        for round_spec in list(self.rounds):
            self.rounds.pop(0)
            if isinstance(round_spec, AIReply):
                return self._finalise(round_spec, turn_started_at)
            if isinstance(round_spec, _SpokenToolRound):
                round_spec = round_spec.calls
            assert request.tool_executor is not None, (
                "ScriptedToolAIProvider was given a tool round but the request "
                "carries no executor — the AI Brain did not wire tools."
            )
            for index, (name, arguments) in enumerate(round_spec):  # type: ignore[union-attr]
                invocation = ToolInvocation(
                    id=f"call_{len(self.results)}_{index}", name=name, arguments=arguments
                )
                self.invocations.append(invocation)
                self.results.append(await request.tool_executor.execute(invocation))
        return self._finalise(default_reply(), turn_started_at)

    async def stream_reply(self, request: AIRequest) -> AsyncIterator[AIStreamEvent]:
        """Streaming twin, emitting `AIToolPhase` before each tool round so
        the voice transport's holding-phrase behaviour is exercised the same
        way a real streamed turn would exercise it."""
        self.requests.append(request)
        turn_started_at = len(self.results)
        for round_spec in list(self.rounds):
            self.rounds.pop(0)
            if isinstance(round_spec, AIReply):
                yield AITextDelta(round_spec.message_to_customer)
                yield AIReplyComplete(self._finalise(round_spec, turn_started_at))
                return
            already_spoke = False
            if isinstance(round_spec, _SpokenToolRound):
                if round_spec.text:
                    already_spoke = True
                    yield AITextDelta(round_spec.text)
                round_spec = round_spec.calls
            assert request.tool_executor is not None
            yield AIToolPhase(
                tuple(name for name, _ in round_spec),  # type: ignore[union-attr]
                model_already_spoke=already_spoke,
            )
            for index, (name, arguments) in enumerate(round_spec):  # type: ignore[union-attr]
                invocation = ToolInvocation(
                    id=f"call_{len(self.results)}_{index}", name=name, arguments=arguments
                )
                self.invocations.append(invocation)
                self.results.append(await request.tool_executor.execute(invocation))
        reply = self._finalise(default_reply(), turn_started_at)
        yield AITextDelta(reply.message_to_customer)
        yield AIReplyComplete(reply)


class FakeCallerIdentityRepository(CallerIdentityRepository):
    """In-memory caller-ID associations, org-scoped like the real one.

    `associations` is exposed so a test can assert that P5 wrote *only* an
    association and never touched a customer field, and `fail_with` lets a
    test drive the best-effort degradation path without a real database
    failure."""

    def __init__(self, customers: FakeCustomerRepository | None = None) -> None:
        self._customers = customers
        # (organization_id, caller_number, customer_id) -> call count
        self.associations: dict[tuple[uuid.UUID, str, uuid.UUID], int] = {}
        self.fail_with: Exception | None = None
        # Separate from `fail_with` so a test can fail the *write* while
        # leaving the read path working, which is the P5 blocker case.
        self.fail_associate_with: Exception | None = None

    async def find_customers_by_caller_number(self, organization_id, caller_number):
        if self.fail_with is not None:
            raise self.fail_with
        if not caller_number or not caller_number.strip():
            return []
        needle = caller_number.strip()
        matches = [
            customer_id
            for (org, number, customer_id) in self.associations
            if org == organization_id and number == needle
        ]
        if self._customers is None:
            return []
        found = []
        for customer_id in matches:
            customer = await self._customers.get_by_id(organization_id, customer_id)
            if customer is not None:
                found.append(customer)
        return found

    async def associate(self, organization_id, *, customer_id, caller_number):
        if self.fail_associate_with is not None:
            raise self.fail_associate_with
        if not caller_number or not caller_number.strip():
            return
        key = (organization_id, caller_number.strip(), customer_id)
        # Counts rather than overwrites, so idempotency is observable.
        self.associations[key] = self.associations.get(key, 0) + 1


class FakeNotificationSettingsRepository(NotificationSettingsRepository):
    """One organization's alert destination, in memory.

    Defaults to nothing configured — the same default the real deployment
    has — so a test that wants the assistant to be allowed to claim an alert
    has to say so explicitly.

    `disabled` mirrors the production split between `get_destination` (which
    filters disabled rows out, so a paused tenant cannot be claimed as
    alerted) and `get_settings` (which does not, so an operator can still see
    what they paused)."""

    def __init__(
        self, destinations: dict[uuid.UUID, tuple[NotificationChannel, str]] | None = None
    ) -> None:
        self.destinations = destinations or {}
        self.disabled: set[uuid.UUID] = set()
        self._timestamps: dict[uuid.UUID, datetime] = {}

    async def get_destination(self, organization_id):
        if organization_id in self.disabled:
            return None
        return self.destinations.get(organization_id)

    async def get_settings(self, organization_id):
        entry = self.destinations.get(organization_id)
        if entry is None:
            return None
        channel, destination = entry
        created = self._timestamps.setdefault(organization_id, datetime.now(timezone.utc))
        return NotificationSettings(
            organization_id=organization_id,
            channel=channel,
            destination_hint=mask_destination(destination),
            is_enabled=organization_id not in self.disabled,
            created_at=created,
            updated_at=datetime.now(timezone.utc),
        )

    async def upsert_settings(self, organization_id, *, channel, destination, is_enabled):
        self.destinations[organization_id] = (channel, destination)
        self._timestamps.setdefault(organization_id, datetime.now(timezone.utc))
        if is_enabled:
            self.disabled.discard(organization_id)
        else:
            self.disabled.add(organization_id)
        settings = await self.get_settings(organization_id)
        assert settings is not None
        return settings

    async def set_enabled(self, organization_id, *, is_enabled):
        if organization_id not in self.destinations:
            return None
        if is_enabled:
            self.disabled.discard(organization_id)
        else:
            self.disabled.add(organization_id)
        return await self.get_settings(organization_id)

    async def delete_settings(self, organization_id):
        existed = organization_id in self.destinations
        self.destinations.pop(organization_id, None)
        self.disabled.discard(organization_id)
        self._timestamps.pop(organization_id, None)
        return existed


class FakeNotificationDeliveryRepository(NotificationDeliveryRepository):
    """Delivery rows keyed by ticket, mirroring the production unique index.

    `claim` returns `True` to exactly one caller per ticket, which is what
    makes the idempotency tests meaningful: a fake that always granted the
    claim would let a duplicate-send bug pass."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, NotificationDelivery] = {}
        self.claims: list[uuid.UUID] = []

    async def get_for_ticket(self, organization_id, ticket_id):
        row = self.rows.get(ticket_id)
        if row is None or row.organization_id != organization_id:
            return None
        return row

    async def claim(self, organization_id, ticket_id, *, channel, provider):
        self.claims.append(ticket_id)
        existing = await self.get_for_ticket(organization_id, ticket_id)
        if existing is not None:
            return existing, False
        now_at = datetime.now(timezone.utc)
        row = NotificationDelivery(
            id=uuid.uuid4(),
            organization_id=organization_id,
            ticket_id=ticket_id,
            channel=channel,
            provider=provider,
            status=DeliveryStatus.PENDING,
            attempts=0,
            error_code=None,
            delivered_at=None,
            created_at=now_at,
            updated_at=now_at,
        )
        self.rows[ticket_id] = row
        return row, True

    async def record_result(self, organization_id, ticket_id, *, status, error_code, provider):
        existing = await self.get_for_ticket(organization_id, ticket_id)
        assert existing is not None, "record_result called without a prior claim"
        updated = replace(
            existing,
            status=status,
            error_code=error_code,
            provider=provider,
            attempts=existing.attempts + 1,
            delivered_at=(
                datetime.now(timezone.utc)
                if status is DeliveryStatus.DELIVERED
                else existing.delivered_at
            ),
            updated_at=datetime.now(timezone.utc),
        )
        self.rows[ticket_id] = updated
        return updated


class FakeNotificationProvider(NotificationPort):
    """A `NotificationPort` whose verdict the test chooses.

    `sends` records every attempt, so a test can assert that exactly one
    alert went out for one ticket — the property idempotency exists to
    provide, and one that a call-count-blind fake could not express."""

    def __init__(
        self,
        *,
        status: DeliveryStatus = DeliveryStatus.DELIVERED,
        error_code: str | None = None,
        delay_seconds: float = 0.0,
        raises: bool = False,
    ) -> None:
        self.status = status
        self.error_code = error_code
        self.delay_seconds = delay_seconds
        self.raises = raises
        self.sends: list[tuple[EmergencyAlert, str | None]] = []

    @property
    def name(self) -> str:
        return "fake"

    async def send(self, alert, destination):
        self.sends.append((alert, destination))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.raises:
            # A provider that violates its own never-raise contract. The
            # service must still return a receipt rather than propagating.
            raise RuntimeError("provider blew up")
        if destination is None:
            # Every real adapter that needs a destination reports
            # NOT_CONFIGURED without one. Mirroring that here matters: a fake
            # that returned DELIVERED regardless would let a tenant with no
            # configuration inherit another tenant's "alerted" state, which
            # is precisely the bug the isolation tests are looking for.
            return NotificationReceipt(
                status=DeliveryStatus.NOT_CONFIGURED,
                provider=self.name,
                error_code="no_destination_configured",
            )
        return NotificationReceipt(
            status=self.status, provider=self.name, error_code=self.error_code
        )
