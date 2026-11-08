"""Go-live preflight: can this deployment safely answer emergency calls?

    python -m app.cli.preflight            # human-readable, exit 1 on any FAIL

Read-only. Complements, and does not repeat, the static checks `Settings`
already enforces at startup in production (DEBUG off, real JWT secret,
explicit https CORS origins, Vapi and OpenAI secrets, no "logging"
providers, complete Twilio paging credentials). Those stop the API from
booting; this checks what can only be seen in the running system:

- the database is reachable and at the migration head the code expects;
- deployment-wide providers that decide whether anyone hears about an
  emergency (alerts, paging, the acknowledgement link base);
- for every business with an ACTIVE voice line: the assistant is enabled,
  an emergency alert destination exists, and the configuration the call
  flow depends on is present (country for phone validation, timezone,
  hours, services, transfer numbers, paging);
- usage-data health (metering M4).

Prints check codes and counts only — never secrets, phone numbers, URLs or
business names (organizations are identified by id).

FAIL means "do not take live calls on this line yet". WARN means "works,
but a caller or the business will get a worse outcome; decide knowingly".
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from sqlalchemy import text

from alembic.config import Config
from alembic.script import ScriptDirectory
from app.application.services.usage_report_service import UsageReportService
from app.core.config import Settings, get_settings
from app.infrastructure.database.repositories.usage_report_repository_impl import (
    SqlAlchemyUsageReportRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    level: str
    code: str
    detail: str


def _expected_head() -> str | None:
    config = Config("alembic.ini")
    return ScriptDirectory.from_config(config).get_current_head()


def settings_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    production = settings.ENVIRONMENT == "production"
    if settings.NOTIFICATION_PROVIDER == "none":
        checks.append(
            Check(
                FAIL if production else WARN,
                "alerts_provider_off",
                "NOTIFICATION_PROVIDER=none: emergency tickets alert nobody",
            )
        )
    else:
        checks.append(
            Check(
                PASS, "alerts_provider", f"NOTIFICATION_PROVIDER={settings.NOTIFICATION_PROVIDER}"
            )
        )
    if settings.PAGING_PROVIDER == "none":
        checks.append(Check(WARN, "paging_off", "PAGING_PROVIDER=none: no on-call SMS pages"))
    else:
        checks.append(Check(PASS, "paging_provider", f"PAGING_PROVIDER={settings.PAGING_PROVIDER}"))
        if not settings.PAGING_ACK_BASE_URL:
            checks.append(
                Check(
                    FAIL,
                    "paging_ack_url_missing",
                    "PAGING_ACK_BASE_URL unset: pages carry no ack link",
                )
            )
    if not settings.VAPI_API_KEY:
        checks.append(Check(WARN, "vapi_api_key_missing", "usage reconciliation cannot run"))
    if production and settings.registration_enabled:
        checks.append(
            Check(WARN, "registration_open", "anyone can create an organization on this deployment")
        )
    return checks


async def database_checks() -> list[Check]:
    checks: list[Check] = []
    async with AsyncSessionLocal() as session:
        current = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        head = _expected_head()
        if current != head:
            checks.append(
                Check(FAIL, "migrations_behind", f"database at {current}, code expects {head}")
            )
        else:
            checks.append(Check(PASS, "migrations_at_head", f"{current}"))

        rows = (
            await session.execute(
                text(
                    """
                    SELECT v.organization_id,
                           o.voice_assistant_enabled,
                           p.country, p.timezone,
                           (SELECT count(*) FROM organization_notification_settings n
                             WHERE n.organization_id = v.organization_id AND n.is_enabled),
                           (SELECT count(*) FROM business_hours h
                             WHERE h.organization_id = v.organization_id),
                           (SELECT count(*) FROM services s
                             WHERE s.organization_id = v.organization_id AND s.is_active),
                           (SELECT count(*) FROM organization_call_transfer_settings t
                             WHERE t.organization_id = v.organization_id AND t.is_enabled),
                           (SELECT count(*) FROM organization_paging_settings g
                             WHERE g.organization_id = v.organization_id AND g.is_enabled)
                      FROM voice_lines v
                      JOIN organizations o ON o.id = v.organization_id
                      LEFT JOIN business_profiles p ON p.organization_id = v.organization_id
                     WHERE v.is_active
                    """
                )
            )
        ).all()
        if not rows:
            checks.append(Check(WARN, "no_active_voice_lines", "no line will answer calls"))
        for org, enabled, country, timezone, alerts, hours, services, transfer, paging in rows:
            tag = f"org {org}"
            if not enabled:
                checks.append(Check(WARN, "assistant_disabled", f"{tag}: kill switch is off"))
            if not alerts:
                checks.append(
                    Check(FAIL, "no_alert_destination", f"{tag}: emergencies would alert nobody")
                )
            if not country:
                checks.append(
                    Check(
                        WARN,
                        "no_country",
                        f"{tag}: national-format phone numbers cannot be validated",
                    )
                )
            if not timezone:
                checks.append(Check(WARN, "no_timezone", f"{tag}: dates use UTC"))
            if not hours:
                checks.append(
                    Check(WARN, "no_hours", f"{tag}: no business hours, no bookable times")
                )
            if not services:
                checks.append(Check(WARN, "no_services", f"{tag}: no services listed"))
            if not transfer:
                checks.append(Check(WARN, "no_transfer", f"{tag}: callers cannot reach a person"))
            if not paging:
                checks.append(Check(WARN, "no_paging", f"{tag}: on-call staff are not paged"))
            if alerts and enabled:
                checks.append(Check(PASS, "line_ready", f"{tag}: alerts configured, assistant on"))

        health = await UsageReportService(
            usage_report_repository=SqlAlchemyUsageReportRepository(session)
        ).health()
    for problem in health.problems:
        checks.append(
            Check(
                WARN, f"usage_{problem}", "see RUNBOOK 'Usage reports and the usage health check'"
            )
        )
    return checks


async def _run() -> int:
    settings = get_settings()
    checks = settings_checks(settings)
    try:
        checks += await database_checks()
    except Exception as exc:
        checks.append(Check(FAIL, "database_unreachable", type(exc).__name__))
    for check in checks:
        print(f"{check.level:4}  {check.code:28}  {check.detail}")
    failed = [c for c in checks if c.level == FAIL]
    print(f"\n{len(failed)} FAIL, {sum(c.level == WARN for c in checks)} WARN")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
