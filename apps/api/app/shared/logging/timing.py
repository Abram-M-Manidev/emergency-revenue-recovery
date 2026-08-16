"""One place that decides what a duration field means.

Every `*_ms` field in the voice-path telemetry is produced here, so no
call site has to remember the unit, the rounding, or that the clock must
be monotonic. `time.perf_counter()` rather than wall clock: a clock
adjustment mid-call would otherwise be able to produce a negative
duration in an incident timeline.

Scope note: every duration built from these marks describes what *this
process* observed. Nothing here can see the caller's speech, Vapi's
endpointing decision, or when TTS audio actually reached the caller —
those are telephony-side measurements and must not be inferred from
these numbers.
"""

from __future__ import annotations

import time


def now() -> float:
    """A monotonic mark to measure from later."""
    return time.perf_counter()


def elapsed_ms(start: float, end: float | None = None) -> float:
    """Milliseconds from `start` to `end` (default: now), 2 decimals."""
    return round(((time.perf_counter() if end is None else end) - start) * 1000, 2)
