"""Centralized exception handling.

Every error response — validation, domain, auth, or unhandled — is rendered
through the same envelope so API consumers never have to branch on shape:

    {
      "error": {
        "code": "ENTITY_NOT_FOUND",
        "message": "...",
        "details": {...}          # optional, omitted when empty
      },
      "request_id": "..."
    }
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.exceptions import (
    AIProviderUnavailableError,
    AuthorizationError,
    ConversationCompletedError,
    ConversationLimitExceededError,
    DomainError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    InactiveAccountError,
    InvalidCredentialsError,
    InvalidTokenError,
    VoiceLineNotFoundError,
)

logger = structlog.get_logger("app.errors")

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _domain_error_code(exc: DomainError) -> str:
    name = exc.__class__.__name__.removesuffix("Error") or "Domain"
    return _CAMEL_BOUNDARY.sub("_", name).upper()

_DOMAIN_ERROR_STATUS: dict[type[DomainError], int] = {
    EntityNotFoundError: status.HTTP_404_NOT_FOUND,
    EntityAlreadyExistsError: status.HTTP_409_CONFLICT,
    InvalidCredentialsError: status.HTTP_401_UNAUTHORIZED,
    InvalidTokenError: status.HTTP_401_UNAUTHORIZED,
    InactiveAccountError: status.HTTP_403_FORBIDDEN,
    AuthorizationError: status.HTTP_403_FORBIDDEN,
    ConversationCompletedError: status.HTTP_409_CONFLICT,
    ConversationLimitExceededError: status.HTTP_409_CONFLICT,
    AIProviderUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    VoiceLineNotFoundError: status.HTTP_404_NOT_FOUND,
}


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body: dict[str, Any] = {
        "error": {"code": code, "message": message},
        "request_id": request_id,
    }
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        status_code = status.HTTP_400_BAD_REQUEST
        for error_type, mapped_status in _DOMAIN_ERROR_STATUS.items():
            if isinstance(exc, error_type):
                status_code = mapped_status
                break

        if status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.error("domain_error", error=exc.__class__.__name__, message=exc.message)
        else:
            logger.info("domain_error", error=exc.__class__.__name__, message=exc.message)

        return _error_response(
            request,
            status_code=status_code,
            code=_domain_error_code(exc),
            message=exc.message,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic's error dicts include a `ctx` entry that, for a custom
        # validator raising `ValueError(...)` (the documented pattern, used
        # throughout this codebase), holds the raw exception instance —
        # not JSON-serializable. `errors(include_url=False)` still leaves
        # that `ctx`/`input` in place, so we rebuild a minimal, always-safe
        # shape rather than passing pydantic's dicts straight through.
        sanitized_errors = [
            {"type": err["type"], "loc": err["loc"], "msg": err["msg"]} for err in exc.errors()
        ]
        logger.info("validation_error", errors=sanitized_errors)
        return _error_response(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="VALIDATION_ERROR",
            message="One or more fields failed validation.",
            details={"errors": sanitized_errors},
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        logger.info("http_exception", status_code=exc.status_code, detail=exc.detail)
        return _error_response(
            request,
            status_code=exc.status_code,
            code=_code_for_status(exc.status_code),
            message=str(exc.detail),
        )

    @app.exception_handler(Exception)
    async def handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", error=str(exc))
        return _error_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="INTERNAL_SERVER_ERROR",
            message="An unexpected error occurred. Please try again later.",
        )


def _code_for_status(status_code: int) -> str:
    return {
        status.HTTP_400_BAD_REQUEST: "BAD_REQUEST",
        status.HTTP_401_UNAUTHORIZED: "UNAUTHORIZED",
        status.HTTP_403_FORBIDDEN: "FORBIDDEN",
        status.HTTP_404_NOT_FOUND: "NOT_FOUND",
        status.HTTP_409_CONFLICT: "CONFLICT",
        status.HTTP_422_UNPROCESSABLE_ENTITY: "VALIDATION_ERROR",
        status.HTTP_429_TOO_MANY_REQUESTS: "RATE_LIMITED",
    }.get(status_code, "HTTP_ERROR")
