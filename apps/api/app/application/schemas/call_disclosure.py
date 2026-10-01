"""Request/response shapes for the caller-disclosure policy.

The response carries the exact sentences callers will hear, rendered by the
same functions the live call uses, so an Owner reviewing the setting sees
precisely what is spoken — not a description of it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.disclosure import (
    DisclosurePolicy,
    DisclosureSettings,
    disclosure_sentence,
    opening_message,
)


class CallDisclosureSettingsResponse(BaseModel):
    ai_disclosure_enabled: bool
    recording_notice_enabled: bool
    #: True when nothing has been saved and the default policy applies.
    is_default: bool
    #: Spoken before ERRS's first reply on a call (null: nothing is said).
    disclosure_sentence: str | None
    #: The whole opening, when the Vapi assistant lets ERRS speak first.
    opening_message: str
    updated_at: datetime | None

    @classmethod
    def build(
        cls,
        policy: DisclosurePolicy,
        *,
        business_name: str | None,
        stored: DisclosureSettings | None,
    ) -> CallDisclosureSettingsResponse:
        return cls(
            ai_disclosure_enabled=policy.ai_disclosure,
            recording_notice_enabled=policy.recording_notice,
            is_default=stored is None,
            disclosure_sentence=disclosure_sentence(policy, business_name),
            opening_message=opening_message(policy, business_name),
            updated_at=stored.updated_at if stored else None,
        )


class ConfigureCallDisclosureRequest(BaseModel):
    ai_disclosure_enabled: bool = True
    recording_notice_enabled: bool = True
