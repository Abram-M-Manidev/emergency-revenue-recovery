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
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
    AIToolPhase,
)
from app.domain.ai.tools import BOOK_APPOINTMENT, ToolErrors, ToolInvocation
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.exceptions import AIProviderUnavailableError
from app.infrastructure.ai.streaming_json import StreamingStringFieldExtractor
from app.shared.logging.timing import elapsed_ms, now

logger = structlog.get_logger("app.ai.openai")

_TOOL_ROUNDS_EXHAUSTED = "The AI Brain kept requesting tools without producing a reply."
_EMPTY_RESPONSE = "OpenAI returned an empty response."

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
        messages = self._messages_for(request)
        max_rounds = self._tool_rounds_for(request)
        booking_failed_unrecovered = False

        # `extra_body` carries `reasoning_effort` rather than the SDK's own
        # parameter: openai==1.59.6 predates GPT-5 and types that parameter
        # as Literal["low", "medium", "high"], which would reject the valid
        # "minimal" value. `extra_body` is merged into the request JSON
        # verbatim, so the wire format is identical either way. Omitted
        # entirely when unset, because sending it to a non-reasoning model
        # (gpt-4.1-mini) is a 400.
        for round_index in range(max_rounds + 1):
            allow_tools = request.tools_enabled and round_index < max_rounds
            with _translated_api_errors():
                response = await client.chat.completions.create(  # type: ignore[call-overload]
                    model=config.model,
                    messages=messages,
                    response_format={"type": "json_schema", "json_schema": _JSON_SCHEMA},
                    extra_body=self._extra_body_for(config),
                    **self._tool_kwargs(request, allow_tools),
                )

            message = response.choices[0].message
            raw_tool_calls = message.tool_calls or []
            # `allow_tools` gates *acting* on tool calls, not just offering
            # them. Without that gate the loop would execute tools returned
            # on the final round too, so `AI_MAX_TOOL_ROUNDS=2` allowed three
            # executions — a budget that silently did not hold.
            if raw_tool_calls and allow_tools:
                accumulated = [
                    _AccumulatedToolCall(
                        id=call.id, name=call.function.name, arguments=call.function.arguments
                    )
                    for call in raw_tool_calls
                ]
                messages.append(_assistant_tool_call_message(accumulated))
                # `any_failed` is unused on this path: it forwards nothing
                # mid-turn, so it has no provisional speech to withhold. The
                # booking flags are used, because whether the turn may hang
                # up has to be identical on both transports.
                outcome = await self._run_tools(request, accumulated)
                messages.extend(outcome.messages)
                booking_failed_unrecovered = _updated_booking_state(
                    booking_failed_unrecovered, outcome
                )
                continue

            if message.content is None:
                # A model that spent its last round asking for tools instead
                # of answering is a different fault from an empty response,
                # and needs a different diagnosis in the logs.
                raise AIProviderUnavailableError(
                    _TOOL_ROUNDS_EXHAUSTED if raw_tool_calls else _EMPTY_RESPONSE
                )
            return replace(
                _assemble_reply(message.content),
                booking_failed_unrecovered=booking_failed_unrecovered,
            )

        raise AIProviderUnavailableError(_TOOL_ROUNDS_EXHAUSTED)

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
        messages = self._messages_for(request)
        max_rounds = self._tool_rounds_for(request)

        # Monotonic, so a clock adjustment mid-call cannot produce a
        # negative duration. All `*_ms` fields below are measured from
        # `started_at` and describe *this process's* view only — nothing
        # here observes the caller's speech, Vapi's endpointing, or TTS.
        started_at = now()
        first_output_at: float | None = None
        total_chunks = 0
        # Turn-scoped, not round-scoped: once a tool has failed, every
        # later round's narration is provisional until that round
        # proves it is the last one.
        tool_failure_this_turn = False
        # Set when a booking attempt fails, cleared when a later attempt in
        # the same turn succeeds. Attached to the reply so `AIBrainService`
        # can withhold completion for this turn only.
        booking_failed_unrecovered = False
        logger.info(
            "ai_stream_started",
            model=config.model,
            profile=request.profile.value,
            history_turns=len(request.history),
            tools_enabled=request.tools_enabled,
        )

        try:
            for round_index in range(max_rounds + 1):
                allow_tools = request.tools_enabled and round_index < max_rounds
                extractor = StreamingStringFieldExtractor("message_to_customer")
                raw: list[str] = []
                tool_calls: dict[int, _AccumulatedToolCall] = {}
                spoke_this_round = False
                # The decoded sentence, not the raw JSON. Echoed back to the
                # model with its own tool-call turn so the next round knows
                # what the caller has already been told and does not repeat
                # it.
                spoken_this_round: list[str] = []
                # Content is normally forwarded the instant it is decoded —
                # that is the whole point of streaming, and what keeps the
                # line from going quiet. The one exception is a round that
                # follows a failed tool: there the model tends to narrate the
                # failure *and* retry in the same response, and the narration
                # is only true until the retry succeeds. On 2026-08-23 a
                # caller heard "I apologize, I was not able to book the 3
                # o'clock appointment" and, three seconds later, a correct
                # confirmation of that same 3 o'clock slot.
                #
                # So after a failure this round's words are held until the
                # round ends and we can see whether tools were requested
                # again. Recovery continues -> the words were provisional and
                # are dropped. Nothing follows -> the failure is real and the
                # caller hears it. Only rounds after a failure are ever held,
                # so the ordinary path streams exactly as before.
                hold_provisional_content = tool_failure_this_turn
                held_content: list[str] = []

                with _translated_api_errors():
                    stream = await client.chat.completions.create(  # type: ignore[call-overload]
                        model=config.model,
                        messages=messages,
                        response_format={"type": "json_schema", "json_schema": _JSON_SCHEMA},
                        extra_body=self._extra_body_for(config),
                        stream=True,
                        **self._tool_kwargs(request, allow_tools),
                    )
                    async for chunk in stream:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta

                        if delta.tool_calls:
                            # Deliberately unconditional. This used to be
                            # guarded on `not spoke_this_round`, on the
                            # assumption that a response carrying both text
                            # and tool calls could not happen. It happens
                            # roughly a third of the time: with
                            # `response_format=json_schema` *and* `tools`,
                            # the model routinely announces what it is about
                            # to do and requests the tool in the same
                            # response, and because `message_to_customer` is
                            # the schema's first property the content always
                            # streams first. The guard therefore discarded
                            # the tool call precisely when the model had
                            # already promised the caller it would act — the
                            # 2026-08-22 call ended on `silence-timed-out`
                            # for exactly this reason, after the assistant
                            # said "please hold on a moment while I do that"
                            # and then never did it.
                            _accumulate_tool_calls(tool_calls, delta.tool_calls)
                            continue

                        piece = delta.content
                        if not piece:
                            continue
                        if tool_calls:
                            # Content arriving after tool calls in the same
                            # round belongs to a response we are about to
                            # discard in favour of the tool round.
                            continue
                        raw.append(piece)
                        total_chunks += 1
                        text = extractor.feed(piece)
                        if text:
                            if hold_provisional_content:
                                # Not yielded, so `spoke_this_round` stays
                                # false and the transport still covers this
                                # round with a progress phrase.
                                held_content.append(text)
                                continue
                            spoke_this_round = True
                            spoken_this_round.append(text)
                            if first_output_at is None:
                                # Time to the first *speakable* character,
                                # which is the number P2 exists to reduce —
                                # not the first JSON token, which the caller
                                # never hears.
                                first_output_at = now()
                                logger.info(
                                    "ai_stream_first_output",
                                    elapsed_ms=elapsed_ms(started_at, first_output_at),
                                )
                            yield AITextDelta(text)

                if tool_calls and allow_tools:
                    accumulated = [tool_calls[index] for index in sorted(tool_calls)]
                    # Announced before the tools run, so the transport can
                    # cover the round-trip with a holding phrase instead of
                    # silence — unless the model already covered it itself,
                    # which `spoke_this_round` reports.
                    yield AIToolPhase(
                        tuple(call.name for call in accumulated),
                        model_already_spoke=spoke_this_round,
                    )
                    # Anything already streamed has reached the caller's ear
                    # and cannot be unsaid, so it travels back as the
                    # assistant's own turn. Without it the next round would
                    # have no record of the announcement and would be liable
                    # to greet the caller again from scratch.
                    #
                    # `raw` — the partial JSON document behind that speech —
                    # is deliberately dropped: its decision fields describe a
                    # turn taken before the tools ran, so only the final
                    # round's document may become the authoritative reply.
                    messages.append(
                        _assistant_tool_call_message(
                            accumulated, content="".join(spoken_this_round) or None
                        )
                    )
                    if held_content:
                        # Recovery is continuing, so whatever the model said
                        # about the last failure has been overtaken. Dropped
                        # rather than spoken, and dropped from `messages` too:
                        # the caller never heard it, so telling the next round
                        # it was said would be a lie in the transcript.
                        logger.info(
                            "ai_stream_provisional_content_discarded",
                            chars=len("".join(held_content)),
                            retrying_with=[call.name for call in accumulated],
                        )
                    outcome = await self._run_tools(request, accumulated)
                    messages.extend(outcome.messages)
                    tool_failure_this_turn = tool_failure_this_turn or outcome.any_failed
                    booking_failed_unrecovered = _updated_booking_state(
                        booking_failed_unrecovered, outcome
                    )
                    continue

                content = "".join(raw)
                if not content:
                    # Same distinction as the non-streaming path: a final
                    # round spent asking for tools is a loop that ran out of
                    # budget, not an empty response.
                    raise AIProviderUnavailableError(
                        _TOOL_ROUNDS_EXHAUSTED if tool_calls else _EMPTY_RESPONSE
                    )

                reply = replace(
                    _assemble_reply(content),
                    booking_failed_unrecovered=booking_failed_unrecovered,
                )
                if held_content:
                    # No further tool round followed, so the failure this
                    # described is the real outcome and the caller must hear
                    # it. Emitted as one delta rather than progressively —
                    # the round is already over — which costs nothing the
                    # caller notices, because the progress phrase has been
                    # covering this stretch since before the tools ran.
                    if first_output_at is None:
                        first_output_at = now()
                    logger.info(
                        "ai_stream_held_content_released",
                        chars=len("".join(held_content)),
                    )
                    yield AITextDelta("".join(held_content))
                logger.info(
                    "ai_stream_completed",
                    elapsed_ms=elapsed_ms(started_at),
                    first_output_ms=(
                        None
                        if first_output_at is None
                        else elapsed_ms(started_at, first_output_at)
                    ),
                    chunks=total_chunks,
                    tool_rounds=round_index,
                    reply_chars=len(reply.message_to_customer),
                )
                yield AIReplyComplete(reply)
                return

            raise AIProviderUnavailableError(_TOOL_ROUNDS_EXHAUSTED)
        except (GeneratorExit, asyncio.CancelledError):
            # The consumer stopped iterating — on a live call this is Vapi
            # abandoning the turn. Distinguished from a provider failure,
            # which raises `AIProviderUnavailableError` and is logged by
            # whoever handles it. Re-raised untouched: swallowing either of
            # these would corrupt generator/task shutdown.
            logger.info(
                "ai_stream_aborted",
                elapsed_ms=elapsed_ms(started_at),
                # `total_chunks`, not a per-round counter: the round-local
                # buffer may not exist yet if the abort landed before the
                # first request was even issued.
                chunks=total_chunks,
                produced_output=first_output_at is not None,
            )
            raise

    # --- shared request construction (identical for both paths) ---

    def _messages_for(self, request: AIRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": request.system_prompt}]
        for turn in request.history:
            role = "user" if turn.role == "customer" else "assistant"
            messages.append({"role": role, "content": turn.content})
        messages.append({"role": "user", "content": request.latest_customer_message})
        return messages

    def _extra_body_for(self, config: _ProfileConfig) -> dict[str, Any] | None:
        if config.reasoning_effort is None:
            return None
        return {"reasoning_effort": config.reasoning_effort}

    def _tool_rounds_for(self, request: AIRequest) -> int:
        """Zero when tools are off, so the loop below collapses to exactly
        the single request/response this class made before tools existed —
        no behavioural change for the text path or for any caller that
        doesn't opt in."""
        return self._settings.AI_MAX_TOOL_ROUNDS if request.tools_enabled else 0

    def _tool_kwargs(self, request: AIRequest, allow_tools: bool) -> dict[str, Any]:
        """Built as kwargs rather than passing `tools=None`, because the SDK
        distinguishes "absent" from "null" and a null is a 400.

        `strict` binds the model to the declared schema, which matters more
        here than usual: a hallucinated argument name would otherwise reach
        the executor as a missing required field and cost the caller a whole
        turn of re-asking for something they already said.

        Tools are withheld on the final round on purpose. That round has no
        budget left to act on a tool call, so offering tools would invite a
        response the loop must then discard — the model is instead forced to
        answer the caller with what it already knows."""
        if not allow_tools:
            return {}
        return {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": definition.name,
                        "description": definition.description,
                        "parameters": definition.parameters,
                        "strict": True,
                    },
                }
                for definition in request.tools
            ]
        }

    async def _run_tools(
        self, request: AIRequest, calls: list[_AccumulatedToolCall]
    ) -> _ToolRoundOutcome:
        """Executes each tool call and renders the results as `role: tool`
        messages, plus whether any of them failed.

        The failure flag drives what the caller hears next: after a failed
        tool the model routinely narrates the failure *and* retries in the
        same breath, so its narration has to be held until we know whether a
        retry follows. See `stream_reply`.

        Strictly sequential, never `asyncio.gather`. Every executor here
        ultimately writes through one request-scoped `AsyncSession`, and a
        SQLAlchemy async session is not safe for concurrent use — running two
        tools at once would interleave statements on the same connection.
        The tools are indexed database calls measured in milliseconds, so
        there is nothing to win by parallelising them anyway."""
        executor = request.tool_executor
        assert executor is not None  # guaranteed by `tools_enabled`

        messages: list[dict[str, Any]] = []
        any_failed = False
        booking_failed = False
        booking_succeeded = False
        for call in calls:
            invocation = _decode_invocation(call)
            if invocation is None:
                # Arguments that are not valid JSON never reach an executor.
                # Reported back as an ordinary failed tool result so the
                # model can retry the call properly, rather than raising and
                # ending the turn.
                logger.warning("ai_tool_arguments_undecodable", tool_name=call.name)
                any_failed = True
                if call.name == BOOK_APPOINTMENT.name:
                    booking_failed = True
                messages.append(
                    _tool_result_message(
                        call.id, {"success": False, "error": ToolErrors.INVALID_ARGUMENTS}
                    )
                )
                continue
            result = await executor.execute(invocation)
            succeeded = bool(result.content.get("success"))
            if not succeeded:
                any_failed = True
            # The provider naming one business tool is deliberate: whether a
            # turn may hang up depends on what that specific tool did, and
            # the alternative — a predicate threaded down from the
            # application layer — would be more plumbing for no more meaning.
            if invocation.name == BOOK_APPOINTMENT.name:
                if succeeded:
                    booking_succeeded = True
                else:
                    booking_failed = True
            messages.append(_tool_result_message(result.id, result.content))
        return _ToolRoundOutcome(
            messages=messages,
            any_failed=any_failed,
            booking_failed=booking_failed,
            booking_succeeded=booking_succeeded,
        )


