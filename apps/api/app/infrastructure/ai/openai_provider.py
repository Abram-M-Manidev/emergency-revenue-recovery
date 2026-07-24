"""The only place in this codebase that knows about OpenAI's SDK, model
names, or request/response shapes. `AIBrainService` depends solely on the
`AIProvider` interface (`app/domain/ai/provider.py`) — swapping providers
later means adding a new class here, not touching application logic.

Uses structured outputs (a JSON schema response format) so the reply is
parsed once, validated, and mapped straight into the domain `AIReply`
dataclass, instead of the service layer parsing free-form text.
"""

from __future__ import annotations

import json

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.core.config import Settings
from app.domain.ai.provider import AIProvider, AIReply, AIRequest
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.exceptions import AIProviderUnavailableError

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
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if not self._settings.OPENAI_API_KEY:
            raise AIProviderUnavailableError()
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self._settings.OPENAI_API_KEY)
        return self._client

    async def generate_reply(self, request: AIRequest) -> AIReply:
        client = self._get_client()

        messages: list[dict[str, str]] = [{"role": "system", "content": request.system_prompt}]
        for turn in request.history:
            role = "user" if turn.role == "customer" else "assistant"
            messages.append({"role": role, "content": turn.content})
        messages.append({"role": "user", "content": request.latest_customer_message})

        response = await client.chat.completions.create(
            model=self._settings.OPENAI_MODEL,
            messages=messages,  # type: ignore[call-overload]
            response_format={"type": "json_schema", "json_schema": _JSON_SCHEMA},
        )
        content = response.choices[0].message.content
        if content is None:
            raise AIProviderUnavailableError("OpenAI returned an empty response.")

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
