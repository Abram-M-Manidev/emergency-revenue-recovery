from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.entities.rbac import DEFAULT_ROLES, PERMISSION_CATALOGUE
from app.domain.entities.role import Role
from app.domain.repositories.role_repository import RoleRepository
from app.infrastructure.database.models.role import PermissionModel, RoleModel


def _to_entity(model: RoleModel) -> Role:
    return Role(
        id=model.id,
        organization_id=model.organization_id,
        name=model.name,
        description=model.description,
        is_system_role=model.is_system_role,
        permission_codes=frozenset(p.code for p in model.permissions),
    )


class SqlAlchemyRoleRepository(RoleRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _get_or_create_permissions(self, codes: tuple[str, ...]) -> dict[str, PermissionModel]:
        result = await self._session.execute(
            select(PermissionModel).where(PermissionModel.code.in_(codes))
        )
        existing = {p.code: p for p in result.scalars().all()}

        catalogue_by_code = {p.code: p for p in PERMISSION_CATALOGUE}
        for code in codes:
            if code not in existing:
                definition = catalogue_by_code[code]
                model = PermissionModel(code=definition.code, description=definition.description)
                self._session.add(model)
                existing[code] = model
        await self._session.flush()
        return existing

    async def seed_default_roles(self, organization_id: uuid.UUID) -> dict[str, Role]:
        all_codes = tuple({code for codes in DEFAULT_ROLES.values() for code in codes})
        permissions_by_code = await self._get_or_create_permissions(all_codes)

        created: dict[str, Role] = {}
        for role_name, codes in DEFAULT_ROLES.items():
            model = RoleModel(
                organization_id=organization_id,
                name=role_name,
                description=f"{role_name} role",
                is_system_role=True,
                permissions=[permissions_by_code[code] for code in codes],
            )
            self._session.add(model)
            await self._session.flush()
            await self._session.refresh(model, attribute_names=["permissions"])
            created[role_name] = _to_entity(model)
        return created

    async def get_by_ids(self, role_ids: list[uuid.UUID]) -> list[Role]:
        if not role_ids:
            return []
        result = await self._session.execute(
            select(RoleModel)
            .where(RoleModel.id.in_(role_ids))
            .options(selectinload(RoleModel.permissions))
        )
        return [_to_entity(model) for model in result.scalars().all()]
