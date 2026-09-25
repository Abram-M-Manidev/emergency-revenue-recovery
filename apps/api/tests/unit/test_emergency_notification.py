"""Emergency notification, and the sentence it licenses.

Before this existed, ESSR told every emergency caller "a dispatcher has been
alerted" while having no outbound notification mechanism at all. The sentence
was false on every call that produced one, and the failure mode is the worst
kind: a caller who believes help is coming stops looking for it.

Two things are under test here, and they are separable:

1. Alerting works, is idempotent, is bounded, and records what happened.
2. The assistant may only claim a dispatcher was alerted when (1) actually
   succeeded — and specifically NOT when the ticket merely exists.

The second is the one that matters. A test suite that only proved alerts get
sent would still permit the original lie on every failure path, so most of
what follows is about failure: no provider, no destination, a refusing
endpoint, a timeout, a provider that raises. In every one of those the ticket
must still exist and the assistant must still be forbidden from claiming an
alert.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.application.services.customer_service import CustomerService
from app.application.services.dispatch_service import DispatchService
from app.application.services.emergency_notification_service import (
    EmergencyNotificationService,
)
from app.application.services.voice_tool_executor import VoiceToolExecutor
from app.domain.ai.tools import CREATE_SERVICE_REQUEST, ToolInvocation
from app.domain.entities.emergency_ticket import EmergencyTicket, TicketStatus
from app.domain.notifications.emergency import (
    DeliveryStatus,
    EmergencyAlert,
    NotificationChannel,
)
from app.infrastructure.notifications.providers import (
    LoggingNotificationProvider,
    NullNotificationProvider,
    WebhookNotificationProvider,
)
from tests.fakes import (
    FakeAppointmentRepository,
    FakeBusinessProfileRepository,
    FakeCallerIdentityRepository,
    FakeConversationOutcomeRepository,
    FakeConversationRepository,
    FakeCustomerRepository,
    FakeEmergencyTicketRepository,
    FakeNotificationDeliveryRepository,
    FakeNotificationProvider,
    FakeNotificationSettingsRepository,
    FakeOfferedSlotRepository,
    FakeRoleRepository,
    FakeServiceRepository,
    FakeTechnicianProfileRepository,
    FakeUserRepository,
    fake_settings,
)

_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()
_DESTINATION = "https://hooks.example.invalid/services/T000/B000/xxxx"


def _ticket(organization_id: uuid.UUID = _ORG_ID) -> EmergencyTicket:
    now = datetime.now(timezone.utc)
    return EmergencyTicket(
        id=uuid.uuid4(),
        organization_id=organization_id,
        conversation_id=uuid.uuid4(),
        matched_service_id=None,
        status=TicketStatus.NEW,
        customer_name="Frank",
        customer_phone="5550001111",
        customer_address="11 69 Street",
        summary="Smell of gas near the furnace.",
        assigned_technician_user_id=None,
        assigned_at=None,
        closed_at=None,
        created_at=now,
        updated_at=now,
    )


class _Tickets:
    """The one read the outbox makes at send time, org-scoped like the real
    repository: a ticket is only visible under its own organization."""

    def __init__(self, *tickets: EmergencyTicket) -> None:
        self.by_id = {ticket.id: ticket for ticket in tickets}

    def add(self, ticket: EmergencyTicket) -> EmergencyTicket:
        self.by_id[ticket.id] = ticket
        return ticket

    async def get_by_id(self, organization_id, ticket_id):
        ticket = self.by_id.get(ticket_id)
        if ticket is None or ticket.organization_id != organization_id:
            return None
        return ticket


def _service(
    provider: FakeNotificationProvider,
    *,
    destinations: dict | None = None,
    deliveries: FakeNotificationDeliveryRepository | None = None,
    tickets: _Tickets | None = None,
    **setting_overrides: object,
) -> tuple[EmergencyNotificationService, FakeNotificationDeliveryRepository, _Tickets]:
    repo = deliveries or FakeNotificationDeliveryRepository()
    ticket_repo = tickets or _Tickets()
    settings_repo = FakeNotificationSettingsRepository(
        destinations
        if destinations is not None
        else {_ORG_ID: (NotificationChannel.WEBHOOK, _DESTINATION)}
    )
    return (
        EmergencyNotificationService(
            provider=provider,
            settings_repository=settings_repo,
            delivery_repository=repo,
            ticket_repository=ticket_repo,  # type: ignore[arg-type]
            settings=fake_settings(**setting_overrides),
        ),
        repo,
        ticket_repo,
    )


def _later(seconds: float = 3600) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# =============================================================================
# The outbox: queued in the ticket's transaction, sent only afterwards
# =============================================================================


@pytest.mark.asyncio
async def test_enqueue_records_a_pending_alert_and_sends_nothing():
    """The whole point of the outbox: nothing leaves the system while the
    ticket's transaction is still open."""
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, repo, tickets = _service(provider)
    ticket = tickets.add(_ticket())

    delivery = await service.enqueue(ticket)

    assert provider.sends == []
    assert delivery.status is DeliveryStatus.PENDING
    assert delivery.is_queued is True
    assert delivery.alerted_a_human is False
    assert repo.rows[ticket.id].next_attempt_at is not None


