"""Against a real Postgres: the public early-access endpoint, and the
contact-correction repository's supersede/confirm semantics and partial
unique index."""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.application.services.lead_service import CURRENT_CONSENT_VERSION
from app.domain.entities.conversation import ConversationChannel
from app.domain.repositories.contact_correction_repository import SERVICE_ADDRESS
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.repositories import (
    SqlAlchemyContactCorrectionRepository,
    SqlAlchemyConversationRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app


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
        await conn.execute(text("SELECT 1"))
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(loop_scope="session")
async def client(database_ready):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _lead(**overrides):
    body = {
        "name": "Priya Contractor",
        "business_name": "Coolside HVAC",
        "email": "Priya@Example.com",
        "business_type": "HVAC",
        "consent": True,
        "consent_version": CURRENT_CONSENT_VERSION,
    }
    body.update(overrides)
    return body


async def _leads() -> list[tuple[str, int]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text("SELECT email, request_count FROM marketing_leads ORDER BY email")
        )
        return [(r[0], r[1]) for r in rows.all()]


@pytest.mark.asyncio(loop_scope="session")
async def test_an_early_access_request_is_stored_with_consent(client: AsyncClient):
    response = await client.post("/api/v1/public/early-access", json=_lead())

    assert response.status_code == 202
    assert response.json() == {"status": "received"}
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT email, consent_version, consented_at FROM marketing_leads "
                    "WHERE email = 'priya@example.com'"
                )
            )
        ).one()
    assert row[1] == CURRENT_CONSENT_VERSION
    assert row[2] is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_a_repeat_request_gets_the_same_answer_and_stays_one_lead(client: AsyncClient):
    """The response must not reveal whether the address was already known."""
    first = await client.post("/api/v1/public/early-access", json=_lead(email="dup@example.com"))
    second = await client.post("/api/v1/public/early-access", json=_lead(email="DUP@example.com"))

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert ("dup@example.com", 2) in await _leads()


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "overrides",
    [
        {"consent": False},
        {"consent_version": "2020-01-01"},
        {"email": "not-an-email"},
        {"business_type": "Landscaping"},
        {"name": "   "},
        {"name": "x" * 121},
    ],
    ids=["no-consent", "stale-consent", "bad-email", "bad-type", "blank-name", "long-name"],
)
async def test_invalid_requests_are_rejected_and_store_nothing(client: AsyncClient, overrides):
    before = await _leads()
    response = await client.post(
        "/api/v1/public/early-access", json=_lead(**{"email": "reject@example.com", **overrides})
    )
    assert response.status_code == 422
    assert await _leads() == before


@pytest.mark.asyncio(loop_scope="session")
async def test_a_honeypot_submission_looks_accepted_but_stores_nothing(client: AsyncClient):
    before = await _leads()
    response = await client.post(
        "/api/v1/public/early-access",
        json=_lead(email="bot@example.com", website="http://spam.example"),
    )
    assert response.status_code == 202
    assert response.json() == {"status": "received"}
    assert await _leads() == before


@pytest.mark.asyncio(loop_scope="session")
async def test_leads_are_not_readable_through_any_tenant_api(client: AsyncClient):
    for path in ("/api/v1/public/early-access", "/api/v1/public/leads"):
        response = await client.get(path)
        assert response.status_code in (404, 405)


@pytest.mark.asyncio(loop_scope="session")
async def test_correction_proposals_supersede_and_confirm_in_postgres(client: AsyncClient):
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Correction Org",
            "full_name": "Owner",
            "email": "owner-corrections@example.com",
            "password": "super-secret-123",
        },
    )
    assert register.status_code == 201
    org_id = uuid.UUID(register.json()["user"]["organization_id"])

    async with AsyncSessionLocal() as session:
        conversation = await SqlAlchemyConversationRepository(session).create(
            organization_id=org_id,
            channel=ConversationChannel.VOICE,
            caller_phone_number=None,
        )
        repo = SqlAlchemyContactCorrectionRepository(session)
        first = await repo.propose(
            org_id, conversation.id, field=SERVICE_ADDRESS, proposed_value="14 Elm", turn_index=4
        )
        second = await repo.propose(
            org_id, conversation.id, field=SERVICE_ADDRESS, proposed_value="41 Elm", turn_index=6
        )
        pending = await repo.get_pending(org_id, conversation.id, field=SERVICE_ADDRESS)
        assert pending is not None and pending.id == second.id
        assert await repo.get_pending(uuid.uuid4(), conversation.id, field=SERVICE_ADDRESS) is None

        await repo.mark_confirmed(org_id, second.id, at=datetime.now(timezone.utc))
        assert await repo.get_pending(org_id, conversation.id, field=SERVICE_ADDRESS) is None
        await session.commit()

        rows = (
            await session.execute(
                text(
                    "SELECT id, confirmed_at IS NOT NULL, superseded_at IS NOT NULL "
                    "FROM contact_correction_requests WHERE conversation_id = :c"
                ),
                {"c": conversation.id},
            )
        ).all()
    by_id = {r[0]: (r[1], r[2]) for r in rows}
    assert by_id[first.id] == (False, True)
    assert by_id[second.id] == (True, False)
