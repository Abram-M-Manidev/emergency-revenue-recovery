"""Orchestrates Milestone 8 (Analytics): a read-only aggregate report over
everything the pipeline built by AI Brain (M3) -> Dispatch (M5) /
Appointments (M6) -> Customers (M7) has produced for an organization.

This service never talks to `AIBrainService`, `DispatchService`,
`AppointmentService`, or `CustomerService` directly — it only reads the
repositories those modules already own
(`ConversationRepository`/`ConversationOutcomeRepository`,
`EmergencyTicketRepository`, `AppointmentRepository`,
`CustomerRepository`), exactly the seam `CustomerService` already used to
compose across modules in Milestone 7. `BusinessProfileRepository` (M2) is
read too, only for the organization's timezone, so "Today" and day-bucket
boundaries line up with the org's local calendar rather than UTC's."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.entities.analytics import BucketCount, DailyCount, DailyRevenue, DateRangePreset
from app.domain.entities.appointment import AppointmentStatus
from app.domain.entities.emergency_ticket import TicketStatus
from app.domain.repositories.appointment_repository import AppointmentRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.customer_repository import CustomerRepository
from app.domain.repositories.emergency_ticket_repository import EmergencyTicketRepository


@dataclass(frozen=True, slots=True)
class AnalyticsSummary:
    """Composed read-side report — never persisted, so (like
    `CustomerService.CustomerHistory`) it lives here in the application
    layer rather than `domain/entities/`."""

    range_start: datetime | None
    range_end: datetime
    total_conversations: int
    conversations_by_day: list[DailyCount]
    conversations_by_channel: list[BucketCount]
    classification_breakdown: list[BucketCount]
    recommended_action_breakdown: list[BucketCount]
    tickets_created: int
    tickets_resolved: int
    average_ticket_resolution_minutes: float | None
    appointments_created: int
    appointments_completed: int
    appointments_no_show: int
    appointment_show_up_rate: float | None
    appointment_status_breakdown: list[BucketCount]
    new_customers: int
    total_customers: int
    ticket_revenue: Decimal
    appointment_revenue: Decimal
    total_revenue: Decimal
    revenue_by_day: list[DailyRevenue]


class AnalyticsService:
    def __init__(
        self,
        *,
        conversation_repository: ConversationRepository,
        conversation_outcome_repository: ConversationOutcomeRepository,
        emergency_ticket_repository: EmergencyTicketRepository,
        appointment_repository: AppointmentRepository,
        customer_repository: CustomerRepository,
        business_profile_repository: BusinessProfileRepository,
    ) -> None:
        self._conversations = conversation_repository
        self._outcomes = conversation_outcome_repository
        self._tickets = emergency_ticket_repository
        self._appointments = appointment_repository
        self._customers = customer_repository
        self._business_profiles = business_profile_repository

    async def get_summary(
        self, organization_id: uuid.UUID, *, preset: DateRangePreset
    ) -> AnalyticsSummary:
        start, end = await self._resolve_range(organization_id, preset)

        total_conversations = await self._conversations.count_in_range(
            organization_id, start=start, end=end
        )
        conversations_by_day = await self._conversations.count_by_day(
            organization_id, start=start, end=end
        )
        conversations_by_channel = await self._conversations.count_by_channel_in_range(
            organization_id, start=start, end=end
        )
        classification_breakdown = await self._outcomes.classification_breakdown(
            organization_id, start=start, end=end
        )
        recommended_action_breakdown = await self._outcomes.recommended_action_breakdown(
            organization_id, start=start, end=end
        )

        tickets_created = await self._tickets.count_created_in_range(
            organization_id, start=start, end=end
        )
        tickets_resolved = await self._tickets.count_closed_in_range(
            organization_id, status=TicketStatus.RESOLVED, start=start, end=end
        )
        average_ticket_resolution_minutes = await self._tickets.average_resolution_minutes(
            organization_id, start=start, end=end
        )
        ticket_revenue = await self._tickets.sum_actual_value_in_range(
            organization_id, status=TicketStatus.RESOLVED, start=start, end=end
        )
        ticket_revenue_by_day = await self._tickets.revenue_by_day(
            organization_id, status=TicketStatus.RESOLVED, start=start, end=end
        )

        appointments_created = await self._appointments.count_created_in_range(
            organization_id, start=start, end=end
        )
        appointments_completed = await self._appointments.count_closed_in_range(
            organization_id, status=AppointmentStatus.COMPLETED, start=start, end=end
        )
        appointments_no_show = await self._appointments.count_closed_in_range(
            organization_id, status=AppointmentStatus.NO_SHOW, start=start, end=end
        )
        appointment_status_breakdown = await self._appointments.status_breakdown_in_range(
            organization_id, start=start, end=end
        )
        appointment_revenue = await self._appointments.sum_actual_value_in_range(
            organization_id, status=AppointmentStatus.COMPLETED, start=start, end=end
        )
        appointment_revenue_by_day = await self._appointments.revenue_by_day(
            organization_id, status=AppointmentStatus.COMPLETED, start=start, end=end
        )

        new_customers = await self._customers.count_new_in_range(
            organization_id, start=start, end=end
        )
        total_customers = await self._customers.count_total(organization_id)

        show_up_total = appointments_completed + appointments_no_show
        appointment_show_up_rate = (
            appointments_completed / show_up_total if show_up_total > 0 else None
        )

        return AnalyticsSummary(
            range_start=start,
            range_end=end,
            total_conversations=total_conversations,
            conversations_by_day=conversations_by_day,
            conversations_by_channel=conversations_by_channel,
            classification_breakdown=classification_breakdown,
            recommended_action_breakdown=recommended_action_breakdown,
            tickets_created=tickets_created,
            tickets_resolved=tickets_resolved,
            average_ticket_resolution_minutes=average_ticket_resolution_minutes,
            appointments_created=appointments_created,
            appointments_completed=appointments_completed,
            appointments_no_show=appointments_no_show,
            appointment_show_up_rate=appointment_show_up_rate,
            appointment_status_breakdown=appointment_status_breakdown,
            new_customers=new_customers,
            total_customers=total_customers,
            ticket_revenue=ticket_revenue,
            appointment_revenue=appointment_revenue,
            total_revenue=ticket_revenue + appointment_revenue,
            revenue_by_day=_merge_daily_revenue(ticket_revenue_by_day, appointment_revenue_by_day),
        )

    async def _resolve_range(
        self, organization_id: uuid.UUID, preset: DateRangePreset
    ) -> tuple[datetime | None, datetime]:
        profile = await self._business_profiles.get_by_organization_id(organization_id)
        # No BusinessProfile configured yet — fall back to UTC, same
        # fallback AppointmentService._ensure_within_business_hours uses.
        org_zone = ZoneInfo(profile.timezone) if profile is not None else timezone.utc

        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(org_zone)

        if preset is DateRangePreset.ALL_TIME:
            return None, now_utc

        if preset is DateRangePreset.TODAY:
            local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            return local_midnight.astimezone(timezone.utc), now_utc

        days_by_preset = {
            DateRangePreset.LAST_7_DAYS: 7,
            DateRangePreset.LAST_30_DAYS: 30,
            DateRangePreset.LAST_90_DAYS: 90,
        }
        days = days_by_preset[preset]
        return now_utc - timedelta(days=days), now_utc


def _merge_daily_revenue(
    ticket_revenue: list[DailyRevenue], appointment_revenue: list[DailyRevenue]
) -> list[DailyRevenue]:
    totals: dict = {}
    for entry in (*ticket_revenue, *appointment_revenue):
        totals[entry.day] = totals.get(entry.day, Decimal("0")) + entry.amount
    return [DailyRevenue(day=day, amount=totals[day]) for day in sorted(totals)]
