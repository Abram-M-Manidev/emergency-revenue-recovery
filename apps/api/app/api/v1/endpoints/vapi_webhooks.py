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

import anyio
import structlog
from fastapi import APIRouter, Body, Depends, Response
from fastapi.responses import JSONResponse, StreamingResponse
from structlog.contextvars import bind_contextvars

from app.api.deps import (
    get_appointment_service,
    get_customer_service,
    get_dispatch_service,
    get_savepoints,
    get_voice_service,
    verify_vapi_secret,
)
from app.application.schemas.voice import VapiChatCompletionRequest
from app.application.services.appointment_service import AppointmentService
from app.application.services.customer_service import CustomerService
from app.application.services.dispatch_service import DispatchService
from app.application.services.voice_service import (
    ChatCompletionResult,
    VoiceService,
    VoiceTextDelta,
)
from app.domain.exceptions import (
    ConversationCompletedError,
    ConversationLimitExceededError,
    DomainError,
    VoiceAssistantDisabledError,
)
from app.domain.transactions import Savepoints
from app.shared.logging.timing import elapsed_ms, now
from app.shared.utils.phone import storable_phone_number

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

# Spoken when a business has switched its own assistant off. Deliberately
# different from the generic fallback above: nothing is broken, so implying a
# fault ("trouble connecting", "try again in a few minutes") would be both
# false and useless — it invites the caller to redial into the same silence.
# This tells them plainly that the automated line is unavailable and sends
# them to a human, which is the only action that helps them.
_ASSISTANT_DISABLED_MESSAGE = (
    "Thanks for calling. Our automated assistant is unavailable at the "
    "moment. Please hold the line for our team, or call back during "
    "business hours and someone will help you."
)

# Spoken when a call reaches `AI_MAX_CONVERSATION_TURNS`. It used to get the
# generic fallback with no `endCall`, so every further utterance hit the same
# limit and heard the same "trouble connecting" sentence until Vapi hung up
# on silence — an unending loop on a call that was, if anything, going well
# enough to run long. This ends the call instead, and says so plainly rather
# than implying an outage.
_TURN_LIMIT_MESSAGE = (
    "I'm sorry, I'm not able to continue this call. Please call back and "
    "we'll pick up where we left off. Thank you for calling."
)

# Spoken when Vapi sends a turn for a conversation the AI Brain has already
# closed — normally impossible, because the closing turn carries `endCall`.
# If it happens anyway (the hang-up was lost, or the caller kept talking), the
# old reply was "trouble connecting" with no `endCall`, repeated on every
# utterance until Vapi gave up on silence. There is nothing more this call can
# record, so it says so and ends.
_ALREADY_COMPLETED_MESSAGE = (
    "This call has already been completed. If you need anything else, "
    "please call us back. Goodbye."
)

# Every column a Vapi-supplied value lands in has a fixed width. These values
# are not ours to trust: a SIP caller ID is a URI, not a phone number, and
# an over-long value is a failed write — on the first turn, the conversation
# row itself, so every turn of that call would fail the same way.
_CALLER_NUMBER_MAX_LENGTH = 32
_ENDED_REASON_MAX_LENGTH = 64
_RECORDING_URL_MAX_LENGTH = 1000


