"""Capturing structlog events in tests, correctly.

Two traps this exists to avoid:

`structlog.testing.capture_logs()` swaps the processor chain for a bare
capture, which drops `merge_contextvars`. The voice-path telemetry binds
its correlation fields (`vapi_call_id`, `turn_id`, `conversation_id`, ...)
into contextvars rather than passing them to every call, so that helper
would report events with their correlation stripped — the exact property
the tests need to assert.

`configure_logging()` sets `cache_logger_on_first_use=True`, which is a
documented one-way door: once a logger has been used, later `configure()`
calls do not reach it. A second `capture_events()` in the same process
would then silently capture nothing. Caching is therefore turned off for
the remainder of the test process — it is purely a performance
optimisation, and reconfigurability matters more here.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import structlog


@contextlib.contextmanager
def capture_events() -> Iterator[list[dict[str, Any]]]:
    """Yields the list events are appended to, with contextvars merged in."""
    capture = structlog.testing.LogCapture()
    original = dict(structlog.get_config())

    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, capture],
        wrapper_class=original["wrapper_class"],
        context_class=original["context_class"],
        logger_factory=original["logger_factory"],
        cache_logger_on_first_use=False,
    )
    try:
        yield capture.entries
    finally:
        structlog.configure(**{**original, "cache_logger_on_first_use": False})


def names(entries: list[dict[str, Any]]) -> list[str]:
    return [entry.get("event") for entry in entries]


def only(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """The single event with this name, asserting there is exactly one."""
    matches = [entry for entry in entries if entry.get("event") == name]
    assert len(matches) == 1, f"expected exactly one {name!r}, got {len(matches)}"
    return matches[0]
