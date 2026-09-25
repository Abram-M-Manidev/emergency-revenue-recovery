"""The emergency-alert outbox, against real PostgreSQL.

The property under test: a dispatcher can only ever be paged about a ticket
that has COMMITTED, every committed ticket's alert is eventually attempted,
and no alert is sent twice.

Why Postgres and not fakes: the guarantees rest on things only the database
provides — the ticket and its outbox row sharing one transaction, the row
lock (`FOR UPDATE SKIP LOCKED`) that stops two workers sending the same
alert, and a rollback that genuinely removes both rows. An in-memory fake
models each of those by construction and so cannot fail when they are wrong.

The recording provider below checks, at the moment it is asked to send,
from a separate connection, that the ticket is visible — i.e. committed.
That is the outbox's core promise made directly observable.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.api.deps import get_ai_provider, get_alert_outbox, verify_vapi_secret
from app.application.services.dispatch_service import DispatchService
from app.application.services.emergency_notification_service import (
    EmergencyNotificationService,
)
from app.domain.entities.conversation import ConversationChannel
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_line import VoiceProvider
from app.domain.notifications.emergency import (
    DeliveryStatus,
    EmergencyAlert,
    NotificationChannel,
    NotificationReceipt,
)
from app.domain.notifications.provider import NotificationPort
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.emergency_ticket import EmergencyTicketModel
from app.infrastructure.database.models.notification import (
    EmergencyNotificationDeliveryModel,
    OrganizationNotificationSettingsModel,
)
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.repositories import (
    SqlAlchemyConversationOutcomeRepository,
    SqlAlchemyConversationRepository,
    SqlAlchemyEmergencyTicketRepository,
    SqlAlchemyNotificationDeliveryRepository,
    SqlAlchemyNotificationSettingsRepository,
    SqlAlchemyRoleRepository,
    SqlAlchemyTechnicianProfileRepository,
    SqlAlchemyUserRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine, get_db
from app.infrastructure.database.transactions import SessionAfterCommit
from app.infrastructure.notifications.outbox import EmergencyAlertOutbox
from app.main import app, fastapi_app
from tests.fakes import ScriptedToolAIProvider, default_reply, fake_settings

_SECRET = "test-vapi-secret-outbox"
_DESTINATION = "https://hooks.example.invalid/services/T000/B000/outbox"

_EMERGENCY = {
    "customer_name": "Dana",
    "customer_phone": "6305550184",
    "service_address": "12 Elm Street, Lisle",
    "problem_description": "Smoke is coming from the furnace.",
    "classification": "emergency",
    "service_name": None,
}


class RecordingProvider(NotificationPort):
    """Records every send and, at send time, whether the ticket it is about
    is visible from a fresh connection — i.e. has committed."""

    def __init__(self, *, status: DeliveryStatus = DeliveryStatus.DELIVERED, delay: float = 0.0):
        self.status = status
        self.delay = delay
        self.sends: list[tuple[EmergencyAlert, str | None]] = []
        self.ticket_committed_at_send: list[bool] = []

    @property
    def name(self) -> str:
        return "recording"

    async def send(self, alert: EmergencyAlert, destination: str | None) -> NotificationReceipt:
        self.sends.append((alert, destination))
        async with AsyncSessionLocal() as session:
            visible = await session.get(EmergencyTicketModel, alert.ticket_id)
        self.ticket_committed_at_send.append(visible is not None)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status is DeliveryStatus.DELIVERED:
            return NotificationReceipt(status=DeliveryStatus.DELIVERED, provider=self.name)
        return NotificationReceipt(status=self.status, provider=self.name, error_code="http_503")


def _outbox(provider: RecordingProvider, **overrides: object) -> EmergencyAlertOutbox:
    return EmergencyAlertOutbox(
        settings=fake_settings(**overrides),
        session_factory=AsyncSessionLocal,
        provider=provider,
    )


def _secret_override(x_vapi_secret: str | None = Header(default=None)) -> None:
    if x_vapi_secret != _SECRET:
        from app.domain.exceptions import InvalidTokenError

        raise InvalidTokenError("Missing or invalid Vapi webhook secret.")


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def database_ready():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Database not reachable, skipping integration test: {exc}")
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _organization(*, configured: bool = True) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(OrganizationModel(id=org_id, name=f"Outbox {org_id.hex[:6]}", slug=f"ob-{org_id.hex[:10]}"))
        await session.flush()
        if configured:
            session.add(
                OrganizationNotificationSettingsModel(
                    organization_id=org_id,
                    channel=NotificationChannel.WEBHOOK,
                    destination=_DESTINATION,
                    is_enabled=True,
                )
            )
        await session.commit()
    return org_id


async def _emergency_conversation(org_id: uuid.UUID) -> uuid.UUID:
    """A conversation whose outcome recommends an emergency ticket — what the
    AI Brain leaves behind for `DispatchService` to act on."""
    async with AsyncSessionLocal() as session:
        conversation = await SqlAlchemyConversationRepository(session).create(
            organization_id=org_id, channel=ConversationChannel.VOICE, caller_phone_number=None
        )
        await SqlAlchemyConversationOutcomeRepository(session).upsert(
            conversation.id,
            classification=CallClassification.EMERGENCY,
            confidence=0.9,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
            matched_service_id=None,
            customer_name="Dana",
            customer_phone="6305550184",
            customer_address="12 Elm Street",
            summary="Smoke from the furnace.",
        )
        await session.commit()
        return conversation.id


def _dispatch(session, outbox: EmergencyAlertOutbox, provider: RecordingProvider) -> DispatchService:
    """`DispatchService` wired exactly as `deps.py` wires it, on one session."""
    notifications = EmergencyNotificationService(
        provider=provider,
        settings_repository=SqlAlchemyNotificationSettingsRepository(session),
        delivery_repository=SqlAlchemyNotificationDeliveryRepository(session),
        ticket_repository=SqlAlchemyEmergencyTicketRepository(session),
        settings=fake_settings(),
        after_commit=SessionAfterCommit(session),
        deliver_after_commit=outbox.deliver,
    )
    return DispatchService(
        emergency_ticket_repository=SqlAlchemyEmergencyTicketRepository(session),
        technician_profile_repository=SqlAlchemyTechnicianProfileRepository(session),
        conversation_outcome_repository=SqlAlchemyConversationOutcomeRepository(session),
        conversation_repository=SqlAlchemyConversationRepository(session),
        user_repository=SqlAlchemyUserRepository(session),
        role_repository=SqlAlchemyRoleRepository(session),
        emergency_notifications=notifications,
    )


async def _rows(org_id: uuid.UUID) -> tuple[list, list]:
    async with AsyncSessionLocal() as session:
        tickets = (
            await session.execute(
                select(EmergencyTicketModel).where(EmergencyTicketModel.organization_id == org_id)
            )
        ).scalars().all()
        deliveries = (
            await session.execute(
                select(EmergencyNotificationDeliveryModel).where(
                    EmergencyNotificationDeliveryModel.organization_id == org_id
                )
            )
        ).scalars().all()
        return list(tickets), list(deliveries)


async def _make_due_now(delivery_id: uuid.UUID) -> None:
    """Stands in for the passage of time until a retry is due."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(EmergencyNotificationDeliveryModel)
            .where(EmergencyNotificationDeliveryModel.id == delivery_id)
            .values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await session.commit()


