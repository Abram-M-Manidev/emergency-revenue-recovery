"""Orchestrates registration, login, token refresh, and logout.

This is the only layer that knows how those flows fit together — it talks
to repositories through their abstract interfaces and to crypto through
the stateless infrastructure/security helpers, but never touches SQLAlchemy
or FastAPI directly, so it can be unit-tested with in-memory fakes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.config import Settings
from app.domain.entities.rbac import OWNER_ROLE_NAME
from app.domain.entities.user import User
from app.domain.exceptions import (
    EntityAlreadyExistsError,
    InactiveAccountError,
    InvalidCredentialsError,
    InvalidTokenError,
)
from app.domain.repositories.organization_repository import OrganizationRepository
from app.domain.repositories.refresh_token_repository import RefreshTokenRepository
from app.domain.repositories.role_repository import RoleRepository
from app.domain.repositories.user_repository import UserRepository
from app.infrastructure.security.jwt import create_access_token
from app.infrastructure.security.password import hash_password, verify_password
from app.infrastructure.security.tokens import (
    hash_token_secret,
    issue_refresh_token,
    parse_refresh_token,
)
from app.shared.utils.slugify import slugify


@dataclass(frozen=True, slots=True)
class IssuedSession:
    user: User
    access_token: str
    access_token_expires_in: int
    refresh_token: str
    refresh_token_id: uuid.UUID
    refresh_token_expires_at: datetime


class AuthService:
    def __init__(
        self,
        *,
        user_repository: UserRepository,
        organization_repository: OrganizationRepository,
        role_repository: RoleRepository,
        refresh_token_repository: RefreshTokenRepository,
        settings: Settings,
    ) -> None:
        self._users = user_repository
        self._organizations = organization_repository
        self._roles = role_repository
        self._refresh_tokens = refresh_token_repository
        self._settings = settings

    async def register(
        self,
        *,
        organization_name: str,
        full_name: str,
        email: str,
        password: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> IssuedSession:
        if await self._users.get_by_email(email) is not None:
            raise EntityAlreadyExistsError("User", "email", email)

        slug = await self._unique_slug(organization_name)
        organization = await self._organizations.create(name=organization_name, slug=slug)

        roles_by_name = await self._roles.seed_default_roles(organization.id)
        owner_role = roles_by_name[OWNER_ROLE_NAME]

        user = await self._users.create(
            organization_id=organization.id,
            email=email,
            hashed_password=hash_password(password),
            full_name=full_name,
            role_ids=[owner_role.id],
        )

        return await self._issue_session(user, user_agent=user_agent, ip_address=ip_address)

    async def login(
        self,
        *,
        email: str,
        password: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> IssuedSession:
        user = await self._users.get_by_email(email)
        if user is None or not verify_password(password, user.hashed_password):
            raise InvalidCredentialsError()
        if not user.is_active:
            raise InactiveAccountError()

        await self._users.record_login(user.id)
        return await self._issue_session(user, user_agent=user_agent, ip_address=ip_address)

    async def refresh(
        self,
        *,
        raw_refresh_token: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> IssuedSession:
        parsed = parse_refresh_token(raw_refresh_token)
        if parsed is None:
            raise InvalidTokenError()
        _, secret = parsed

        record = await self._refresh_tokens.get_active_by_hash(hash_token_secret(secret))
        if record is None or not record.is_active:
            raise InvalidTokenError("Refresh token is invalid, expired, or has been revoked.")

        user = await self._users.get_by_id(record.user_id)
        if user is None or not user.is_active:
            raise InvalidTokenError("Refresh token no longer maps to an active user.")

        session = await self._issue_session(user, user_agent=user_agent, ip_address=ip_address)

        await self._refresh_tokens.revoke(record.id, replaced_by_id=session.refresh_token_id)
        return session

    async def logout(self, *, raw_refresh_token: str) -> None:
        parsed = parse_refresh_token(raw_refresh_token)
        if parsed is None:
            return
        _, secret = parsed
        record = await self._refresh_tokens.get_active_by_hash(hash_token_secret(secret))
        if record is not None:
            await self._refresh_tokens.revoke(record.id)

    async def _issue_session(
        self, user: User, *, user_agent: str | None, ip_address: str | None
    ) -> IssuedSession:
        access_token = create_access_token(
            user_id=user.id,
            organization_id=user.organization_id,
            permissions=user.permission_codes,
            is_superuser=user.is_superuser,
            settings=self._settings,
        )

        issued = issue_refresh_token()
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=self._settings.REFRESH_TOKEN_EXPIRE_DAYS
        )
        await self._refresh_tokens.create(
            token_id=issued.token_id,
            user_id=user.id,
            token_hash=issued.token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )

        return IssuedSession(
            user=user,
            access_token=access_token,
            access_token_expires_in=self._settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            refresh_token=issued.raw_value,
            refresh_token_id=issued.token_id,
            refresh_token_expires_at=expires_at,
        )

    async def _unique_slug(self, organization_name: str) -> str:
        base_slug = slugify(organization_name)
        candidate = base_slug
        suffix = 1
        while await self._organizations.get_by_slug(candidate) is not None:
            suffix += 1
            candidate = f"{base_slug}-{suffix}"
        return candidate
