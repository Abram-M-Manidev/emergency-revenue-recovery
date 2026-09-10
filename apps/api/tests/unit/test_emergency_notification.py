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
from datetime import datetime, timezone

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


def _service(
    provider: FakeNotificationProvider,
    *,
    destinations: dict | None = None,
    deliveries: FakeNotificationDeliveryRepository | None = None,
    **setting_overrides: object,
) -> tuple[EmergencyNotificationService, FakeNotificationDeliveryRepository]:
    repo = deliveries or FakeNotificationDeliveryRepository()
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
            settings=fake_settings(**setting_overrides),
        ),
        repo,
    )


# =============================================================================
# The service: delivery state, idempotency, bounds
# =============================================================================


@pytest.mark.asyncio
async def test_a_successful_alert_is_recorded_as_delivered():
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, repo = _service(provider)
    ticket = _ticket()

    delivery = await service.notify_ticket(ticket)

    assert delivery.status is DeliveryStatus.DELIVERED
    assert delivery.alerted_a_human is True
    assert delivery.attempts == 1
    assert delivery.delivered_at is not None
    assert repo.rows[ticket.id].status is DeliveryStatus.DELIVERED
    # The destination reached the provider and came from settings, not from
    # anything the caller or the model supplied.
    assert provider.sends[0][1] == _DESTINATION


@pytest.mark.asyncio
async def test_a_refused_alert_is_recorded_as_failed_and_never_claims_success():
    provider = FakeNotificationProvider(
        status=DeliveryStatus.FAILED, error_code="http_500"
    )
    service, _ = _service(provider)

    delivery = await service.notify_ticket(_ticket())

    assert delivery.status is DeliveryStatus.FAILED
    assert delivery.alerted_a_human is False
    assert delivery.error_code == "http_500"


@pytest.mark.asyncio
async def test_an_organization_with_no_destination_is_not_configured_not_failed():
    """The distinction an operator needs: nobody set alerting up here, which
    is an onboarding gap rather than an incident."""
    provider = FakeNotificationProvider(status=DeliveryStatus.NOT_CONFIGURED)
    service, _ = _service(provider, destinations={})

    delivery = await service.notify_ticket(_ticket())

    assert delivery.status is DeliveryStatus.NOT_CONFIGURED
    assert delivery.alerted_a_human is False
    # No destination was handed over, because there was none.
    assert provider.sends[0][1] is None


@pytest.mark.asyncio
async def test_notifying_the_same_ticket_twice_sends_exactly_one_alert():
    """The tool loop alerts, then the webhook's outcome sync runs on the same
    conversation and would alert again. One gas leak must not page the
    on-call engineer twice."""
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, _ = _service(provider)
    ticket = _ticket()

    first = await service.notify_ticket(ticket)
    second = await service.notify_ticket(ticket)

    assert len(provider.sends) == 1
    assert first.status is DeliveryStatus.DELIVERED
    assert second.status is DeliveryStatus.DELIVERED
    assert second.attempts == 1


@pytest.mark.asyncio
async def test_concurrent_notifications_for_one_ticket_send_once():
    """Two requests for the same call arriving together — which Vapi produces
    routinely as a transcript grows."""
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, _ = _service(provider)
    ticket = _ticket()

    await asyncio.gather(service.notify_ticket(ticket), service.notify_ticket(ticket))

    assert len(provider.sends) == 1


@pytest.mark.asyncio
async def test_a_failed_alert_is_retried_within_budget():
    """A transient outage should not permanently mark a call unalerted."""
    provider = FakeNotificationProvider(
        status=DeliveryStatus.FAILED, error_code="transport_error"
    )
    service, _ = _service(provider, NOTIFICATION_MAX_ATTEMPTS=2)
    ticket = _ticket()

    await service.notify_ticket(ticket)
    provider.status = DeliveryStatus.DELIVERED
    provider.error_code = None
    recovered = await service.notify_ticket(ticket)

    assert len(provider.sends) == 2
    assert recovered.status is DeliveryStatus.DELIVERED
    assert recovered.alerted_a_human is True