@pytest.mark.asyncio
async def test_enqueue_registers_the_send_to_run_after_commit():
    delivered: list[uuid.UUID] = []

    class _Hooks:
        def __init__(self) -> None:
            self.callbacks = []

        def register(self, callback) -> None:
            self.callbacks.append(callback)

    async def deliver(ticket_id: uuid.UUID) -> None:
        delivered.append(ticket_id)

    hooks = _Hooks()
    service = EmergencyNotificationService(
        provider=FakeNotificationProvider(),
        settings_repository=FakeNotificationSettingsRepository(
            {_ORG_ID: (NotificationChannel.WEBHOOK, _DESTINATION)}
        ),
        delivery_repository=FakeNotificationDeliveryRepository(),
        settings=fake_settings(),
        after_commit=hooks,  # type: ignore[arg-type]
        deliver_after_commit=deliver,
    )
    ticket = _ticket()
    await service.enqueue(ticket)

    # Nothing ran yet — it runs only when the commit hook fires.
    assert delivered == []
    for callback in hooks.callbacks:
        await callback()
    assert delivered == [ticket.id]


@pytest.mark.asyncio
async def test_a_successful_alert_is_recorded_as_delivered():
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, repo, tickets = _service(provider)
    ticket = tickets.add(_ticket())
    await service.enqueue(ticket)

    delivery = await service.deliver_next_due()

    assert delivery is not None
    assert delivery.status is DeliveryStatus.DELIVERED
    assert delivery.alerted_a_human is True
    assert delivery.attempts == 1
    assert delivery.delivered_at is not None
    assert delivery.next_attempt_at is None
    # The destination came from settings, not from anything the caller or
    # the model supplied.
    assert provider.sends[0][1] == _DESTINATION


@pytest.mark.asyncio
async def test_a_refused_alert_leaves_the_ticket_and_schedules_a_retry():
    provider = FakeNotificationProvider(status=DeliveryStatus.FAILED, error_code="http_500")
    service, repo, tickets = _service(provider)
    ticket = tickets.add(_ticket())
    await service.enqueue(ticket)

    delivery = await service.deliver_next_due()

    assert delivery is not None
    assert delivery.status is DeliveryStatus.FAILED
    assert delivery.alerted_a_human is False
    assert delivery.error_code == "http_500"
    assert delivery.is_queued is True, "a failed alert with budget left must be retried"
    assert ticket.id in tickets.by_id


@pytest.mark.asyncio
async def test_an_organization_with_no_destination_is_not_configured_and_never_sent():
    """An onboarding gap, not an incident: recorded as such at enqueue time,
    never retried, never claimed."""
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, repo, tickets = _service(provider, destinations={})
    ticket = tickets.add(_ticket())

    delivery = await service.enqueue(ticket)

    assert delivery.status is DeliveryStatus.NOT_CONFIGURED
    assert delivery.is_queued is False
    assert await service.deliver_next_due(at=_later()) is None
    assert provider.sends == []


@pytest.mark.asyncio
async def test_queuing_the_same_ticket_twice_sends_exactly_one_alert():
    """The tool loop and the webhook's outcome sync both sync one ticket.
    One gas leak must not page the on-call engineer twice."""
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, repo, tickets = _service(provider)
    ticket = tickets.add(_ticket())

    await service.enqueue(ticket)
    await service.enqueue(ticket)
    await service.deliver_next_due()
    assert await service.deliver_next_due(at=_later()) is None

    assert len(repo.rows) == 1
    assert len(provider.sends) == 1


