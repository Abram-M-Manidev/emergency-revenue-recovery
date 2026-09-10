"""Unit tests for the tool loop inside `OpenAIProvider`.

`test_voice_tool_executor.py` proves that a tool call does real work.
This module proves the other half: that a tool call actually *happens* —
that OpenAI's streamed tool-call fragments are reassembled correctly, that
the results are fed back in the shape the API requires, and that the loop
terminates.

Driven by a scripted stand-in for `AsyncOpenAI`, injected the same way
`test_openai_provider.py` already injects one (`provider._clients[...]`).
Nothing here makes a network call.

The chunk boundaries below are deliberately hostile — an id in one frame,
a function name in another, argument JSON split mid-token across three —
because that is what a real stream does and it is exactly where a naive
accumulator breaks.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.domain.ai.provider import (
    AIModelProfile,
    AIReplyComplete,
    AIRequest,
    AITextDelta,
    AIToolPhase,
)
from app.domain.ai.tools import (
    CHECK_AVAILABILITY,
    CREATE_SERVICE_REQUEST,
    ToolErrors,
    ToolExecutor,
    ToolInvocation,
    ToolResult,
)
from app.domain.exceptions import AIProviderUnavailableError
from app.infrastructure.ai.openai_provider import OpenAIProvider

_REPLY_DOC = json.dumps(
    {
        "message_to_customer": "You're confirmed for Monday at 8 AM.",
        "classification": "non_emergency",
        "confidence": 0.95,
        "recommended_action": "book_appointment",
        "matched_service_name": "Air Conditioning Repair",
        "customer_name": "Lucky",
        "customer_phone": "123456789",
        "customer_address": "16th Street, California",
        "is_conversation_complete": True,
        "summary": "Booked an AC repair visit.",
    }
)


class _RecordingExecutor(ToolExecutor):
    """Captures what the provider actually asked for, and replies with a
    canned result so the loop can continue."""

    def __init__(self, content: dict | None = None) -> None:
        self.invocations: list[ToolInvocation] = []
        self._content = content or {"success": True, "slots": []}

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        self.invocations.append(invocation)
        return ToolResult(id=invocation.id, name=invocation.name, content=self._content)


def _tool_call_delta(index: int, *, call_id: str = "", name: str = "", arguments: str = ""):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _chunk(*, content: str | None = None, tool_calls: list | None = None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


class _ScriptedCompletions:
    """Returns one scripted round per `create()` call, recording the kwargs
    and the message history it was handed each time."""

    def __init__(self, rounds: list[list]) -> None:
        self._rounds = rounds
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        chunks = self._rounds[min(len(self.calls) - 1, len(self._rounds) - 1)]
        if kwargs.get("stream"):
            return _ScriptedStream(chunks)
        return _as_completion(chunks)


class _ScriptedStream:
    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self._chunks:
            yield chunk


def _as_completion(chunks: list):
    """Collapses a scripted stream into the non-streamed message shape, so
    one script drives both provider paths."""
    content = "".join(
        c.choices[0].delta.content for c in chunks if c.choices[0].delta.content
    ) or None
    calls: dict[int, dict] = {}
    for chunk in chunks:
        for delta in chunk.choices[0].delta.tool_calls or []:
            slot = calls.setdefault(delta.index, {"id": "", "name": "", "arguments": ""})
            if delta.id:
                slot["id"] = delta.id
            if delta.function.name:
                slot["name"] = delta.function.name
            if delta.function.arguments:
                slot["arguments"] += delta.function.arguments
    tool_calls = [
        SimpleNamespace(
            id=slot["id"],
            function=SimpleNamespace(name=slot["name"], arguments=slot["arguments"]),
        )
        for _, slot in sorted(calls.items())
    ] or None
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


class _ScriptedClient:
    def __init__(self, rounds: list[list]) -> None:
        self.chat = SimpleNamespace(completions=_ScriptedCompletions(rounds))


def _provider(
    rounds: list[list], monkeypatch: pytest.MonkeyPatch, **setting_overrides: object
) -> tuple[OpenAIProvider, _ScriptedClient]:
    settings = get_settings()
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-fake-key-for-testing")
    for key, value in setting_overrides.items():
        monkeypatch.setattr(settings, key, value)
    provider = OpenAIProvider(settings)
    client = _ScriptedClient(rounds)
    provider._clients[AIModelProfile.REALTIME] = client  # type: ignore[assignment]
    return provider, client


def _request(executor: ToolExecutor | None, *, with_tools: bool = True) -> AIRequest:
    return AIRequest(
        system_prompt="You are the after-hours assistant.",
        history=(),
        latest_customer_message="My AC is running but not cooling.",
        profile=AIModelProfile.REALTIME,
        tools=(CHECK_AVAILABILITY, CREATE_SERVICE_REQUEST) if with_tools else (),
        tool_executor=executor,
    )


async def _drain(provider: OpenAIProvider, request: AIRequest):
    deltas: list[str] = []
    phases: list[AIToolPhase] = []
    reply = None
    async for event in provider.stream_reply(request):
        if isinstance(event, AITextDelta):
            deltas.append(event.text)
        elif isinstance(event, AIToolPhase):
            phases.append(event)
        elif isinstance(event, AIReplyComplete):
            reply = event.reply
    return deltas, phases, reply


# --- The loop ----------------------------------------------------------------


# One tool call spread across four frames, matching how OpenAI actually
# streams them: the id and function name arrive whole in the first frame,
# and only `arguments` is fragmented — here split mid-key and mid-value,
# which is where a naive accumulator breaks.
_SPLIT_TOOL_ROUND = [
    _chunk(tool_calls=[_tool_call_delta(0, call_id="call_abc", name="check_availability")]),
    _chunk(tool_calls=[_tool_call_delta(0, arguments='{"service_na')]),
    _chunk(tool_calls=[_tool_call_delta(0, arguments='me": "Air Conditioning Repair", "prefe')]),
    _chunk(tool_calls=[_tool_call_delta(0, arguments='rred_date": "2026-08-24"}')]),
]
_REPLY_ROUND = [_chunk(content=piece) for piece in (_REPLY_DOC[:40], _REPLY_DOC[40:])]


@pytest.mark.asyncio
async def test_a_streamed_tool_call_reaches_the_executor_fully_reassembled(
    monkeypatch: pytest.MonkeyPatch,
):
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPLIT_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    _, _, reply = await _drain(provider, _request(executor))

    assert len(executor.invocations) == 1
    invocation = executor.invocations[0]
    # The argument JSON was split mid-key and mid-value across three frames.
    assert invocation.id == "call_abc"
    assert invocation.name == "check_availability"
    assert invocation.arguments == {
        "service_name": "Air Conditioning Repair",
        "preferred_date": "2026-08-24",
    }
    assert reply is not None
    assert reply.message_to_customer == "You're confirmed for Monday at 8 AM."


@pytest.mark.asyncio
async def test_the_tool_round_speaks_nothing_and_announces_a_tool_phase(
    monkeypatch: pytest.MonkeyPatch,
):
    """The caller must not hear a syllable until the tools have run — that is
    what stops a promise preceding its result."""
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPLIT_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    deltas, phases, _ = await _drain(provider, _request(executor))

    assert [p.tool_names for p in phases] == [("check_availability",)]
    assert "".join(deltas) == "You're confirmed for Monday at 8 AM."


@pytest.mark.asyncio
async def test_the_tool_result_is_fed_back_in_the_shape_the_api_requires(
    monkeypatch: pytest.MonkeyPatch,
):
    """A `role: tool` message is only valid as a reply to an assistant
    message carrying the matching `tool_call_id`; omitting it is a 400."""
    executor = _RecordingExecutor({"success": True, "slots": [{"start_time": "08:00"}]})
    provider, client = _provider([_SPLIT_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    await _drain(provider, _request(executor))

    second_round_messages = client.chat.completions.calls[1]["messages"]
    assistant_turn, tool_turn = second_round_messages[-2], second_round_messages[-1]

    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["content"] is None
    assert assistant_turn["tool_calls"][0]["id"] == "call_abc"
    assert assistant_turn["tool_calls"][0]["function"]["name"] == "check_availability"

    assert tool_turn["role"] == "tool"
    assert tool_turn["tool_call_id"] == "call_abc"
    assert json.loads(tool_turn["content"]) == {
        "success": True,
        "slots": [{"start_time": "08:00"}],
    }


@pytest.mark.asyncio
async def test_two_tool_calls_in_one_round_are_kept_apart_by_index(
    monkeypatch: pytest.MonkeyPatch,
):
    """Fragments are keyed by `index`, not `id` — the id only arrives in the
    first frame, so keying on it would drop every later fragment."""
    parallel_round = [
        _chunk(
            tool_calls=[
                _tool_call_delta(0, call_id="call_a", name="check_availability"),
                _tool_call_delta(1, call_id="call_b", name="create_service_request"),
            ]
        ),
        _chunk(
            tool_calls=[
                _tool_call_delta(0, arguments='{"days_to_search": 3}'),
                _tool_call_delta(1, arguments='{"customer_name": "Lucky"}'),
            ]
        ),
    ]
    executor = _RecordingExecutor()
    provider, _ = _provider([parallel_round, _REPLY_ROUND], monkeypatch)

    await _drain(provider, _request(executor))

    assert [i.name for i in executor.invocations] == [
        "check_availability",
        "create_service_request",
    ]
    assert executor.invocations[0].arguments == {"days_to_search": 3}
    assert executor.invocations[1].arguments == {"customer_name": "Lucky"}


@pytest.mark.asyncio
async def test_undecodable_arguments_never_reach_the_executor(
    monkeypatch: pytest.MonkeyPatch,
):
    """Truncated argument JSON is reported back as an ordinary failed tool
    result so the model can retry, rather than raising and ending the turn."""
    broken_round = [
        _chunk(
            tool_calls=[
                _tool_call_delta(
                    0, call_id="call_x", name="check_availability", arguments='{"service_na'
                )
            ]
        )
    ]
    executor = _RecordingExecutor()
    provider, client = _provider([broken_round, _REPLY_ROUND], monkeypatch)

    _, _, reply = await _drain(provider, _request(executor))

    assert executor.invocations == []
    tool_turn = client.chat.completions.calls[1]["messages"][-1]
    assert json.loads(tool_turn["content"]) == {
        "success": False,
        "error": ToolErrors.INVALID_ARGUMENTS,
    }
    # The turn still completed and the caller still got a sentence.
    assert reply is not None


@pytest.mark.asyncio
async def test_a_model_that_only_ever_calls_tools_fails_loudly_instead_of_looping(
    monkeypatch: pytest.MonkeyPatch,
):
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPLIT_TOOL_ROUND], monkeypatch, AI_MAX_TOOL_ROUNDS=2)

    with pytest.raises(AIProviderUnavailableError):
        await _drain(provider, _request(executor))

    # Bounded by the configured round limit, not unbounded.
    assert len(executor.invocations) == 2


@pytest.mark.asyncio
async def test_tools_are_withheld_on_the_final_round(monkeypatch: pytest.MonkeyPatch):
    """The last round has no budget left to act on a tool call, so offering
    tools would only invite a response the loop must discard."""
    executor = _RecordingExecutor()
    provider, client = _provider([_SPLIT_TOOL_ROUND], monkeypatch, AI_MAX_TOOL_ROUNDS=1)

    with pytest.raises(AIProviderUnavailableError):
        await _drain(provider, _request(executor))

    assert "tools" in client.chat.completions.calls[0]
    assert "tools" not in client.chat.completions.calls[1]


@pytest.mark.asyncio
async def test_tool_definitions_are_sent_in_strict_mode(monkeypatch: pytest.MonkeyPatch):
    executor = _RecordingExecutor()
    provider, client = _provider([_SPLIT_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    await _drain(provider, _request(executor))

    tools = client.chat.completions.calls[0]["tools"]
    assert [t["function"]["name"] for t in tools] == [
        "check_availability",
        "create_service_request",
    ]
    assert all(t["function"]["strict"] is True for t in tools)
    assert all(t["function"]["parameters"]["additionalProperties"] is False for t in tools)


@pytest.mark.asyncio
async def test_a_request_without_tools_behaves_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch,
):
    """The pre-tool path must be untouched: one request, no `tools` key."""
    provider, client = _provider([_REPLY_ROUND], monkeypatch)

    deltas, phases, reply = await _drain(provider, _request(None, with_tools=False))

    assert len(client.chat.completions.calls) == 1
    assert "tools" not in client.chat.completions.calls[0]
    assert phases == []
    assert reply is not None
    assert "".join(deltas) == "You're confirmed for Monday at 8 AM."


@pytest.mark.asyncio
async def test_tools_are_ignored_when_no_executor_is_wired(
    monkeypatch: pytest.MonkeyPatch,
):
    """A half-wired request degrades to the pre-tool behaviour rather than
    crashing mid-call."""
    provider, client = _provider([_REPLY_ROUND], monkeypatch)

    request = AIRequest(
        system_prompt="x",
        history=(),
        latest_customer_message="y",
        profile=AIModelProfile.REALTIME,
        tools=(CHECK_AVAILABILITY,),
        tool_executor=None,
    )
    _, _, reply = await _drain(provider, request)

    assert reply is not None
    assert "tools" not in client.chat.completions.calls[0]


# --- Content and tool calls in the SAME response (the 2026-08-22 regression) --
#
# With `response_format=json_schema` and `tools` sent together, the model
# routinely announces what it is about to do *and* requests the tool in one
# response. Measured on six identical live requests: two of the six. Because
# `message_to_customer` is the schema's first property, the announcement
# always streams before the tool-call deltas.
#
# The provider used to ignore tool calls once any text had been spoken. The
# result on a real phone call was the assistant saying "please hold on a
# moment while I do that", never doing it, and Vapi ending the call on
# `silence-timed-out`.

_ANNOUNCEMENT = json.dumps(
    {
        "message_to_customer": "I'll check our availability for you now.",
        "classification": "non_emergency",
        "confidence": 0.9,
        "recommended_action": "book_appointment",
        "matched_service_name": "Air Conditioning Repair",
        "customer_name": "Laki",
        "customer_phone": "123456789",
        "customer_address": "16 California",
        "is_conversation_complete": False,
        "summary": "Caller reports AC running but not cooling.",
    }
)

# Content first, then the tool call — the exact production ordering.
_SPOKE_THEN_TOOL_ROUND = [
    *[_chunk(content=piece) for piece in (_ANNOUNCEMENT[:45], _ANNOUNCEMENT[45:])],
    _chunk(tool_calls=[_tool_call_delta(0, call_id="call_spoke", name="check_availability")]),
    _chunk(tool_calls=[_tool_call_delta(0, arguments='{"service_name": "Air Conditioning Repair"}')]),
]


@pytest.mark.asyncio
async def test_tool_calls_survive_content_arriving_first(monkeypatch: pytest.MonkeyPatch):
    """The regression itself. Before the fix the executor was never called."""
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPOKE_THEN_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    await _drain(provider, _request(executor))

    assert [i.name for i in executor.invocations] == ["check_availability"]
    assert executor.invocations[0].arguments == {"service_name": "Air Conditioning Repair"}


@pytest.mark.asyncio
async def test_the_announcement_still_reaches_the_caller(monkeypatch: pytest.MonkeyPatch):
    """The fix must not buy correctness by withholding speech — the whole
    point of streaming is that the caller hears something immediately."""
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPOKE_THEN_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    deltas, _, _ = await _drain(provider, _request(executor))

    assert "".join(deltas).startswith("I'll check our availability for you now.")


@pytest.mark.asyncio
async def test_the_tool_phase_reports_that_the_model_already_spoke(
    monkeypatch: pytest.MonkeyPatch,
):
    """So the transport can skip a holding phrase that would be the same
    sentence twice."""
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPOKE_THEN_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    _, phases, _ = await _drain(provider, _request(executor))

    assert len(phases) == 1
    assert phases[0].tool_names == ("check_availability",)
    assert phases[0].model_already_spoke is True


@pytest.mark.asyncio
async def test_a_silent_tool_round_still_reports_no_speech(monkeypatch: pytest.MonkeyPatch):
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPLIT_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    _, phases, _ = await _drain(provider, _request(executor))

    assert phases[0].model_already_spoke is False


@pytest.mark.asyncio
async def test_the_announcement_never_becomes_the_final_reply(
    monkeypatch: pytest.MonkeyPatch,
):
    """The intermediate document describes a turn taken *before* the tools
    ran. Only the final round's document is authoritative — otherwise the
    assistant's decision fields would predate the result they claim to
    reflect."""
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPOKE_THEN_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    _, _, reply = await _drain(provider, _request(executor))

    assert reply is not None
    assert reply.message_to_customer == "You're confirmed for Monday at 8 AM."
    assert reply.is_conversation_complete is True  # the announcement said False


@pytest.mark.asyncio
async def test_the_spoken_announcement_is_echoed_back_to_the_next_round(
    monkeypatch: pytest.MonkeyPatch,
):
    """The caller has heard it, so the next round must know it was said —
    otherwise the model has no record of its own turn and may greet the
    caller again from scratch."""
    executor = _RecordingExecutor()
    provider, client = _provider([_SPOKE_THEN_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    await _drain(provider, _request(executor))

    assistant_turn = client.chat.completions.calls[1]["messages"][-2]
    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["content"] == "I'll check our availability for you now."
    assert assistant_turn["tool_calls"][0]["id"] == "call_spoke"
    # And the raw JSON document behind that speech is NOT echoed.
    assert "message_to_customer" not in (assistant_turn["content"] or "")


@pytest.mark.asyncio
async def test_the_tool_result_reaches_the_next_round_after_speech(
    monkeypatch: pytest.MonkeyPatch,
):
    executor = _RecordingExecutor({"success": True, "slots": [{"start_time": "09:00"}]})
    provider, client = _provider([_SPOKE_THEN_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    await _drain(provider, _request(executor))

    tool_turn = client.chat.completions.calls[1]["messages"][-1]
    assert tool_turn["role"] == "tool"
    assert tool_turn["tool_call_id"] == "call_spoke"
    assert json.loads(tool_turn["content"])["slots"] == [{"start_time": "09:00"}]


@pytest.mark.asyncio
async def test_book_appointment_also_survives_content_first_ordering(
    monkeypatch: pytest.MonkeyPatch,
):
    """The mechanism is tool-agnostic, and it is strictly worse here: the
    model would announce "I'll book that now", the call would be dropped, and
    the caller would hold for a booking that never happened."""
    booking_round = [
        _chunk(content=_ANNOUNCEMENT[:45]),
        _chunk(content=_ANNOUNCEMENT[45:]),
        _chunk(tool_calls=[_tool_call_delta(0, call_id="call_book", name="book_appointment")]),
        _chunk(
            tool_calls=[
                _tool_call_delta(0, arguments='{"date": "2026-08-24", "start_time": "09:00"}')
            ]
        ),
    ]
    executor = _RecordingExecutor({"success": True, "appointment_id": "appt-1"})
    provider, _ = _provider([booking_round, _REPLY_ROUND], monkeypatch)

    await _drain(provider, _request(executor))

    assert [i.name for i in executor.invocations] == ["book_appointment"]
    assert executor.invocations[0].arguments == {"date": "2026-08-24", "start_time": "09:00"}


@pytest.mark.asyncio
async def test_content_arriving_after_tool_calls_is_not_spoken(
    monkeypatch: pytest.MonkeyPatch,
):
    """Trailing content belongs to a document the loop is about to discard;
    speaking it would leak a pre-tool decision to the caller."""
    trailing = [
        _chunk(tool_calls=[_tool_call_delta(0, call_id="call_t", name="check_availability")]),
        _chunk(tool_calls=[_tool_call_delta(0, arguments="{}")]),
        _chunk(content='{"message_to_customer":"stale text that must not be spoken"'),
    ]
    executor = _RecordingExecutor()
    provider, _ = _provider([trailing, _REPLY_ROUND], monkeypatch)

    deltas, phases, reply = await _drain(provider, _request(executor))

    assert "stale text" not in "".join(deltas)
    assert phases[0].model_already_spoke is False
    assert reply is not None


@pytest.mark.asyncio
async def test_two_concatenated_documents_do_not_cost_the_caller_the_turn(
    monkeypatch: pytest.MonkeyPatch,
):
    """Observed live: one streamed response carried two schema-conforming
    documents separated by a newline, and strict `json.loads` rejected the
    pair, so the caller got the "trouble connecting" fallback instead of the
    sentence they had already been read.

    The first document is the one whose text was streamed, so it is the one
    the reply must be assembled from."""
    doubled = [_chunk(content=_REPLY_DOC), _chunk(content="\n" + _ANNOUNCEMENT)]
    provider, _ = _provider([doubled], monkeypatch)

    deltas, _, reply = await _drain(provider, _request(None, with_tools=False))

    assert reply is not None
    assert reply.message_to_customer == "You're confirmed for Monday at 8 AM."
    assert reply.is_conversation_complete is True
    # Only the first document's sentence was ever spoken.
    assert "".join(deltas) == "You're confirmed for Monday at 8 AM."


@pytest.mark.asyncio
async def test_a_genuinely_malformed_document_still_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    """Tolerating a trailing second document must not become tolerating
    anything — a truncated response has no valid reply in it and must still
    raise rather than reach the caller half-formed.

    `ValueError` (via `JSONDecodeError`) is the pre-existing contract, also
    asserted by `test_openai_provider.py`'s truncated-stream test; this
    pins that `raw_decode` did not quietly widen it."""
    truncated = [_chunk(content='{"message_to_customer": "half a sen')]
    provider, _ = _provider([truncated], monkeypatch)

    with pytest.raises(ValueError):
        await _drain(provider, _request(None, with_tools=False))


# --- Provisional narration after a failed tool (the 2026-08-23 regression) ----
#
# After a tool fails, the model routinely narrates the failure *and* retries
# in the same response. On a real call the caller heard "I apologize, I was
# not able to book the 3 o'clock appointment" and, three seconds later, a
# correct confirmation of that same 3 o'clock slot — because the retry
# succeeded. Words that a later round overturns must not reach the caller.


class _ScriptedFailingExecutor(ToolExecutor):
    """Fails the first N calls, then succeeds — the shape of a real recovery."""

    def __init__(self, failures: int = 1) -> None:
        self.invocations: list[ToolInvocation] = []
        self._remaining_failures = failures

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        self.invocations.append(invocation)
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            content: dict = {"success": False, "error": ToolErrors.SLOT_NOT_OFFERED}
        else:
            content = {"success": True, "appointment_id": "appt-1", "status": "confirmed"}
        return ToolResult(id=invocation.id, name=invocation.name, content=content)


_APOLOGY_DOC = json.dumps(
    {
        "message_to_customer": "I apologize. I was not able to book the 3 o'clock appointment.",
        "classification": "non_emergency",
        "confidence": 0.9,
        "recommended_action": "book_appointment",
        "matched_service_name": "Air Conditioning Repair",
        "customer_name": "Frank",
        "customer_phone": "123456789",
        "customer_address": "59th Street",
        "is_conversation_complete": False,
        "summary": "Booking attempt failed.",
    }
)

# Round 1: book, and it fails.
_FAILING_BOOK_ROUND = [
    _chunk(tool_calls=[_tool_call_delta(0, call_id="call_b1", name="book_appointment")]),
    _chunk(tool_calls=[_tool_call_delta(0, arguments='{"date": "2026-08-24", "start_time": "03:00"}')]),
]
# Round 2: apologise *and* retry — the response shape that broke the call.
_APOLOGISE_AND_RETRY_ROUND = [
    *[_chunk(content=piece) for piece in (_APOLOGY_DOC[:50], _APOLOGY_DOC[50:])],
    _chunk(tool_calls=[_tool_call_delta(0, call_id="call_c1", name="check_availability")]),
    _chunk(tool_calls=[_tool_call_delta(0, arguments="{}")]),
]
# Round 3: book again, this time successfully.
_RETRY_BOOK_ROUND = [
    _chunk(tool_calls=[_tool_call_delta(0, call_id="call_b2", name="book_appointment")]),
    _chunk(tool_calls=[_tool_call_delta(0, arguments='{"date": "2026-08-24", "start_time": "15:00"}')]),
]


@pytest.mark.asyncio
async def test_a_failure_the_model_recovers_from_is_never_spoken(
    monkeypatch: pytest.MonkeyPatch,
):
    """Case 1. The caller must not hear an apology the same turn retracts."""
    executor = _ScriptedFailingExecutor(failures=1)
    provider, _ = _provider(
        [_FAILING_BOOK_ROUND, _APOLOGISE_AND_RETRY_ROUND, _RETRY_BOOK_ROUND, _REPLY_ROUND],
        monkeypatch,
    )

    deltas, _, reply = await _drain(provider, _request(executor))

    spoken = "".join(deltas)
    assert "apolog" not in spoken.lower()
    assert "not able to book" not in spoken
    # And the turn still ended with the real answer.
    assert spoken == "You're confirmed for Monday at 8 AM."
    assert reply is not None
    assert reply.message_to_customer == "You're confirmed for Monday at 8 AM."


@pytest.mark.asyncio
async def test_recovery_still_runs_every_tool(monkeypatch: pytest.MonkeyPatch):
    """Suppressing the narration must not suppress the work."""
    executor = _ScriptedFailingExecutor(failures=1)
    provider, _ = _provider(
        [_FAILING_BOOK_ROUND, _APOLOGISE_AND_RETRY_ROUND, _RETRY_BOOK_ROUND, _REPLY_ROUND],
        monkeypatch,
    )

    await _drain(provider, _request(executor))

    assert [i.name for i in executor.invocations] == [
        "book_appointment",
        "check_availability",
        "book_appointment",
    ]
    # The successful retry used the corrected time.
    assert executor.invocations[-1].arguments == {"date": "2026-08-24", "start_time": "15:00"}


@pytest.mark.asyncio
async def test_the_caller_is_never_left_silent_during_recovery(
    monkeypatch: pytest.MonkeyPatch,
):
    """Case 4. Every held round still announces a tool phase, so the
    transport's progress phrase covers the gap the withheld words leave."""
    executor = _ScriptedFailingExecutor(failures=1)
    provider, _ = _provider(
        [_FAILING_BOOK_ROUND, _APOLOGISE_AND_RETRY_ROUND, _RETRY_BOOK_ROUND, _REPLY_ROUND],
        monkeypatch,
    )

    _, phases, _ = await _drain(provider, _request(executor))

    assert [p.tool_names for p in phases] == [
        ("book_appointment",),
        ("check_availability",),
        ("book_appointment",),
    ]
    # The held round reports no speech, so the transport knows to fill it.
    assert [p.model_already_spoke for p in phases] == [False, False, False]


