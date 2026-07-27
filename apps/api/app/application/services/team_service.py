"""Orchestrates Milestone 9's Team management: letting an Owner/Admin add,
list, deactivate/reactivate, and re-role the other users in their own
organization.

`Permissions.USERS_READ`/`USERS_MANAGE` have existed in the RBAC catalogue
since Milestone 1's `seed_default_roles` and are already correctly seeded
onto every existing org's Owner/Admin roles — this service is the first
thing to actually consume them.

Invitation reuses the exact temporary-password pattern
`DispatchService.create_technician` established in Milestone 5 (an
Owner/Admin sets a password directly; there is no email infrastructure
anywhere in this codebase to send an invite link instead). Unlike
Dispatch, this service only ever assigns the `Owner`/`Admin`/`Member`
roles — `Technician` remains exclusively `POST /dispatch/technicians`'s
responsibility, since that endpoint also creates the `TechnicianProfile`
row this service has no business creating."""

from __future__ import annotations

import uuid
from typing import Literal

from app.domain.entities.rbac import DEFAULT_ROLES, OWNER_ROLE_NAME
from app.domain.entities.user import User
from app.domain.exceptions import (
    AuthorizationError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    LastOwnerError,
)
from app.domain.repositories.role_repository import RoleRepository
from app.domain.repositories.user_repository import UserRepository
from app.infrastructure.security.password import hash_password

InvitableRole = Literal["Admin", "Member"]
AssignableRole = Literal["Owner", "Admin", "Member"]

_INVITABLE_ROLES: tuple[str, ...] = ("Admin", "Member")
_ASSIGNABLE_ROLES: tuple[str, ...] = ("Owner", "Admin", "Member")


class TeamService:
    def __init__(
        self,
        *,
        user_repository: UserRepository,
        role_repository: RoleRepository,
    ) -> None:
        self._users = user_repository
        self._roles = role_repository

    async def list_members(self, organization_id: uuid.UUID) -> list[User]:
        return await self._users.list_by_organization_id(organization_id)

    async def invite_member(
        self,
        organization_id: uuid.UUID,
        *,
        full_name: str,
        email: str,
        temporary_password: str,
        role_name: InvitableRole,
    ) -> User:
        if role_name not in _INVITABLE_ROLES:
            raise AuthorizationError(
                "New members can only be invited as Admin or Member. "
                "Promote to Owner afterwards if needed."
            )
        if await self._users.get_by_email(email) is not None:
            raise EntityAlreadyExistsError("User", "email", email)

        role = await self._roles.get_or_create_by_name(
            organization_id, role_name, DEFAULT_ROLES[role_name]
        )
        return await self._users.create(
            organization_id=organization_id,
            email=email,
            hashed_password=hash_password(temporary_password),
            full_name=full_name,
            role_ids=[role.id],
        )

    async def set_member_active(
        self,
        organization_id: uuid.UUID,
        target_user_id: uuid.UUID,
        *,
        is_active: bool,
        acting_user_id: uuid.UUID,
    ) -> User:
        target = await self._get_member(organization_id, target_user_id)

        if not is_active:
            if target_user_id == acting_user_id:
                raise AuthorizationError("You cannot deactivate your own account.")
            if self._is_owner(target):
                await self._ensure_other_active_owner_exists(organization_id, target.id)

        return await self._users.set_active(target.id, is_active=is_active)

    async def set_member_role(
        self,
        organization_id: uuid.UUID,
        target_user_id: uuid.UUID,
        *,
        role_name: AssignableRole,
        acting_user_id: uuid.UUID,
    ) -> User:
        if role_name not in _ASSIGNABLE_ROLES:
            raise AuthorizationError(
                "The Technician role is managed from Dispatch's technician "
                "roster, not from Team."
            )

        target = await self._get_member(organization_id, target_user_id)

        if role_name != OWNER_ROLE_NAME and self._is_owner(target):
            await self._ensure_other_active_owner_exists(organization_id, target.id)

        role = await self._roles.get_or_create_by_name(
            organization_id, role_name, DEFAULT_ROLES[role_name]
        )
        return await self._users.set_roles(target.id, role_ids=[role.id])

    async def _get_member(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None or user.organization_id != organization_id:
            # Cross-tenant id: from the caller's point of view, another
            # org's user simply doesn't exist — same convention as
            # CustomerService.get_customer / DispatchService's technician
            # ownership checks.
            raise EntityNotFoundError("User", str(user_id))
        return user

    @staticmethod
    def _is_owner(user: User) -> bool:
        return any(role.name == OWNER_ROLE_NAME for role in user.roles)

    async def _ensure_other_active_owner_exists(
        self, organization_id: uuid.UUID, excluding_user_id: uuid.UUID
    ) -> None:
        members = await self._users.list_by_organization_id(organization_id)
        other_active_owners = [
            member
            for member in members
            if member.id != excluding_user_id and member.is_active and self._is_owner(member)
        ]
        if not other_active_owners:
            raise LastOwnerError()