# --- A / B: the ticket and its alert share one transaction ---------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_ticket_and_its_alert_commit_together_and_the_send_follows_the_commit(
    database_ready,
):
    """A. Through the real request dependency: `get_db` commits, and only
    then runs the post-commit send — which finds the ticket committed."""
    org_id = await _organization()
    conversation_id = await _emergency_conversation(org_id)
    provider = RecordingProvider()
    outbox = _outbox(provider)

    request_session = get_db()
    session = await request_session.__anext__()
    ticket = await _dispatch(session, outbox, provider).sync_ticket_from_outcome(
        org_id, conversation_id
    )
    assert ticket is not None
    # Inside the transaction: queued, nothing sent.
    assert provider.sends == []
    with pytest.raises(StopAsyncIteration):
        await request_session.__anext__()  # clean exit -> commit -> hooks

    tickets, deliveries = await _rows(org_id)
    assert len(tickets) == 1 and len(deliveries) == 1
    assert deliveries[0].emergency_ticket_id == tickets[0].id
    assert len(provider.sends) == 1
    assert provider.ticket_committed_at_send == [True]
    assert deliveries[0].status is DeliveryStatus.DELIVERED


@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_request_leaves_neither_the_ticket_nor_its_alert(database_ready):
    """B. The request raises after the ticket was created: both rows roll
    back, and the post-commit send never runs — nobody is paged about a
    ticket that does not exist."""
    org_id = await _organization()
    conversation_id = await _emergency_conversation(org_id)
    provider = RecordingProvider()
    outbox = _outbox(provider)

    request_session = get_db()
    session = await request_session.__anext__()
    await _dispatch(session, outbox, provider).sync_ticket_from_outcome(org_id, conversation_id)
    with pytest.raises(RuntimeError):
        await request_session.athrow(RuntimeError("the turn failed after the ticket"))

    tickets, deliveries = await _rows(org_id)
    assert tickets == [] and deliveries == []
    assert provider.sends == []
    # And nothing is left for the poller to find either.
    assert await outbox.deliver() == 0
    assert provider.sends == []


