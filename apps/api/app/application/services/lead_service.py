"""Early-access requests from the public site.

The public endpoint must not become an oracle for "is this address already
on your list?", so a new request, a repeat request and a bot caught by the
honeypot all get the same answer. Nothing here logs the person's details:
the log line carries only the business type."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from app.domain.repositories.lead_repository import LeadRepository

logger = structlog.get_logger("app.leads")

#: The version of the consent wording shown next to the form. Bump it, and
#: the frontend's copy, whenever that wording changes.
CURRENT_CONSENT_VERSION = "2026-11-08"

BUSINESS_TYPES = ("HVAC", "Plumbing", "Electrical", "Multi-trade", "Other")


class LeadService:
    def __init__(self, *, lead_repository: LeadRepository) -> None:
        self._leads = lead_repository

    async def request_early_access(
        self,
        *,
        name: str,
        business_name: str,
        email: str,
        business_type: str,
        consent_version: str,
        honeypot: str | None,
    ) -> None:
        if honeypot:
            # A field humans never see was filled in. Accepted and dropped,
            # so the bot learns nothing from the response.
            logger.info("early_access_honeypot_dropped")
            return
        await self._leads.record(
            name=name.strip(),
            business_name=business_name.strip(),
            email=email.strip().lower(),
            business_type=business_type,
            consent_version=consent_version,
            consented_at=datetime.now(timezone.utc),
        )
        logger.info("early_access_requested", business_type=business_type)
