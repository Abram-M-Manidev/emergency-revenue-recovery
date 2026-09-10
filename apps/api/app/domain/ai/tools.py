"""The business tools the AI Brain may invoke mid-turn: provider-agnostic
definitions, the request/result value objects, and the port that executes
them.

Kept in `domain` for the same reason `ai/provider.py` is: it has zero
framework or infrastructure imports. `OpenAIProvider` (infrastructure)
translates these into OpenAI's `tools` wire format; `VoiceToolExecutor`
(application) implements `ToolExecutor` and is the only thing that touches
services or the database.

Why tools exist at all
----------------------
Before them the AI Brain produced its entire spoken turn in one shot and
the backend reacted to `recommended_action` *afterwards* (see
`conversation_outcome.py`'s docstring and the `sync_*_from_outcome`
methods). That made the required booking flow structurally impossible: the
model had to finish speaking before any backend action ran, and never saw a
result. The live call of 2026-08-22 is exactly that failure — the assistant
said "I will schedule your air conditioning repair appointment" while the
appointment row it referred to sat at `status=REQUESTED` with
`scheduled_start_at=NULL`, because nothing in the system could check
availability or book a time.

Tools do not replace the outcome-sync seam. `create_service_request` writes
the same `ConversationOutcome` that seam already reads, so Dispatch,
Customers, and Analytics keep working unchanged whether a turn used tools
or not.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One callable tool, described the way every major LLM provider
    describes them: a name, a sentence of intent, and a JSON Schema for the
    arguments. Deliberately not an OpenAI `ChatCompletionToolParam` — that
    mapping belongs to `OpenAIProvider`, exactly as model names already do."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """A tool call the model asked for. `arguments` is already decoded; a
    payload that would not parse never becomes a `ToolInvocation` (see
    `OpenAIProvider._decode_invocation`), so executors never have to defend
    against malformed JSON."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What the model receives back. `content` is always a JSON object with
    a boolean `success` key, so the assistant branches on the *result*
    rather than on prose — the single property the whole "never claim an
    action that did not happen" rule rests on."""

    id: str
    name: str
    content: dict[str, Any]


class ToolExecutor(ABC):
    """Port for running a tool the model asked for.

    Implementations must never raise. Every failure — validation, timeout,
    an unknown tool, an unexpected exception — has to come back as a
    `ToolResult` carrying `success: false` and a machine-readable `error`
    code. A raised exception would abort a live turn mid-sentence, which on
    a phone call is a silent hang-up; a structured failure instead lets the
    assistant apologise, ask for what is missing, or offer another slot."""

    @abstractmethod
    async def execute(self, invocation: ToolInvocation) -> ToolResult: ...