@pytest.mark.asyncio
async def test_concurrent_workers_send_one_alert_once():
    """The post-commit send and a poller tick landing together. The row lock
    lets exactly one of them have the alert."""
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED, delay_seconds=0.05)
    service, repo, tickets = _service(provider)
    ticket = tickets.add(_ticket())
    await service.enqueue(ticket)

    results = await asyncio.gather(service.deliver_next_due(), service.deliver_next_due())

    assert len(provider.sends) == 1
    assert sum(result is not None for result in results) == 1


@pytest.mark.asyncio
async def test_a_failed_alert_is_retried_when_due_and_eventually_delivered():
    provider = FakeNotificationProvider(status=DeliveryStatus.FAILED, error_code="transport_error")
    service, repo, tickets = _service(provider, NOTIFICATION_MAX_ATTEMPTS=3)
    ticket = tickets.add(_ticket())
    await service.enqueue(ticket)
    await service.deliver_next_due()

    # Not due yet: backoff is honoured.
    assert await service.deliver_next_due() is None
    provider.status = DeliveryStatus.DELIVERED
    provider.error_code = None
    recovered = await service.deliver_next_due(at=_later())

    assert recovered is not None
    assert recovered.status is DeliveryStatus.DELIVERED
    assert recovered.attempts == 2
    assert len(provider.sends) == 2


@pytest.mark.asyncio
async def test_retries_stop_at_the_configured_budget():
    provider = FakeNotificationProvider(status=DeliveryStatus.FAILED, error_code="timeout")
    service, repo, tickets = _service(provider, NOTIFICATION_MAX_ATTEMPTS=2)
    ticket = tickets.add(_ticket())
    await service.enqueue(ticket)

    for hours in range(1, 6):
        await service.deliver_next_due(at=_later(3600 * hours))

    assert len(provider.sends) == 2, "the attempt budget was not enforced"
    assert repo.rows[ticket.id].next_attempt_at is None
    assert repo.rows[ticket.id].is_queued is False


@pytest.mark.asyncio
async def test_retry_delays_back_off():
    provider = FakeNotificationProvider(status=DeliveryStatus.FAILED, error_code="http_503")
    service, repo, tickets = _service(
        provider, NOTIFICATION_MAX_ATTEMPTS=5, NOTIFICATION_RETRY_BASE_SECONDS=10.0
    )
    ticket = tickets.add(_ticket())
    await service.enqueue(ticket)
    at = datetime.now(timezone.utc)
    gaps = []
    for _ in range(3):
        delivery = await service.deliver_next_due(at=at)
        assert delivery is not None and delivery.next_attempt_at is not None
        gaps.append((delivery.next_attempt_at - at).total_seconds())
        at = delivery.next_attempt_at
    assert gaps == [10.0, 30.0, 90.0]


@pytest.mark.asyncio
async def test_a_delivered_alert_is_never_re_sent_even_with_budget_left():
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, repo, tickets = _service(provider, NOTIFICATION_MAX_ATTEMPTS=5)
    ticket = tickets.add(_ticket())
    await service.enqueue(ticket)

    for hours in range(3):
        await service.deliver_next_due(at=_later(3600 * hours))
        await service.enqueue(ticket)

    assert len(provider.sends) == 1


@pytest.mark.asyncio
async def test_a_slow_provider_is_cut_off_and_recorded_as_a_failed_attempt():
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED, delay_seconds=0.5)
    service, repo, tickets = _service(provider, NOTIFICATION_TIMEOUT_SECONDS=0.05)
    ticket = tickets.add(_ticket())
    await service.enqueue(ticket)

    delivery = await service.deliver_next_due()

    assert delivery is not None
    assert delivery.status is DeliveryStatus.FAILED
    assert delivery.error_code == "timeout"
    assert delivery.alerted_a_human is False


@pytest.mark.asyncio
async def test_a_provider_that_raises_is_recorded_not_propagated():
    provider = FakeNotificationProvider(raises=True)
    service, repo, tickets = _service(provider)
    ticket = tickets.add(_ticket())
    await service.enqueue(ticket)

    delivery = await service.deliver_next_due()

    assert delivery is not None
    assert delivery.status is DeliveryStatus.FAILED
    assert delivery.error_code == "provider_error"
    assert delivery.is_queued is True


