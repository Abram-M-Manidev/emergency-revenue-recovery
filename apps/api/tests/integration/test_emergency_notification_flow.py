"""Emergency notification against a real PostgreSQL database.

The unit suite proves the rules. This proves the parts only real SQL can:

- `claim()` really is atomic. Its idempotency rests on INSERT ... ON CONFLICT
  DO NOTHING against a unique index, and an in-memory fake enforces that by
  construction — so a missing or misspelled index would sail through every
  unit test while, in production, paging the on-call engineer twice for one
  gas leak.
- The delivery row survives the request that wrote it and reads back with the
  status it was given, which is what a dispatcher reviewing a missed
  emergency the next morning actually depends on.
- Settings are organization-scoped in the query, not merely in the caller.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.domain.notifications.emergency import DeliveryStatus, NotificationChannel
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.notification import (
    OrganizationNotificationSettingsModel,
)
from app.infrastructure.database.repositories.notification_repository_impl import (
    SqlAlchemyNotificationDeliveryRepository,
    SqlAlchemyNotificationSettingsRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine

_DESTINATION = "https://hooks.example.invalid/services/T000/B000/secret"


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


async def _seed_org_and_ticket() -> tuple[uuid.UUID, uuid.UUID]:
    """A minimal organization, conversation and emergency ticket, written as
    raw SQL so this module stays independent of the service layer it exists
    to check the storage beneath."""
    organization_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    ticket_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, slug, is_active, created_at, "
                "updated_at) VALUES (:id, :name, :slug, true, now(), now())"
            ),
            {"id": organization_id, "name": "Notify Co", "slug": f"notify-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO conversations (id, organization_id, channel, status, "
                "started_at, created_at, updated_at) VALUES (:id, :org, 'voice', "
                "'active', now(), now(), now())"
            ),
            {"id": conversation_id, "org": organization_id},
        )
        await session.execute(
            text(
                "INSERT INTO emergency_tickets (id, organization_id, conversation_id, "
                "status, summary, created_at, updated_at) VALUES (:id, :org, :conv, "
                "'new', :summary, now(), now())"
            ),
            {
                "id": ticket_id,
                "org": organization_id,
                "conv": conversation_id,
                "summary": "Smell of gas near the furnace.",
            },
        )
        await session.commit()
    return organization_id, ticket_id


@pytest.mark.asyncio(loop_scope="session")
async def test_claim_grants_the_send_to_exactly_one_caller(database_ready):
    organization_id, ticket_id = await _seed_org_and_ticket()

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyNotificationDeliveryRepository(session)
        first, first_is_ours = await repo.claim(
            organization_id, ticket_id, channel=NotificationChannel.WEBHOOK, provider="webhook"
        )
        second, second_is_ours = await repo.claim(
            organization_id, ticket_id, channel=NotificationChannel.WEBHOOK, provider="webhook"
        )
        await session.commit()

    assert first_is_ours is True
    assert second_is_ours is False, "two callers both won the right to send"
    assert first.id == second.id
    assert first.status is DeliveryStatus.PENDING


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_claims_in_separate_transactions_yield_one_winner(database_ready):
    """The production shape: separate sessions, as four uvicorn workers
    would have. This is the case an in-memory fake cannot exercise at all."""
    organization_id, ticket_id = await _seed_org_and_ticket()

    async def attempt() -> bool:
        async with AsyncSessionLocal() as session:
            repo = SqlAlchemyNotificationDeliveryRepository(session)
            _, is_ours = await repo.claim(
                organization_id,
                ticket_id,
                channel=NotificationChannel.WEBHOOK,
                provider="webhook",
            )
            await session.commit()
            return is_ours

    results = await asyncio.gather(attempt(), attempt(), attempt(), return_exceptions=True)

    granted = [r for r in results if r is True]
    assert len(granted) == 1, f"expected exactly one winner, got {results}"

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM emergency_notification_deliveries "
                    "WHERE emergency_ticket_id = :tid"
                ),
                {"tid": ticket_id},
            )
        ).scalar_one()
    assert rows == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_a_recorded_result_persists_and_reads_back(database_ready):
    organization_id, ticket_id = await _seed_org_and_ticket()

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyNotificationDeliveryRepository(session)
        await repo.claim(
            organization_id, ticket_id, channel=NotificationChannel.WEBHOOK, provider="webhook"
        )
        await repo.record_result(
            organization_id,
            ticket_id,
            status=DeliveryStatus.DELIVERED,
            error_code=None,
            provider="webhook",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyNotificationDeliveryRepository(session)
        delivery = await repo.get_for_ticket(organization_id, ticket_id)
        # Another tenant asking about the same ticket must see nothing.
        leaked = await repo.get_for_ticket(uuid.uuid4(), ticket_id)

    assert delivery is not None
    assert delivery.status is DeliveryStatus.DELIVERED
    assert delivery.alerted_a_human is True
    assert delivery.attempts == 1
    assert delivery.delivered_at is not None
    assert leaked is None


@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_result_records_the_code_and_claims_nothing(database_ready):
    organization_id, ticket_id = await _seed_org_and_ticket()

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyNotificationDeliveryRepository(session)
        await repo.claim(
            organization_id, ticket_id, channel=NotificationChannel.WEBHOOK, provider="webhook"
        )
        delivery = await repo.record_result(
            organization_id,
            ticket_id,
            status=DeliveryStatus.FAILED,
            error_code="http_500",
            provider="webhook",
        )
        await session.commit()

    assert delivery.status is DeliveryStatus.FAILED
    assert delivery.alerted_a_human is False
    assert delivery.error_code == "http_500"
    assert delivery.delivered_at is None


@pytest.mark.asyncio(loop_scope="session")
async def test_settings_are_scoped_to_the_organization_that_configured_them(
    database_ready,
):
    organization_id, _ = await _seed_org_and_ticket()
    other_organization_id, _ = await _seed_org_and_ticket()

    async with AsyncSessionLocal() as session:
        session.add(
            OrganizationNotificationSettingsModel(
                id=uuid.uuid4(),
                organization_id=organization_id,
                channel=NotificationChannel.WEBHOOK,
                destination=_DESTINATION,
                is_enabled=True,
            )
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyNotificationSettingsRepository(session)
        configured = await repo.get_destination(organization_id)
        unconfigured = await repo.get_destination(other_organization_id)

    assert configured == (NotificationChannel.WEBHOOK, _DESTINATION)
    assert unconfigured is None, "a destination leaked across tenants"


@pytest.mark.asyncio(loop_scope="session")
async def test_disabled_settings_read_as_unconfigured(database_ready):
    """An operator switching alerting off for maintenance should not have to
    delete the destination — and while it is off the assistant must be as
    silent about dispatchers as if it had never been set."""
    organization_id, _ = await _seed_org_and_ticket()

    async with AsyncSessionLocal() as session:
        session.add(
            OrganizationNotificationSettingsModel(
                id=uuid.uuid4(),
                organization_id=organization_id,
                channel=NotificationChannel.WEBHOOK,
                destination=_DESTINATION,
                is_enabled=False,
            )
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyNotificationSettingsRepository(session)
        assert await repo.get_destination(organization_id) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_one_settings_row_per_organization_is_enforced_by_the_database(
    database_ready,
):
    organization_id, _ = await _seed_org_and_ticket()

    async with AsyncSessionLocal() as session:
        session.add(
            OrganizationNotificationSettingsModel(
                id=uuid.uuid4(),
                organization_id=organization_id,
                channel=NotificationChannel.WEBHOOK,
                destination=_DESTINATION,
                is_enabled=True,
            )
        )
        await session.commit()

    from sqlalchemy.exc import IntegrityError

    async with AsyncSessionLocal() as session:
        session.add(
            OrganizationNotificationSettingsModel(
                id=uuid.uuid4(),
                organization_id=organization_id,
                channel=NotificationChannel.WEBHOOK,
                destination="https://hooks.example.invalid/second",
                is_enabled=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# --- The real webhook provider, against a real HTTP server -------------------
#
# No Slack account and no credentials: a throwaway localhost listener is
# enough to prove the parts that matter about the real adapter — that it
# performs an actual HTTP POST, that the body carries the job, and that it
# maps real status codes onto our vocabulary. Everything above this point
# used a fake provider, so without this the only genuinely-shipped adapter
# would be untested against a socket.


class _CapturingServer:
    """A minimal HTTP/1.1 listener that records one request and answers with
    a configured status.

    Hand-rolled over `asyncio.start_server` rather than pulling in a test
    HTTP-server dependency, matching this codebase's preference for small
    local primitives over a new package for a problem this size."""

    def __init__(self, status_line: str = "200 OK") -> None:
        self.status_line = status_line
        self.body: bytes | None = None
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> _CapturingServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/hook"

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        header_block = await reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in header_block.decode("latin-1").split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        self.body = await reader.readexactly(length) if length else b""
        writer.write(
            f"HTTP/1.1 {self.status_line}\r\nContent-Length: 0\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()


@pytest.mark.asyncio(loop_scope="session")
async def test_the_webhook_provider_really_posts_and_reports_delivered():
    from app.infrastructure.notifications.providers import WebhookNotificationProvider

    alert = _alert_for_provider_test()
    async with _CapturingServer("200 OK") as server:
        provider = WebhookNotificationProvider(timeout_seconds=5.0)
        receipt = await provider.send(alert, server.url)

    assert receipt.status is DeliveryStatus.DELIVERED
    assert receipt.status.alerted_a_human is True
    assert server.body is not None
    payload = json.loads(server.body)
    assert payload["event"] == "emergency_ticket_created"
    assert payload["ticket_id"] == str(alert.ticket_id)
    assert "Smell of gas" in payload["text"]
    assert payload["idempotency_key"] == f"emergency_ticket:{alert.ticket_id}"


@pytest.mark.asyncio(loop_scope="session")
async def test_the_webhook_provider_treats_202_as_delivered():
    """PagerDuty answers 202. Any 2xx means the endpoint took responsibility
    for the message, which is the only distinction that matters here."""
    from app.infrastructure.notifications.providers import WebhookNotificationProvider

    async with _CapturingServer("202 Accepted") as server:
        receipt = await WebhookNotificationProvider(timeout_seconds=5.0).send(
            _alert_for_provider_test(), server.url
        )

    assert receipt.status is DeliveryStatus.DELIVERED


@pytest.mark.asyncio(loop_scope="session")
async def test_a_rejecting_endpoint_is_failed_and_never_claims_an_alert():
    from app.infrastructure.notifications.providers import WebhookNotificationProvider

    async with _CapturingServer("500 Internal Server Error") as server:
        receipt = await WebhookNotificationProvider(timeout_seconds=5.0).send(
            _alert_for_provider_test(), server.url
        )

    assert receipt.status is DeliveryStatus.FAILED
    assert receipt.status.alerted_a_human is False
    assert receipt.error_code == "http_500"


def _alert_for_provider_test():
    from datetime import datetime, timezone

    from app.domain.notifications.emergency import EmergencyAlert

    return EmergencyAlert(
        organization_id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        summary="Smell of gas near the furnace.",
        customer_name="Frank",
        customer_phone="5550001111",
        customer_address="11 69 Street",
        created_at=datetime.now(timezone.utc),
    )
