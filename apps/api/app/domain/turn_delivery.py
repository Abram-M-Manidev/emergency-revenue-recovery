"""What the model may treat as having been said on a voice call.

A turn is stored as two messages: the caller's words, then ERRS's reply.
When Vapi drops the request before ERRS finishes streaming the reply (the
caller kept talking, or hung up), the turn is marked interrupted. See
`VoiceService.mark_turn_interrupted`. The two halves then mean different
things:

- The caller's words DID reach ERRS. They stay in the model's history,
  unless the caller's next message repeats them. Vapi re-sends a still-growing
  utterance in full, so on the 2026-10-30 test call "Hello. I'm making a test
  call. You know, not really emergency." was followed by the same words plus
  "So my air conditioner stopped cooling...". Keeping both would show the
  model the caller saying everything twice.
- ERRS's reply did NOT reach the caller, so it is left out of the history.

"Interrupted" is decided from the transport: Vapi took the stream's final
`[DONE]` frame or it did not. That is evidence about the HTTP response, not
proof of audio playback. A reply Vapi received in full can still go unspoken,
and only Vapi's own transcript can show that (see
`voice_history_divergence`).

Pure functions; no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from difflib import SequenceMatcher
from typing import TypeVar

from app.domain.entities.conversation_message import ConversationMessage, MessageRole

T = TypeVar("T")

# How closely the start of a later message must match an earlier one for it
# to count as a re-send, compared character by character over the earlier
# message's length in words. Not 1.0: the transcriber revises words as an
# utterance grows ("stop" -> "stopped"), and an exact-prefix rule would then
# keep both copies. Character-level so that one revised word in a short
# utterance does not count as a whole word wrong.
REPEAT_SIMILARITY_FLOOR = 0.85


def normalised_words(text: str) -> list[str]:
    return "".join(ch if ch.isalnum() else " " for ch in text.lower()).split()


def repeats(earlier: str, later: str) -> bool:
    """True when `later` starts by saying `earlier` again: Vapi re-sending
    a caller's utterance with more words added."""
    first, then = normalised_words(earlier), normalised_words(later)
    if not first or len(then) < len(first):
        return False
    said, again = " ".join(first), " ".join(then[: len(first)])
    return SequenceMatcher(None, said, again).ratio() >= REPEAT_SIMILARITY_FLOOR


def model_history(
    history: list[ConversationMessage], latest_utterance: str
) -> list[ConversationMessage]:
    """The conversation as the model should see it before answering
    `latest_utterance`:
    - every reply the caller heard;
    - every caller message, except an interrupted one whose words the
      caller's next message (or `latest_utterance`) says again.

    The full record, interrupted replies included, stays in storage."""
    kept: list[ConversationMessage] = []
    for index, message in enumerate(history):
        if message.role is MessageRole.ASSISTANT:
            if message.interrupted_at is None:
                kept.append(message)
            continue
        if message.interrupted_at is not None and repeats(
            message.content, _next_caller_words(history, index, latest_utterance)
        ):
            continue
        kept.append(message)
    return kept


def delivered_once_owed(
    history: Iterable[ConversationMessage], raised_by: Callable[[str], Iterable[T]]
) -> frozenset[T]:
    """What ERRS has already said, for things it says once: on the first
    reply after the caller raises them (a safety instruction, for example).

    Something raised stays owed until a reply reaches the caller. If the
    reply that carried it was dropped, it is said on the next reply,
    whether or not the caller's next message raises it again."""
    owed: set[T] = set()
    said: set[T] = set()
    for message in history:
        if message.role is not MessageRole.CUSTOMER:
            continue
        owed.update(raised_by(message.content))
        if message.interrupted_at is None:
            said |= owed
    return frozenset(said)


def _next_caller_words(history: list[ConversationMessage], index: int, latest: str) -> str:
    for message in history[index + 1 :]:
        if message.role is MessageRole.CUSTOMER:
            return message.content
    return latest