@pytest.mark.asyncio
async def test_one_tenants_destination_is_never_used_for_another():
    """The destination is read per send, scoped by the delivery row's own
    organization. A tenant with no configuration must not inherit another's."""
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, repo, tickets = _service(provider)
    mine = tickets.add(_ticket(_ORG_ID))
    other = tickets.add(_ticket(_OTHER_ORG_ID))

    await service.enqueue(mine)
    other_delivery = await service.enqueue(other)
    await service.deliver_next_due(at=_later())

    assert [destination for _, destination in provider.sends] == [_DESTINATION]
    assert other_delivery.status is DeliveryStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_a_row_pointing_at_another_tenants_ticket_is_never_sent():
    """Impossible through the write path, but a corrupted or hand-edited
    outbox row must not carry one tenant's emergency to another tenant's
    destination."""
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, repo, tickets = _service(provider)
    foreign = tickets.add(_ticket(_OTHER_ORG_ID))
    await repo.enqueue(
        _ORG_ID,
        foreign.id,
        channel=NotificationChannel.WEBHOOK,
        provider="fake",
        status=DeliveryStatus.PENDING,
        next_attempt_at=datetime.now(timezone.utc),
    )

    delivery = await service.deliver_next_due()

    assert provider.sends == []
    assert delivery is not None
    assert delivery.status is DeliveryStatus.FAILED
    assert delivery.error_code == "ticket_unavailable"
    assert delivery.next_attempt_at is None


@pytest.mark.asyncio
async def test_delivery_state_is_readable_without_sending():
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, repo, tickets = _service(provider)
    ticket = tickets.add(_ticket())
    await service.enqueue(ticket)
    await service.deliver_next_due()

    found = await service.get_delivery(_ORG_ID, ticket.id)
    missing = await service.get_delivery(_OTHER_ORG_ID, ticket.id)

    assert found is not None and found.alerted_a_human is True
    assert missing is None
    assert len(provider.sends) == 1


@pytest.mark.asyncio
async def test_the_alert_carries_details_learned_after_the_ticket_opened():
    """Built at send time from the ticket as it is then, so an address the
    caller gave after the ticket was created still reaches the dispatcher."""
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, repo, tickets = _service(provider)
    ticket = tickets.add(replace(_ticket(), customer_address=None))
    await service.enqueue(ticket)
    tickets.add(replace(ticket, customer_address="11 69 Street"))

    await service.deliver_next_due()

    assert provider.sends[0][0].customer_address == "11 69 Street"


# =============================================================================
# The providers themselves
# =============================================================================


@pytest.mark.asyncio
async def test_the_null_provider_never_reports_delivery():
    """The default. A no-op that reported success would reproduce the exact
    failure this module exists to remove, one layer down."""
    receipt = await NullNotificationProvider().send(_alert(), None)

    assert receipt.status is DeliveryStatus.NOT_CONFIGURED
    assert receipt.status.alerted_a_human is False


@pytest.mark.asyncio
async def test_the_logging_provider_reports_delivery_for_development_only():
    receipt = await LoggingNotificationProvider().send(_alert(), None)

    assert receipt.status is DeliveryStatus.DELIVERED
    assert receipt.provider == "logging"


@pytest.mark.asyncio
async def test_the_webhook_provider_without_a_destination_is_not_configured():
    provider = WebhookNotificationProvider(timeout_seconds=1.0)

    receipt = await provider.send(_alert(), None)

    assert receipt.status is DeliveryStatus.NOT_CONFIGURED
    assert receipt.error_code == "no_destination_configured"


@pytest.mark.asyncio
async def test_the_webhook_provider_reports_transport_failures_without_leaking_the_url():
    """An unroutable host. The destination is a credential for Slack and
    Teams, so it must not appear in the error we keep."""
    provider = WebhookNotificationProvider(timeout_seconds=1.0)

    receipt = await provider.send(_alert(), "http://127.0.0.1:9/never-listening")

    assert receipt.status is DeliveryStatus.FAILED
    assert receipt.error_code == "transport_error"
    assert "127.0.0.1" not in (receipt.error_code or "")


def test_the_webhook_payload_carries_the_job_but_not_the_transcript():
    from app.infrastructure.notifications.providers import _alert_payload

    payload = _alert_payload(_alert())

    # What a dispatcher needs to act.
    assert "Smell of gas" in str(payload["text"])
    assert payload["customer_phone"] == "5550001111"
    assert payload["customer_address"] == "11 69 Street"
    assert payload["event"] == "emergency_ticket_created"
    # Stable dedupe key, derived from the ticket rather than the attempt.
    assert str(payload["idempotency_key"]).startswith("emergency_ticket:")


