from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FAQEntry:
    id: uuid.UUID
    organization_id: uuid.UUID
    question: str
    answer: str
    category: str | None
    is_active: bool
