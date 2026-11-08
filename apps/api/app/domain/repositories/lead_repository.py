"""Early-access requests from the public marketing site.

Not tenant data: a lead belongs to the platform operator, not to any
organization, and no tenant-facing API reads it. One row per email address
(case-insensitive); a repeat request refreshes the row instead of adding a
second one, so a person who submits twice is still one lead and the public
endpoint never has to say whether an address was already known.

Consent is stored with the exact notice version the person agreed to, so a
later change to the wording never silently re-dates an old agreement."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Lead:
    id: uuid.UUID
    name: str
    business_name: str
    email: str
    business_type: str
    consent_version: str
    consented_at: datetime
    request_count: int
    created_at: datetime
    updated_at: datetime


class LeadRepository(ABC):
    @abstractmethod
    async def record(
        self,
        *,
        name: str,
        business_name: str,
        email: str,
        business_type: str,
        consent_version: str,
        consented_at: datetime,
    ) -> Lead:
        """Inserts, or refreshes the existing row for the same email."""
        ...

    @abstractmethod
    async def list_all(self, *, limit: int) -> list[Lead]: ...