@pytest.mark.asyncio
async def test_a_terminal_failure_is_still_spoken(monkeypatch: pytest.MonkeyPatch):
    """Case 2. Nothing recovers, so the failure is the real outcome and the
    caller has to hear it — withholding it would leave them with silence and
    no explanation."""
    executor = _ScriptedFailingExecutor(failures=1)
    terminal = [_chunk(content=piece) for piece in (_APOLOGY_DOC[:50], _APOLOGY_DOC[50:])]
    provider, _ = _provider([_FAILING_BOOK_ROUND, terminal], monkeypatch)

    deltas, _, reply = await _drain(provider, _request(executor))

    assert "".join(deltas) == (
        "I apologize. I was not able to book the 3 o'clock appointment."
    )
    assert reply is not None
    assert reply.is_conversation_complete is False


@pytest.mark.asyncio
async def test_narration_before_any_failure_is_streamed_immediately(
    monkeypatch: pytest.MonkeyPatch,
):
    """Case 3/6. The holding-back is scoped to rounds *after* a failure —
    the ordinary announce-and-act response must still reach the caller at
    once, which is what fixed the `silence-timed-out` call."""
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPOKE_THEN_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    deltas, phases, _ = await _drain(provider, _request(executor))

    assert "".join(deltas).startswith("I'll check our availability for you now.")
    assert phases[0].model_already_spoke is True
    assert [i.name for i in executor.invocations] == ["check_availability"]


