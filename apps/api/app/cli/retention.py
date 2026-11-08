"""Transcript and recording-link retention, run by the operator.

    python -m app.cli.retention --days 90            # dry run: counts only
    python -m app.cli.retention --days 90 --apply    # redact

For calls on COMPLETED conversations that started more than `--days` ago:
- every message's text is replaced with `[redacted after N days]`;
- the voice call's `recording_url` is cleared.

What it deliberately does NOT touch:
- tickets, appointments, customers and outcomes. Those are the business's
  own job records (who to call back, where to send a technician) and are
  kept and deleted by the business under its agreement, not by age;
- the recording itself, which lives at the voice provider (Vapi keeps it
  14 days on the current plan) — clearing the link does not delete it there;
- usage and reconciliation records, which hold no transcript text.

Rows are redacted, not deleted, so counts, timings and analytics stay
correct and nothing referencing a message breaks. It is idempotent: an
already-redacted message is skipped. Dry run is the default; nothing is
written without `--apply`. The window is the operator's decision, recorded
in the customer agreement (see docs/legal/DATA_RETENTION_POLICY.md).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.conversation import ConversationStatus
from app.infrastructure.database.models.conversation import ConversationModel
from app.infrastructure.database.models.conversation_message import ConversationMessageModel
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.session import AsyncSessionLocal

logger = structlog.get_logger("app.retention")

_MIN_DAYS = 7


def redaction_marker(days: int) -> str:
    return f"[redacted after {days} days]"


async def apply_retention(
    session: AsyncSession, *, days: int, now: datetime, apply: bool
) -> dict[str, int]:
    """Counts what is (or would be) redacted. Writes only when `apply`."""
    cutoff = now - timedelta(days=days)
    marker = redaction_marker(days)
    expired = select(ConversationModel.id).where(
        ConversationModel.started_at < cutoff,
        ConversationModel.status == ConversationStatus.COMPLETED,
    )
    message_filter = and_(
        ConversationMessageModel.conversation_id.in_(expired),
        ConversationMessageModel.content != marker,
    )
    recording_filter = and_(
        VoiceCallModel.conversation_id.in_(expired),
        VoiceCallModel.recording_url.is_not(None),
    )
    messages = (
        await session.execute(select(func.count()).where(message_filter))
    ).scalar_one()
    recordings = (
        await session.execute(select(func.count()).where(recording_filter))
    ).scalar_one()

    if apply:
        await session.execute(
            update(ConversationMessageModel)
            .where(message_filter)
            .values(content=marker)
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            update(VoiceCallModel)
            .where(recording_filter)
            .values(recording_url=None)
            .execution_options(synchronize_session=False)
        )
    counts = {"messages": int(messages), "recording_links": int(recordings)}
    logger.info("retention_run", days=days, applied=apply, **counts)
    return counts


async def _run(days: int, apply: bool) -> int:
    async with AsyncSessionLocal() as session:
        counts = await apply_retention(
            session, days=days, now=datetime.now(timezone.utc), apply=apply
        )
        if apply:
            await session.commit()
    verb = "redacted" if apply else "would redact"
    print(f"{verb}: {counts['messages']} messages, {counts['recording_links']} recording links")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.days < _MIN_DAYS:
        parser.error(f"--days must be at least {_MIN_DAYS}")
    return asyncio.run(_run(args.days, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
