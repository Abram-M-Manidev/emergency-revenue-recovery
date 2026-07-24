from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.user import User


class UserRepository(ABC):
    @abstractmethod
    async def get_by_id(self, user_id: uuid.UUID) -> User | None: ...

    @abstractmethod
    async def get_by_email(self, email: str) -> User | None: ...

    @abstractmethod
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        email: str,
        hashed_password: str,
        full_name: str,
        role_ids: list[uuid.UUID],
        is_superuser: bool = False,
    ) -> User: ...

    @abstractmethod
    async def record_login(self, user_id: uuid.UUID) -> None: ...