@pytest.mark.asyncio
async def test_the_final_confirmation_is_still_spoken_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
):
    """Case 5. Holding the apology must not swallow the answer that follows."""
    executor = _ScriptedFailingExecutor(failures=1)
    provider, _ = _provider(
        [_FAILING_BOOK_ROUND, _APOLOGISE_AND_RETRY_ROUND, _RETRY_BOOK_ROUND, _REPLY_ROUND],
        monkeypatch,
    )

    deltas, _, reply = await _drain(provider, _request(executor))

    assert "".join(deltas) == "You're confirmed for Monday at 8 AM."
    assert reply is not None and reply.is_conversation_complete is True


@pytest.mark.asyncio
async def test_withheld_narration_is_not_echoed_back_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
):
    """The caller never heard it, so telling the next round it was said would
    put a sentence in the transcript that was never spoken."""
    executor = _ScriptedFailingExecutor(failures=1)
    provider, client = _provider(
        [_FAILING_BOOK_ROUND, _APOLOGISE_AND_RETRY_ROUND, _RETRY_BOOK_ROUND, _REPLY_ROUND],
        monkeypatch,
    )

    await _drain(provider, _request(executor))

    assistant_turns = [
        message
        for call in client.chat.completions.calls
        for message in call["messages"]
        if message.get("role") == "assistant"
    ]
    assert all("apolog" not in (turn.get("content") or "").lower() for turn in assistant_turns)


