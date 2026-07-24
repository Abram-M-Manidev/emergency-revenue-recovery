"""The provider abstraction (per ARCHITECTURE.md) for the AI Brain's
language model calls. `AIBrainService` (application layer) depends only on
this interface; `OpenAIProvider` (infrastructure layer) is the only thing
that knows about OpenAI's SDK, request/response shapes, or model names.

Kept in `domain` rather than `application` because it has zero framework or
infrastructure imports — it is exactly as framework-free as a repository
interface, just not backed by a database."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction


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


class AIProvider(ABC):
    @abstractmethod
    async def generate_reply(self, request: AIRequest) -> AIReply: ...
