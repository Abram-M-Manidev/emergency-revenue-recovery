"""What one call actually did — evidence for a real-call acceptance test.

    python -m app.cli.call_check <vapi_call_id>          # ERRS's own records
    python -m app.cli.call_check <vapi_call_id> --vapi   # + Vapi's timeline (read-only GET)

Built for docs/launch/REAL_CALL_ACCEPTANCE_PLAN.md: after each scenario, run
this and paste the output into the acceptance sheet. It answers the
questions the plan asks without anyone reading the database by hand:
classification and action, the ticket and whether its alert was DELIVERED
(not merely sent), the on-call page and whether anyone ACKNOWLEDGED it,
transfer attempts, the callback follow-up, the appointment, interrupted
replies, address-correction proposals, the caller notice, and usage.

With --vapi it also reads the call from Vapi (one GET, read-only, needs
VAPI_API_KEY) and reports, for L7, when the call started, whether a
recording exists, and when the first assistant line and the first caller
line were spoken — plus whether that first assistant line contained the AI
and recording notice. Timings only: no transcript text, no numbers.

Prints states, counts and timings — never names, phone numbers, addresses
or transcript text.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.database.session import AsyncSessionLocal
from app.infrastructure.telephony.vapi_calls_client import VapiCallsClient

_LOCAL_QUERIES: dict[str, str] = {
    "voice_call": """
        SELECT v.ended_reason, v.duration_seconds, v.disclosure_sent_at IS NOT NULL,
               v.disclosed_ai, v.disclosed_recording, v.recording_url IS NOT NULL
          FROM voice_calls v WHERE v.conversation_id = :c""",
    "outcome": """
        SELECT o.classification, o.recommended_action, o.customer_phone IS NOT NULL,
               o.customer_address IS NOT NULL
          FROM conversation_outcomes o WHERE o.conversation_id = :c""",
    "ticket": "SELECT t.id, t.status FROM emergency_tickets t WHERE t.conversation_id = :c",
    "alerts": """
        SELECT d.status, d.attempts FROM emergency_notification_deliveries d
          JOIN emergency_tickets t ON t.id = d.emergency_ticket_id
         WHERE t.conversation_id = :c""",
    "pages": """
        SELECT p.status, p.acknowledged_at IS NOT NULL, p.escalated_at IS NOT NULL
          FROM emergency_pages p JOIN emergency_tickets t ON t.id = p.emergency_ticket_id
         WHERE t.conversation_id = :c""",
    "page_messages": """
        SELECT n.status, count(*) FROM emergency_page_notifications n
          JOIN emergency_pages p ON p.id = n.page_id
          JOIN emergency_tickets t ON t.id = p.emergency_ticket_id
         WHERE t.conversation_id = :c GROUP BY n.status""",
    "transfers": """
        SELECT a.reason, a.status FROM call_transfer_attempts a
         WHERE a.conversation_id = :c ORDER BY a.created_at""",
    "follow_up": """
        SELECT f.reason, f.possible_emergency, f.resolved_at IS NOT NULL
          FROM call_follow_ups f WHERE f.conversation_id = :c""",
    "appointment": """
        SELECT a.status, a.scheduled_start_at IS NOT NULL FROM appointments a
         WHERE a.conversation_id = :c""",
    "messages": """
        SELECT m.role, count(*), count(m.interrupted_at) FROM conversation_messages m
         WHERE m.conversation_id = :c GROUP BY m.role""",
    "corrections": """
        SELECT c.field, c.confirmed_at IS NOT NULL, c.superseded_at IS NOT NULL
          FROM contact_correction_requests c WHERE c.conversation_id = :c""",
    "usage": """
        SELECT u.billable_minutes, u.is_billable, u.exclusion_reason, u.source
          FROM call_usage_records u WHERE u.vapi_call_id = :v""",
}


async def local_report(vapi_call_id: str) -> list[str]:
    lines: list[str] = []
    async with AsyncSessionLocal() as session:
        conversation_id = (
            await session.execute(
                text("SELECT conversation_id FROM voice_calls WHERE vapi_call_id = :v"),
                {"v": vapi_call_id},
            )
        ).scalar()
        if conversation_id is None:
            return [f"no ERRS voice call recorded for {vapi_call_id}"]
        lines.append(f"conversation {conversation_id}")
        for name, sql in _LOCAL_QUERIES.items():
            # Each query in its own savepoint: one failing (a schema older
            # than this tool) is reported, and the rest still prints.
            try:
                async with session.begin_nested():
                    rows = (
                        await session.execute(text(sql), {"c": conversation_id, "v": vapi_call_id})
                    ).all()
            except Exception as exc:
                lines.append(f"{name:13} (unavailable: {type(exc).__name__})")
                continue
            values = [tuple(_plain(v) for v in row) for row in rows]
            lines.append(f"{name:13} {values if values else '-'}")
    return lines


def _plain(value: Any) -> Any:
    return (
        getattr(value, "value", getattr(value, "name", value))
        if not isinstance(value, str | int | float | bool | None)
        else value
    )


def vapi_timeline(call: dict[str, Any]) -> list[str]:
    """Timing facts from a raw Vapi call object, for L7. Never returns text."""
    raw_artifact = call.get("artifact")
    artifact: dict[str, Any] = raw_artifact if isinstance(raw_artifact, dict) else {}
    messages = artifact.get("messages") or call.get("messages") or []
    recording = bool(
        artifact.get("recordingUrl")
        or artifact.get("stereoRecordingUrl")
        or (isinstance(artifact.get("recording"), dict) and artifact["recording"])
        or call.get("recordingUrl")
    )
    first_bot: dict[str, Any] | None = None
    first_user: dict[str, Any] | None = None
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in ("bot", "assistant") and first_bot is None:
            first_bot = message
        elif role == "user" and first_user is None:
            first_user = message
    lines = [
        f"vapi status    {call.get('status')}  endedReason={call.get('endedReason')}",
        f"started        {call.get('startedAt')}  ended={call.get('endedAt')}",
        f"recording      {'present' if recording else 'none'}",
    ]
    if first_bot is not None:
        spoken = str(first_bot.get("message") or "").lower()
        lines.append(
            "first bot line "
            f"at {_seconds(first_bot)}s  ai_notice={'automated assistant' in spoken}  "
            f"recording_notice={'recorded' in spoken}"
        )
    else:
        lines.append("first bot line none")
    lines.append(
        f"first caller   {'at ' + str(_seconds(first_user)) + 's' if first_user else 'none'}"
    )
    if first_bot is not None and first_user is not None:
        before = (_seconds(first_bot) or 0) <= (_seconds(first_user) or 0)
        lines.append(f"notice before caller spoke: {before}")
    return lines


def _seconds(message: dict[str, Any] | None) -> float | None:
    if not message:
        return None
    value = message.get("secondsFromStart")
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _run(vapi_call_id: str, with_vapi: bool) -> int:
    for line in await local_report(vapi_call_id):
        print(line)
    if not with_vapi:
        return 0
    settings = get_settings()
    if not settings.VAPI_API_KEY:
        print("vapi           VAPI_API_KEY not set", file=sys.stderr)
        return 2
    client = VapiCallsClient(api_key=settings.VAPI_API_KEY, base_url=settings.VAPI_API_BASE_URL)
    call = await client.get_call_raw(vapi_call_id)
    if call is None:
        print("vapi           call not found (outside retention, or another org)")
        return 1
    for line in vapi_timeline(call):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("vapi_call_id")
    parser.add_argument("--vapi", action="store_true", help="also read the call from Vapi")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.vapi_call_id, args.vapi))


if __name__ == "__main__":
    raise SystemExit(main())
