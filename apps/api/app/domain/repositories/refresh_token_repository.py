from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RefreshTokenRecord:
    id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime
    is_active: bool


class RefreshTokenRepository(ABC):
    @abstractmethod
    async def create(
        self,
        *,
        token_id: uuid.UUID,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> uuid.UUID:
        """Persist a new refresh token under `token_id` and return it.

        `token_id` must be caller-supplied (from `issue_refresh_token()`)
        rather than generated here — it's already embedded in the
        client-visible token string, and rotation (`revoke(...,
        replaced_by_id=...)`) depends on that id matching this row's actual
        primary key.
        """
        ...

    @abstractmethod
    async def get_active_by_hash(self, token_hash: str) -> RefreshTokenRecord | None: ...

    @abstractmethod
    async def revoke(self, token_id: uuid.UUID, *, replaced_by_id: uuid.UUID | None = None) -> None: ...

    @abstractmethod
    async def revoke_all_for_user(self, user_id: uuid.UUID) -> None: ...
