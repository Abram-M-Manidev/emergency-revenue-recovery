"""Cross-cutting HTTP middleware.

Kept deliberately thin: each middleware has exactly one responsibility so
new cross-cutting concerns (rate limiting, tracing, etc.) can be added in
future milestones without touching existing ones.

`SecurityHeadersMiddleware`, `RateLimitMiddleware`, and
`MaxBodySizeMiddleware` (Milestone 10) never raise `HTTPException` to
signal rejection — they build the response via `app.core.errors.error_response`
directly instead. Middleware added through `app.add_middleware()` sits
*outside* Starlette's `ExceptionMiddleware` (which is what actually
dispatches to `@app.exception_handler(HTTPException)`), so an exception
raised here would only ever reach the generic 500 handler, never the
specific status code intended.
"""

from __future__ import annotations

import time
import uuid

import structlog
from fastapi import status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import get_settings
from app.core.errors import error_response
from app.domain.exceptions import InvalidTokenError
from app.infrastructure.security.jwt import decode_access_token
from app.infrastructure.security.rate_limiter import InMemoryRateLimiter

REQUEST_ID_HEADER = "X-Request-ID"
_RATE_LIMIT_WINDOW_SECONDS = 60.0

logger = structlog.get_logger("app.request")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attaches a unique request id to every request/response pair.

    The id is bound into structlog's contextvars so every log line emitted
    while handling the request — from any layer — automatically includes it.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER, str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs one structured event per request with method, path, status, and latency."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start_time = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds baseline security response headers to every response.

    Deliberately does not set a Content-Security-Policy: a real CSP needs
    per-route nonce wiring through Next.js middleware and an audit of
    every inline style/script path on the frontend — out of scope for
    this pass (see the Milestone 10 plan's non-goals).
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        if get_settings().is_production:
            # Meaningless (and potentially harmful if the deployment is ever
            # briefly served over plain HTTP) outside a real production/TLS
            # deployment, so it's scoped to production only.
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Rejects a request whose declared `Content-Length` exceeds
    `settings.MAX_REQUEST_BODY_BYTES`, before the body is ever read."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None

            if declared_size is not None and declared_size > settings.MAX_REQUEST_BODY_BYTES:
                logger.info(
                    "request_body_too_large",
                    declared_size=declared_size,
                    limit=settings.MAX_REQUEST_BODY_BYTES,
                )
                return error_response(
                    request,
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    code="REQUEST_ENTITY_TOO_LARGE",
                    message="Request body exceeds the maximum allowed size.",
                )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limiting, keyed by the authenticated user (JWT
    subject) when present, else the client IP. `/api/v1/auth/*` gets a
    stricter tier (brute-force deterrent); everything else gets a looser
    default tier (abuse/cost-runaway safety net). Health-check routes are
    exempt so orchestrator liveness/readiness probes are never throttled.

    Bypassed entirely in the test environment (`settings.is_testing`) —
    the same special-casing `app/infrastructure/database/session.py`'s
    `create_engine()` already applies for a different reason. Without
    this, the ~15 integration test files that each register/log in at
    least once would collectively exceed the auth-tier limit within a
    single fast pytest run (they all share one client identity, since
    register/login are unauthenticated and the test client's IP never
    changes), throttling unrelated tests. Real enforcement is covered by
    `tests/unit/test_rate_limit_middleware.py`, which drives this
    middleware directly against a small standalone app with production-
    like settings rather than through the shared test app.
    """

    def __init__(self, app: ASGIApp, *, limiter: InMemoryRateLimiter | None = None) -> None:
        super().__init__(app)
        self._limiter = limiter or InMemoryRateLimiter()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        if settings.is_testing:
            return await call_next(request)

        path = request.url.path
        if path.startswith(f"{settings.API_V1_PREFIX}/health"):
            return await call_next(request)

        is_auth_path = path.startswith(f"{settings.API_V1_PREFIX}/auth")
        tier = "auth" if is_auth_path else "default"
        limit = (
            settings.RATE_LIMIT_AUTH_PER_MINUTE
            if is_auth_path
            else settings.RATE_LIMIT_DEFAULT_PER_MINUTE
        )
        key = f"{tier}:{self._identify(request)}"

        allowed = self._limiter.check(key, limit=limit, window_seconds=_RATE_LIMIT_WINDOW_SECONDS)
        if not allowed:
            logger.info("rate_limit_exceeded", tier=tier, path=path)
            response = error_response(
                request,
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="RATE_LIMITED",
                message="Too many requests. Please slow down and try again shortly.",
            )
            response.headers["Retry-After"] = str(int(_RATE_LIMIT_WINDOW_SECONDS))
            return response

        return await call_next(request)

    @staticmethod
    def _identify(request: Request) -> str:
        """Prefers the JWT subject, so a user's own limit follows them
        across IPs/devices; falls back to the client IP for anonymous
        requests. A missing/invalid token isn't an error at this layer —
        it just falls back to IP, same as no token at all. The actual 401
        for a bad token still comes from the normal auth dependency chain
        downstream."""
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[len("Bearer ") :]
            try:
                claims = decode_access_token(token, settings=get_settings())
                return f"user:{claims.user_id}"
            except InvalidTokenError:
                pass
        client = request.client
        return f"ip:{client.host}" if client else "ip:unknown"
