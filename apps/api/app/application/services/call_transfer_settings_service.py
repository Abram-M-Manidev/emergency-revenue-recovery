"""Owner-managed configuration of where calls may be handed to a person."""

from __future__ import annotations

import uuid

from app.domain.call_transfer.settings import (
    CallTransferSettings,
    InvalidTransferNumberError,
    validate_transfer_number,
)
from app.domain.repositories.call_transfer_repository import CallTransferSettingsRepository
from app.domain.repositories.voice_line_repository import VoiceLineRepository
from app.shared.utils.phone import normalize_phone_number


class CallTransferSettingsService:
    def __init__(
        self,
        settings_repository: CallTransferSettingsRepository,
        voice_line_repository: VoiceLineRepository,
    ) -> None:
        self._settings = settings_repository
        self._voice_lines = voice_line_repository

    async def get(self, organization_id: uuid.UUID) -> CallTransferSettings | None:
        return await self._settings.get(organization_id)

    async def configure(
        self,
        organization_id: uuid.UUID,
        *,
        business_hours_number: str | None,
        after_hours_number: str | None,
        transfer_emergencies: bool,
        is_enabled: bool,
    ) -> CallTransferSettings:
        line = await self._voice_lines.get_by_organization_id(organization_id)
        ai_number = normalize_phone_number(line.phone_number) if line is not None else None
        forbidden = frozenset({ai_number}) if ai_number else frozenset()
        office = validate_transfer_number(business_hours_number, forbidden=forbidden)
        on_call = validate_transfer_number(after_hours_number, forbidden=forbidden)
        if is_enabled and office is None and on_call is None:
            raise InvalidTransferNumberError(
                "Enter at least one number (office or on-call), or switch human transfer off."
            )
        return await self._settings.upsert(
            CallTransferSettings(
                organization_id=organization_id,
                business_hours_number=office,
                after_hours_number=on_call,
                transfer_emergencies=transfer_emergencies,
                is_enabled=is_enabled,
            )
        )

    async def delete(self, organization_id: uuid.UUID) -> None:
        await self._settings.delete(organization_id)
