from app.domain.exceptions.base import (
    AIProviderUnavailableError,
    AuthorizationError,
    ConversationCompletedError,
    ConversationLimitExceededError,
    DomainError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    InactiveAccountError,
    InvalidCredentialsError,
    InvalidTicketStatusTransitionError,
    InvalidTokenError,
    VoiceLineNotFoundError,
)

__all__ = [
    "AIProviderUnavailableError",
    "AuthorizationError",
    "ConversationCompletedError",
    "ConversationLimitExceededError",
    "DomainError",
    "EntityAlreadyExistsError",
    "EntityNotFoundError",
    "InactiveAccountError",
    "InvalidCredentialsError",
    "InvalidTicketStatusTransitionError",
    "InvalidTokenError",
    "VoiceLineNotFoundError",
]