# --- C / D: failure is contained and retried -----------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_alert_leaves_the_ticket_and_is_retried_until_delivered(database_ready):
    """C and D. The endpoint is down: the ticket is untouched and the row is
    rescheduled. Not retried before it is due; delivered once it is."""
    org_id = await _organization()
    conversation_id = await _emergency_conversation(org_id)
    provider = RecordingProvider(status=DeliveryStatus.FAILED)
    outbox = _outbox(provider)

    request_session = get_db()
    session = await request_session.__anext__()
    await _dispatch(session, outbox, provider).sync_ticket_from_outcome(org_id, conversation_id)
    with pytest.raises(StopAsyncIteration):
        await request_session.__anext__()

    tickets, deliveries = await _rows(org_id)
    assert len(tickets) == 1, "a notification failure must never lose the ticket"
    failed = deliveries[0]
    assert failed.status is DeliveryStatus.FAILED
    assert failed.attempts == 1
    assert failed.next_attempt_at is not None

    provider.status = DeliveryStatus.DELIVERED
    assert await outbox.deliver() == 0, "retried before its backoff elapsed"
    await _make_due_now(failed.id)
    assert await outbox.deliver() == 1

    _, deliveries = await _rows(org_id)
    assert deliveries[0].status is DeliveryStatus.DELIVERED
    assert deliveries[0].attempts == 2
    assert deliveries[0].next_attempt_at is None
    assert len(provider.sends) == 2


# --- E / G: at most one send per alert -----------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_workers_send_one_alert_exactly_once(database_ready):
    """E. The post-commit send and three poller ticks racing, in separate
    transactions: the row lock gives the alert to exactly one of them."""
    org_id = await _organization()
    conversation_id = await _emergency_conversation(org_id)
    provider = RecordingProvider(delay=0.2)
    outbox = _outbox(provider)
    async with AsyncSessionLocal() as session:
        await _dispatch(session, outbox, provider).sync_ticket_from_outcome(org_id, conversation_id)
        await session.commit()

    results = await asyncio.gather(*(outbox.deliver() for _ in range(4)))

    assert sum(results) == 1
    assert len(provider.sends) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_a_delivered_alert_can_never_be_sent_again(database_ready):
    """G. Even a row wrongly made due again (a replay, a hand edit) is never
    selected once it is delivered."""
    org_id = await _organization()
    conversation_id = await _emergency_conversation(org_id)
    provider = RecordingProvider()
    outbox = _outbox(provider)
    async with AsyncSessionLocal() as session:
        await _dispatch(session, outbox, provider).sync_ticket_from_outcome(org_id, conversation_id)
        await session.commit()
    assert await outbox.deliver() == 1

    _, deliveries = await _rows(org_id)
    await _make_due_now(deliveries[0].id)
    assert await outbox.deliver() == 0
    # Re-syncing the same conversation does not queue a second alert either.
    async with AsyncSessionLocal() as session:
        await _dispatch(session, outbox, provider).sync_ticket_from_outcome(org_id, conversation_id)
        await session.commit()
    assert await outbox.deliver() == 0
    assert len(provider.sends) == 1