@pytest.mark.asyncio
async def test_retries_stop_at_the_configured_budget():
    provider = FakeNotificationProvider(status=DeliveryStatus.FAILED, error_code="timeout")
    service, _ = _service(provider, NOTIFICATION_MAX_ATTEMPTS=2)
    ticket = _ticket()

    for _ in range(5):
        await service.notify_ticket(ticket)

    assert len(provider.sends) == 2, "the attempt budget was not enforced"


@pytest.mark.asyncio
async def test_a_delivered_alert_is_never_re_sent_even_with_budget_left():
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, _ = _service(provider, NOTIFICATION_MAX_ATTEMPTS=5)
    ticket = _ticket()

    for _ in range(3):
        await service.notify_ticket(ticket)

    assert len(provider.sends) == 1


@pytest.mark.asyncio
async def test_an_unconfigured_organization_is_not_retried_on_every_turn():
    """Retrying a missing destination cannot fix it, and would spend the
    timeout budget as dead air on every later turn of the call."""
    provider = FakeNotificationProvider(status=DeliveryStatus.NOT_CONFIGURED)
    service, _ = _service(provider, destinations={}, NOTIFICATION_MAX_ATTEMPTS=5)
    ticket = _ticket()

    for _ in range(3):
        await service.notify_ticket(ticket)

    assert len(provider.sends) == 1


@pytest.mark.asyncio
async def test_a_slow_provider_is_cut_off_and_reported_as_failed():
    """A live caller hears every second of this as silence, so the bound is
    load-bearing — and exceeding it must be a truthful FAILED, not a crash."""
    provider = FakeNotificationProvider(
        status=DeliveryStatus.DELIVERED, delay_seconds=0.5
    )
    service, _ = _service(provider, NOTIFICATION_TIMEOUT_SECONDS=0.05)

    delivery = await service.notify_ticket(_ticket())

    assert delivery.status is DeliveryStatus.FAILED
    assert delivery.error_code == "timeout"
    assert delivery.alerted_a_human is False


@pytest.mark.asyncio
async def test_a_provider_that_raises_does_not_break_the_turn():
    """The port forbids raising, but a defective adapter must still degrade
    to a weaker sentence rather than dropping a call."""
    provider = FakeNotificationProvider(raises=True)
    service, _ = _service(provider)

    delivery = await service.notify_ticket(_ticket())

    assert delivery.status is DeliveryStatus.FAILED
    assert delivery.error_code == "provider_error"
    assert delivery.alerted_a_human is False


@pytest.mark.asyncio
async def test_one_tenants_destination_is_never_used_for_another():
    """The destination is read per call, scoped by organization. A second
    tenant with no configuration must not inherit the first tenant's."""
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, _ = _service(provider)

    await service.notify_ticket(_ticket(_ORG_ID))
    other = await service.notify_ticket(_ticket(_OTHER_ORG_ID))

    assert provider.sends[0][1] == _DESTINATION
    assert provider.sends[1][1] is None, "a destination leaked across tenants"
    assert other.alerted_a_human is False


