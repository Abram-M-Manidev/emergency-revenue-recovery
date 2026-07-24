"""Flat per-org keyword/phrase list used to flag a call as an emergency.

Deliberately simple for this milestone (no severity taxonomy) — the AI
Brain milestone can layer smarter matching on top of this list later."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmergencyKeyword:
    id: uuid.UUID
    organization_id: uuid.UUID
    phrase: str
    notes: str | None
