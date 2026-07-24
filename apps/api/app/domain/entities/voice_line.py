"""Maps an inbound telephony identifier (a Vapi assistant / phone number) to
the organization it belongs to. Without this, an inbound call has no way to
resolve which tenant's Business Knowledge and conversation history it should
use — `BusinessProfile.phone_number` is a display/contact field, not a
routing key.

Provisioning (creating the Vapi assistant, importing the Twilio number) is
an ops-side step done outside this app (Milestone 4 scope decision); a
`VoiceLine` row is created once that's done, and the app only ever reads
it to route inbound webhooks and to show read-only status in Settings."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class VoiceProvider(str, Enum):
    VAPI = "vapi"


@dataclass(frozen=True, slots=True)
class VoiceLine:
    id: uuid.UUID
    organization_id: uuid.UUID
    provider: VoiceProvider
    vapi_assistant_id: str
    vapi_phone_number_id: str | None
    phone_number: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