@pytest.mark.asyncio
async def test_delivery_state_is_readable_without_sending():
    provider = FakeNotificationProvider(status=DeliveryStatus.DELIVERED)
    service, _ = _service(provider)
    ticket = _ticket()
    await service.notify_ticket(ticket)

    found = await service.get_delivery(_ORG_ID, ticket.id)
    missing = await service.get_delivery(_OTHER_ORG_ID, ticket.id)

    assert found is not None and found.alerted_a_human is True
    # Scoped by organization, so another tenant cannot read this state.
    assert missing is None
    assert len(provider.sends) == 1


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
    """`create_service_request` over real services, with the notification
    outcome under the test's control."""

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
        notifications = (
            EmergencyNotificationService(
                provider=self.provider,
                settings_repository=FakeNotificationSettingsRepository(
                    destinations
                    if destinations is not None
                    else {_ORG_ID: (NotificationChannel.WEBHOOK, _DESTINATION)}
                ),
                delivery_repository=self.deliveries,
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
            emergency_notification_service=notifications,
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


class _NoHours:
    async def get_weekly_hours(self, organization_id):
        return []

    async def list_exceptions(self, organization_id):
        return []


@pytest.mark.asyncio
async def test_a_delivered_alert_licenses_the_dispatcher_sentence():
    harness = _ToolHarness(notification_status=DeliveryStatus.DELIVERED)

    result = await harness.report_emergency()

    assert result["success"] is True
    assert result["dispatcher_alerted"] is True
    assert result["notification_status"] == "delivered"
    assert "A dispatcher has been alerted" in result["next_step"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [DeliveryStatus.FAILED, DeliveryStatus.NOT_CONFIGURED],
)
async def test_an_undelivered_alert_forbids_the_dispatcher_sentence(
    status: DeliveryStatus,
):
    """The heart of it. The ticket exists either way; the sentence does not."""
    harness = _ToolHarness(notification_status=status)

    result = await harness.report_emergency()

    # The emergency is still recorded — a failed alert must never lose the
    # ticket, or a degraded notification becomes a lost emergency.
    assert result["success"] is True
    assert result["service_request_type"] == "emergency_ticket"
    ticket = await harness.tickets.get_by_conversation_id(harness.conversation_id)
    assert ticket is not None

    # But nothing may claim a human was told.
    assert result["dispatcher_alerted"] is False
    assert result["notification_status"] == status.value
    guidance = result["next_step"]
    assert "could NOT be confirmed" in guidance
    assert "Do NOT say a dispatcher has been alerted" in guidance
    assert "emergency services" in guidance


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
    """`describe_progress` is prompt text on every subsequent turn. It used to
    assert "dispatcher alerted" unconditionally, which put the false sentence
    back into the prompt even once the tool result had stopped claiming it."""
    failed = _ToolHarness(notification_status=DeliveryStatus.FAILED)
    await failed.report_emergency()
    delivered = _ToolHarness(notification_status=DeliveryStatus.DELIVERED)
    await delivered.report_emergency()

    failed_progress = await failed.factory.describe_progress(
        _ORG_ID, failed.conversation_id
    )
    delivered_progress = await delivered.factory.describe_progress(
        _ORG_ID, delivered.conversation_id
    )

    assert failed_progress is not None
    assert "could NOT be confirmed" in failed_progress
    assert "Do NOT tell the caller a dispatcher has been alerted" in failed_progress

    assert delivered_progress is not None
    assert "A dispatcher has been alerted and will contact them" in delivered_progress
    assert "could NOT be confirmed" not in delivered_progress


@pytest.mark.asyncio
async def test_one_alert_per_emergency_even_when_the_tool_runs_twice():
    """`create_service_request` is safe to call again when a detail is
    corrected, and the model does. The caller's on-call engineer should not
    hear about it twice."""
    harness = _ToolHarness(notification_status=DeliveryStatus.DELIVERED)

    first = await harness.report_emergency()
    second = await harness.report_emergency()

    assert first["dispatcher_alerted"] is True
    assert second["dispatcher_alerted"] is True
    assert harness.provider is not None
    assert len(harness.provider.sends) == 1


@pytest.mark.asyncio
async def test_the_alert_never_carries_the_destination_into_the_tool_result():
    """Tool results are handed to the model, which puts them within reach of
    prompt-injection and of anything downstream that logs a turn. The
    destination is a credential and must not be in there."""
    harness = _ToolHarness(notification_status=DeliveryStatus.DELIVERED)

    result = await harness.report_emergency()

    rendered = " ".join(str(value) for value in result.values())
    assert _DESTINATION not in rendered
    assert "hooks.example.invalid" not in rendered