class ToolExecutorFactory(ABC):
    """Produces an executor already scoped to one conversation.

    The two-step (factory, then bind) exists to make cross-tenant execution
    unrepresentable rather than merely avoided. `AIBrainService` binds the
    organization and conversation it resolved from the authenticated request
    — a `VoiceLine` lookup on the phone path, the JWT on the text path — and
    the bound executor carries them for its whole life. No tool schema
    accepts an organization id, and no executor reads one from arguments, so
    there is nothing a model could emit, or a caller could say aloud, that
    redirects a write to another tenant.

    `turn_index` travels the same way and for the same reason. It is the
    count of conversation messages already persisted when this turn began —
    a number the backend owns outright — and it is what lets
    `select_appointment_slot` tell "the caller answered our offer" from "the
    model offered and immediately claimed an answer". Passed in rather than
    queried by the executor because `AIBrainService` has already read the
    history to build the prompt; re-reading it per tool call would be a
    second query with a chance to disagree with the first."""

    @abstractmethod
    def bind(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        turn_index: int,
    ) -> ToolExecutor: ...

    @abstractmethod
    async def describe_progress(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> str | None:
        """What this call has already accomplished, as prompt text — or None
        when nothing has happened yet.

        Tool results are not part of the stored transcript; only the spoken
        sentences are. So on every new turn the model sees what it *said* but
        not what it *did*, and re-derives the whole plan from scratch. A
        verification call on 2026-08-22 showed exactly that: across seven
        turns the model called `create_service_request` seven times and
        `check_availability` ten times, never once reaching
        `book_appointment`, because each turn it believed it was starting the
        intake again.

        This closes that gap with facts rather than memory. The state is read
        back out of the database — the service request that exists, whether a
        time is booked — so it is true regardless of what the model recalls,
        and survives a retry, a superseded turn, or a worker restart.

        Lives on this port rather than on a repository because
        `AIBrainService` deliberately knows nothing about Dispatch or
        Appointments (see `conversation_outcome.py`). It already depends on
        this factory to *perform* business actions; asking the same seam what
        it has already performed adds no new coupling."""
        ...


class ToolErrors:
    """Stable `error` codes for failed tool results.

    Machine-readable strings rather than prose, so the system prompt can
    name the ones the assistant must recover from and tests can assert on
    them without matching English."""

    MISSING_REQUIRED_FIELDS = "MISSING_REQUIRED_FIELDS"
    NO_SERVICE_REQUEST = "NO_SERVICE_REQUEST"
    EMERGENCY_NOT_BOOKABLE = "EMERGENCY_NOT_BOOKABLE"
    SLOT_UNAVAILABLE = "SLOT_UNAVAILABLE"
    SLOT_NOT_OFFERED = "SLOT_NOT_OFFERED"
    #: The caller was read this time but has not chosen it. Distinct
    #: from SLOT_NOT_OFFERED because the recovery is "ask them which one",
    #: not "go and fetch real times".
    SLOT_NOT_SELECTED = "SLOT_NOT_SELECTED"
    #: A selection was attempted in the same turn the slot was offered,
    #: so the caller has not spoken since hearing it.
    SLOT_NOT_YET_HEARD = "SLOT_NOT_YET_HEARD"
    SLOT_IN_THE_PAST = "SLOT_IN_THE_PAST"
    OUTSIDE_BUSINESS_HOURS = "OUTSIDE_BUSINESS_HOURS"
    INVALID_SLOT = "INVALID_SLOT"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


CREATE_SERVICE_REQUEST = ToolDefinition(
    name="create_service_request",
    description=(
        "Record the caller's service request in the business's system. Call "
        "this once you know the caller's name, callback phone number, "
        "service address, and what is wrong. This creates the real record "
        "staff will work from, so call it before checking availability or "
        "booking. Safe to call again if a detail was corrected."
    ),
    parameters={
        "type": "object",
        "properties": {
            "customer_name": {
                "type": "string",
                "description": "The caller's full name as they gave it.",
            },
            "customer_phone": {
                "type": "string",
                "description": "The callback number the caller stated.",
            },
            "service_address": {
                "type": "string",
                "description": "The address the technician should visit.",
            },
            "problem_description": {
                "type": "string",
                "description": (
                    "One or two sentences describing the reported problem, in "
                    "the caller's own terms."
                ),
            },
            "classification": {
                "type": "string",
                "enum": ["emergency", "non_emergency"],
                "description": (
                    "Use the business's own emergency rules from the system "
                    "prompt. 'emergency' opens a dispatch ticket; "
                    "'non_emergency' opens a bookable appointment request."
                ),
            },
            "service_name": {
                "type": ["string", "null"],
                "description": (
                    "The exact name of the matching service from the "
                    "'Services offered' list, or null if none clearly "
                    "matches. Determines the visit length."
                ),
            },
        },
        "required": [
            "customer_name",
            "customer_phone",
            "service_address",
            "problem_description",
            "classification",
            "service_name",
        ],
        "additionalProperties": False,
    },
)

CHECK_AVAILABILITY = ToolDefinition(
    name="check_availability",
    description=(
        "Look up real, currently-bookable appointment slots. This is the "
        "ONLY source of appointment times — never invent, guess, or recall "
        "a time that did not come from this tool. Returns slots in the "
        "business's local timezone."
    ),
    parameters={
        "type": "object",
        "properties": {
            "service_name": {
                "type": ["string", "null"],
                "description": (
                    "Exact service name from the 'Services offered' list, so "
                    "the slot length matches the job. Null uses a default "
                    "visit length."
                ),
            },
            "preferred_date": {
                "type": ["string", "null"],
                "description": (
                    "YYYY-MM-DD in the business's local timezone. Null starts "
                    "the search from the earliest bookable time."
                ),
            },
            "earliest_time": {
                "type": ["string", "null"],
                "description": "HH:MM, 24-hour, local. Null means no lower bound.",
            },
            "latest_time": {
                "type": ["string", "null"],
                "description": (
                    "HH:MM, 24-hour, local — the latest acceptable START "
                    "time. Null means no upper bound."
                ),
            },
            "days_to_search": {
                "type": ["integer", "null"],
                "description": "How many days forward to search. Null uses the default.",
            },
        },
        "required": [
            "service_name",
            "preferred_date",
            "earliest_time",
            "latest_time",
            "days_to_search",
        ],
        "additionalProperties": False,
    },
)

BOOK_APPOINTMENT = ToolDefinition(
    name="book_appointment",
    description=(
        "Reserve an appointment time the caller has chosen. Call this "
        "straight after select_appointment_slot has confirmed their choice — "
        "do not offer the same list again. Booking a time that has not been "
        "recorded by select_appointment_slot will fail with "
        "SLOT_NOT_SELECTED. Identify the time EITHER by slot_id (if you called "
        "check_availability in this same turn) OR by date and start_time, "
        "which you can take from the times you already read out. The slot is "
        "re-verified as still free before anything is written, so this can "
        "fail with SLOT_UNAVAILABLE even for a time that was free moments "
        "ago. Only after this returns success may you tell the caller the "
        "appointment is confirmed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "slot_id": {
                "type": ["string", "null"],
                "description": (
                    "The slot_id copied verbatim from a check_availability "
                    "result in THIS turn. Null if you are booking a time you "
                    "offered on an earlier turn — use date and start_time "
                    "for that."
                ),
            },
            "date": {
                "type": ["string", "null"],
                "description": (
                    "YYYY-MM-DD in the business's local timezone, for the "
                    "time the caller chose. Required whenever slot_id is null."
                ),
            },
            "start_time": {
                "type": ["string", "null"],
                "description": (
                    "HH:MM, 24-hour, local — the start of the chosen time "
                    "(9 AM is \"09:00\", 2 PM is \"14:00\"). Required "
                    "whenever slot_id is null."
                ),
            },
        },
        "required": ["slot_id", "date", "start_time"],
        "additionalProperties": False,
    },
)