def _alert() -> EmergencyAlert:
    ticket = _ticket()
    return EmergencyAlert(
        organization_id=ticket.organization_id,
        ticket_id=ticket.id,
        conversation_id=ticket.conversation_id,
        summary=ticket.summary,
        customer_name=ticket.customer_name,
        customer_phone=ticket.customer_phone,
        customer_address=ticket.customer_address,
        created_at=ticket.created_at,
    )


# =============================================================================
# Truthfulness, through the tool the assistant actually reads
# =============================================================================


class _ToolHarness:
    """`create_service_request` over real services, with the outbox and the
    provider's verdict under the test's control. The tool never sends: the
    alert is queued with the ticket and `deliver()` stands in for the
    post-commit send."""

    def __init__(
        self,
        *,
        notification_status: DeliveryStatus | None = DeliveryStatus.DELIVERED,
        destinations: dict | None = None,
    ) -> None:
        self.settings = fake_settings()
        self.conversations = FakeConversationRepository()
        self.outcomes = FakeConversationOutcomeRepository(self.conversations)
        self.tickets = FakeEmergencyTicketRepository()
        self.appointments = FakeAppointmentRepository()
        self.customers = FakeCustomerRepository()
        self.provider = (
            FakeNotificationProvider(status=notification_status)
            if notification_status is not None
            else None
        )
        self.deliveries = FakeNotificationDeliveryRepository()
        self.notifications = (
            EmergencyNotificationService(
                provider=self.provider,
                settings_repository=FakeNotificationSettingsRepository(
                    destinations
                    if destinations is not None
                    else {_ORG_ID: (NotificationChannel.WEBHOOK, _DESTINATION)}
                ),
                delivery_repository=self.deliveries,
                ticket_repository=self.tickets,
                settings=self.settings,
            )
            if self.provider is not None
            else None
        )
        technicians = FakeTechnicianProfileRepository()
        from app.application.services.appointment_service import AppointmentService

        self.factory = VoiceToolExecutor(
            appointment_service=AppointmentService(
                appointment_repository=self.appointments,
                technician_profile_repository=technicians,
                conversation_outcome_repository=self.outcomes,
                service_repository=FakeServiceRepository([]),
                business_hours_repository=_NoHours(),
                business_profile_repository=FakeBusinessProfileRepository(None),
                offered_slot_repository=FakeOfferedSlotRepository(),
            ),
            dispatch_service=DispatchService(
                emergency_ticket_repository=self.tickets,
                technician_profile_repository=technicians,
                conversation_outcome_repository=self.outcomes,
                conversation_repository=self.conversations,
                user_repository=FakeUserRepository(),
                role_repository=FakeRoleRepository(),
                emergency_notifications=self.notifications,
            ),
            customer_service=CustomerService(
                customer_repository=self.customers,
                conversation_outcome_repository=self.outcomes,
                emergency_ticket_repository=self.tickets,
                appointment_repository=self.appointments,
                caller_identity_repository=FakeCallerIdentityRepository(self.customers),
            ),
            conversation_outcome_repository=self.outcomes,
            service_repository=FakeServiceRepository([]),
            business_profile_repository=FakeBusinessProfileRepository(None),
            offered_slot_repository=FakeOfferedSlotRepository(),
            settings=self.settings,
            emergency_notification_service=self.notifications,
        )
        self.conversation_id = uuid.uuid4()
        self.executor = self.factory.bind(_ORG_ID, self.conversation_id, 2)

    async def report_emergency(self) -> dict:
        result = await self.executor.execute(
            ToolInvocation(
                id="call_1",
                name=CREATE_SERVICE_REQUEST.name,
                arguments={
                    "customer_name": "Frank",
                    "customer_phone": "5550001111",
                    "service_address": "11 69 Street",
                    "problem_description": "Smell of gas near the furnace.",
                    "classification": "emergency",
                    "service_name": None,
                },
            )
        )
        return result.content

    async def deliver(self) -> None:
        """What the post-commit hook does once the turn has committed."""
        assert self.notifications is not None
        await self.notifications.deliver_next_due()


class _NoHours:
    async def get_weekly_hours(self, organization_id):
        return []

    async def list_exceptions(self, organization_id):
        return []


