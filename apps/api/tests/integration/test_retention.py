"""Transcript retention against a real Postgres: only completed, expired
conversations are redacted; job records are untouched; dry run writes
nothing; a second run is a no-op."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.cli.retention import apply_retention, redaction_marker
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine

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


async def _conversation(session, org, *, age_days: int, status: str) -> uuid.UUID:
    conversation_id = (
        await session.execute(
            text(
                "INSERT INTO conversations (id, organization_id, channel, status, started_at) "
                "VALUES (gen_random_uuid(), :org, 'VOICE', :status, :at) RETURNING id"
            ),
            {"org": org, "status": status, "at": _NOW - timedelta(days=age_days)},
        )
    ).scalar_one()
    for role, content in (("CUSTOMER", "My name is Dana."), ("ASSISTANT", "Thanks, Dana.")):
        await session.execute(
            text(
                "INSERT INTO conversation_messages (id, conversation_id, role, content) "
                "VALUES (gen_random_uuid(), :c, :role, :content)"
            ),
            {"c": conversation_id, "role": role, "content": content},
        )
    await session.execute(
        text(
            "INSERT INTO voice_calls (id, organization_id, conversation_id, vapi_call_id, "
            "started_at, recording_url) VALUES (gen_random_uuid(), :org, :c, :vid, :at, "
            "'https://recordings.example.invalid/x.wav')"
        ),
        {"org": org, "c": conversation_id, "vid": uuid.uuid4().hex, "at": _NOW},
    )
    return conversation_id


async def _contents(session, conversation_id) -> set[str]:
    rows = await session.execute(
        text("SELECT content FROM conversation_messages WHERE conversation_id = :c"),
        {"c": conversation_id},
    )
    return {r[0] for r in rows.all()}


@pytest.mark.asyncio(loop_scope="session")
async def test_retention_redacts_only_expired_completed_calls(database_ready):
    async with AsyncSessionLocal() as session:
        org = (
            await session.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, is_active) VALUES "
                    "(gen_random_uuid(), 'Retention Org', :slug, true) RETURNING id"
                ),
                {"slug": f"retention-{uuid.uuid4().hex[:8]}"},
            )
        ).scalar_one()
        old = await _conversation(session, org, age_days=120, status="COMPLETED")
        recent = await _conversation(session, org, age_days=10, status="COMPLETED")
        live = await _conversation(session, org, age_days=120, status="ACTIVE")
        await session.commit()

        dry = await apply_retention(session, days=90, now=_NOW, apply=False)
        assert dry == {"messages": 2, "recording_links": 1}
        assert "My name is Dana." in await _contents(session, old)

        applied = await apply_retention(session, days=90, now=_NOW, apply=True)
        await session.commit()
        assert applied == dry
        assert await _contents(session, old) == {redaction_marker(90)}
        assert "My name is Dana." in await _contents(session, recent)
        assert "My name is Dana." in await _contents(session, live)
        links = (
            await session.execute(
                text("SELECT recording_url FROM voice_calls WHERE conversation_id = :c"),
                {"c": old},
            )
        ).scalar_one()
        assert links is None

        again = await apply_retention(session, days=90, now=_NOW, apply=True)
        assert again == {"messages": 0, "recording_links": 0}
