"""Unit tests for the incremental JSON string extractor.

These are the tests that matter most for P2: the extractor is what stands
between OpenAI's raw JSON token stream and what the caller actually hears.
If it mis-parses, a live emergency caller hears garbage — so every chunk
boundary that could plausibly occur is exercised here, offline and
deterministically, rather than discovered on a phone call."""

from __future__ import annotations

import json

import pytest

from app.infrastructure.ai.streaming_json import StreamingStringFieldExtractor

_FIELD = "message_to_customer"


def _drain(chunks: list[str]) -> str:
    extractor = StreamingStringFieldExtractor(_FIELD)
    return "".join(extractor.feed(chunk) for chunk in chunks)


def _as_single_chars(document: str) -> list[str]:
    """The most hostile chunking possible: one character at a time, so every
    token, escape, and delimiter is split."""
    return list(document)


def test_extracts_value_from_a_single_chunk():
    doc = '{"message_to_customer": "Help is on the way.", "confidence": 0.9}'
    assert _drain([doc]) == "Help is on the way."


def test_value_split_across_many_chunks():
    """A. and B. — the ordinary streaming case."""
    chunks = ['{"message_to_cus', 'tomer": "Help is ', "on the ", 'way.", "confidence": 0.9}']
    assert _drain(chunks) == "Help is on the way."


def test_character_by_character_delivery():
    """D. and E. — no chunk boundary may change the result."""
    doc = '{"message_to_customer": "Turn off the valve, then wait.", "confidence": 1}'
    assert _drain(_as_single_chars(doc)) == "Turn off the valve, then wait."


def test_escaped_quotes_inside_the_value():
    """C. — an escaped quote must not be mistaken for the terminator."""
    doc = json.dumps({"message_to_customer": 'She said "help" loudly', "confidence": 1})
    assert _drain([doc]) == 'She said "help" loudly'


def test_escaped_quote_split_across_the_backslash():
    """The backslash arrives in one chunk and the quote in the next."""
    assert _drain(['{"message_to_customer": "say \\', '"hi\\', '" now"}']) == 'say "hi" now'


def test_escape_sequences_are_decoded():
    doc = json.dumps({"message_to_customer": "line1\nline2\ttabbed\\slash"})
    assert _drain([doc]) == "line1\nline2\ttabbed\\slash"


def test_unicode_escape_decoded_and_split_safely():
    """E. — a \\uXXXX escape split mid-sequence must not emit a partial
    character."""
    assert _drain(['{"message_to_customer": "caf\\u00e', '9 open"}']) == "café open"
    assert _drain(['{"message_to_customer": "caf\\', 'u00e9 open"}']) == "café open"


def test_surrogate_pair_split_across_chunks():
    """Emoji arrive as a \\ud83d\\ude00 pair; half a pair is not a
    character and must never be emitted alone."""
    doc = '{"message_to_customer": "ok \\ud83d\\ude00 done"}'
    assert _drain([doc]) == "ok 😀 done"
    assert _drain(_as_single_chars(doc)) == "ok 😀 done"


def test_key_split_across_chunks_is_still_found():
    assert _drain(['{"message_to_cus', 'tomer":"found"}']) == "found"


def test_whitespace_between_key_colon_and_value():
    assert _drain(['{"message_to_customer"   :    "spaced"}']) == "spaced"


def test_preceding_fields_are_ignored():
    doc = '{"other": "not this", "message_to_customer": "this one"}'
    assert _drain([doc]) == "this one"


def test_stops_at_the_closing_quote_and_ignores_later_fields():
    """Decision fields follow the value in the real schema — none of their
    text may leak into what the caller hears."""
    doc = (
        '{"message_to_customer": "Dispatching now.", '
        '"classification": "emergency", "summary": "furnace failure"}'
    )
    assert _drain([doc]) == "Dispatching now."


def test_finished_flag_and_no_further_output():
    extractor = StreamingStringFieldExtractor(_FIELD)
    extractor.feed('{"message_to_customer": "done"}')
    assert extractor.finished is True
    assert extractor.feed('{"message_to_customer": "again"}') == ""


def test_incomplete_document_yields_what_arrived():
    """Partial JSON must still produce the characters seen so far, without
    inventing a terminator."""
    extractor = StreamingStringFieldExtractor(_FIELD)
    out = extractor.feed('{"message_to_customer": "half a sen')
    assert out == "half a sen"
    assert extractor.finished is False


def test_value_never_emits_json_syntax():
    """The guarantee that protects the caller: only decoded field content
    is ever returned, never braces, quotes, or key names."""
    doc = '{"message_to_customer": "safe text", "classification": "emergency"}'
    assert _drain(_as_single_chars(doc)) == "safe text"


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11, 23])
def test_any_fixed_chunk_size_produces_identical_output(size: int):
    """The strongest statement available: chunking must be irrelevant."""
    doc = json.dumps(
        {
            "message_to_customer": 'Turn off the "main" valve — café \U0001f600\nthen wait.',
            "classification": "emergency",
        }
    )
    chunks = [doc[i : i + size] for i in range(0, len(doc), size)]
    expected = json.loads(doc)["message_to_customer"]
    assert _drain(chunks) == expected
