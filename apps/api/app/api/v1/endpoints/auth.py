"""Authentication endpoints.

The access token is returned in the JSON body for the SPA to hold in
memory; the refresh token is set as an httpOnly cookie so it's never
exposed to client-side JavaScript (mitigating XSS token theft).
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, Request, Response, status

from app.api.deps import get_auth_service, get_current_user
from app.application.schemas.auth import (
    AuthResponse,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserProfileResponse,
)
from app.application.services.auth_service import AuthService, IssuedSession
from app.core.config import Settings, get_settings
from app.domain.entities.user import User
from app.domain.exceptions import InvalidTokenError

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE_NAME = "refresh_token"
# Path must be "/" — not just "/api/v1/auth" — because the frontend's edge
# middleware (running against localhost:3000 paths like /dashboard) checks
# for this cookie's presence to gate protected routes. Cookies scope by
# host, not port, so localhost:3000 and localhost:8000 share a jar, but a
# narrower Path here would make the cookie invisible to /dashboard requests
# even though it's present on /api/v1/auth ones — the exact bug that made
# every successful login redirect straight back to /login.
REFRESH_COOKIE_PATH = "/"


def _set_refresh_cookie(response: Response, session: IssuedSession, settings: Settings) -> None:
    max_age = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=session.refresh_token,
        max_age=max_age,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
    )


def _to_profile(user: User) -> UserProfileResponse:
    return UserProfileResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        organization_id=user.organization_id,
        is_superuser=user.is_superuser,
        roles=[role.name for role in user.roles],
        permissions=sorted(user.permission_codes),
    )


def _to_auth_response(session: IssuedSession) -> AuthResponse:
    return AuthResponse(
        tokens=TokenResponse(
            access_token=session.access_token,
            expires_in=session.access_token_expires_in,
        ),
        user=_to_profile(session.user),
    )


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    session = await auth_service.register(
        organization_name=payload.organization_name,
        full_name=payload.full_name,
        email=payload.email,
        password=payload.password,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    _set_refresh_cookie(response, session, settings)
    return _to_auth_response(session)


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    session = await auth_service.login(
        email=payload.email,
        password=payload.password,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    _set_refresh_cookie(response, session, settings)
    return _to_auth_response(session)


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    if refresh_token is None:
        raise InvalidTokenError("No refresh token was provided.")

    session = await auth_service.refresh(
        raw_refresh_token=refresh_token,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    _set_refresh_cookie(response, session, settings)
    return _to_auth_response(session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    auth_service: AuthService = Depends(get_auth_service),
) -> None:
    if refresh_token is not None:
        await auth_service.logout(raw_refresh_token=refresh_token)
    response.delete_cookie(key=REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)


@router.get("/me", response_model=UserProfileResponse)
async def me(user: User = Depends(get_current_user)) -> UserProfileResponse:
    return _to_profile(user)