@dataclass(frozen=True, slots=True)
class _ToolRoundOutcome:
    """What one round of tool execution produced.

    `booking_failed`/`booking_succeeded` are tracked separately from
    `any_failed` because they drive a different decision: whether the turn is
    allowed to end the call. A failed availability check is recoverable
    conversation; a failed booking the model then apologises for and hangs up
    on is a caller left with nothing."""

    messages: list[dict[str, Any]]
    any_failed: bool
    booking_failed: bool
    booking_succeeded: bool


@dataclass
class _AccumulatedToolCall:
    """One tool call, reassembled from however many stream deltas carried
    it. Mutable and not slotted because it is filled in incrementally as
    fragments arrive — the id, the name, and each slice of the argument
    JSON can all land in different chunks."""

    id: str = ""
    name: str = ""
    arguments: str = ""


def _accumulate_tool_calls(target: dict[int, _AccumulatedToolCall], deltas: Any) -> None:
    """Merges streamed tool-call fragments by their `index`.

    `index`, not `id`, is the key: the id arrives in the first fragment
    only, while every later fragment carries just an argument slice and the
    index. Keying on id would drop everything after the first chunk."""
    for delta in deltas:
        call = target.setdefault(delta.index, _AccumulatedToolCall())
        if delta.id:
            call.id = delta.id
        function = getattr(delta, "function", None)
        if function is None:
            continue
        if function.name:
            call.name = function.name
        if function.arguments:
            call.arguments += function.arguments


