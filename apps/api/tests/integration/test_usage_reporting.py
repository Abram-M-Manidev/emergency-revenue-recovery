"""Metering M3/M4 against a real Postgres: the per-organization usage report
(tenant-isolated, unattributed calls never counted) and the health facts."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.application.services.usage_report_service import UsageReportService
from app.domain.usage import (
    DurationSource,
    ExclusionReason,
    PeriodBasis,
    UsageAttribution,
    UsageSource,
)
from app.domain.usage_reconciliation import ReconciliationOutcome, ReconciliationRunStatus
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.usage import (
    CallUsageRecordModel,
    UsageReconciliationRunModel,
    VapiCallReconciliationModel,
)
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.repositories.usage_report_repository_impl import (
    SqlAlchemyUsageReportRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app

_PERIOD = date(2026, 11, 1)
_NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


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


async def _register(client: AsyncClient, name: str) -> tuple[uuid.UUID, dict[str, str]]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": name,
            "full_name": "Owner",
            "email": f"owner-{uuid.uuid4().hex[:8]}@example.com",
            "password": "super-secret-123",
        },
    )
    assert response.status_code == 201
    body = response.json()
    return (
        uuid.UUID(body["user"]["organization_id"]),
        {"Authorization": f"Bearer {body['tokens']['access_token']}"},
    )


def _record(org: uuid.UUID | None, *, minutes: int, excluded: ExclusionReason | None = None):
    return CallUsageRecordModel(
        organization_id=org,
        vapi_call_id=f"call_{uuid.uuid4().hex}",
        attribution=UsageAttribution.VOICE_LINE if org else UsageAttribution.UNATTRIBUTED,
        billing_period=_PERIOD,
        period_basis=PeriodBasis.STARTED_AT,
        duration_source=DurationSource.TIMESTAMPS,
        billable_seconds=minutes * 60,
        billable_minutes=minutes,
        is_billable=excluded is None,
        exclusion_reason=excluded,
        rules_version="m2-2026-10-30",
        source=UsageSource.WEBHOOK,
        first_reported_at=_NOW - timedelta(days=1),
        last_reported_at=_NOW - timedelta(days=1),
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_the_usage_report_counts_only_this_organizations_calls(client: AsyncClient):
    mine, my_headers = await _register(client, "Usage Org A")
    theirs, _ = await _register(client, "Usage Org B")

    async with AsyncSessionLocal() as session:
        billed = _record(mine, minutes=3)
        session.add_all(
            [
                billed,
                _record(mine, minutes=2),
                _record(mine, minutes=0, excluded=ExclusionReason.UNDER_MINIMUM),
                _record(theirs, minutes=40),
                _record(None, minutes=99),  # unattributed: nobody's
            ]
        )
        await session.flush()
        session.add(
            VapiCallReconciliationModel(
                vapi_call_id=billed.vapi_call_id,
                call_usage_record_id=billed.id,
                outcome=ReconciliationOutcome.MATCHED,
                discrepancies=[],
                filled_fields=[],
                checked_at=_NOW,
            )
        )
        await session.commit()

    response = await client.get("/api/v1/usage/summary?period=2026-11", headers=my_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["calls"] == 3
    assert body["billable_calls"] == 2
    assert body["billable_minutes"] == 5
    assert body["excluded_calls"] == {"under_minimum": 1}
    assert body["reconciliation"] == {"matched": 1}
    assert body["unreconciled_calls"] == 2
    assert body["rules_versions"] == ["m2-2026-10-30"]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_usage_report_needs_a_session_and_a_valid_period(client: AsyncClient):
    _, headers = await _register(client, "Usage Org C")
    assert (await client.get("/api/v1/usage/summary")).status_code == 401
    assert (
        await client.get("/api/v1/usage/summary?period=2026-13", headers=headers)
    ).status_code == 422
    empty = await client.get("/api/v1/usage/summary?period=2025-01", headers=headers)
    assert empty.status_code == 200 and empty.json()["calls"] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_health_reports_missing_usage_failed_runs_and_unattributed_calls(
    client: AsyncClient,
):
    org, _ = await _register(client, "Usage Org D")
    async with AsyncSessionLocal() as session:
        conversation_id = (
            await session.execute(
                text(
                    "INSERT INTO conversations (id, organization_id, channel, status, started_at) "
                    "VALUES (gen_random_uuid(), :org, 'VOICE', 'COMPLETED', :at) RETURNING id"
                ),
                {"org": org, "at": _NOW - timedelta(days=1)},
            )
        ).scalar_one()
        # A call that ended a day ago and never got a usage record.
        session.add(
            VoiceCallModel(
                organization_id=org,
                conversation_id=conversation_id,
                vapi_call_id=f"call_{uuid.uuid4().hex}",
                started_at=_NOW - timedelta(days=1),
            )
        )
        session.add(
            UsageReconciliationRunModel(
                window_start=_NOW - timedelta(days=2),
                window_end=_NOW - timedelta(days=1),
                status=ReconciliationRunStatus.FAILED,
                started_at=_NOW - timedelta(hours=1),
                finished_at=_NOW - timedelta(hours=1),
                error="VapiApiError",
            )
        )
        await session.commit()

        health = await UsageReportService(
            usage_report_repository=SqlAlchemyUsageReportRepository(session)
        ).health(now=_NOW)

    assert "calls_missing_usage" in health.problems
    assert "reconciliation_last_run_failed" in health.problems
    assert "reconciliation_stale" in health.problems  # never succeeded
    # The unattributed record from the first test is within the lookback.
    assert "unattributed_usage" in health.problems
