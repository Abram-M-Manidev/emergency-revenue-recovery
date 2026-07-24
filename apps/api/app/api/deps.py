"""Shared FastAPI dependencies: DB session, service construction, and the
authentication/authorization chain used by every protected route.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.auth_service import AuthService
from app.core.config import Settings, get_settings
from app.domain.entities.user import User
from app.domain.exceptions import AuthorizationError, InvalidTokenError
from app.infrastructure.database.repositories import (
    SqlAlchemyOrganizationRepository,
    SqlAlchemyRefreshTokenRepository,
    SqlAlchemyRoleRepository,
    SqlAlchemyUserRepository,
)
from app.infrastructure.database.session import get_db
from app.infrastructure.security.jwt import decode_access_token

_bearer_scheme = HTTPBearer(auto_error=False)


def get_auth_service(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthService:
    return AuthService(
        user_repository=SqlAlchemyUserRepository(db),
        organization_repository=SqlAlchemyOrganizationRepository(db),
        role_repository=SqlAlchemyRoleRepository(db),
        refresh_token_repository=SqlAlchemyRefreshTokenRepository(db),
        settings=settings,
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    if credentials is None:
        raise InvalidTokenError("Missing bearer token.")

    claims = decode_access_token(credentials.credentials, settings=settings)

    user_repository = SqlAlchemyUserRepository(db)
    user = await user_repository.get_by_id(claims.user_id)
    if user is None or not user.is_active:
        raise InvalidTokenError("Token no longer maps to an active user.")
    return user


def require_permission(permission_code: str) -> Callable[[User], User]:
    """Dependency factory: raises 403 unless the current user holds the
    given permission code (superusers always pass)."""

    def _check(user: User = Depends(get_current_user)) -> User:
        if not user.has_permission(permission_code):
            raise AuthorizationError(
                f"This action requires the '{permission_code}' permission."
            )
        return user

    return _check