def _decode_invocation(call: _AccumulatedToolCall) -> ToolInvocation | None:
    """Turns a reassembled call into a domain `ToolInvocation`, or None when
    the accumulated arguments are not a JSON object.

    Returning None rather than raising keeps the "a tool never ends a call"
    guarantee reaching all the way down to malformed model output."""
    try:
        arguments = json.loads(call.arguments or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, dict):
        return None
    return ToolInvocation(id=call.id, name=call.name, arguments=arguments)


def _assistant_tool_call_message(
    calls: list[_AccumulatedToolCall], content: str | None = None
) -> dict[str, Any]:
    """The assistant turn that asked for the tools, echoed back verbatim.

    Required by the API: a `role: tool` message is only valid as a reply to
    an assistant message containing the matching `tool_call_id`. Omitting it
    is a 400, not a silent degradation.

    `content` carries whatever the model said in that same response — the
    common "I'll check that for you now" that arrives alongside a tool call.
    The caller has already heard it, so the next round is told about it
    rather than left to rediscover the conversation."""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments or "{}"},
            }
            for call in calls
        ],
    }


def _tool_result_message(tool_call_id: str, content: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps(content)}


def _updated_booking_state(previously_failed: bool, outcome: _ToolRoundOutcome) -> bool:
    """Carries the failed-booking flag across rounds of one turn.

    A success anywhere in the turn clears it, and clears it even when the
    same round also contains a failure: parallel attempts where one lands are
    a booked appointment, which is exactly the case that must still be
    allowed to end the call."""
    if outcome.booking_succeeded:
        return False
    return previously_failed or outcome.booking_failed


@contextmanager
def _translated_api_errors() -> Iterator[None]:
    """Maps the SDK's transport failures onto the one domain error the whole
    app already knows how to make speakable.

    Extracted so the streaming and non-streaming paths cannot drift on which
    exceptions they translate — they previously carried two identical copies
    of this three-branch block, and the tool loop would have made it four."""
    try:
        yield
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


def _assemble_reply(content: str) -> AIReply:
    """The single place a complete response becomes an `AIReply`, shared by
    the streaming and non-streaming paths so the two can never diverge on
    validation or field mapping.

    Decodes the *first* JSON document rather than requiring the response to
    be exactly one. A live streamed turn produced two schema-conforming
    documents concatenated with a newline, and `json.loads` rejected the
    pair outright ("Extra data") — costing the caller a turn they should
    have heard. Taking the first is the only reading consistent with what
    was actually spoken: `StreamingStringFieldExtractor` stops at the first
    `message_to_customer`, so the first document is by definition the one
    whose sentence reached the caller. Validation is unchanged — whatever is
    decoded still has to satisfy `_ReplyPayload`."""
    document, _ = json.JSONDecoder().raw_decode(content.lstrip())
    payload = _ReplyPayload.model_validate(document)
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
