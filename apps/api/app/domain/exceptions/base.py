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


class InvalidTicketStatusTransitionError(DomainError):
    def __init__(
        self, message: str = "This status change is not allowed for the ticket's current state."
    ) -> None:
        super().__init__(message)


class InvalidAppointmentStatusTransitionError(DomainError):
    def __init__(
        self,
        message: str = "This status change is not allowed for the appointment's current state.",
    ) -> None:
        super().__init__(message)


class AppointmentOutsideBusinessHoursError(DomainError):
    def __init__(
        self, message: str = "The selected time is outside the organization's business hours."
    ) -> None:
        super().__init__(message)


class AppointmentSlotUnavailableError(DomainError):
    """Raised when a specific appointment time cannot be taken because it is
    already at capacity.

    Distinct from `AppointmentOutsideBusinessHoursError`: that one means the
    business is shut, this one means the business is open and full. The
    caller-facing recovery differs — a closed day needs a different day, a
    full slot needs a different time — and the voice assistant is told to
    offer alternatives only for this one."""

    def __init__(
        self, message: str = "That appointment time is no longer available."
    ) -> None:
        super().__init__(message)


class AppointmentSlotInThePastError(DomainError):
    """Raised when a requested appointment time has already passed.

    Enforced on the AI booking path only. Staff scheduling deliberately does
    not raise this — recording a visit that already happened is a legitimate
    admin action — but an assistant offering a caller a time in the past
    never is."""

    def __init__(
        self, message: str = "That appointment time has already passed."
    ) -> None:
        super().__init__(message)


class SlotNotOfferedError(DomainError):
    """Raised when a booking is attempted for a time this conversation was
    never offered.

    The structural half of "never claim an appointment the caller did not
    choose". A live call booked a slot the caller had not been read: the
    model held a valid time from its prompt and went straight to
    `book_appointment`. Re-checking availability could not catch it — the
    slot really was free — so the check has to be against what was *offered*,
    not what is possible.

    Distinct from `AppointmentSlotUnavailableError`, which means the time was
    legitimately offered and has since been taken. That one invites the
    assistant to offer alternatives; this one means it must go and fetch real
    options first."""

    def __init__(
        self,
        message: str = "That time was not offered to this caller, so it cannot be booked.",
    ) -> None:
        super().__init__(message)


class SlotNotSelectedError(DomainError):
    """Raised when a booking is attempted for a time the caller was offered
    but never chose.

    The second half of appointment consent, and the one `SlotNotOfferedError`
    could not cover. A real-model run offered three times and booked one in
    the same turn: every one of those was genuinely offered, so an
    offered-only check approved the write even though the caller had not
    spoken since hearing them.

    Distinct from `SlotNotOfferedError` because the recovery differs. Not
    offered means the assistant is holding an invented time and must go and
    fetch real ones. Not selected means the times are real and already spoken
    — it must simply ask the caller which of them they want, and wait for the
    answer."""

    def __init__(
        self,
        message: str = "The caller has not chosen that time, so it cannot be booked.",
    ) -> None:
        super().__init__(message)


class AvailabilityUnavailableError(DomainError):
    """Raised when an availability search is attempted without a configured
    `AvailabilityProvider`.

    Deliberately not silently degraded to "no slots": an empty result means
    the business is fully booked, while this means nothing was ever checked.
    Conflating the two would let the assistant tell a caller there is no
    availability on the strength of a wiring mistake."""

    def __init__(
        self,
        message: str = "Appointment availability is not configured. Contact your administrator.",
    ) -> None:
        super().__init__(message)


class LastOwnerError(DomainError):
    def __init__(
        self,
        message: str = "An organization must always retain at least one active Owner.",
    ) -> None:
        super().__init__(message)


class VoiceAssistantDisabledError(DomainError):
    """Raised when an inbound call reaches an organization whose voice
    assistant has been switched off.

    Deliberately distinct from `VoiceLineNotFoundError`, which means the
    line maps to no tenant at all. Both end the call politely, but they are
    different operational facts and the caller deserves different words: an
    unmapped line is a misconfiguration nobody knows about, while a disabled
    assistant is a deliberate act by that business, and the caller should be
    pointed at a human rather than told the number does not work.

    Enforced in `VoiceService`, on the shared path both the streaming and
    non-streaming transports take, so the switch cannot be true for one and
    false for the other."""

    def __init__(
        self,
        message: str = "This organization's voice assistant is currently disabled.",
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
