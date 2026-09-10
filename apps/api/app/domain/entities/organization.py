from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Organization:
    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    # The per-tenant voice kill switch. Distinct from `is_active`, which
    # disables the whole account including the dashboard: this disables only
    # the inbound phone assistant, so an operator whose AI is misbehaving on
    # one business's line can stop it while that business keeps working its
    # dispatch queue, its appointments, and its customer records.
    #
    # Defaults True so every existing organization keeps the behaviour it
    # has today; turning the assistant off is always an explicit act.
    voice_assistant_enabled: bool = True