# --- F: tenancy ----------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_an_outbox_row_can_never_carry_another_tenants_ticket(database_ready):
    """F. A row claiming organization A but pointing at organization B's
    ticket. The foreign key cannot catch it; the send-time read-back, scoped
    by the row's own organization, must. Nothing is sent to A's destination,
    and the row is closed rather than retried forever."""
    org_a = await _organization()
    org_b = await _organization()
    conversation_b = await _emergency_conversation(org_b)
    provider = RecordingProvider()
    outbox = _outbox(provider)
    async with AsyncSessionLocal() as session:
        b_ticket = await _dispatch(session, outbox, provider).sync_ticket_from_outcome(
            org_b, conversation_b
        )
        await session.commit()
    assert b_ticket is not None
    # B's own alert goes to B's destination.
    assert await outbox.deliver() == 1

    # A forged row: A's organization, B's ticket. The unique index allows one
    # row per ticket, so replace B's with the forgery.
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(EmergencyNotificationDeliveryModel)
            .where(EmergencyNotificationDeliveryModel.emergency_ticket_id == b_ticket.id)
            .values(
                organization_id=org_a,
                status=DeliveryStatus.PENDING,
                next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await session.commit()

    assert await outbox.deliver() == 1
    assert len(provider.sends) == 1, "B's emergency was sent under A's organization"
    async with AsyncSessionLocal() as session:
        forged = (
            await session.execute(
                select(EmergencyNotificationDeliveryModel).where(
                    EmergencyNotificationDeliveryModel.emergency_ticket_id == b_ticket.id
                )
            )
        ).scalar_one()
    assert forged.status is DeliveryStatus.FAILED
    assert forged.error_code == "ticket_unavailable"
    assert forged.next_attempt_at is None


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unconfigured_tenant_is_recorded_and_never_sent(database_ready):
    org_id = await _organization(configured=False)
    conversation_id = await _emergency_conversation(org_id)
    provider = RecordingProvider()
    outbox = _outbox(provider)
    async with AsyncSessionLocal() as session:
        await _dispatch(session, outbox, provider).sync_ticket_from_outcome(org_id, conversation_id)
        await session.commit()

    assert await outbox.deliver() == 0
    tickets, deliveries = await _rows(org_id)
    assert len(tickets) == 1
    assert deliveries[0].status is DeliveryStatus.NOT_CONFIGURED
    assert deliveries[0].next_attempt_at is None
    assert provider.sends == []


# --- Crash recovery --------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_an_alert_stranded_by_a_crash_is_sent_by_the_poller(database_ready):
    """The process died after committing the ticket but before the
    post-commit send ran. The row is still due; the next poll sends it."""
    org_id = await _organization()
    conversation_id = await _emergency_conversation(org_id)
    provider = RecordingProvider()
    outbox = _outbox(provider)
    async with AsyncSessionLocal() as session:
        # Committed WITHOUT running the hooks: exactly the crash window.
        await _dispatch(session, outbox, provider).sync_ticket_from_outcome(org_id, conversation_id)
        await session.commit()
    assert provider.sends == []

    assert await outbox.deliver() == 1
    assert provider.ticket_committed_at_send == [True]


# --- End to end through the live voice route -----------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def voice_client(database_ready):
    scripted = ScriptedToolAIProvider()
    recording = RecordingProvider()
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: scripted
    fastapi_app.dependency_overrides[verify_vapi_secret] = _secret_override
    fastapi_app.dependency_overrides[get_alert_outbox] = lambda: _outbox(recording)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, scripted, recording
    for dependency in (get_ai_provider, verify_vapi_secret, get_alert_outbox):
        fastapi_app.dependency_overrides.pop(dependency, None)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("stream", [False, True])
async def test_a_live_emergency_turn_queues_then_sends_after_commit(voice_client, stream: bool):
    """The whole path: the tool opens the ticket and queues its alert; the
    caller is told the team is BEING alerted (not that it has been); the
    alert goes out after the request commits; the next turn may say so."""
    client, scripted, recording = voice_client
    sends_before = len(recording.sends)
    org_id = await _organization()
    assistant_id = f"asst_outbox_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(
            VoiceLineModel(
                organization_id=org_id,
                provider=VoiceProvider.VAPI,
                vapi_assistant_id=assistant_id,
                vapi_phone_number_id=None,
                phone_number=None,
                is_active=True,
            )
        )
        await session.commit()
    call_id = f"call_outbox_{uuid.uuid4().hex[:8]}"

    scripted.queue_tool_round([("create_service_request", _EMERGENCY)])
    scripted.queue_reply(
        default_reply(
            message_to_customer="Your emergency is logged and the team is being alerted now.",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        )
    )
    response = await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json={
            "call": {"id": call_id, "assistantId": assistant_id},
            "messages": [{"role": "user", "content": "Smoke from my furnace!"}],
            "stream": stream,
        },
        headers={"x-vapi-secret": _SECRET},
    )
    assert response.status_code == 200, response.text

    tool_result = scripted.results[-1].content
    assert tool_result["success"] is True
    assert tool_result["dispatcher_alerted"] is False
    assert tool_result["notification_status"] == "pending"

    assert len(recording.sends) == sends_before + 1
    assert recording.ticket_committed_at_send[-1] is True
    _, deliveries = await _rows(org_id)
    assert deliveries[0].status is DeliveryStatus.DELIVERED

    # The next turn reads the delivered state back.
    async with AsyncSessionLocal() as session:
        voice_call = (
            await session.execute(select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == call_id))
        ).scalar_one()
    scripted.queue_reply(default_reply(message_to_customer="Help is being arranged."))
    await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json={
            "call": {"id": call_id, "assistantId": assistant_id},
            "messages": [{"role": "user", "content": "Is someone coming?"}],
        },
        headers={"x-vapi-secret": _SECRET},
    )
    prompt = scripted.requests[-1].system_prompt
    assert "A dispatcher has been alerted and will contact them" in prompt
    assert voice_call.conversation_id is not None
    assert len(recording.sends) == sends_before + 1, "the second turn re-sent the alert"


@pytest.mark.asyncio(loop_scope="session")
async def test_the_poller_loop_survives_a_failing_tick(database_ready, monkeypatch):
    """`run_forever` must not die on one bad tick — a dead poller silently
    stops every retry in the deployment."""
    provider = RecordingProvider()
    outbox = _outbox(provider, NOTIFICATION_OUTBOX_POLL_SECONDS=0.01)
    calls = 0

    async def flaky_deliver(ticket_id=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(outbox, "deliver", flaky_deliver)
    task = asyncio.create_task(outbox.run_forever())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls > 2
