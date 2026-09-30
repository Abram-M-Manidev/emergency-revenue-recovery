"""Emergency paging and escalation, against real PostgreSQL.

What is under test: a named on-call person is paged only about a ticket that
has COMMITTED; a page that nobody acknowledges escalates to the backup and
then ends explicitly `unresolved`; acknowledgement is explicit, idempotent,
tenant-bound and authorised; every notification is sent at least once and
never twice by concurrent workers; a crash at any point is recovered; and a
paging failure can never cost the emergency ticket.

Why Postgres rather than fakes: the guarantees rest on the ticket and its
page sharing one transaction, on savepoints, on `FOR UPDATE SKIP LOCKED`, on
the page lock that orders an acknowledgement against an escalation, and on
the lease/fence columns. An in-memory fake models each of those by
construction and so cannot fail when they are wrong.

Nothing here reaches a real provider: pages go to `FakePagingProvider`, and
every number is in the 555-01xx block reserved for fictional use.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update

from app.api.deps import get_ai_provider, get_paging_worker, verify_vapi_secret
from app.application.services.dispatch_service import DispatchService
from app.application.services.emergency_paging_service import EmergencyPagingService
from app.domain.entities.conversation import ConversationChannel
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_line import VoiceProvider
from app.domain.exceptions import InvalidAcknowledgementLinkError
from app.domain.paging.page import (
    AcknowledgementMethod,
    PageMessage,
    PageNotificationStatus,
    PageStatus,
    PagingOutcome,
    PagingReceipt,
    RecipientRole,
)
from app.domain.paging.port import PagingProvider
from app.domain.paging.settings import PagingChannel
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.emergency_ticket import EmergencyTicketModel
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.paging import (
    EmergencyPageModel,
    EmergencyPageNotificationModel,
    OrganizationPagingSettingsModel,
)
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.repositories import (
    SqlAlchemyConversationOutcomeRepository,
    SqlAlchemyConversationRepository,
    SqlAlchemyEmergencyTicketRepository,
    SqlAlchemyOrganizationRepository,
    SqlAlchemyRoleRepository,
    SqlAlchemyTechnicianProfileRepository,
    SqlAlchemyUserRepository,
)
from app.infrastructure.database.repositories.paging_repository_impl import (
    SqlAlchemyEmergencyPageRepository,
    SqlAlchemyPagingSettingsRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine, get_db
from app.infrastructure.database.transactions import SessionAfterCommit, SqlAlchemySavepoints
from app.infrastructure.paging.ack_links import HmacAckLinkSigner
from app.infrastructure.paging.worker import EmergencyPagingWorker
from app.main import app, fastapi_app
from tests.fakes import ScriptedToolAIProvider, default_reply, fake_settings
from tests.log_capture import capture_events

# 555-01xx is reserved for fictional use: no real subscriber can be paged.
_PRIMARY = "+16305550101"
_BACKUP = "+16305550102"
_AI_LINE = "+16305550199"
_CALLER_PHONE = "6305550184"
_CALLER_NAME = "Dana Testcaller"
_CALLER_ADDRESS = "12 Elm Street, Lisle"
_ACK_BASE = "https://essr.example.invalid"
_SECRET = "test-vapi-secret-paging"


class Clock:
    """A controllable clock shared by the request-side service and the
    worker, so time can pass without sleeping."""

    def __init__(self) -> None:
        self.current = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FakePagingProvider(PagingProvider):
    """Records every page and, at send time, whether the ticket it is about
    is visible from a fresh connection — i.e. has committed."""

    def __init__(
        self,
        *,
        decide: Callable[[PageMessage], PagingOutcome] | None = None,
        delay: float = 0.0,
        channels: tuple[PagingChannel, ...] = (PagingChannel.SMS, PagingChannel.VOICE),
    ) -> None:
        self.decide = decide or (lambda _message: PagingOutcome.ACCEPTED)
        self.delay = delay
        self.channels = channels
        self.sends: list[PageMessage] = []
        self.ticket_committed_at_send: list[bool] = []

    @property
    def name(self) -> str:
        return "fake"

    def supports(self, channel: PagingChannel) -> bool:
        return channel in self.channels

    async def send(self, message: PageMessage) -> PagingReceipt:
        self.sends.append(message)
        async with AsyncSessionLocal() as session:
            visible = await session.get(EmergencyTicketModel, message.ticket_id)
        self.ticket_committed_at_send.append(visible is not None)
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self.decide(message)
        if outcome is PagingOutcome.ACCEPTED:
            return PagingReceipt(outcome, self.name, provider_message_id=f"SM{len(self.sends):04d}")
        return PagingReceipt(outcome, self.name, error_code=f"fake_{outcome.value}")

    def to(self, number: str) -> list[PageMessage]:
        return [m for m in self.sends if m.to == number]


def _settings(**overrides: object):
    return fake_settings(PAGING_ACK_BASE_URL=_ACK_BASE, **overrides)


def _worker(
    provider: FakePagingProvider, clock: Clock | None = None, **overrides: object
) -> EmergencyPagingWorker:
    return EmergencyPagingWorker(
        settings=_settings(**overrides),
        session_factory=AsyncSessionLocal,
        provider=provider,
        clock=clock,
    )


def _signer() -> HmacAckLinkSigner:
    return HmacAckLinkSigner(fake_settings().JWT_SECRET_KEY)


def _paging_service(session, *, clock: Clock | None = None, worker=None) -> EmergencyPagingService:
    return EmergencyPagingService(
        settings_repository=SqlAlchemyPagingSettingsRepository(session),
        page_repository=SqlAlchemyEmergencyPageRepository(session),
        ticket_repository=SqlAlchemyEmergencyTicketRepository(session),
        organization_repository=SqlAlchemyOrganizationRepository(session),
        settings=_settings(),
        signer=_signer(),
        after_commit=SessionAfterCommit(session) if worker is not None else None,
        deliver_after_commit=worker.deliver if worker is not None else None,
        clock=clock,
    )


def _dispatch(session, *, clock: Clock | None = None, worker=None, paging=None) -> DispatchService:
    """`DispatchService` wired as `deps.py` wires it, on one session."""
    return DispatchService(
        emergency_ticket_repository=SqlAlchemyEmergencyTicketRepository(session),
        technician_profile_repository=SqlAlchemyTechnicianProfileRepository(session),
        conversation_outcome_repository=SqlAlchemyConversationOutcomeRepository(session),
        conversation_repository=SqlAlchemyConversationRepository(session),
        user_repository=SqlAlchemyUserRepository(session),
        role_repository=SqlAlchemyRoleRepository(session),
        emergency_paging=paging or _paging_service(session, clock=clock, worker=worker),
        savepoints=SqlAlchemySavepoints(session),
    )


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


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _quiesce(database_ready):
    """Each test drives its own clock forward. Without this, one test's
    "five minutes later" would make every EARLIER test's pages overdue too,
    and its worker would escalate them. Pages from earlier tests are removed
    before each test (their tenants and tickets stay)."""
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM emergency_pages"))
        await session.commit()
    yield


async def _tenant(
    *,
    primary: str | None = _PRIMARY,
    backup: str | None = _BACKUP,
    sms: bool = True,
    voice: bool = False,
    enabled: bool = True,
    timeout: int = 300,
    configured: bool = True,
) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            OrganizationModel(id=org_id, name=f"Paging {org_id.hex[:6]}", slug=f"pg-{org_id.hex[:10]}")
        )
        await session.flush()
        if configured:
            session.add(
                OrganizationPagingSettingsModel(
                    organization_id=org_id,
                    is_enabled=enabled,
                    primary_number=primary,
                    backup_number=backup,
                    sms_enabled=sms,
                    voice_enabled=voice,
                    ack_timeout_seconds=timeout,
                )
            )
        await session.commit()
    return org_id


async def _emergency_conversation(org_id: uuid.UUID) -> uuid.UUID:
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
            customer_name=_CALLER_NAME,
            customer_phone=_CALLER_PHONE,
            customer_address=_CALLER_ADDRESS,
            summary="Smoke from the furnace.",
        )
        await session.commit()
        return conversation.id


async def _ticket_through_request(org_id: uuid.UUID, worker, clock: Clock | None = None):
    """Creates the ticket through the real request dependency: `get_db`
    commits, and only then runs the post-commit first send."""
    conversation_id = await _emergency_conversation(org_id)
    request_session = get_db()
    session = await request_session.__anext__()
    ticket = await _dispatch(session, clock=clock, worker=worker).sync_ticket_from_outcome(
        org_id, conversation_id
    )
    assert ticket is not None
    with pytest.raises(StopAsyncIteration):
        await request_session.__anext__()
    return ticket


async def _ticket_committed_without_hooks(org_id: uuid.UUID, clock: Clock | None = None):
    """Commits the ticket and its page WITHOUT running the post-commit send —
    exactly the window a crash between commit and send leaves."""
    conversation_id = await _emergency_conversation(org_id)
    async with AsyncSessionLocal() as session:
        ticket = await _dispatch(session, clock=clock).sync_ticket_from_outcome(
            org_id, conversation_id
        )
        await session.commit()
    assert ticket is not None
    return ticket


async def _page(org_id: uuid.UUID) -> EmergencyPageModel | None:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(EmergencyPageModel).where(EmergencyPageModel.organization_id == org_id)
            )
        ).scalar_one_or_none()


async def _notifications(org_id: uuid.UUID) -> list[EmergencyPageNotificationModel]:
    async with AsyncSessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(EmergencyPageNotificationModel)
                    .where(EmergencyPageNotificationModel.organization_id == org_id)
                    .order_by(
                        EmergencyPageNotificationModel.created_at,
                        EmergencyPageNotificationModel.channel,
                    )
                )
            ).scalars().all()
        )


async def _tickets(org_id: uuid.UUID) -> list[EmergencyTicketModel]:
    async with AsyncSessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(EmergencyTicketModel).where(EmergencyTicketModel.organization_id == org_id)
                )
            ).scalars().all()
        )


def _token_from(message: PageMessage) -> str:
    match = re.search(r"/ack#(\S+)", message.body)
    assert match, "the SMS carries no acknowledgement link"
    return match.group(1)


async def _ack_link(token: str, clock: Clock | None = None):
    async with AsyncSessionLocal() as session:
        result = await _paging_service(session, clock=clock).acknowledge_with_link(token)
        await session.commit()
    return result


# =============================================================================
# 1-6: paging work is transactional, and sent only after commit
# =============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_an_emergency_creates_a_page_and_it_is_sent_only_after_commit(database_ready):
    """1, 3, 4. The ticket's transaction creates the page and the primary's
    SMS; nothing is sent inside it; the post-commit hook sends it, and the
    provider finds the ticket already committed."""
    org_id = await _tenant()
    provider = FakePagingProvider()
    worker = _worker(provider)

    conversation_id = await _emergency_conversation(org_id)
    request_session = get_db()
    session = await request_session.__anext__()
    ticket = await _dispatch(session, worker=worker).sync_ticket_from_outcome(org_id, conversation_id)
    assert ticket is not None
    assert provider.sends == [], "a page left the building before the ticket committed"
    with pytest.raises(StopAsyncIteration):
        await request_session.__anext__()

    page = await _page(org_id)
    assert page is not None and page.emergency_ticket_id == ticket.id
    assert page.status is PageStatus.PAGING_PRIMARY
    assert page.escalate_at is not None
    [sms] = await _notifications(org_id)
    assert (sms.role, sms.channel, sms.status) == (
        RecipientRole.PRIMARY,
        PagingChannel.SMS,
        PageNotificationStatus.SENT,
    )
    assert sms.attempts == 1 and sms.provider_message_id == "SM0001"
    assert provider.ticket_committed_at_send == [True]
    [message] = provider.sends
    assert message.to == _PRIMARY and message.channel is PagingChannel.SMS
    assert _CALLER_PHONE in message.body and "/ack#" in message.body
    # Sent is not acknowledged.
    assert (await _page(org_id)).status is PageStatus.PAGING_PRIMARY


@pytest.mark.asyncio(loop_scope="session")
async def test_a_rolled_back_request_pages_nobody(database_ready):
    """2. The request fails after the ticket: ticket, page and notifications
    all roll back, the post-commit send never runs, and nothing is left for
    the poller."""
    org_id = await _tenant()
    provider = FakePagingProvider()
    worker = _worker(provider)

    conversation_id = await _emergency_conversation(org_id)
    request_session = get_db()
    session = await request_session.__anext__()
    await _dispatch(session, worker=worker).sync_ticket_from_outcome(org_id, conversation_id)
    with pytest.raises(RuntimeError):
        await request_session.athrow(RuntimeError("the turn failed after the ticket"))

    assert await _tickets(org_id) == []
    assert await _page(org_id) is None
    assert await _notifications(org_id) == []
    await worker.run_once()
    assert provider.sends == []


@pytest.mark.asyncio(loop_scope="session")
async def test_primary_voice_only(database_ready):
    """5. A voice-only tenant places one automated call. Its spoken body says
    answering is not acknowledging, and carries no callback number."""
    org_id = await _tenant(sms=False, voice=True)
    provider = FakePagingProvider()
    await _ticket_through_request(org_id, _worker(provider))

    [call] = provider.sends
    assert call.channel is PagingChannel.VOICE and call.to == _PRIMARY
    assert "does not acknowledge" in call.body
    assert _CALLER_PHONE not in call.body
    [row] = await _notifications(org_id)
    assert row.status is PageNotificationStatus.SENT
    assert (await _page(org_id)).status is PageStatus.PAGING_PRIMARY


@pytest.mark.asyncio(loop_scope="session")
async def test_primary_on_both_channels(database_ready):
    """6. Both channels: one SMS and one call, each its own outbox row."""
    org_id = await _tenant(sms=True, voice=True)
    provider = FakePagingProvider()
    await _ticket_through_request(org_id, _worker(provider))

    assert sorted(m.channel.value for m in provider.sends) == ["sms", "voice"]
    rows = await _notifications(org_id)
    assert {(r.channel, r.status) for r in rows} == {
        (PagingChannel.SMS, PageNotificationStatus.SENT),
        (PagingChannel.VOICE, PageNotificationStatus.SENT),
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_partial_channel_failure_keeps_the_working_channel(database_ready):
    """Partial failure: voice is rejected, SMS goes through. The page is still
    `sent` for the caller, and the recipient is NOT treated as unreachable —
    so escalation is not brought forward."""
    org_id = await _tenant(sms=True, voice=True)
    clock = Clock()
    provider = FakePagingProvider(
        decide=lambda m: PagingOutcome.REJECTED
        if m.channel is PagingChannel.VOICE
        else PagingOutcome.ACCEPTED
    )
    await _ticket_through_request(org_id, _worker(provider, clock), clock)

    rows = {r.channel: r for r in await _notifications(org_id)}
    assert rows[PagingChannel.SMS].status is PageNotificationStatus.SENT
    assert rows[PagingChannel.VOICE].status is PageNotificationStatus.FAILED
    assert rows[PagingChannel.VOICE].error_code == "fake_rejected"
    page = await _page(org_id)
    assert page.escalate_at >= clock() + timedelta(seconds=299)
    async with AsyncSessionLocal() as session:
        state = await _paging_service(session).caller_state(org_id, page.emergency_ticket_id)
    assert state.value == "sent"


# =============================================================================
# 7-10: failure, retry, idempotency, restart
# =============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_page_is_retried_with_backoff_and_the_ticket_survives(database_ready):
    """7, 8, 26, 27. The provider is down: the ticket is untouched, the
    notification is `retrying`, it is not retried before it is due, and is
    sent once the provider recovers."""
    org_id = await _tenant()
    clock = Clock()
    outcome = {"value": PagingOutcome.FAILED}
    provider = FakePagingProvider(decide=lambda _m: outcome["value"])
    worker = _worker(provider, clock)
    await _ticket_through_request(org_id, worker, clock)

    assert len(await _tickets(org_id)) == 1, "a paging failure must never lose the ticket"
    [row] = await _notifications(org_id)
    assert row.status is PageNotificationStatus.RETRYING
    assert row.attempts == 1 and row.error_code == "fake_failed"
    assert row.next_attempt_at == clock() + timedelta(seconds=20)

    outcome["value"] = PagingOutcome.ACCEPTED
    assert await worker.deliver() == 0, "retried before its backoff elapsed"
    clock.advance(21)
    assert await worker.deliver() == 1
    [row] = await _notifications(org_id)
    assert row.status is PageNotificationStatus.SENT and row.attempts == 2
    assert row.next_attempt_at is None
    # 9. Every attempt of one notification carries the same idempotency key.
    assert len({m.idempotency_key for m in provider.sends}) == 1
    assert len(provider.sends) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_provider_timeout_and_exception_are_recorded_not_raised(database_ready):
    """A provider that hangs or raises becomes a recorded, retried failure."""
    org_id = await _tenant(sms=True, voice=True)
    clock = Clock()

    class Misbehaving(FakePagingProvider):
        async def send(self, message: PageMessage) -> PagingReceipt:
            self.sends.append(message)
            if message.channel is PagingChannel.SMS:
                await asyncio.sleep(5)
            raise RuntimeError(f"boom {message.to}")

    provider = Misbehaving()
    worker = _worker(provider, clock, PAGING_SEND_TIMEOUT_SECONDS=0.2)
    with capture_events() as events:
        await _ticket_through_request(org_id, worker, clock)

    rows = {r.channel: r for r in await _notifications(org_id)}
    assert rows[PagingChannel.SMS].error_code == "timeout"
    assert rows[PagingChannel.VOICE].error_code == "provider_error"
    assert all(r.status is PageNotificationStatus.RETRYING for r in rows.values())
    assert _PRIMARY not in json.dumps(events, default=str)


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_workers_send_each_notification_exactly_once(database_ready):
    """9. The post-commit send and several poller ticks racing: the row
    claim gives each notification to exactly one of them."""
    org_id = await _tenant(sms=True, voice=True)
    provider = FakePagingProvider(delay=0.2)
    worker = _worker(provider)
    await _ticket_committed_without_hooks(org_id)

    results = await asyncio.gather(*(worker.deliver() for _ in range(5)))
    assert sum(results) == 2
    assert len(provider.sends) == 2
    assert {m.channel for m in provider.sends} == {PagingChannel.SMS, PagingChannel.VOICE}


@pytest.mark.asyncio(loop_scope="session")
async def test_a_page_stranded_by_a_crash_is_sent_by_a_restarted_worker(database_ready):
    """10, 29. The process died after committing the ticket and its page but
    before the post-commit send ran. A brand-new worker (a restart) finds
    the queued notification and sends it."""
    org_id = await _tenant()
    await _ticket_committed_without_hooks(org_id)
    provider = FakePagingProvider()

    restarted = _worker(provider)
    await restarted.run_once()
    assert len(provider.sends) == 1
    assert provider.ticket_committed_at_send == [True]
    [row] = await _notifications(org_id)
    assert row.status is PageNotificationStatus.SENT


@pytest.mark.asyncio(loop_scope="session")
async def test_a_send_accepted_before_a_crash_is_resent_once_its_lease_expires(database_ready):
    """28. The provider ACCEPTED, then the worker died before recording it.
    The row stays `sending`; nobody touches it while the lease runs; after
    the lease, a restarted worker sends again — at-least-once, with the SAME
    idempotency key, counted as a second attempt."""
    org_id = await _tenant()
    clock = Clock()
    await _ticket_committed_without_hooks(org_id, clock)
    provider = FakePagingProvider()

    async with AsyncSessionLocal() as session:
        service = _paging_service(session, clock=clock)
        service._provider = provider  # the crashing worker's provider
        claimed = await service.claim_next_send()
        await session.commit()
    assert claimed is not None and claimed.message is not None
    receipt = await service.send(claimed)
    assert receipt.outcome is PagingOutcome.ACCEPTED
    # -- crash: `record_send_result` never runs --

    [row] = await _notifications(org_id)
    assert row.status is PageNotificationStatus.SENDING and row.attempts == 1
    restarted = _worker(provider, clock)
    assert await restarted.deliver() == 0, "re-sent while the first worker's lease was live"

    clock.advance(10 + 31)
    assert await restarted.deliver() == 1
    [row] = await _notifications(org_id)
    assert row.status is PageNotificationStatus.SENT and row.attempts == 2
    assert len(provider.sends) == 2
    assert provider.sends[0].idempotency_key == provider.sends[1].idempotency_key


@pytest.mark.asyncio(loop_scope="session")
async def test_a_crash_on_the_last_attempt_is_closed_not_resent(database_ready):
    """29. A worker died holding the notification's last allowed attempt.
    After the lease, the row is closed as `worker_lost` rather than sent
    beyond its budget — and the recipient, now unreachable, is escalated."""
    org_id = await _tenant()
    clock = Clock()
    await _ticket_committed_without_hooks(org_id, clock)
    provider = FakePagingProvider()

    async with AsyncSessionLocal() as session:
        service = _paging_service(session, clock=clock)
        service._settings = _settings(PAGING_MAX_ATTEMPTS=1)
        assert await service.claim_next_send() is not None
        await session.commit()

    clock.advance(41)
    await _worker(provider, clock, PAGING_MAX_ATTEMPTS=1).deliver()
    [row] = await _notifications(org_id)
    assert row.status is PageNotificationStatus.FAILED and row.error_code == "worker_lost"
    assert provider.sends == []
    page = await _page(org_id)
    assert page.escalate_at <= clock(), "an unreachable primary must be escalated at once"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_stale_worker_cannot_overwrite_a_newer_attempt(database_ready):
    """The fence: worker A's lease expired and worker B re-claimed and sent.
    A's late result is discarded; B's stands."""
    org_id = await _tenant()
    clock = Clock()
    await _ticket_committed_without_hooks(org_id, clock)
    provider = FakePagingProvider()

    async with AsyncSessionLocal() as session:
        stale = await _paging_service(session, clock=clock).claim_next_send()
        await session.commit()
    clock.advance(41)
    assert await _worker(provider, clock).deliver() == 1

    async with AsyncSessionLocal() as session:
        late = await _paging_service(session, clock=clock).record_send_result(
            stale, PagingReceipt(PagingOutcome.FAILED, "fake", error_code="late")
        )
        await session.commit()
    assert late is None
    [row] = await _notifications(org_id)
    assert row.status is PageNotificationStatus.SENT and row.error_code is None


# =============================================================================
# 11-13, 19-21: acknowledgement
# =============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_acknowledging_by_link_stops_escalation_and_is_idempotent(database_ready):
    """11, 12. The primary's link acknowledges: the page stops escalating and
    unsent notifications are canceled. A second use changes nothing."""
    org_id = await _tenant(sms=True, voice=True)
    clock = Clock()
    outcome = {"value": PagingOutcome.ACCEPTED}
    provider = FakePagingProvider(
        decide=lambda m: outcome["value"] if m.channel is PagingChannel.SMS else PagingOutcome.FAILED
    )
    worker = _worker(provider, clock)
    await _ticket_through_request(org_id, worker, clock)
    sms = next(m for m in provider.sends if m.channel is PagingChannel.SMS)
    token = _token_from(sms)

    first = await _ack_link(token, clock)
    assert first.newly_acknowledged is True
    page = await _page(org_id)
    assert page.status is PageStatus.ACKNOWLEDGED
    assert page.acknowledged_by_role is RecipientRole.PRIMARY
    assert page.acknowledged_via is AcknowledgementMethod.LINK
    assert page.escalate_at is None
    voice = next(r for r in await _notifications(org_id) if r.channel is PagingChannel.VOICE)
    assert voice.status is PageNotificationStatus.CANCELED, "a retry was still pending after ack"

    second = await _ack_link(token, clock)
    assert second.newly_acknowledged is False
    assert (await _page(org_id)).acknowledged_at == page.acknowledged_at

    # And nothing escalates or sends afterwards, however late it gets.
    clock.advance(3600)
    sends = len(provider.sends)
    await worker.run_once()
    assert len(provider.sends) == sends
    assert (await _page(org_id)).status is PageStatus.ACKNOWLEDGED


@pytest.mark.asyncio(loop_scope="session")
async def test_forged_tampered_cross_page_and_expired_links_are_refused(database_ready):
    """13. The token is the whole authority, so it must be unforgeable,
    bound to its page and role, and expire."""
    clock = Clock()
    org_a = await _tenant()
    org_b = await _tenant()
    provider = FakePagingProvider()
    worker = _worker(provider, clock)
    await _ticket_through_request(org_a, worker, clock)
    await _ticket_through_request(org_b, worker, clock)
    token_a = _token_from(provider.to(_PRIMARY)[0])
    page_b = await _page(org_b)

    # Garbage, a flipped character, and a token for a page id that exists
    # but signed with the wrong key: all the same refusal.
    forged = HmacAckLinkSigner("a-completely-different-secret-value").issue(
        page_b.id, RecipientRole.PRIMARY
    )
    flipped = token_a[:-2] + ("A" if token_a[-2] != "A" else "B") + token_a[-1]
    for bad in ("not-a-token", flipped, forged, token_a + "x"):
        with pytest.raises(InvalidAcknowledgementLinkError):
            await _ack_link(bad, clock)
    assert (await _page(org_b)).status is PageStatus.PAGING_PRIMARY
    assert (await _page(org_a)).status is PageStatus.PAGING_PRIMARY

    # A's link acknowledges A — and only A.
    await _ack_link(token_a, clock)
    assert (await _page(org_a)).status is PageStatus.ACKNOWLEDGED
    assert (await _page(org_b)).status is PageStatus.PAGING_PRIMARY

    # Expired: B's own, genuine link, after the TTL.
    token_b = _token_from(provider.sends[-1])
    clock.advance(24 * 3600 + 1)
    with pytest.raises(InvalidAcknowledgementLinkError):
        await _ack_link(token_b, clock)


@pytest.mark.asyncio(loop_scope="session")
async def test_acknowledgement_after_unresolved_still_counts(database_ready):
    """A late "I have it" is recorded, not refused: unresolved -> acknowledged."""
    org_id = await _tenant(backup=None)
    clock = Clock()
    provider = FakePagingProvider()
    worker = _worker(provider, clock)
    await _ticket_through_request(org_id, worker, clock)
    clock.advance(301)
    await worker.run_once()
    assert (await _page(org_id)).status is PageStatus.UNRESOLVED

    await _ack_link(_token_from(provider.sends[0]), clock)
    page = await _page(org_id)
    assert page.status is PageStatus.ACKNOWLEDGED and page.unresolved_reason == "no_backup_configured"


# =============================================================================
# 14-18, 24: escalation
# =============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_no_acknowledgement_escalates_to_the_backup_then_ends_unresolved(database_ready):
    """14, 15, 18. Primary paged; the window closes; the backup is paged on
    every channel with a fresh window; that closes too; the page ends
    explicitly `unresolved` — never quietly, never "acknowledged"."""
    org_id = await _tenant(sms=True, voice=True, timeout=120)
    clock = Clock()
    provider = FakePagingProvider()
    worker = _worker(provider, clock)
    with capture_events() as events:
        await _ticket_through_request(org_id, worker, clock)
        assert len(provider.to(_PRIMARY)) == 2

        clock.advance(119)
        await worker.run_once()
        assert (await _page(org_id)).status is PageStatus.PAGING_PRIMARY, "escalated early"

        clock.advance(2)
        await worker.run_once()
        page = await _page(org_id)
        assert page.status is PageStatus.PAGING_BACKUP
        assert page.escalated_at is not None
        assert page.escalate_at == clock() + timedelta(seconds=120)
        assert {m.channel for m in provider.to(_BACKUP)} == {PagingChannel.SMS, PagingChannel.VOICE}
        backup_sms = next(m for m in provider.to(_BACKUP) if m.channel is PagingChannel.SMS)
        assert backup_sms.idempotency_key.endswith(":backup:sms")

        clock.advance(121)
        await worker.run_once()
    page = await _page(org_id)
    assert page.status is PageStatus.UNRESOLVED
    assert page.unresolved_reason == "no_acknowledgement"
    assert page.acknowledged_at is None and page.escalate_at is None
    assert len(provider.sends) == 4, "anyone was paged twice"
    unresolved = [e for e in events if e.get("event") == "emergency_page_unresolved"]
    assert len(unresolved) == 1 and unresolved[0]["log_level"] == "error"


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_escalations_page_the_backup_once(database_ready):
    """16. Several workers find the same overdue page at once: exactly one
    escalates it, the backup gets one notification per channel, and a
    replayed escalation adds nothing."""
    org_id = await _tenant(sms=True, voice=True)
    clock = Clock()
    await _ticket_committed_without_hooks(org_id, clock)
    provider = FakePagingProvider(delay=0.05)
    worker = _worker(provider, clock)
    await worker.deliver()

    clock.advance(301)
    results = await asyncio.gather(*(worker.escalate() for _ in range(4)))
    assert sum(results) == 1
    backup_rows = [r for r in await _notifications(org_id) if r.role is RecipientRole.BACKUP]
    assert len(backup_rows) == 2
    assert len(provider.to(_BACKUP)) == 2

    # A replay (crash after the escalation committed) cannot add a second set.
    async with AsyncSessionLocal() as session:
        page = await _page(org_id)
        added = await SqlAlchemyEmergencyPageRepository(session).add_notifications(
            org_id, page.id, [], now=clock()
        )
        await session.commit()
    assert added == 0
    assert len([r for r in await _notifications(org_id) if r.role is RecipientRole.BACKUP]) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unreachable_primary_is_escalated_without_waiting(database_ready):
    """Primary unavailable: every channel to the primary is rejected, so the
    backup is paged at once instead of after the whole window."""
    org_id = await _tenant(sms=True, voice=True)
    clock = Clock()
    provider = FakePagingProvider(
        decide=lambda m: PagingOutcome.REJECTED if m.to == _PRIMARY else PagingOutcome.ACCEPTED
    )
    worker = _worker(provider, clock)
    await _ticket_through_request(org_id, worker, clock)
    assert (await _page(org_id)).escalate_at <= clock()

    await worker.run_once()
    assert (await _page(org_id)).status is PageStatus.PAGING_BACKUP
    assert len(provider.to(_BACKUP)) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unreachable_backup_ends_unresolved(database_ready):
    """17. Backup unavailable too: the page ends unresolved with the reason
    recorded, and nobody is told anyone acknowledged."""
    org_id = await _tenant()
    clock = Clock()
    provider = FakePagingProvider(
        decide=lambda m: PagingOutcome.REJECTED if m.to == _BACKUP else PagingOutcome.ACCEPTED
    )
    worker = _worker(provider, clock)
    await _ticket_through_request(org_id, worker, clock)
    clock.advance(301)
    # One tick: escalate, the backup's only page is rejected, the deadline is
    # brought forward, and the same tick's escalation pass closes the page —
    # no second acknowledgement window is waited out for a page that never
    # reached anyone.
    await worker.run_once()
    page = await _page(org_id)
    assert page.status is PageStatus.UNRESOLVED
    assert page.unresolved_reason == "backup_unreachable"


@pytest.mark.asyncio(loop_scope="session")
async def test_no_backup_configured_ends_unresolved_after_the_primary_window(database_ready):
    """24. Missing backup: nobody to escalate to, so the page is unresolved."""
    org_id = await _tenant(backup=None)
    clock = Clock()
    provider = FakePagingProvider()
    worker = _worker(provider, clock)
    await _ticket_through_request(org_id, worker, clock)
    clock.advance(301)
    await worker.run_once()
    page = await _page(org_id)
    assert page.status is PageStatus.UNRESOLVED
    assert page.unresolved_reason == "no_backup_configured"
    assert provider.to(_BACKUP) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_acknowledgement_before_the_escalation_boundary_wins(database_ready):
    """19a. The acknowledgement lands first: the escalation finds nothing to
    do, and the backup is never paged."""
    org_id = await _tenant()
    clock = Clock()
    provider = FakePagingProvider()
    worker = _worker(provider, clock)
    await _ticket_through_request(org_id, worker, clock)

    clock.advance(301)  # overdue, but the ack is processed first
    await _ack_link(_token_from(provider.sends[0]), clock)
    assert await worker.escalate() == 0
    assert provider.to(_BACKUP) == []
    assert (await _page(org_id)).status is PageStatus.ACKNOWLEDGED


@pytest.mark.asyncio(loop_scope="session")
async def test_acknowledgement_just_after_escalation_cancels_the_backup_pages(database_ready):
    """19b. The escalation lands first and queues the backup; the primary's
    acknowledgement arrives a moment later. It still counts — as the
    primary's — and the backup's unsent pages are canceled."""
    org_id = await _tenant(sms=True, voice=True)
    clock = Clock()
    provider = FakePagingProvider()
    worker = _worker(provider, clock)
    await _ticket_through_request(org_id, worker, clock)
    token = _token_from(next(m for m in provider.sends if m.channel is PagingChannel.SMS))

    clock.advance(301)
    async with AsyncSessionLocal() as session:
        await _paging_service(session, clock=clock).escalate_next_due()
        await session.commit()  # escalated, backup queued, not yet sent
    await _ack_link(token, clock)

    page = await _page(org_id)
    assert page.status is PageStatus.ACKNOWLEDGED
    assert page.acknowledged_by_role is RecipientRole.PRIMARY
    backup = [r for r in await _notifications(org_id) if r.role is RecipientRole.BACKUP]
    assert backup and all(r.status is PageNotificationStatus.CANCELED for r in backup)
    await worker.run_once()
    assert provider.to(_BACKUP) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_acknowledgement_racing_escalation_is_always_consistent(database_ready):
    """19c. Truly concurrent, several times over: whichever wins the page
    lock, the end state is acknowledged, and no backup page is sent after
    the acknowledgement."""
    for _ in range(5):
        org_id = await _tenant()
        clock = Clock()
        provider = FakePagingProvider()
        worker = _worker(provider, clock)
        await _ticket_through_request(org_id, worker, clock)
        token = _token_from(provider.sends[0])
        clock.advance(301)

        async def escalate(clock: Clock = clock) -> None:
            async with AsyncSessionLocal() as session:
                await _paging_service(session, clock=clock).escalate_next_due()
                await session.commit()

        await asyncio.gather(escalate(), _ack_link(token, clock))
        await worker.run_once()
        page = await _page(org_id)
        assert page.status is PageStatus.ACKNOWLEDGED
        assert provider.to(_BACKUP) == []


# =============================================================================
# 22-23, 26: configuration, and the ticket's durability
# =============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_disabled_missing_and_channel_less_configurations_page_nobody(database_ready):
    """22, 23. Paging off, no configuration, no primary, or no channel: the
    ticket is created and nobody is paged — never a half-started page."""
    provider = FakePagingProvider()
    worker = _worker(provider)
    for org_id in (
        await _tenant(enabled=False),
        await _tenant(configured=False),
        await _tenant(primary=None, backup=None),
        await _tenant(sms=False, voice=False),
    ):
        await _ticket_through_request(org_id, worker)
        assert len(await _tickets(org_id)) == 1
        assert await _page(org_id) is None
    assert provider.sends == []


@pytest.mark.asyncio(loop_scope="session")
async def test_a_database_failure_while_paging_never_costs_the_ticket(database_ready):
    """26. The page's own insert fails at the database (the transaction is
    genuinely aborted, not merely raised through). The savepoint around
    paging undoes only the page; the ticket commits."""
    org_id = await _tenant()
    conversation_id = await _emergency_conversation(org_id)

    class ExplodingPages(SqlAlchemyEmergencyPageRepository):
        async def create(self, *args, **kwargs):
            await self._session.execute(text("SELECT 1 / 0"))
            raise AssertionError("unreachable")

    with capture_events() as events:
        request_session = get_db()
        session = await request_session.__anext__()
        paging = _paging_service(session)
        paging._pages = ExplodingPages(session)
        ticket = await _dispatch(session, paging=paging).sync_ticket_from_outcome(
            org_id, conversation_id
        )
        with pytest.raises(StopAsyncIteration):
            await request_session.__anext__()

    assert ticket is not None
    [stored] = await _tickets(org_id)
    assert stored.id == ticket.id
    assert await _page(org_id) is None
    failed = [e for e in events if e.get("event") == "emergency_page_enqueue_failed"]
    assert len(failed) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_resyncing_a_ticket_never_starts_a_second_page(database_ready):
    """The tool loop and the webhook's outcome sync both sync one ticket:
    exactly one page, and its notifications are not duplicated."""
    org_id = await _tenant(sms=True, voice=True)
    conversation_id = await _emergency_conversation(org_id)
    for _ in range(3):
        async with AsyncSessionLocal() as session:
            await _dispatch(session).sync_ticket_from_outcome(org_id, conversation_id)
            await session.commit()
    assert len(await _notifications(org_id)) == 2
    async with AsyncSessionLocal() as session:
        pages = (
            await session.execute(
                select(EmergencyPageModel).where(EmergencyPageModel.organization_id == org_id)
            )
        ).scalars().all()
    assert len(pages) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_a_notification_is_never_sent_for_another_tenants_ticket(database_ready):
    """20. A corrupted row claiming tenant A but belonging to B's page is
    never sent: the send-time read-back is scoped by the row's own tenant."""
    org_a = await _tenant()
    org_b = await _tenant()
    await _ticket_committed_without_hooks(org_b)
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(EmergencyPageNotificationModel)
            .where(EmergencyPageNotificationModel.organization_id == org_b)
            .values(organization_id=org_a)
        )
        await session.commit()
    provider = FakePagingProvider()
    await _worker(provider).deliver()
    assert provider.sends == []


