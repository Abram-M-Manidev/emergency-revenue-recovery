"""System-wide permission catalogue and the default role templates every
new organization is seeded with.

Business-feature permissions (calls, tickets, appointments, ...) are added
in the milestones that introduce those features — this file only covers
what Milestone 1 needs to prove RBAC works: managing the org itself and
its users.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PermissionDefinition:
    code: str
    description: str


class Permissions:
    ORGANIZATION_MANAGE = "organization:manage"
    USERS_READ = "users:read"
    USERS_MANAGE = "users:manage"


PERMISSION_CATALOGUE: tuple[PermissionDefinition, ...] = (
    PermissionDefinition(Permissions.ORGANIZATION_MANAGE, "Manage organization settings"),
    PermissionDefinition(Permissions.USERS_READ, "View users within the organization"),
    PermissionDefinition(Permissions.USERS_MANAGE, "Invite, edit, and deactivate users"),
)

DEFAULT_ROLES: dict[str, tuple[str, ...]] = {
    "Owner": (
        Permissions.ORGANIZATION_MANAGE,
        Permissions.USERS_READ,
        Permissions.USERS_MANAGE,
    ),
    "Admin": (
        Permissions.USERS_READ,
        Permissions.USERS_MANAGE,
    ),
    "Member": (
        Permissions.USERS_READ,
    ),
}

OWNER_ROLE_NAME = "Owner"
