"""A business's caller-disclosure policy: reading it for a live call, and
letting the Owner change it. See `app/domain/disclosure.py` for what ERRS
does and does not control."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog

from app.domain.disclosure import (
    DEFAULT_DISCLOSURE_POLICY,
    DisclosurePolicy,
    DisclosureSettings,
)
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.disclosure_repository import DisclosureSettingsRepository
from app.domain.repositories.organization_repository import OrganizationRepository
from app.domain.transactions import NullSavepoints, Savepoints

logger = structlog.get_logger("app.voice.disclosure")


@dataclass(frozen=True, slots=True)
class CallDisclosureContext:
    """Everything a live call needs to speak its notice."""

    policy: DisclosurePolicy
    business_name: str | None
    #: True when the stored policy could not be read and the default (both
    #: notices) is being used instead.
    fell_back: bool = False


class CallDisclosureService:
    def __init__(
        self,
        *,
        settings_repository: DisclosureSettingsRepository,
        organization_repository: OrganizationRepository | None = None,
        business_profile_repository: BusinessProfileRepository | None = None,
        savepoints: Savepoints | None = None,
    ) -> None:
        self._settings = settings_repository
        self._organizations = organization_repository
        self._profiles = business_profile_repository
        self._savepoints = savepoints or NullSavepoints()

    # --- Live calls --------------------------------------------------------------

    async def context_for(self, organization_id: uuid.UUID) -> CallDisclosureContext:
        """Never raises, and never returns "say nothing" because something
        broke: a failed read falls back to the DEFAULT policy, which gives
        both notices. Over-disclosing on a bad day is acceptable;
        under-disclosing is not. Each lookup is in its own savepoint, inside
        the `try`, so a failure cannot poison the turn's transaction."""
        policy = DEFAULT_DISCLOSURE_POLICY
        fell_back = False
        try:
            async with self._savepoints.isolate():
                stored = await self._settings.get(organization_id)
            if stored is not None:
                policy = stored.policy
        except Exception as exc:
            fell_back = True
            logger.error(
                "call_disclosure_policy_unreadable",
                organization_id=str(organization_id),
                error=type(exc).__name__,
            )
        return CallDisclosureContext(
            policy=policy,
            business_name=await self._business_name(organization_id),
            fell_back=fell_back,
        )

    async def _business_name(self, organization_id: uuid.UUID) -> str | None:
        """The public business name — the profile's display name, else the
        organization's. Best-effort: without one the notice names "this
        business"."""
        try:
            if self._profiles is not None:
                async with self._savepoints.isolate():
                    profile = await self._profiles.get_by_organization_id(organization_id)
                if profile is not None and profile.display_name.strip():
                    return profile.display_name
            if self._organizations is not None:
                async with self._savepoints.isolate():
                    organization = await self._organizations.get_by_id(organization_id)
                if organization is not None:
                    return organization.name
        except Exception as exc:
            logger.warning(
                "call_disclosure_name_unreadable",
                organization_id=str(organization_id),
                error=type(exc).__name__,
            )
        return None

    # --- Owner configuration -------------------------------------------------------

    async def get_settings(self, organization_id: uuid.UUID) -> DisclosureSettings | None:
        return await self._settings.get(organization_id)

    async def configure(
        self, organization_id: uuid.UUID, *, ai_disclosure: bool, recording_notice: bool
    ) -> DisclosureSettings:
        settings = await self._settings.upsert(
            organization_id,
            DisclosurePolicy(ai_disclosure=ai_disclosure, recording_notice=recording_notice),
        )
        logger.info(
            "call_disclosure_policy_changed",
            organization_id=str(organization_id),
            ai_disclosure=ai_disclosure,
            recording_notice=recording_notice,
        )
        return settings

    async def reset(self, organization_id: uuid.UUID) -> None:
        """Back to the default policy (both notices on)."""
        await self._settings.delete(organization_id)
