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

from app.domain.ai.tools import ToolDefinition, ToolExecutor
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
    # Both default to "no tools", so every existing caller and every test
    # fake behaves exactly as it did before tools existed. A provider that
    # cannot call tools may ignore them entirely — the reply contract is
    # unchanged either way.
    tools: tuple[ToolDefinition, ...] = ()
    tool_executor: ToolExecutor | None = None

    @property
    def tools_enabled(self) -> bool:
        """Tools are only usable when there is both something to call and
        something to call it with. Keeping the two fields independent but
        requiring both means a wiring mistake degrades to the pre-tool
        behaviour rather than to a crash mid-call."""
        return bool(self.tools) and self.tool_executor is not None


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
    # Not model output — per-turn metadata the provider attaches after the
    # reply is assembled.
    #
    # True when `book_appointment` was invoked during this turn, failed, and
    # no later attempt in the same turn succeeded. `is_conversation_complete`
    # is the model's own assertion, and it will set it while apologising for
    # a booking that did not happen — hanging up on a caller who has just
    # been told their appointment could not be made. `AIBrainService` uses
    # this to withhold completion for that one turn only.
    #
    # Defaults False, so every existing construction site and every provider
    # that runs no tools behaves exactly as before.
    booking_failed_unrecovered: bool = False


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


@dataclass(frozen=True, slots=True)
class AIToolPhase:
    """Emitted when the model has asked for tools and the provider is about
    to run them.

    Exists purely so the transport above can cover the gap. A tool round is
    a second model call, and on a live phone call that gap is silence the
    caller is listening to — the failure mode that already hung one call up
    on `silence-timed-out`. `VoiceService` turns this event into a short
    holding phrase; the text/simulation path ignores it.

    `model_already_spoke` says whether the caller has *already* heard
    something in this same round. A response may carry both content and tool
    calls — the model announcing "I'll check that now" and requesting the
    tool in one breath — and when it does, a holding phrase on top would be
    the second "one moment" in a row. The flag lets the transport stay quiet
    in exactly that case without having to track the stream itself.

    Carries no model output of its own, so this event can never put words in
    the caller's ear ahead of a result."""

    tool_names: tuple[str, ...]
    model_already_spoke: bool = False


AIStreamEvent = AITextDelta | AIToolPhase | AIReplyComplete


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
