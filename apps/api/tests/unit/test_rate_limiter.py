import time

from app.infrastructure.security.rate_limiter import InMemoryRateLimiter


def test_allows_calls_within_the_limit():
    limiter = InMemoryRateLimiter()
    for _ in range(5):
        assert limiter.check("key", limit=5, window_seconds=60.0) is True


def test_rejects_calls_past_the_limit():
    limiter = InMemoryRateLimiter()
    for _ in range(5):
        limiter.check("key", limit=5, window_seconds=60.0)

    assert limiter.check("key", limit=5, window_seconds=60.0) is False


def test_different_keys_have_independent_windows():
    limiter = InMemoryRateLimiter()
    for _ in range(5):
        limiter.check("key-a", limit=5, window_seconds=60.0)

    assert limiter.check("key-a", limit=5, window_seconds=60.0) is False
    assert limiter.check("key-b", limit=5, window_seconds=60.0) is True


def test_window_resets_after_it_elapses():
    limiter = InMemoryRateLimiter()
    window_seconds = 0.05
    for _ in range(3):
        limiter.check("key", limit=3, window_seconds=window_seconds)
    assert limiter.check("key", limit=3, window_seconds=window_seconds) is False

    time.sleep(window_seconds * 1.5)

    assert limiter.check("key", limit=3, window_seconds=window_seconds) is True


def test_sweep_drops_stale_entries():
    limiter = InMemoryRateLimiter(sweep_interval_seconds=0.0, max_entry_age_seconds=0.01)
    limiter.check("stale-key", limit=10, window_seconds=60.0)
    assert "stale-key" in limiter._windows

    time.sleep(0.02)
    limiter.check("new-key", limit=10, window_seconds=60.0)

    assert "stale-key" not in limiter._windows
    assert "new-key" in limiter._windows
