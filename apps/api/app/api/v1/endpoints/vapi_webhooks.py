"""Vapi-facing webhook endpoints: the Custom-LLM turn handler
(`/chat/completions`) and call-lifecycle events (`/events`). Authenticated
by a shared secret header (`verify_vapi_secret`, `app/api/deps.py`), not a
user JWT — these are server-to-server requests from Vapi's platform, not an
app user acting on their own organization.

`/chat/completions` deliberately never returns a JSON error envelope: Vapi
is a voice agent, not a JSON API consumer, and needs something speakable
back so the caller hears a graceful message instead of dead air or an
abrupt hangup. See `_completion_response`'s callers below.

The response is emitted in whichever transport the caller asked for. Vapi
sets `stream: true` on live calls and reads the reply as an OpenAI-style
SSE token stream; anything else (the text/simulation path, tests, manual
curl) gets the original single JSON body. Both carry identical semantics —
same text, same `endCall` tool call, same `finish_reason` — so only the
framing differs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import APIRouter, Body, Depends, Response
from fastapi.responses import JSONResponse, StreamingResponse
from structlog.contextvars import bind_contextvars

from app.api.deps import (
    get_appointment_service,
    get_customer_service,
    get_dispatch_service,
    get_emergency_notification_service,
    get_voice_service,
    verify_vapi_secret,
)
from app.application.schemas.voice import VapiChatCompletionRequest
from app.application.services.appointment_service import AppointmentService
from app.application.services.customer_service import CustomerService
from app.application.services.dispatch_service import DispatchService
from app.application.services.emergency_notification_service import (
    EmergencyNotificationService,
)
from app.application.services.voice_service import (
    ChatCompletionResult,
    VoiceService,
    VoiceTextDelta,
)
from app.domain.entities.emergency_ticket import EmergencyTicket
from app.domain.exceptions import DomainError
from app.shared.logging.timing import elapsed_ms, now

router = APIRouter(
    prefix="/voice/vapi", tags=["voice-vapi-webhooks"], dependencies=[Depends(verify_vapi_secret)]
)

logger = structlog.get_logger("app.voice.vapi")

# Reported back as the `model` field. Deliberately not an OpenAI model name:
# the caller is talking to the AI Brain, whose actual model is chosen
# server-side per channel (see AIModelProfile) and is not Vapi's business.
_MODEL_NAME = "errs-ai-brain"

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
    notifications: EmergencyNotificationService = Depends(
        get_emergency_notification_service
    ),
) -> Response:
    # Bound before anything else so every later event in this turn — across
    # every layer — carries the call id without it being threaded through.
    # `turn_id` distinguishes the several requests Vapi can send for one
    # spoken utterance, which `vapi_call_id` alone cannot.
    bind_contextvars(vapi_call_id=payload.call.id, turn_id=uuid.uuid4().hex[:12])
    logger.info(
        "voice_request_received",
        streaming=payload.stream,
        message_count=len(payload.messages),
        has_customer_number=bool(payload.call.customer and payload.call.customer.number),
    )

    customer_utterance = _latest_customer_utterance(payload)
    if customer_utterance is None:
        logger.warning("vapi_chat_completion_no_user_message", vapi_call_id=payload.call.id)
        return _completion_response(
            _FALLBACK_MESSAGE, should_end_call=False, stream=payload.stream
        )

    if payload.stream:
        # Live calls: forward the model's sentence as it is written so Vapi
        # can start speaking before the decision fields exist. The whole
        # turn runs inside the returned generator — which is safe only
        # because FastAPI >= 0.118 keeps `yield` dependencies (the DB
        # session, and with it P1's transaction-scoped advisory lock) alive
        # until the response body is finished.
        stats = _StreamStats()
        return StreamingResponse(
            _instrumented_stream(
                stats,
                _streamed_completion(
                    stats=stats,
                    payload=payload,
                    customer_utterance=customer_utterance,
                    service=service,
                    dispatch_service=dispatch_service,
                    appointment_service=appointment_service,
                    customer_service=customer_service,
                    notifications=notifications,
                ),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

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
        return _completion_response(
            _FALLBACK_MESSAGE, should_end_call=True, stream=payload.stream
        )

    try:
        ticket = await dispatch_service.sync_ticket_from_outcome(
            result.organization_id, result.conversation_id
        )
        await _alert_dispatcher(
            ticket, notifications, correlation={"vapi_call_id": payload.call.id}
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
        #
        # `caller_number` (P5) records which telephony line this customer
        # called from, so a later call is recognised. Association
        # bookkeeping only — C1's field-level rules are untouched.
        await customer_service.sync_customer_from_outcome(
            result.organization_id,
            result.conversation_id,
            caller_number=payload.call.customer.number if payload.call.customer else None,
        )
    except DomainError as exc:
        # Same reasoning as the dispatch/appointment syncs above.
        logger.warning(
            "customer_sync_failed",
            error=exc.__class__.__name__,
            message=exc.message,
            vapi_call_id=payload.call.id,
        )

    return _completion_response(
        result.reply_text, should_end_call=result.should_end_call, stream=payload.stream
    )


@dataclass
class _StreamStats:
    """Shared between the turn generator, which fills it in, and
    `_instrumented_stream`, which reports it. Separated because the
    completion/abort event has to be emitted from a wrapper that survives
    a `GeneratorExit` raised at *any* of this generator's yield points —
    and wrapping was preferable to re-indenting the P2 turn logic inside
    an outer `try`."""

    started_at: float = 0.0
    first_content_at: float | None = None
    content_frames: int = 0
    end_call_emitted: bool = False


async def _instrumented_stream(
    stats: _StreamStats, inner: AsyncGenerator[str, None]
) -> AsyncIterator[str]:
    """Passes frames through untouched, and records how the stream ended.

    Deliberately not buffering: each frame is forwarded as it arrives, so
    SSE framing, ordering, and back-pressure are exactly what `inner`
    produced. The only additions are two terminal log events.

    `aclosing` is load-bearing, not tidiness. Without it, a `GeneratorExit`
    raised here on client disconnect unwinds this wrapper but leaves
    `inner` to be finalised whenever the event loop next sweeps its async
    generators — so `_streamed_completion`'s `finally` work, and with it
    the exit of P1's `call_lock.hold(...)` and the request's transaction,
    would happen late instead of immediately. Before this wrapper existed
    Starlette closed the turn generator directly; `aclosing` restores
    exactly that timing."""
    try:
        async with contextlib.aclosing(inner) as stream:
            async for frame in stream:
                yield frame
    except (GeneratorExit, asyncio.CancelledError):
        # Vapi hung up on the turn — the case that produced the orphaned
        # customer messages before P4. Re-raised untouched; this only
        # observes it (fixing it is H3, deliberately out of scope here).
        logger.info(
            "voice_stream_aborted",
            elapsed_ms=elapsed_ms(stats.started_at),
            content_frames=stats.content_frames,
            reached_first_content=stats.first_content_at is not None,
        )
        raise
    logger.info(
        "voice_stream_completed",
        elapsed_ms=elapsed_ms(stats.started_at),
        first_content_ms=(
            None
            if stats.first_content_at is None
            else elapsed_ms(stats.started_at, stats.first_content_at)
        ),
        content_frames=stats.content_frames,
        end_call_emitted=stats.end_call_emitted,
    )


async def _streamed_completion(
    *,
    stats: _StreamStats,
    payload: VapiChatCompletionRequest,
    customer_utterance: str,
    service: VoiceService,
    dispatch_service: DispatchService,
    appointment_service: AppointmentService,
    customer_service: CustomerService,
    notifications: EmergencyNotificationService,
) -> AsyncGenerator[str, None]:
    """Emits the turn as OpenAI-compatible SSE while it is still being
    generated.

    Frame order is: role, then one content frame per model delta, then the
    downstream syncs, then (if the AI Brain ended the conversation) the
    `endCall` tool call, then `finish_reason`, then `[DONE]`.

    The syncs run *after* the content frames because the first byte now
    leaves before the turn is finished — but they still run inside this
    generator, synchronously, before `[DONE]`. That is deliberate: an
    emergency ticket must exist before the turn is acknowledged as
    complete, so this is explicitly NOT the detached fire-and-forget
    behaviour of P3."""
    frames = _SseFrameWriter()

    # Generator entry, NOT handler entry: the handler returned its
    # StreamingResponse object before this line ran. `voice_request_received`
    # marks the handler; the gap between the two is Starlette beginning to
    # consume the body. Every duration below is measured from here.
    stats.started_at = now()
    logger.info("voice_stream_started")

    yield frames.role()

    result = None
    spoke = False
    try:
        async for event in service.handle_chat_completion_stream(
            vapi_call_id=payload.call.id,
            assistant_id=payload.call.assistantId or payload.assistantId,
            phone_number_id=payload.call.phoneNumberId or payload.phoneNumberId,
            customer_number=payload.call.customer.number if payload.call.customer else None,
            customer_utterance=customer_utterance,
        ):
            if isinstance(event, VoiceTextDelta):
                if event.text:
                    spoke = True
                    stats.content_frames += 1
                    if stats.first_content_at is None:
                        # The first frame Vapi can hand to TTS. This is the
                        # closest thing we can observe to "the caller is
                        # about to hear something" — it is NOT audio start,
                        # which only Vapi sees.
                        stats.first_content_at = now()
                        logger.info(
                            "voice_stream_first_content",
                            elapsed_ms=elapsed_ms(stats.started_at, stats.first_content_at),
                        )
                    yield frames.content(event.text)
            else:
                result = event.result
    except DomainError as exc:
        # Same contract as the non-streaming path: every domain failure must
        # still be speakable. Only decoded reply text is ever emitted, so a
        # malformed or truncated JSON document cannot reach the caller.
        logger.error(
            "vapi_chat_completion_stream_domain_error",
            error=exc.__class__.__name__,
            message=exc.message,
            vapi_call_id=payload.call.id,
            partial_speech=spoke,
        )
        yield frames.content(_FALLBACK_MESSAGE)
        # No `endCall`: the turn never produced an authoritative
        # `is_conversation_complete`, so hanging up here would be a guess.
        yield frames.finish("stop")
        yield frames.done()
        return

    if result is None:
        logger.error("vapi_chat_completion_stream_no_result", vapi_call_id=payload.call.id)
        yield frames.content(_FALLBACK_MESSAGE)
        yield frames.finish("stop")
        yield frames.done()
        return

    syncs_started_at = now()
    await _run_outcome_syncs(
        result=result,
        vapi_call_id=payload.call.id,
        caller_number=payload.call.customer.number if payload.call.customer else None,
        dispatch_service=dispatch_service,
        appointment_service=appointment_service,
        customer_service=customer_service,
        notifications=notifications,
    )
    # Runs after the caller is already hearing the reply, so this is not
    # latency they perceive — but it does delay `[DONE]`, and on a final
    # turn therefore the hang-up. Worth being able to see.
    logger.info("voice_outcome_syncs_completed", elapsed_ms=elapsed_ms(syncs_started_at))

    finish_reason = "stop"
    if result.should_end_call:
        stats.end_call_emitted = True
        logger.info("voice_end_call_emitted")
        yield frames.tool_call(_end_call_tool_call())
        finish_reason = "tool_calls"
    yield frames.finish(finish_reason)
    yield frames.done()


async def _run_outcome_syncs(
    *,
    result: ChatCompletionResult,
    vapi_call_id: str,
    caller_number: str | None,
    dispatch_service: DispatchService,
    appointment_service: AppointmentService,
    customer_service: CustomerService,
    notifications: EmergencyNotificationService,
) -> None:
    """The three downstream syncs, in the order the non-streaming path runs
    them and with the same isolate-and-continue behaviour."""
    try:
        ticket = await dispatch_service.sync_ticket_from_outcome(
            result.organization_id, result.conversation_id
        )
        await _alert_dispatcher(
            ticket, notifications, correlation={"vapi_call_id": vapi_call_id}
        )
    except DomainError as exc:
        logger.warning(
            "dispatch_sync_failed",
            error=exc.__class__.__name__,
            message=exc.message,
            vapi_call_id=vapi_call_id,
        )

    try:
        await appointment_service.sync_appointment_from_outcome(
            result.organization_id, result.conversation_id
        )
    except DomainError as exc:
        logger.warning(
            "appointment_sync_failed",
            error=exc.__class__.__name__,
            message=exc.message,
            vapi_call_id=vapi_call_id,
        )

    try:
        # Last, so it can link whichever ticket/appointment the two calls
        # above just created. `caller_number` is P5 association capture.
        await customer_service.sync_customer_from_outcome(
            result.organization_id, result.conversation_id, caller_number=caller_number
        )
    except DomainError as exc:
        logger.warning(
            "customer_sync_failed",
            error=exc.__class__.__name__,
            message=exc.message,
            vapi_call_id=vapi_call_id,
        )


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


def _end_call_tool_call() -> dict[str, Any]:
    """The `endCall` tool call the AI Brain's "conversation is complete"
    signal is expressed as. The assistant must have `endCall` in its Vapi
    tool list (it does — set at assistant-creation time) for Vapi to act on
    this."""
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": "endCall", "arguments": "{}"},
    }


def _completion_payload(content: str, *, should_end_call: bool) -> dict[str, Any]:
    """The non-streamed `chat.completion` body. Unchanged from the original
    implementation — the text/simulation path and every existing test
    depend on this exact shape."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if should_end_call:
        message["tool_calls"] = [_end_call_tool_call()]
        finish_reason = "tool_calls"

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": _MODEL_NAME,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }


