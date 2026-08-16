"""The provider abstraction (per ARCHITECTURE.md) for the AI Brain's
language model calls. `AIBrainService` (application layer) depends only on
this interface; `OpenAIProvider` (infrastructure layer) is the only thing
that knows about OpenAI's SDK, request/response shapes, or model names.

Kept in `domain` rather than `application` because it has zero framework or
infrastructure imports — it is exactly as framework-free as a repository
interface, just not backed by a database."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum

from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction


class AIModelProfile(str, Enum):
    """Which trade-off the caller of this provider needs, expressed as
    intent rather than as a model name.

    The domain deliberately does not know that "quality" means gpt-5 or
    that "realtime" means gpt-4.1-mini — that mapping is configuration,
    owned by the infrastructure layer (`OpenAIProvider`), exactly like the
    SDK and request shapes already are. Adding a provider or renaming a
    model must never require a change here."""

    QUALITY = "quality"
    """Accuracy over latency. Backs the text/simulation dashboard."""

    REALTIME = "realtime"
    """Latency over depth. Backs live phone calls, where response time is
    silence the caller is listening to."""


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    role: str  # "customer" | "assistant" — kept as plain str to avoid a
    # domain->domain.entities.conversation_message import for what is, from
    # the provider's point of view, just a role label in a transcript.
    content: str


@dataclass(frozen=True, slots=True)
class AIRequest:
    system_prompt: str
    history: tuple[ConversationTurn, ...]
    latest_customer_message: str
    # Defaults to QUALITY so any caller that doesn't care about latency
    # keeps the pre-existing behaviour without opting in.
    profile: AIModelProfile = AIModelProfile.QUALITY


@dataclass(frozen=True, slots=True)
class AIReply:
    message_to_customer: str
    classification: CallClassification
    confidence: float
    recommended_action: RecommendedAction
    matched_service_name: str | None
    customer_name: str | None
    customer_phone: str | None
    customer_address: str | None
    is_conversation_complete: bool
    summary: str


@dataclass(frozen=True, slots=True)
class AITextDelta:
    """A fragment of `message_to_customer` as the model produces it.

    Only the caller-facing sentence is ever streamed. Every decision field
    (`classification`, `recommended_action`, `is_conversation_complete`, ...)
    is withheld until the complete response has been assembled and
    validated, because a half-parsed decision is worse than a late one — it
    could hang up on a caller mid-emergency."""

    text: str


@dataclass(frozen=True, slots=True)
class AIReplyComplete:
    """Terminal event: the fully assembled, schema-validated reply. Exactly
    one of these ends a successful stream, and it is the only authoritative
    source of the decision fields."""

    reply: AIReply


AIStreamEvent = AITextDelta | AIReplyComplete


class AIProvider(ABC):
    @abstractmethod
    async def generate_reply(self, request: AIRequest) -> AIReply: ...

    async def stream_reply(self, request: AIRequest) -> AsyncIterator[AIStreamEvent]:
        """Incremental variant of `generate_reply`.

        The default implementation is deliberately non-streaming: it awaits
        the complete reply and emits it as a single delta followed by the
        terminal event. That keeps every existing provider — including the
        test fakes — correct without modification, and means a provider
        that cannot stream degrades to today's behaviour rather than
        failing. `OpenAIProvider` overrides it with genuine token
        streaming."""
        reply = await self.generate_reply(request)
        yield AITextDelta(reply.message_to_customer)
        yield AIReplyComplete(reply)
