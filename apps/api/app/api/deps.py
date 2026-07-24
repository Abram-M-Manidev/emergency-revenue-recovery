"""Shared FastAPI dependencies: DB session, service construction, and the
authentication/authorization chain used by every protected route.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ai_brain_service import AIBrainService
from app.application.services.auth_service import AuthService
from app.application.services.business_knowledge_service import BusinessKnowledgeService
from app.core.config import Settings, get_settings
from app.domain.ai.provider import AIProvider
from app.domain.entities.user import User
from app.domain.exceptions import AuthorizationError, InvalidTokenError
from app.infrastructure.ai.openai_provider import OpenAIProvider
from app.infrastructure.database.repositories import (
    SqlAlchemyBusinessHoursRepository,
    SqlAlchemyBusinessProfileRepository,
    SqlAlchemyConversationOutcomeRepository,
    SqlAlchemyConversationRepository,
    SqlAlchemyEmergencyKeywordRepository,
    SqlAlchemyFAQRepository,
    SqlAlchemyOrganizationRepository,
    SqlAlchemyRefreshTokenRepository,
    SqlAlchemyRoleRepository,
    SqlAlchemyServiceAreaRepository,
    SqlAlchemyServiceRepository,
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


def get_business_knowledge_service(
    db: AsyncSession = Depends(get_db),
) -> BusinessKnowledgeService:
    return BusinessKnowledgeService(
        business_profile_repository=SqlAlchemyBusinessProfileRepository(db),
        business_hours_repository=SqlAlchemyBusinessHoursRepository(db),
        service_area_repository=SqlAlchemyServiceAreaRepository(db),
        service_repository=SqlAlchemyServiceRepository(db),
        emergency_keyword_repository=SqlAlchemyEmergencyKeywordRepository(db),
        faq_repository=SqlAlchemyFAQRepository(db),
    )


def get_ai_provider(settings: Settings = Depends(get_settings)) -> AIProvider:
    return OpenAIProvider(settings)


def get_ai_brain_service(
    db: AsyncSession = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
    settings: Settings = Depends(get_settings),
) -> AIBrainService:
    return AIBrainService(
        conversation_repository=SqlAlchemyConversationRepository(db),
        conversation_outcome_repository=SqlAlchemyConversationOutcomeRepository(db),
        ai_provider=ai_provider,
        business_profile_repository=SqlAlchemyBusinessProfileRepository(db),
        business_hours_repository=SqlAlchemyBusinessHoursRepository(db),
        service_repository=SqlAlchemyServiceRepository(db),
        service_area_repository=SqlAlchemyServiceAreaRepository(db),
        faq_repository=SqlAlchemyFAQRepository(db),
        emergency_keyword_repository=SqlAlchemyEmergencyKeywordRepository(db),
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
