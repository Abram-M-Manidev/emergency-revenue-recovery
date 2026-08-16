"""The only place in this codebase that knows about OpenAI's SDK, model
names, or request/response shapes. `AIBrainService` depends solely on the
`AIProvider` interface (`app/domain/ai/provider.py`) — swapping providers
later means adding a new class here, not touching application logic.

Uses structured outputs (a JSON schema response format) so the reply is
parsed once, validated, and mapped straight into the domain `AIReply`
dataclass, instead of the service layer parsing free-form text.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel

from app.core.config import ReasoningEffort, Settings
from app.domain.ai.provider import (
    AIModelProfile,
    AIProvider,
    AIReply,
    AIReplyComplete,
    AIRequest,
    AIStreamEvent,
    AITextDelta,
)
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.exceptions import AIProviderUnavailableError
from app.infrastructure.ai.streaming_json import StreamingStringFieldExtractor
from app.shared.logging.timing import elapsed_ms, now

logger = structlog.get_logger("app.ai.openai")

_JSON_SCHEMA: dict = {
    "name": "ai_brain_reply",
    "schema": {
        "type": "object",
        "properties": {
            "message_to_customer": {"type": "string"},
            "classification": {
                "type": "string",
                "enum": [c.value for c in CallClassification],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "recommended_action": {
                "type": "string",
                "enum": [a.value for a in RecommendedAction],
            },
            "matched_service_name": {"type": ["string", "null"]},
            "customer_name": {"type": ["string", "null"]},
            "customer_phone": {"type": ["string", "null"]},
            "customer_address": {"type": ["string", "null"]},
            "is_conversation_complete": {"type": "boolean"},
            "summary": {"type": "string"},
        },
        "required": [
            "message_to_customer",
            "classification",
            "confidence",
            "recommended_action",
            "matched_service_name",
            "customer_name",
            "customer_phone",
            "customer_address",
            "is_conversation_complete",
            "summary",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}


@dataclass(frozen=True, slots=True)
class _ProfileConfig:
    """The concrete OpenAI knobs one `AIModelProfile` resolves to. Timeout
    and retries live here rather than on a single shared client because the
    two profiles need genuinely different failure behaviour, not just
    different models — see `Settings.OPENAI_REALTIME_TIMEOUT_SECONDS`."""

    model: str
    reasoning_effort: ReasoningEffort | None
    timeout_seconds: float
    max_retries: int


class _ReplyPayload(BaseModel):
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


class OpenAIProvider(AIProvider):
    """Raises `AIProviderUnavailableError` (a domain error, mapped to 503 by
    `core/errors.py`) when invoked without an `OPENAI_API_KEY` — deliberately
    not raised at construction time, so the rest of the app still boots
    without one configured (matching today's placeholder-only
    `OPENAI_API_KEY` setting)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # One client per profile: `timeout`/`max_retries` are client-level
        # knobs in this SDK, and the profiles need different values for
        # both. Built lazily and cached, so a process that only ever serves
        # text traffic never constructs the realtime client.
        self._clients: dict[AIModelProfile, AsyncOpenAI] = {}

    def _config_for(self, profile: AIModelProfile) -> _ProfileConfig:
        if profile is AIModelProfile.REALTIME:
            return _ProfileConfig(
                model=self._settings.OPENAI_REALTIME_MODEL,
                reasoning_effort=self._settings.OPENAI_REALTIME_REASONING_EFFORT,
                timeout_seconds=self._settings.OPENAI_REALTIME_TIMEOUT_SECONDS,
                max_retries=self._settings.OPENAI_REALTIME_MAX_RETRIES,
            )
        return _ProfileConfig(
            model=self._settings.OPENAI_MODEL,
            reasoning_effort=self._settings.OPENAI_REASONING_EFFORT,
            timeout_seconds=self._settings.OPENAI_TIMEOUT_SECONDS,
            max_retries=self._settings.OPENAI_MAX_RETRIES,
        )

    def _get_client(self, profile: AIModelProfile = AIModelProfile.QUALITY) -> AsyncOpenAI:
        if not self._settings.OPENAI_API_KEY:
            raise AIProviderUnavailableError()
        client = self._clients.get(profile)
        if client is None:
            # `timeout`/`max_retries` are the SDK's own client-level knobs —
            # it already implements correct backoff for transient failures,
            # so no hand-rolled retry loop is needed here. Without an
            # explicit timeout, a hung call could block a live emergency
            # call's request indefinitely.
            config = self._config_for(profile)
            client = AsyncOpenAI(
                api_key=self._settings.OPENAI_API_KEY,
                timeout=config.timeout_seconds,
                max_retries=config.max_retries,
            )
            self._clients[profile] = client
        return client

    async def generate_reply(self, request: AIRequest) -> AIReply:
        client = self._get_client(request.profile)
        config = self._config_for(request.profile)

        # `extra_body` carries `reasoning_effort` rather than the SDK's own
        # parameter: openai==1.59.6 predates GPT-5 and types that parameter
        # as Literal["low", "medium", "high"], which would reject the valid
        # "minimal" value. `extra_body` is merged into the request JSON
        # verbatim, so the wire format is identical either way. Omitted
        # entirely when unset, because sending it to a non-reasoning model
        # (gpt-4.1-mini) is a 400.
        try:
            response = await client.chat.completions.create(
                model=config.model,
                messages=self._messages_for(request),  # type: ignore[call-overload]
                response_format={"type": "json_schema", "json_schema": _JSON_SCHEMA},
                extra_body=self._extra_body_for(config),
            )
        except APITimeoutError as exc:
            raise AIProviderUnavailableError("The AI Brain timed out. Please try again.") from exc
        except APIConnectionError as exc:
            raise AIProviderUnavailableError(
                "Could not reach the AI Brain. Please try again shortly."
            ) from exc
        except APIStatusError as exc:
            raise AIProviderUnavailableError(
                "The AI Brain is temporarily unavailable. Please try again shortly."
            ) from exc

        content = response.choices[0].message.content
        if content is None:
            raise AIProviderUnavailableError("OpenAI returned an empty response.")

        return _assemble_reply(content)

    async def stream_reply(self, request: AIRequest) -> AsyncIterator[AIStreamEvent]:
        """Genuine token streaming: `message_to_customer` is forwarded as
        the model writes it, so Vapi can start speaking before the decision
        fields even exist.

        The decision fields are deliberately NOT read from the stream. They
        are parsed only from the complete document at the end, through the
        same `_ReplyPayload` validation the non-streaming path uses — so a
        malformed or truncated response raises instead of producing a
        half-formed outcome or a spurious hang-up.

        `message_to_customer` is the first property in `_JSON_SCHEMA`, and
        OpenAI emits strict-schema properties in declaration order, which
        is what makes the caller-facing text available before anything
        else."""
        client = self._get_client(request.profile)
        config = self._config_for(request.profile)

        extractor = StreamingStringFieldExtractor("message_to_customer")
        raw: list[str] = []

        # Monotonic, so a clock adjustment mid-call cannot produce a
        # negative duration. All `*_ms` fields below are measured from
        # `started_at` and describe *this process's* view only — nothing
        # here observes the caller's speech, Vapi's endpointing, or TTS.
        started_at = now()
        first_output_at: float | None = None
        logger.info(
            "ai_stream_started",
            model=config.model,
            profile=request.profile.value,
            history_turns=len(request.history),
        )

        try:
            try:
                stream = await client.chat.completions.create(
                    model=config.model,
                    messages=self._messages_for(request),  # type: ignore[call-overload]
                    response_format={"type": "json_schema", "json_schema": _JSON_SCHEMA},
                    extra_body=self._extra_body_for(config),
                    stream=True,
                )
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    piece = chunk.choices[0].delta.content
                    if not piece:
                        continue
                    raw.append(piece)
                    text = extractor.feed(piece)
                    if text:
                        if first_output_at is None:
                            # Time to the first *speakable* character, which
                            # is the number P2 exists to reduce — not the
                            # first JSON token, which the caller never hears.
                            first_output_at = now()
                            logger.info(
                                "ai_stream_first_output",
                                elapsed_ms=elapsed_ms(started_at, first_output_at),
                            )
                        yield AITextDelta(text)
            except APITimeoutError as exc:
                raise AIProviderUnavailableError(
                    "The AI Brain timed out. Please try again."
                ) from exc
            except APIConnectionError as exc:
                raise AIProviderUnavailableError(
                    "Could not reach the AI Brain. Please try again shortly."
                ) from exc
            except APIStatusError as exc:
                raise AIProviderUnavailableError(
                    "The AI Brain is temporarily unavailable. Please try again shortly."
                ) from exc

            content = "".join(raw)
            if not content:
                raise AIProviderUnavailableError("OpenAI returned an empty response.")

            reply = _assemble_reply(content)
            logger.info(
                "ai_stream_completed",
                elapsed_ms=elapsed_ms(started_at),
                first_output_ms=(
                    None if first_output_at is None else elapsed_ms(started_at, first_output_at)
                ),
                chunks=len(raw),
                reply_chars=len(reply.message_to_customer),
            )
            yield AIReplyComplete(reply)
        except (GeneratorExit, asyncio.CancelledError):
            # The consumer stopped iterating — on a live call this is Vapi
            # abandoning the turn. Distinguished from a provider failure,
            # which raises `AIProviderUnavailableError` and is logged by
            # whoever handles it. Re-raised untouched: swallowing either of
            # these would corrupt generator/task shutdown.
            logger.info(
                "ai_stream_aborted",
                elapsed_ms=elapsed_ms(started_at),
                chunks=len(raw),
                produced_output=first_output_at is not None,
            )
            raise

    # --- shared request construction (identical for both paths) ---

    def _messages_for(self, request: AIRequest) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [{"role": "system", "content": request.system_prompt}]
        for turn in request.history:
            role = "user" if turn.role == "customer" else "assistant"
            messages.append({"role": role, "content": turn.content})
        messages.append({"role": "user", "content": request.latest_customer_message})
        return messages

    def _extra_body_for(self, config: _ProfileConfig) -> dict[str, Any] | None:
        if config.reasoning_effort is None:
            return None
        return {"reasoning_effort": config.reasoning_effort}


def _assemble_reply(content: str) -> AIReply:
    """The single place a complete response becomes an `AIReply`, shared by
    the streaming and non-streaming paths so the two can never diverge on
    validation or field mapping."""
    payload = _ReplyPayload.model_validate(json.loads(content))
    return AIReply(
        message_to_customer=payload.message_to_customer,
        classification=payload.classification,
        confidence=payload.confidence,
        recommended_action=payload.recommended_action,
        matched_service_name=payload.matched_service_name,
        customer_name=payload.customer_name,
        customer_phone=payload.customer_phone,
        customer_address=payload.customer_address,
        is_conversation_complete=payload.is_conversation_complete,
        summary=payload.summary,
    )