class _SseFrameWriter:
    """Builds OpenAI-compatible `chat.completion.chunk` frames.

    `id`/`created` are fixed at construction and reused for every frame, as
    a real OpenAI stream does, so a client accumulating deltas sees one
    coherent completion rather than a series of unrelated ones."""

    def __init__(self) -> None:
        self._id = f"chatcmpl-{uuid.uuid4().hex}"
        self._created = int(time.time())

    def _frame(self, delta: dict[str, Any], finish_reason: str | None = None) -> str:
        body = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": _MODEL_NAME,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(body)}\n\n"

    def role(self) -> str:
        return self._frame({"role": "assistant"})

    def content(self, text: str) -> str:
        return self._frame({"content": text})

    def tool_call(self, tool_call: dict[str, Any]) -> str:
        # `index` is required on a streamed tool call so the client knows
        # which call successive deltas belong to.
        return self._frame({"tool_calls": [{"index": 0, **tool_call}]})

    def finish(self, finish_reason: str) -> str:
        return self._frame({}, finish_reason)

    def done(self) -> str:
        return "data: [DONE]\n\n"


async def _completion_chunks(content: str, *, should_end_call: bool) -> AsyncIterator[str]:
    """Single-shot SSE for text that already exists in full — the fallback
    messages and the `stream: false` callers that still ask for SSE.

    The genuinely progressive path is `_streamed_completion`; this one has
    nothing to stream progressively, so it emits one content delta rather
    than fabricating chunks that would arrive simultaneously anyway."""
    frames = _SseFrameWriter()
    yield frames.role()
    yield frames.content(content)

    finish_reason = "stop"
    if should_end_call:
        yield frames.tool_call(_end_call_tool_call())
        finish_reason = "tool_calls"

    yield frames.finish(finish_reason)
    yield frames.done()


