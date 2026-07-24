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


class ConversationCompletedError(DomainError):
    def __init__(self, message: str = "This conversation has already ended.") -> None:
        super().__init__(message)


class ConversationLimitExceededError(DomainError):
    def __init__(
        self, message: str = "This conversation has reached its maximum number of turns."
    ) -> None:
        super().__init__(message)


class AIProviderUnavailableError(DomainError):
    def __init__(
        self, message: str = "The AI Brain is not configured. Contact your administrator."
    ) -> None:
        super().__init__(message)


class VoiceLineNotFoundError(DomainError):
    """Raised when an inbound call's assistant/phone number id doesn't map
    to any configured organization. The Vapi webhook endpoint (a voice
    agent, not a JSON API consumer) catches this itself and responds with a
    speakable fallback message instead of an error envelope — see
    `app/api/v1/endpoints/vapi_webhooks.py`. Still registered with the
    generic domain-error handler (`app/core/errors.py`) as a defensive
    fallback for any other caller."""

    def __init__(self, message: str = "No organization is configured for this phone line.") -> None:
        super().__init__(message)