# --- The round budget ---------------------------------------------------------


def test_the_round_budget_means_that_many_tool_rounds():
    """`AI_MAX_TOOL_ROUNDS` counts rounds that may *execute tools*; the loop
    runs one more with tools withheld, in which the model must answer. So the
    ceiling on model calls per turn is N+1, and it is a fixed range — there is
    no path that iterates freely."""
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.AI_MAX_TOOL_ROUNDS == 5

    tool_rounds = [i for i in range(settings.AI_MAX_TOOL_ROUNDS + 1) if i < settings.AI_MAX_TOOL_ROUNDS]
    assert len(tool_rounds) == 5
    assert settings.AI_MAX_TOOL_ROUNDS + 1 == 6  # total model calls


@pytest.mark.asyncio
async def test_the_budget_still_bounds_a_model_that_only_calls_tools(
    monkeypatch: pytest.MonkeyPatch,
):
    """Raising the ceiling must not have removed it."""
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPLIT_TOOL_ROUND], monkeypatch, AI_MAX_TOOL_ROUNDS=5)

    with pytest.raises(AIProviderUnavailableError):
        await _drain(provider, _request(executor))

    assert len(executor.invocations) == 5


# --- The non-streaming path --------------------------------------------------


@pytest.mark.asyncio
async def test_the_non_streaming_path_runs_the_same_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    """`generate_reply` backs the text/simulation dashboard, which must reach
    the same records as a phone call."""
    executor = _RecordingExecutor()
    provider, client = _provider([_SPLIT_TOOL_ROUND, _REPLY_ROUND], monkeypatch)

    reply = await provider.generate_reply(_request(executor))

    assert len(client.chat.completions.calls) == 2
    assert executor.invocations[0].name == "check_availability"
    assert reply.message_to_customer == "You're confirmed for Monday at 8 AM."
    assert reply.is_conversation_complete is True


@pytest.mark.asyncio
async def test_the_non_streaming_path_also_bounds_its_rounds(
    monkeypatch: pytest.MonkeyPatch,
):
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPLIT_TOOL_ROUND], monkeypatch, AI_MAX_TOOL_ROUNDS=2)

    with pytest.raises(AIProviderUnavailableError):
        await provider.generate_reply(_request(executor))

    assert len(executor.invocations) == 2
