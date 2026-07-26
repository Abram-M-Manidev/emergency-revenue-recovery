"""backfill customers permissions

Revision ID: b3c4d5e6f7a8
Revises: a7b8c9d0e1f2
Create Date: 2026-07-26 09:10:00.000000

Permissions are persisted as rows at org-registration time
(`RoleRepository.seed_default_roles`), not re-derived from `DEFAULT_ROLES`
at check time — `User.has_permission` only ever reads a role's
already-persisted `permission_codes`. Adding `customers:read`/
`customers:manage` to `app/domain/entities/rbac.py`'s `DEFAULT_ROLES` only
affects organizations registered *after* this ships. This data migration
backfills both permissions onto every existing system role
(Owner/Admin/Member/Technician) across every organization created before
Milestone 7 — Owner/Admin get both, Member/Technician get read-only,
matching the shipped `DEFAULT_ROLES` entries exactly.

Idempotent (safe to re-run): both the permission-row lookup and the
role<->permission link insert check for an existing row first.
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_PERMISSIONS: dict[str, str] = {
    "customers:read": "View customer records and history",
    "customers:manage": "Create and edit customer records",
}

_ROLE_PERMISSION_MAP: dict[str, list[str]] = {
    "Owner": ["customers:read", "customers:manage"],
    "Admin": ["customers:read", "customers:manage"],
    "Member": ["customers:read"],
    "Technician": ["customers:read"],
}


def upgrade() -> None:
    conn = op.get_bind()

    permissions_t = sa.table(
        "permissions",
        sa.column("id", sa.UUID()),
        sa.column("code", sa.String()),
        sa.column("description", sa.String()),
    )
    roles_t = sa.table(
        "roles",
        sa.column("id", sa.UUID()),
        sa.column("name", sa.String()),
        sa.column("is_system_role", sa.Boolean()),
    )
    role_permissions_t = sa.table(
        "role_permissions",
        sa.column("role_id", sa.UUID()),
        sa.column("permission_id", sa.UUID()),
    )

    permission_ids: dict[str, uuid.UUID] = {}
    for code, description in _NEW_PERMISSIONS.items():
        existing_id = conn.execute(
            sa.select(permissions_t.c.id).where(permissions_t.c.code == code)
        ).scalar_one_or_none()
        if existing_id is None:
            existing_id = uuid.uuid4()
            conn.execute(
                permissions_t.insert().values(id=existing_id, code=code, description=description)
            )
        permission_ids[code] = existing_id

    rows = conn.execute(
        sa.select(roles_t.c.id, roles_t.c.name).where(
            roles_t.c.is_system_role.is_(True),
            roles_t.c.name.in_(_ROLE_PERMISSION_MAP.keys()),
        )
    ).fetchall()
    for role_id, role_name in rows:
        for code in _ROLE_PERMISSION_MAP[role_name]:
            perm_id = permission_ids[code]
            already_linked = conn.execute(
                sa.select(role_permissions_t.c.role_id).where(
                    role_permissions_t.c.role_id == role_id,
                    role_permissions_t.c.permission_id == perm_id,
                )
            ).scalar_one_or_none()
            if already_linked is None:
                conn.execute(
                    role_permissions_t.insert().values(role_id=role_id, permission_id=perm_id)
                )


def downgrade() -> None:
    conn = op.get_bind()

    permissions_t = sa.table(
        "permissions", sa.column("id", sa.UUID()), sa.column("code", sa.String())
    )
    role_permissions_t = sa.table(
        "role_permissions",
        sa.column("role_id", sa.UUID()),
        sa.column("permission_id", sa.UUID()),
    )

    codes = list(_NEW_PERMISSIONS.keys())
    permission_ids = conn.execute(
        sa.select(permissions_t.c.id).where(permissions_t.c.code.in_(codes))
    ).scalars().all()

    if permission_ids:
        conn.execute(
            role_permissions_t.delete().where(
                role_permissions_t.c.permission_id.in_(permission_ids)
            )
        )
        conn.execute(permissions_t.delete().where(permissions_t.c.id.in_(permission_ids)))