# =============================================================================
# 30: no PII or secrets in logs
# =============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_the_whole_lifecycle_logs_no_numbers_names_addresses_or_tokens(database_ready):
    org_id = await _tenant(sms=True, voice=True)
    clock = Clock()
    outcome = {"value": PagingOutcome.FAILED}
    provider = FakePagingProvider(decide=lambda _m: outcome["value"])
    worker = _worker(provider, clock)
    with capture_events() as events:
        await _ticket_through_request(org_id, worker, clock)
        outcome["value"] = PagingOutcome.ACCEPTED
        clock.advance(301)
        await worker.run_once()
        clock.advance(30)
        await worker.run_once()
        token = _token_from(provider.to(_BACKUP)[0])
        with pytest.raises(InvalidAcknowledgementLinkError):
            await _ack_link(token + "tampered", clock)
        await _ack_link(token, clock)

    assert (await _page(org_id)).status is PageStatus.ACKNOWLEDGED
    dumped = json.dumps(events, default=str)
    for secret in (
        _PRIMARY, _BACKUP, "5550101", "5550102", _CALLER_PHONE, _CALLER_NAME,
        _CALLER_ADDRESS, "Elm Street", token, "Smoke from the furnace",
    ):
        assert secret not in dumped, f"{secret!r} reached the logs"
    names = {e.get("event") for e in events}
    assert {
        "emergency_page_created",
        "emergency_page_notification_attempted",
        "emergency_page_escalated",
        "emergency_page_acknowledged",
        "emergency_page_ack_link_rejected",
    } <= names


