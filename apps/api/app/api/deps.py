"""Shared FastAPI dependencies: DB session, service construction, and the
authentication/authorization chain used by every protected route.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ai_brain_service import AIBrainService
from app.application.services.auth_service import AuthService
from app.application.services.business_knowledge_service import BusinessKnowledgeService
from app.application.services.dispatch_service import DispatchService
from app.application.services.voice_service import VoiceService
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
    SqlAlchemyEmergencyTicketRepository,
    SqlAlchemyFAQRepository,
    SqlAlchemyOrganizationRepository,
    SqlAlchemyRefreshTokenRepository,
    SqlAlchemyRoleRepository,
    SqlAlchemyServiceAreaRepository,
    SqlAlchemyServiceRepository,
    SqlAlchemyTechnicianProfileRepository,
    SqlAlchemyUserRepository,
    SqlAlchemyVoiceCallRepository,
    SqlAlchemyVoiceLineRepository,
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


def get_voice_service(
    db: AsyncSession = Depends(get_db),
    ai_brain_service: AIBrainService = Depends(get_ai_brain_service),
) -> VoiceService:
    return VoiceService(
        voice_line_repository=SqlAlchemyVoiceLineRepository(db),
        voice_call_repository=SqlAlchemyVoiceCallRepository(db),
        conversation_repository=SqlAlchemyConversationRepository(db),
        ai_brain_service=ai_brain_service,
    )


def get_dispatch_service(
    db: AsyncSession = Depends(get_db),
) -> DispatchService:
    return DispatchService(
        emergency_ticket_repository=SqlAlchemyEmergencyTicketRepository(db),
        technician_profile_repository=SqlAlchemyTechnicianProfileRepository(db),
        conversation_outcome_repository=SqlAlchemyConversationOutcomeRepository(db),
        conversation_repository=SqlAlchemyConversationRepository(db),
        user_repository=SqlAlchemyUserRepository(db),
        role_repository=SqlAlchemyRoleRepository(db),
    )


def verify_vapi_secret(
    x_vapi_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Guards the Vapi-facing webhook routes. Deliberately not the JWT
    `get_current_user` chain — these are server-to-server requests from
    Vapi, not an app user. This only proves the request came from our Vapi
    account; it never determines *which* organization the call belongs to
    — that is always resolved separately via `VoiceLineRepository` inside
    `VoiceService`, the same way a valid JWT proves "a real user" while
    RBAC/org-scoping separately proves "which org's data.\""""
    expected = settings.VAPI_SERVER_SECRET
    if not expected or not x_vapi_secret or not hmac.compare_digest(x_vapi_secret, expected):
        raise InvalidTokenError("Missing or invalid Vapi webhook secret.")


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
