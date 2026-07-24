"""Import every model here so `Base.metadata` is fully populated for Alembic
autogenerate and for `Base.metadata.create_all()` in tests."""

from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.refresh_token import RefreshTokenModel
from app.infrastructure.database.models.role import (
    PermissionModel,
    RoleModel,
    role_permissions,
    user_roles,
)
from app.infrastructure.database.models.user import UserModel

__all__ = [
    "OrganizationModel",
    "PermissionModel",
    "RefreshTokenModel",
    "RoleModel",
    "UserModel",
    "role_permissions",
    "user_roles",
]
