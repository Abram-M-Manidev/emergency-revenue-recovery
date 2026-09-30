"""Owner-managed configuration of who is paged about an emergency."""

from __future__ import annotations

import uuid

from app.domain.paging.settings import PagingSettings, validate_paging_settings
from app.domain.repositories.paging_repository import PagingSettingsRepository
from app.domain.repositories.voice_line_repository import VoiceLineRepository
from app.shared.utils.phone import normalize_phone_number


class PagingSettingsService:
    def __init__(
        self,
        settings_repository: PagingSettingsRepository,
        voice_line_repository: VoiceLineRepository,
    ) -> None:
        self._settings = settings_repository
        self._voice_lines = voice_line_repository

    async def get(self, organization_id: uuid.UUID) -> PagingSettings | None:
        return await self._settings.get(organization_id)

    async def configure(
        self,
        organization_id: uuid.UUID,
        *,
        is_enabled: bool,
        primary_number: str | None,
        backup_number: str | None,
        sms_enabled: bool,
        voice_enabled: bool,
        ack_timeout_seconds: int,
    ) -> PagingSettings:
        """Validates and stores the whole configuration, replacing any
        previous one. Raises `InvalidPagingSettingsError` (a 422)."""
        line = await self._voice_lines.get_by_organization_id(organization_id)
        ai_number = normalize_phone_number(line.phone_number) if line is not None else None
        validated = validate_paging_settings(
            organization_id=organization_id,
            is_enabled=is_enabled,
            primary_number=primary_number,
            backup_number=backup_number,
            sms_enabled=sms_enabled,
            voice_enabled=voice_enabled,
            ack_timeout_seconds=ack_timeout_seconds,
            forbidden_numbers=frozenset({ai_number}) if ai_number else frozenset(),
        )
        return await self._settings.upsert(validated)

    async def delete(self, organization_id: uuid.UUID) -> None:
        """Removes the configuration and the stored numbers with it. Pages
        already running keep the numbers they snapshotted; a page that has
        not escalated yet will find no backup and end unresolved."""
        await self._settings.delete(organization_id)