SELECT_APPOINTMENT_SLOT = ToolDefinition(
    name="select_appointment_slot",
    description=(
        "Record which of the times you offered the caller has just chosen. "
        "Call this the moment they pick one — including when they answer "
        "indirectly (\"the first one\", \"the 9:30\", \"yeah that works\", "
        "\"the morning one\"). You interpret what they meant; this records "
        "it. Call it again if they change their mind, which replaces the "
        "previous choice. An appointment CANNOT be booked until this has "
        "returned success for the time you are about to book. If the caller "
        "has not actually named a time, do not call this — ask them which "
        "one they want instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "slot_id": {
                "type": ["string", "null"],
                "description": (
                    "The slot_id copied verbatim from a check_availability "
                    "result, if you still have it. Null otherwise."
                ),
            },
            "date": {
                "type": ["string", "null"],
                "description": (
                    "YYYY-MM-DD in the business's local timezone, for the "
                    "time the caller chose. Required whenever slot_id is null."
                ),
            },
            "start_time": {
                "type": ["string", "null"],
                "description": (
                    "HH:MM, 24-hour, local — the start of the time the caller "
                    "chose (9 AM is \"09:00\", 2 PM is \"14:00\"). Required "
                    "whenever slot_id is null."
                ),
            },
        },
        "required": ["slot_id", "date", "start_time"],
        "additionalProperties": False,
    },
)

VOICE_TOOLS: tuple[ToolDefinition, ...] = (
    CREATE_SERVICE_REQUEST,
    CHECK_AVAILABILITY,
    SELECT_APPOINTMENT_SLOT,
    BOOK_APPOINTMENT,
)
"""The tool set offered on conversations that can transact. Ordered the way
the flow runs, so the listing itself hints at the sequence."""
