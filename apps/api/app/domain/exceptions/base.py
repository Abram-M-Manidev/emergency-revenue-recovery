"""Domain-level exceptions.

These carry no knowledge of HTTP — they express failures in business terms.
The API layer (app/core/exception_handlers.py) is responsible for mapping
each of these to the appropriate HTTP response.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain/application errors."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class EntityNotFoundError(DomainError):
    def __init__(self, entity: str, identifier: str) -> None:
        self.entity = entity
        self.identifier = identifier
        super().__init__(f"{entity} with identifier '{identifier}' was not found.")


class EntityAlreadyExistsError(DomainError):
    def __init__(self, entity: str, field: str, value: str) -> None:
        self.entity = entity
        self.field = field
        self.value = value
        super().__init__(f"{entity} with {field} '{value}' already exists.")


class InvalidCredentialsError(DomainError):
    def __init__(self, message: str = "Invalid email or password.") -> None:
        super().__init__(message)


class InactiveAccountError(DomainError):
    def __init__(self, message: str = "This account is inactive.") -> None:
        super().__init__(message)


class InvalidTokenError(DomainError):
    def __init__(self, message: str = "Token is invalid or has expired.") -> None:
        super().__init__(message)


class AuthorizationError(DomainError):
    def __init__(self, message: str = "You do not have permission to perform this action.") -> None:
        super().__init__(message)
