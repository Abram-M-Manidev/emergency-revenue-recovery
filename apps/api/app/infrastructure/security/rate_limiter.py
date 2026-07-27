"""In-memory, fixed-window rate limiter.

Deliberately not a pip dependency (e.g. `slowapi`) — the algorithm is
narrow and well-understood, matching this codebase's existing preference
for small hand-rolled primitives over a new library for a problem this
size (see the `hmac.compare_digest`-based webhook auth in
`app/api/deps.py`).

Per-process only: under the production compose's multi-worker uvicorn,
each worker holds its own counters, so the real ceiling is roughly
(limit x worker count) — an accepted trade-off for Milestone 10, not a
correctness bug. `check()` never awaits, so despite running under asyncio
it never yields control mid-update — no lock is needed.
"""

from __future__ import annotations

import time


class InMemoryRateLimiter:
    """Fixed-window limiter: each key gets `limit` calls per
    `window_seconds`, then the window resets once it elapses."""

    def __init__(
        self,
        *,
        sweep_interval_seconds: float = 300.0,
        max_entry_age_seconds: float = 3600.0,
    ) -> None:
        self._windows: dict[str, tuple[float, int]] = {}
        self._sweep_interval_seconds = sweep_interval_seconds
        self._max_entry_age_seconds = max_entry_age_seconds
        self._last_swept_at = time.monotonic()

    def check(self, key: str, *, limit: int, window_seconds: float) -> bool:
        """Returns True if this call is allowed, False if `key` has already
        used up its `limit` calls within the current window."""
        now = time.monotonic()
        self._sweep_if_due(now)

        window_start, count = self._windows.get(key, (now, 0))
        if now - window_start >= window_seconds:
            window_start, count = now, 0

        count += 1
        self._windows[key] = (window_start, count)
        return count <= limit

    def _sweep_if_due(self, now: float) -> None:
        """Drops entries whose window is long expired, so memory doesn't
        grow unbounded with one-off/rotating keys (e.g. many distinct IPs)."""
        if now - self._last_swept_at < self._sweep_interval_seconds:
            return
        self._last_swept_at = now
        stale_keys = [
            key
            for key, (window_start, _count) in self._windows.items()
            if now - window_start >= self._max_entry_age_seconds
        ]
        for key in stale_keys:
            del self._windows[key]