def _completion_response(content: str, *, should_end_call: bool, stream: bool) -> Response:
    if stream:
        return StreamingResponse(
            _completion_chunks(content, should_end_call=should_end_call),
            media_type="text/event-stream",
            # SSE must not be cached, and `X-Accel-Buffering: no` stops an
            # intermediary (nginx-style proxy) holding frames back — which
            # would reintroduce exactly the silence this transport fixes.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return JSONResponse(_completion_payload(content, should_end_call=should_end_call))


def _first_present(data: dict[str, Any], *keys: str) -> Any | None:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None


async def _alert_dispatcher(
    ticket: EmergencyTicket | None,
    notifications: EmergencyNotificationService,
    *,
    correlation: dict[str, str],
) -> None:
    """Sends the outbound emergency alert for a ticket the outcome sync just
    created, on the paths that do not go through the tool loop.

    The tool loop alerts inside `create_service_request`, because there the
    assistant is about to speak and needs the answer. Here nobody is waiting
    on it — the turn is already decided — but the alert still has to happen,
    or a call that produced a ticket without using tools would leave a real
    emergency with nobody told.

    Idempotent by ticket, so the two paths overlapping (which they routinely
    do: the tool loop creates the ticket and this sync then re-runs on the
    same conversation) sends exactly one alert. Never raises: a turn that has
    already reached the caller must not be failed by an alerting problem.
    """
    if ticket is None:
        return
    try:
        await notifications.notify_ticket(ticket)
    except Exception:
        logger.error("emergency_notification_failed", exc_info=True, **correlation)
