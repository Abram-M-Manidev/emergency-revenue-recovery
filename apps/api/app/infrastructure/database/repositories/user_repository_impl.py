from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.entities.role import Role
from app.domain.entities.user import User
from app.domain.repositories.user_repository import UserRepository
from app.infrastructure.database.models.role import RoleModel
from app.infrastructure.database.models.user import UserModel


def _role_to_entity(model: RoleModel) -> Role:
    return Role(
        id=model.id,
        organization_id=model.organization_id,
        name=model.name,
        description=model.description,
        is_system_role=model.is_system_role,
        permission_codes=frozenset(p.code for p in model.permissions),
    )


def _to_entity(model: UserModel) -> User:
    return User(
        id=model.id,
        organization_id=model.organization_id,
        email=model.email,
        hashed_password=model.hashed_password,
        full_name=model.full_name,
        is_active=model.is_active,
        is_superuser=model.is_superuser,
        created_at=model.created_at,
        updated_at=model.updated_at,
        last_login_at=model.last_login_at,
        roles=tuple(_role_to_entity(r) for r in model.roles),
    )


_LOAD_OPTIONS = (selectinload(UserModel.roles).selectinload(RoleModel.permissions),)


class SqlAlchemyUserRepository(UserRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self._session.execute(
            select(UserModel).where(UserModel.id == user_id).options(*_LOAD_OPTIONS)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(
            select(UserModel).where(UserModel.email == email).options(*_LOAD_OPTIONS)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        email: str,
        hashed_password: str,
        full_name: str,
        role_ids: list[uuid.UUID],
        is_superuser: bool = False,
    ) -> User:
        roles: list[RoleModel] = []
        if role_ids:
            result = await self._session.execute(
                select(RoleModel).where(RoleModel.id.in_(role_ids))
            )
            roles = list(result.scalars().all())

        model = UserModel(
            organization_id=organization_id,
            email=email,
            hashed_password=hashed_password,
            full_name=full_name,
            is_superuser=is_superuser,
            roles=roles,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model, attribute_names=["roles"])
        for role in model.roles:
            await self._session.refresh(role, attribute_names=["permissions"])
        return _to_entity(model)

    async def record_login(self, user_id: uuid.UUID) -> None:
        model = await self._session.get(UserModel, user_id)
        if model is not None:
            model.last_login_at = datetime.now(timezone.utc)
            await self._session.flush()

    async def list_by_organization_id(self, organization_id: uuid.UUID) -> list[User]:
        result = await self._session.execute(
            select(UserModel)
            .where(UserModel.organization_id == organization_id)
            .options(*_LOAD_OPTIONS)
            .order_by(UserModel.created_at.asc())
        )
        return [_to_entity(model) for model in result.scalars().all()]

    async def set_active(self, user_id: uuid.UUID, *, is_active: bool) -> User:
        result = await self._session.execute(
            select(UserModel).where(UserModel.id == user_id).options(*_LOAD_OPTIONS)
        )
        model = result.scalar_one()
        model.is_active = is_active
        await self._session.flush()
        # `updated_at` has `onupdate=func.now()` (server-computed), so the
        # flush above expires it — a plain column, not "roles" (untouched
        # here), needs the refresh.
        await self._session.refresh(model, attribute_names=["updated_at"])
        return _to_entity(model)

    async def set_roles(self, user_id: uuid.UUID, *, role_ids: list[uuid.UUID]) -> User:
        result = await self._session.execute(
            select(UserModel).where(UserModel.id == user_id).options(*_LOAD_OPTIONS)
        )
        model = result.scalar_one()
        roles: list[RoleModel] = []
        if role_ids:
            role_result = await self._session.execute(
                select(RoleModel).where(RoleModel.id.in_(role_ids))
            )
            roles = list(role_result.scalars().all())
        model.roles = roles
        await self._session.flush()
        await self._session.refresh(model, attribute_names=["roles"])
        for role in model.roles:
            await self._session.refresh(role, attribute_names=["permissions"])
        return _to_entity(model)
