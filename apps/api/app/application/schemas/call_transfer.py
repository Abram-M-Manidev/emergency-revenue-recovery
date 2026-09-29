"""Request/response shapes for human-fallback (call transfer) configuration.

Unlike a webhook URL, a transfer number is not a credential, so the numbers
are returned in full — to Owners only (`organization:manage`), who need to
see exactly which line a caller will be put through to. They are still kept
out of prompts, tool results and logs (see `domain/call_transfer/settings.py`).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CallTransferSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    business_hours_number: str | None
    after_hours_number: str | None
    transfer_emergencies: bool
    is_enabled: bool
    created_at: datetime | None
    updated_at: datetime | None


class ConfigureCallTransferRequest(BaseModel):
    """Set (or replace) where this organization's calls may be handed to a
    person. Numbers are validated (E.164, and not the business's own AI
    line) in `CallTransferSettingsService`; lengths are capped here only to
    reject obvious nonsense early."""

    business_hours_number: str | None = Field(default=None, max_length=40)
    after_hours_number: str | None = Field(default=None, max_length=40)
    transfer_emergencies: bool = False
    is_enabled: bool = True
