from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.repositories.refresh_token_repository import (
    RefreshTokenRecord,
    RefreshTokenRepository,
)
from app.infrastructure.database.models.refresh_token import RefreshTokenModel


class SqlAlchemyRefreshTokenRepository(RefreshTokenRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
        model = RefreshTokenModel(
            id=token_id,
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self._session.add(model)
        await self._session.flush()
        return model.id

    async def get_active_by_hash(self, token_hash: str) -> RefreshTokenRecord | None:
        result = await self._session.execute(
            select(RefreshTokenModel).where(RefreshTokenModel.token_hash == token_hash)
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None

        is_active = model.revoked_at is None and model.expires_at > datetime.now(timezone.utc)
        return RefreshTokenRecord(
            id=model.id, user_id=model.user_id, expires_at=model.expires_at, is_active=is_active
        )

    async def revoke(self, token_id: uuid.UUID, *, replaced_by_id: uuid.UUID | None = None) -> None:
        model = await self._session.get(RefreshTokenModel, token_id)
        if model is not None and model.revoked_at is None:
            model.revoked_at = datetime.now(timezone.utc)
            model.replaced_by_id = replaced_by_id
            await self._session.flush()

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> None:
        result = await self._session.execute(
            select(RefreshTokenModel).where(
                RefreshTokenModel.user_id == user_id, RefreshTokenModel.revoked_at.is_(None)
            )
        )
        now = datetime.now(timezone.utc)
        for model in result.scalars().all():
            model.revoked_at = now
        await self._session.flush()