@router.post("/chat/completions")
async def vapi_chat_completions(
    payload: VapiChatCompletionRequest,
    service: VoiceService = Depends(get_voice_service),
    dispatch_service: DispatchService = Depends(get_dispatch_service),
    appointment_service: AppointmentService = Depends(get_appointment_service),
    customer_service: CustomerService = Depends(get_customer_service),
    savepoints: Savepoints = Depends(get_savepoints),
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
    caller_number = _caller_number(payload)

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
                    savepoints=savepoints,
                    caller_number=caller_number,
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
            customer_number=caller_number,
            customer_utterance=customer_utterance,
        )
    except DomainError as exc:
        # A tenant that has switched its own assistant off. Checked before
        # the generic handler below because it is not a failure: the caller
        # gets a truthful sentence pointing them at a human, and the call
        # ends rather than looping them through an assistant that will not
        # answer.
        if isinstance(exc, VoiceAssistantDisabledError):
            logger.info(
                "vapi_chat_completion_assistant_disabled",
                vapi_call_id=payload.call.id,
            )
            return _completion_response(
                _ASSISTANT_DISABLED_MESSAGE, should_end_call=True, stream=payload.stream
            )
        if isinstance(exc, ConversationLimitExceededError):
            logger.warning("vapi_chat_completion_turn_limit_reached")
            return _completion_response(
                _TURN_LIMIT_MESSAGE, should_end_call=True, stream=payload.stream
            )
        if isinstance(exc, ConversationCompletedError):
            logger.warning("vapi_chat_completion_after_completion")
            return _completion_response(
                _ALREADY_COMPLETED_MESSAGE, should_end_call=True, stream=payload.stream
            )
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

    # Same three syncs, same order and same isolation as the streaming path —
    # one implementation, so the two transports cannot drift apart.
    await _run_outcome_syncs(
        result=result,
        vapi_call_id=payload.call.id,
        caller_number=caller_number,
        dispatch_service=dispatch_service,
        appointment_service=appointment_service,
        customer_service=customer_service,
        savepoints=savepoints,
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
    except Exception as exc:
        # Anything else escaping the turn generator. Observability only —
        # the exception is re-raised untouched, so recovery behaviour is
        # exactly what it was.
        #
        # Until this existed such a failure was completely silent *here*:
        # the two clauses above cover the abort and the normal end, so an
        # ordinary exception left the wrapper without logging either, and
        # the only trace was uvicorn's "Exception in ASGI application" —
        # a bare traceback carrying none of the correlation ids bound to
        # this turn. On 2026-09-24 that cost hours: a `varchar(32)`
        # overflow rolled back a turn the caller had already heard, and the
        # call looked like conversational amnesia rather than a failed
        # write.
        #
        # `reached_first_content` is the field that names the damage: true
        # means the caller was spoken to and the transaction then rolled
        # back, so the backend has no record of something the caller
        # believes happened. `vapi_call_id`, `turn_id`, `conversation_id`
        # and `organization_id` are already bound into the log context by
        # the webhook and `VoiceService`, so they attach for free.
        #
        # The exception type and message are safe to record; neither is
        # derived from caller speech. The raw exception is deliberately not
        # formatted into the message, and `exc_info` is off, because a
        # driver error can quote the offending parameters — which on this
        # path are the caller's own name, number and address.
        logger.error(
            "voice_stream_failed",
            elapsed_ms=elapsed_ms(stats.started_at),
            content_frames=stats.content_frames,
            reached_first_content=stats.first_content_at is not None,
            error=type(exc).__name__,
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
    **turn: Any,
) -> AsyncGenerator[str, None]:
    """The streamed turn, decoupled from the caller's connection.

    The turn itself (`_turn_frames`) runs in its own task and hands frames
    over a queue; this generator only relays them. That separation is what a
    caller hanging up needs. Starlette cancels the response task the moment
    Vapi drops the stream, and when this generator WAS the turn, that
    cancellation landed wherever the turn happened to be — including inside
    an in-flight asyncpg query. SQLAlchemy then invalidated the connection in
    the middle of the transaction: `get_db`'s commit raised
    `PendingRollbackError`, the whole turn was lost, and the connection was
    left `idle in transaction` holding the call's advisory lock (found by the
    real-model matrix, 2026-09-25). A caller who reports sparks and smoke and
    then hangs up to get out of the house lost their emergency ticket.

    Now a hang-up cancels only this relay. The turn is allowed to finish —
    its tool writes, its persistence, its syncs — under a shielded, bounded
    wait, and `get_db` then commits it normally, exactly as H3 established
    for a disconnect: the caller is gone, but the ticket and its queued alert
    are not. If the turn outlives `_HANGUP_DRAIN_SECONDS` it is cancelled
    after all, and `get_db` rolls back and discards the connection cleanly."""
    frames: asyncio.Queue[str | None] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for frame in _turn_frames(**turn):
                await frames.put(frame)
        finally:
            await frames.put(None)

    producer = asyncio.create_task(produce())
    try:
        while True:
            frame = await frames.get()
            if frame is None:
                break
            yield frame
        await producer
    except BaseException:
        if not producer.done():
            logger.info("voice_turn_draining_after_hangup")
            # Shielded: Starlette's cancel scope would otherwise cancel this
            # wait too, at every await, and the whole point is to let the
            # turn reach a clean end before the session is torn down.
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(_HANGUP_DRAIN_SECONDS):
                    await asyncio.wait({producer})
                if not producer.done():
                    producer.cancel()
                    await asyncio.wait({producer})
                    logger.error("voice_turn_abandoned_after_hangup")
                else:
                    logger.info("voice_turn_completed_after_hangup")
        raise


# How long a turn may keep running after the caller has hung up before it is
# abandoned. A turn is bounded by the realtime model timeout plus tool rounds;
# this comfortably covers a normal one without letting a stuck turn pin a
# connection indefinitely.
_HANGUP_DRAIN_SECONDS = 30.0


async def _turn_frames(
    *,
    stats: _StreamStats,
    payload: VapiChatCompletionRequest,
    customer_utterance: str,
    service: VoiceService,
    dispatch_service: DispatchService,
    appointment_service: AppointmentService,
    customer_service: CustomerService,
    savepoints: Savepoints,
    caller_number: str | None,
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
            customer_number=caller_number,
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
    except VoiceAssistantDisabledError:
        # The tenant switched its own assistant off. Same truthful sentence
        # the non-streaming path uses, and the call ends here rather than
        # leaving the caller waiting on an assistant that will not answer.
        logger.info("vapi_chat_completion_stream_assistant_disabled")
        yield frames.content(_ASSISTANT_DISABLED_MESSAGE)
        yield frames.tool_call(_end_call_tool_call())
        yield frames.finish("tool_calls")
        yield frames.done()
        return
    except (ConversationLimitExceededError, ConversationCompletedError) as exc:
        # Both raised before any model call, so nothing has been spoken yet,
        # and both mean this call can record nothing more: end it plainly
        # rather than answering every further utterance with a fault.
        limit = isinstance(exc, ConversationLimitExceededError)
        logger.warning(
            "vapi_chat_completion_stream_turn_limit_reached"
            if limit
            else "vapi_chat_completion_stream_after_completion"
        )
        yield frames.content(_TURN_LIMIT_MESSAGE if limit else _ALREADY_COMPLETED_MESSAGE)
        yield frames.tool_call(_end_call_tool_call())
        yield frames.finish("tool_calls")
        yield frames.done()
        return
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
    except Exception as exc:
        # Anything else — a lost database connection, a defect. Before this
        # the exception escaped mid-stream: the SSE body ended without
        # `[DONE]`, Vapi had nothing to speak, and the caller sat in silence
        # until `silence-timed-out`. Now they hear the same speakable
        # fallback as for a domain failure and can simply try again.
        #
        # Swallowing it here means the request's transaction still commits
        # whatever the turn already did (a tool's booking or ticket, each in
        # its own savepoint) — the same direction every other failure path
        # takes. If the transaction itself is broken, the commit fails and is
        # logged as `db_transaction_rolled_back`. Type only in the log: a
        # driver error can quote caller details.
        logger.error(
            "voice_stream_failed",
            error=type(exc).__name__,
            partial_speech=spoke,
            recovered=True,
        )
        yield frames.content(_FALLBACK_MESSAGE)
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
        caller_number=caller_number,
        dispatch_service=dispatch_service,
        appointment_service=appointment_service,
        customer_service=customer_service,
        savepoints=savepoints,
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
    savepoints: Savepoints,
) -> None:
    """The three downstream syncs, in a fixed order, each isolated from the
    others and from the turn.

    Each runs in its own savepoint and any failure — not only a
    `DomainError` — is logged and contained. These run after the caller has
    heard the whole reply; an unexpected database error here used to escape
    the generator, roll back the request, and take the turn with it: the
    emergency ticket the caller had just been told was logged, the booking
    they had just heard confirmed. Now a failed sync costs only its own
    writes, which the next turn's sync repeats anyway."""

    async def _dispatch() -> None:
        # A ticket created here has its emergency alert queued in the same
        # savepoint (the outbox); it is sent only after the request commits.
        await dispatch_service.sync_ticket_from_outcome(
            result.organization_id, result.conversation_id
        )

    async def _appointment() -> None:
        await appointment_service.sync_appointment_from_outcome(
            result.organization_id, result.conversation_id
        )

    async def _customer() -> None:
        # Last, so it can link whichever ticket/appointment the two calls
        # above just created. `caller_number` is P5 association capture.
        await customer_service.sync_customer_from_outcome(
            result.organization_id, result.conversation_id, caller_number=caller_number
        )

    for event, sync in (
        ("dispatch_sync_failed", _dispatch),
        ("appointment_sync_failed", _appointment),
        ("customer_sync_failed", _customer),
    ):
        try:
            async with savepoints.isolate():
                await sync()
        except DomainError as exc:
            logger.warning(
                event,
                error=exc.__class__.__name__,
                message=exc.message,
                vapi_call_id=vapi_call_id,
            )
        except Exception as exc:
            # Type only, never the message: a driver error quotes the
            # offending parameters, which here are caller PII.
            logger.error(event, error=type(exc).__name__, vapi_call_id=vapi_call_id)


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
    if not isinstance(message, dict):
        logger.warning("vapi_event_malformed")
        return {"status": "ok"}
    message_type = message.get("type")

    if message_type != "end-of-call-report":
        logger.info("vapi_event_ignored", type=message_type)
        return {"status": "ok"}

    call = message.get("call") or {}
    vapi_call_id = call.get("id") if isinstance(call, dict) else None
    if not vapi_call_id or not isinstance(vapi_call_id, str):
        logger.warning("vapi_end_of_call_report_missing_call_id")
        return {"status": "ok"}

    await service.handle_end_of_call_report(
        vapi_call_id=vapi_call_id,
        ended_reason=_bounded_text(message.get("endedReason"), _ENDED_REASON_MAX_LENGTH),
        duration_seconds=_duration_seconds(
            _first_present(message, "durationSeconds", "duration")
        ),
        recording_url=_recording_url(
            _first_present(message, "recordingUrl", "stereoRecordingUrl")
        ),
    )
    return {"status": "ok"}


def _bounded_text(value: Any, max_length: int) -> str | None:
    """A Vapi enum-ish string, cut to its column. `endedReason` values are
    Vapi's own vocabulary and some of its error reasons run long; an
    over-long one would fail the end-of-call write, leaving the call never
    marked ended and its conversation never completed."""
    if not isinstance(value, str):
        return None
    return value[:max_length]


def _duration_seconds(value: Any) -> int | None:
    """Vapi reports duration as a number, sometimes fractional; `int()` of
    a string such as "12.5" raised and turned the report into a 500."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _recording_url(value: Any) -> str | None:
    """Dropped rather than truncated when too long: a cut URL is a broken
    link that looks like a working one."""
    if not isinstance(value, str) or len(value) > _RECORDING_URL_MAX_LENGTH:
        return None
    return value


def _caller_number(payload: VapiChatCompletionRequest) -> str | None:
    """The caller ID, in a form every column it reaches can hold.

    Kept verbatim whenever it fits, so the keys existing caller-identity
    associations were recorded under still match. Only a value too long for
    a phone column — a SIP URI rather than a number — is reduced to its
    digits, and dropped if even that does not fit. Unlike a number the
    caller states, this one is only ever a lookup hint, so losing it costs
    recognition, never a record."""
    raw = payload.call.customer.number if payload.call.customer else None
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if len(raw) <= _CALLER_NUMBER_MAX_LENGTH:
        return raw
    reduced = storable_phone_number(raw)
    logger.warning("vapi_caller_number_too_long", reduced_to_digits=reduced is not None)
    return reduced


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