@pytest.mark.asyncio
async def test_the_tool_itself_never_sends_an_alert():
    """The turn that creates the ticket is inside an uncommitted transaction.
    Sending from there is exactly how a dispatcher got paged about a ticket
    that then rolled back."""
    harness = _ToolHarness(notification_status=DeliveryStatus.DELIVERED)

    result = await harness.report_emergency()

    assert result["success"] is True
    assert harness.provider is not None and harness.provider.sends == []
    assert harness.deliveries.rows, "the alert was not queued with the ticket"


@pytest.mark.asyncio
async def test_a_queued_alert_licenses_being_alerted_but_not_alerted():
    harness = _ToolHarness(notification_status=DeliveryStatus.DELIVERED)

    result = await harness.report_emergency()

    assert result["dispatcher_alerted"] is False
    assert result["notification_status"] == "pending"
    guidance = result["next_step"]
    assert "being sent right now" in guidance
    assert "Do NOT say a dispatcher HAS been alerted" in guidance
    assert "emergency services" in guidance


@pytest.mark.asyncio
async def test_an_unconfigured_organization_forbids_the_dispatcher_sentence():
    """The heart of it. The ticket exists either way; the sentence does not."""
    harness = _ToolHarness(notification_status=DeliveryStatus.DELIVERED, destinations={})

    result = await harness.report_emergency()

    assert result["success"] is True
    assert result["service_request_type"] == "emergency_ticket"
    assert await harness.tickets.get_by_conversation_id(harness.conversation_id) is not None
    assert result["dispatcher_alerted"] is False
    assert result["notification_status"] == "not_configured"
    guidance = result["next_step"]
    assert "could NOT be confirmed" in guidance
    assert "Do NOT say a dispatcher has been alerted" in guidance


@pytest.mark.asyncio
async def test_a_missing_notification_service_still_forbids_the_claim():
    """A deployment wiring mistake must not silently restore the old lie."""
    harness = _ToolHarness(notification_status=None)

    result = await harness.report_emergency()

    assert result["success"] is True
    assert result["dispatcher_alerted"] is False
    assert "Do NOT say a dispatcher has been alerted" in result["next_step"]


@pytest.mark.asyncio
async def test_a_later_turn_reads_the_alert_state_rather_than_assuming_it():
    """`describe_progress` is prompt text on every subsequent turn, read from
    the outbox row: queued, delivered and failed each license a different
    sentence."""
    queued = _ToolHarness(notification_status=DeliveryStatus.DELIVERED)
    await queued.report_emergency()
    delivered = _ToolHarness(notification_status=DeliveryStatus.DELIVERED)
    await delivered.report_emergency()
    await delivered.deliver()
    failed = _ToolHarness(notification_status=DeliveryStatus.DELIVERED, destinations={})
    await failed.report_emergency()

    queued_progress = await queued.factory.describe_progress(_ORG_ID, queued.conversation_id)
    delivered_progress = await delivered.factory.describe_progress(
        _ORG_ID, delivered.conversation_id
    )
    failed_progress = await failed.factory.describe_progress(_ORG_ID, failed.conversation_id)

    assert queued_progress is not None
    assert "being sent now" in queued_progress
    assert "Do NOT say a dispatcher HAS been alerted" in queued_progress

    assert delivered_progress is not None
    assert "A dispatcher has been alerted and will contact them" in delivered_progress
    assert "could NOT be confirmed" not in delivered_progress

    assert failed_progress is not None
    assert "could NOT be confirmed" in failed_progress
    assert "Do NOT tell the caller a dispatcher has been alerted" in failed_progress


@pytest.mark.asyncio
async def test_one_alert_per_emergency_even_when_the_tool_runs_twice():
    """`create_service_request` is safe to call again when a detail is
    corrected, and the model does. The on-call engineer hears about it once."""
    harness = _ToolHarness(notification_status=DeliveryStatus.DELIVERED)

    await harness.report_emergency()
    await harness.report_emergency()
    await harness.deliver()
    await harness.deliver()

    assert len(harness.deliveries.rows) == 1
    assert harness.provider is not None
    assert len(harness.provider.sends) == 1


@pytest.mark.asyncio
async def test_the_alert_never_carries_the_destination_into_the_tool_result():
    """Tool results are handed to the model. The destination is a credential
    and must not be in there."""
    harness = _ToolHarness(notification_status=DeliveryStatus.DELIVERED)

    result = await harness.report_emergency()

    rendered = " ".join(str(value) for value in result.values())
    assert _DESTINATION not in rendered
    assert "hooks.example.invalid" not in rendered
