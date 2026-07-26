"""Vapi-facing webhook endpoints: the Custom-LLM turn handler
(`/chat/completions`) and call-lifecycle events (`/events`). Authenticated
by a shared secret header (`verify_vapi_secret`, `app/api/deps.py`), not a
user JWT — these are server-to-server requests from Vapi's platform, not an
app user acting on their own organization.

`/chat/completions` deliberately never returns a JSON error envelope: Vapi
is a voice agent, not a JSON API consumer, and needs something speakable
back so the caller hears a graceful message instead of dead air or an
abrupt hangup. See `_completion_response`'s callers below."""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Body, Depends

from app.api.deps import (
    get_appointment_service,
    get_customer_service,
    get_dispatch_service,
    get_voice_service,
    verify_vapi_secret,
)
from app.application.schemas.voice import VapiChatCompletionRequest
from app.application.services.appointment_service import AppointmentService
from app.application.services.customer_service import CustomerService
from app.application.services.dispatch_service import DispatchService
from app.application.services.voice_service import VoiceService
from app.domain.exceptions import DomainError

router = APIRouter(
    prefix="/voice/vapi", tags=["voice-vapi-webhooks"], dependencies=[Depends(verify_vapi_secret)]
)

logger = structlog.get_logger("app.voice.vapi")

_FALLBACK_MESSAGE = (
    "I'm sorry, I'm having trouble connecting to our system right now. "
    "Please try calling back in a few minutes."
)


@router.post("/chat/completions")
async def vapi_chat_completions(
    payload: VapiChatCompletionRequest,
    service: VoiceService = Depends(get_voice_service),
    dispatch_service: DispatchService = Depends(get_dispatch_service),
    appointment_service: AppointmentService = Depends(get_appointment_service),
    customer_service: CustomerService = Depends(get_customer_service),
) -> dict[str, Any]:
    customer_utterance = _latest_customer_utterance(payload)
    if customer_utterance is None:
        logger.warning("vapi_chat_completion_no_user_message", vapi_call_id=payload.call.id)
        return _completion_response(_FALLBACK_MESSAGE, should_end_call=False)

    try:
        result = await service.handle_chat_completion(
            vapi_call_id=payload.call.id,
            assistant_id=payload.call.assistantId or payload.assistantId,
            phone_number_id=payload.call.phoneNumberId or payload.phoneNumberId,
            customer_number=payload.call.customer.number if payload.call.customer else None,
            customer_utterance=customer_utterance,
        )
    except DomainError as exc:
        # A misconfigured line (`VoiceLineNotFoundError`), a conversation
        # the AI Brain already ended (`ConversationCompletedError`), or the
        # AI provider being unavailable (`AIProviderUnavailableError`) all
        # land here — every one of them should still be speakable to the
        # caller rather than surfaced as an HTTP error Vapi has no voice
        # for.
        logger.error(
            "vapi_chat_completion_domain_error",
            error=exc.__class__.__name__,
            message=exc.message,
            vapi_call_id=payload.call.id,
        )
        return _completion_response(_FALLBACK_MESSAGE, should_end_call=True)

    try:
        await dispatch_service.sync_ticket_from_outcome(
            result.organization_id, result.conversation_id
        )
    except DomainError as exc:
        # Same reasoning as the text-conversation endpoint: the call turn
        # itself already succeeded and must still reach the caller.
        logger.warning(
            "dispatch_sync_failed",
            error=exc.__class__.__name__,
            message=exc.message,
            vapi_call_id=payload.call.id,
        )

    try:
        await appointment_service.sync_appointment_from_outcome(
            result.organization_id, result.conversation_id
        )
    except DomainError as exc:
        # Same reasoning as the dispatch sync above: the call turn itself
        # already succeeded and must still reach the caller.
        logger.warning(
            "appointment_sync_failed",
            error=exc.__class__.__name__,
            message=exc.message,
            vapi_call_id=payload.call.id,
        )

    try:
        # Runs last, after dispatch/appointment sync, so it can link
        # whichever ticket/appointment those two calls just created — see
        # `CustomerService.sync_customer_from_outcome`'s docstring.
        await customer_service.sync_customer_from_outcome(
            result.organization_id, result.conversation_id
        )
    except DomainError as exc:
        # Same reasoning as the dispatch/appointment syncs above.
        logger.warning(
            "customer_sync_failed",
            error=exc.__class__.__name__,
            message=exc.message,
            vapi_call_id=payload.call.id,
        )

    return _completion_response(result.reply_text, should_end_call=result.should_end_call)


@router.post("/events")
async def vapi_events(
    payload: dict[str, Any] = Body(...),
    service: VoiceService = Depends(get_voice_service),
) -> dict[str, str]:
    """Vapi's Server URL lifecycle events (status updates, transcripts,
    end-of-call reports, ...). Accepted as a raw dict rather than a strict
    schema — event shapes vary by type and this route only acts on one of
    them; everything else is acknowledged and ignored so a webhook Vapi
    treats as fire-and-forget never fails noisily here for an event type we
    don't act on."""
    message = payload.get("message", payload)
    message_type = message.get("type")

    if message_type != "end-of-call-report":
        logger.info("vapi_event_ignored", type=message_type)
        return {"status": "ok"}

    call = message.get("call") or {}
    vapi_call_id = call.get("id")
    if not vapi_call_id:
        logger.warning("vapi_end_of_call_report_missing_call_id")
        return {"status": "ok"}

    duration_raw = _first_present(message, "durationSeconds", "duration")
    await service.handle_end_of_call_report(
        vapi_call_id=vapi_call_id,
        ended_reason=message.get("endedReason"),
        duration_seconds=int(duration_raw) if duration_raw is not None else None,
        recording_url=_first_present(message, "recordingUrl", "stereoRecordingUrl"),
    )
    return {"status": "ok"}


def _latest_customer_utterance(payload: VapiChatCompletionRequest) -> str | None:
    for message in reversed(payload.messages):
        if message.role == "user" and message.content:
            return message.content
    return None


def _completion_response(content: str, *, should_end_call: bool) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if should_end_call:
        # Matches Vapi's documented `endCallFunctionEnabled` mechanism: the
        # assistant must have that flag set in its Vapi dashboard config
        # (an ops-side setup step, same as the rest of provisioning).
        # Flagged to verify against a live Vapi call during integration
        # QA — if unreliable, Vapi's `endCallPhrases` mechanism is the
        # documented fallback and needs no response-shape change at all.
        message["tool_calls"] = [
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": "endCall", "arguments": "{}"},
            }
        ]
        finish_reason = "tool_calls"

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "errs-ai-brain",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }


def _first_present(data: dict[str, Any], *keys: str) -> Any | None:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None