# =============================================================================
# HTTP: settings, RBAC, tenancy, the public link endpoint
# =============================================================================


@pytest_asyncio.fixture(loop_scope="session")
async def client(database_ready):
    provider = FakePagingProvider()
    worker = _worker(provider)
    fastapi_app.dependency_overrides[get_paging_worker] = lambda: worker
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, provider
    fastapi_app.dependency_overrides.pop(get_paging_worker, None)


async def _register(client: AsyncClient, org_name: str) -> tuple[str, uuid.UUID]:
    email = f"owner-{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": org_name,
            "full_name": "Owner Owner",
            "email": email,
            "password": "super-secret-123",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["tokens"]["access_token"], uuid.UUID(body["user"]["organization_id"])


async def _teammate(client: AsyncClient, owner: str, role: str) -> str:
    email = f"{role.lower()}-{uuid.uuid4().hex[:10]}@example.com"
    if role == "Technician":
        response = await client.post(
            "/api/v1/dispatch/technicians",
            headers=_auth(owner),
            json={
                "full_name": "Tech Nician",
                "email": email,
                "phone_number": "+16305550103",
                "temporary_password": "member-secret-123",
            },
        )
    else:
        response = await client.post(
            "/api/v1/team/members",
            headers=_auth(owner),
            json={
                "full_name": "Mem Ber",
                "email": email,
                "temporary_password": "member-secret-123",
                "role": role,
            },
        )
    assert response.status_code in (200, 201), response.text
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "member-secret-123"}
    )
    assert login.status_code == 200, login.text
    return login.json()["tokens"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_VALID = {
    "is_enabled": True,
    "primary_number": "+1 (630) 555-0101",
    "backup_number": _BACKUP,
    "sms_enabled": True,
    "voice_enabled": True,
    "ack_timeout_seconds": 300,
}


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_configures_paging_and_malformed_settings_are_refused(client):
    """25. Owner-only; normalised on save; every malformed variant is a 422
    that never echoes the submitted number."""
    ac, _ = client
    owner, org_id = await _register(ac, "Paging Settings Co")
    assert (await ac.get("/api/v1/organizations/current/paging", headers=_auth(owner))).json() is None

    saved = await ac.put("/api/v1/organizations/current/paging", headers=_auth(owner), json=_VALID)
    assert saved.status_code == 200, saved.text
    assert saved.json()["primary_number"] == _PRIMARY

    async with AsyncSessionLocal() as session:
        session.add(VoiceLineModel(organization_id=org_id, provider=VoiceProvider.VAPI,
                                   vapi_assistant_id=f"asst_{uuid.uuid4().hex[:8]}",
                                   phone_number=_AI_LINE, is_active=True))
        await session.commit()

    bad_payloads = [
        {**_VALID, "primary_number": "5550101"},  # not E.164
        {**_VALID, "primary_number": "+1630555010199999999"},
        {**_VALID, "backup_number": _PRIMARY},  # same as primary
        {**_VALID, "primary_number": _AI_LINE},  # the AI line itself
        {**_VALID, "primary_number": None},  # enabled with no primary
        {**_VALID, "sms_enabled": False, "voice_enabled": False},
        {**_VALID, "ack_timeout_seconds": 30},
        {**_VALID, "ack_timeout_seconds": 7200},
        {**_VALID, "primary_number": None, "is_enabled": False},  # backup w/o primary
    ]
    for payload in bad_payloads:
        response = await ac.put(
            "/api/v1/organizations/current/paging", headers=_auth(owner), json=payload
        )
        assert response.status_code == 422, (payload, response.text)
        for number in (_PRIMARY, _BACKUP, _AI_LINE, "5550101"):
            assert number not in response.text

    # Off may be saved incomplete, so an Owner can always switch paging off.
    off = await ac.put(
        "/api/v1/organizations/current/paging",
        headers=_auth(owner),
        json={**_VALID, "is_enabled": False, "primary_number": None, "backup_number": None},
    )
    assert off.status_code == 200
    assert (await ac.delete("/api/v1/organizations/current/paging", headers=_auth(owner))).status_code == 204
    assert (await ac.get("/api/v1/organizations/current/paging", headers=_auth(owner))).json() is None


@pytest.mark.asyncio(loop_scope="session")
async def test_paging_settings_and_acknowledgement_are_authorised(client):
    """21, 13, 20. Settings: Owner only. Reading a page: any dispatch reader,
    numbers masked. Acknowledging: Owner/Admin/Technician, not a read-only
    Member. Another tenant's ticket: not found."""
    ac, provider = client
    owner, org_id = await _register(ac, "Paging RBAC Co")
    other_owner, _ = await _register(ac, "Paging RBAC Other Co")
    member = await _teammate(ac, owner, "Member")
    admin = await _teammate(ac, owner, "Admin")
    technician = await _teammate(ac, owner, "Technician")

    for token in (member, admin, technician):
        assert (await ac.put("/api/v1/organizations/current/paging", headers=_auth(token),
                             json=_VALID)).status_code == 403
        assert (await ac.get("/api/v1/organizations/current/paging",
                             headers=_auth(token))).status_code == 403
    assert (await ac.put("/api/v1/organizations/current/paging", headers=_auth(owner),
                         json=_VALID)).status_code == 200

    ticket = await _ticket_through_request(org_id, _worker(provider))
    path = f"/api/v1/dispatch/tickets/{ticket.id}/paging"

    viewed = await ac.get(path, headers=_auth(member))
    assert viewed.status_code == 200
    body = viewed.text
    assert viewed.json()["status"] == "paging_primary"
    assert _PRIMARY not in body and "0101" in body, "the number must be masked, not hidden"

    assert (await ac.post(f"{path}/acknowledge", headers=_auth(member))).status_code == 403
    assert (await ac.post(f"{path}/acknowledge", headers=_auth(other_owner))).status_code == 404
    assert (await ac.get(path, headers=_auth(other_owner))).json() is None
    assert (await _page(org_id)).status is PageStatus.PAGING_PRIMARY

    acked = await ac.post(f"{path}/acknowledge", headers=_auth(technician))
    assert acked.status_code == 200
    assert acked.json() == {
        "status": "acknowledged",
        "acknowledged_at": acked.json()["acknowledged_at"],
        "already_acknowledged": False,
    }
    again = await ac.post(f"{path}/acknowledge", headers=_auth(admin))
    assert again.json()["already_acknowledged"] is True
    page = await _page(org_id)
    assert page.acknowledged_via is AcknowledgementMethod.DASHBOARD
    assert page.acknowledged_by_user_id is not None

    listed = await ac.get("/api/v1/dispatch/pages", headers=_auth(owner))
    assert [p["ticket_id"] for p in listed.json()] == [str(ticket.id)]
    assert (await ac.get("/api/v1/dispatch/pages", headers=_auth(other_owner))).json() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_the_public_link_endpoint(client):
    """The link endpoint needs no session, acknowledges exactly its page, and
    answers every bad token with one 404 that never echoes the token."""
    ac, provider = client
    org_id = await _tenant()
    await _ticket_through_request(org_id, _worker(provider))
    token = _token_from(provider.to(_PRIMARY)[-1])

    for bad in ("zzNotARealToken42", token[:-1] + ("A" if token[-1] != "A" else "B")):
        response = await ac.post("/api/v1/paging/acknowledge", json={"token": bad})
        assert response.status_code == 404
        assert bad not in response.text
    assert (await ac.get(f"/api/v1/paging/acknowledge?token={token}")).status_code == 405

    first = await ac.post("/api/v1/paging/acknowledge", json={"token": token})
    assert first.status_code == 200 and first.json()["already_acknowledged"] is False
    second = await ac.post("/api/v1/paging/acknowledge", json={"token": token})
    assert second.json()["already_acknowledged"] is True
    assert (await _page(org_id)).acknowledged_via is AcknowledgementMethod.LINK


# =============================================================================
# The assistant: what it may say is exactly what the records show
# =============================================================================


def _secret_override(x_vapi_secret: str | None = Header(default=None)) -> None:
    if x_vapi_secret != _SECRET:
        from app.domain.exceptions import InvalidTokenError

        raise InvalidTokenError("Missing or invalid Vapi webhook secret.")


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("stream", [False, True])
async def test_a_live_emergency_turn_reports_paging_truthfully(database_ready, stream: bool):
    """On the ticket's own turn the page is only queued; after commit it is
    sent; the next turn may say "paged" (not "acknowledged"); after the
    recipient acknowledges, the turn after may say so — and no turn is ever
    licensed to say anyone is on the way."""
    scripted = ScriptedToolAIProvider()
    provider = FakePagingProvider()
    worker = _worker(provider)
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: scripted
    fastapi_app.dependency_overrides[verify_vapi_secret] = _secret_override
    fastapi_app.dependency_overrides[get_paging_worker] = lambda: worker
    try:
        org_id = await _tenant()
        assistant = f"asst_page_{uuid.uuid4().hex[:8]}"
        async with AsyncSessionLocal() as session:
            session.add(VoiceLineModel(organization_id=org_id, provider=VoiceProvider.VAPI,
                                       vapi_assistant_id=assistant, phone_number=_AI_LINE,
                                       is_active=True))
            await session.commit()
        call_id = f"call_page_{uuid.uuid4().hex[:8]}"

        async def turn(utterance: str) -> None:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.post(
                    "/api/v1/voice/vapi/chat/completions",
                    json={
                        "call": {"id": call_id, "assistantId": assistant},
                        "messages": [{"role": "user", "content": utterance}],
                        "stream": stream,
                    },
                    headers={"x-vapi-secret": _SECRET},
                )
            assert response.status_code == 200, response.text

        scripted.queue_tool_round([("create_service_request", {
            "customer_name": _CALLER_NAME,
            "customer_phone": _CALLER_PHONE,
            "service_address": _CALLER_ADDRESS,
            "problem_description": "Smoke is coming from the furnace.",
            "classification": "emergency",
            "service_name": None,
        })])
        scripted.queue_reply(default_reply(
            message_to_customer="Your emergency is logged and the on-call technician is being paged.",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        ))
        await turn("Smoke from my furnace!")

        result = scripted.results[-1].content
        assert result["on_call_paging"] == "queued"
        assert "being paged now" in result["next_step"]
        for leak in (_PRIMARY, _BACKUP, "5550101"):
            assert leak not in json.dumps(result)
        assert len(provider.to(_PRIMARY)) == 1, "not sent after the turn committed"

        scripted.queue_reply(default_reply(message_to_customer="Help is being arranged."))
        await turn("Has anyone been told?")
        prompt = scripted.requests[-1].system_prompt
        assert "A page has been sent to the on-call technician" in prompt
        assert "has acknowledged this emergency" not in prompt
        assert _PRIMARY not in prompt and "5550101" not in prompt

        await _ack_link(_token_from(provider.to(_PRIMARY)[0]))
        scripted.queue_reply(default_reply(message_to_customer="They have it."))
        await turn("Did they get it?")
        prompt = scripted.requests[-1].system_prompt
        assert "The on-call technician has acknowledged this emergency" in prompt
        assert "Do NOT say they are on the way" in prompt
        assert len(provider.sends) == 1, "a later turn re-paged"
    finally:
        for dependency in (get_ai_provider, verify_vapi_secret, get_paging_worker):
            fastapi_app.dependency_overrides.pop(dependency, None)
