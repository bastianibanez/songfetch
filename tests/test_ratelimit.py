"""The global throttle. Pure timing arithmetic -- no yt-dlp, no sleeping."""

from __future__ import annotations

import threading
import time

from ytmp3.ratelimit import MAX_INTERVAL, RateLimiter, looks_throttled


def test_a_429_warning_is_recognised():
    assert looks_throttled("WARNING: Unable to download webpage: HTTP Error 429")


def test_the_prose_form_is_recognised_too():
    assert looks_throttled("ERROR: Too Many Requests")


def test_an_ordinary_failure_is_not_throttling():
    assert not looks_throttled("ERROR: Video unavailable")


def test_concurrency_is_capped():
    """Two at a time means the third waits, however many threads ask."""
    limiter = RateLimiter(max_concurrent=2, min_interval=0)
    live = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()

    def worker():
        nonlocal live, peak
        with limiter.slot():
            with lock:
                live += 1
                peak = max(peak, live)
            release.wait(1.0)
            with lock:
                live -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    release.set()
    for t in threads:
        t.join(2.0)

    assert peak <= 2


def test_starts_are_spaced_even_when_slots_are_free():
    """Concurrency alone is not enough: four idle workers fire together."""
    limiter = RateLimiter(max_concurrent=4, min_interval=0.05)
    starts: list[float] = []

    def worker():
        with limiter.slot():
            starts.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5.0)

    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g > 0 for g in gaps), gaps
    assert sum(gaps) >= 0.05, "four starts collapsed into one instant"


def test_a_penalty_widens_the_interval_for_everyone():
    limiter = RateLimiter(min_interval=0.1, cooldown=0)
    before = limiter.interval
    limiter.penalize()
    assert limiter.interval > before
    assert limiter.penalties == 1


def test_the_interval_stops_growing_at_the_ceiling():
    """A long outage must not park the limiter for minutes at a time."""
    limiter = RateLimiter(min_interval=1.0, cooldown=0)
    for _ in range(20):
        limiter.penalize()
    assert limiter.interval == MAX_INTERVAL


def test_a_cooldown_blocks_the_next_start():
    limiter = RateLimiter(max_concurrent=4, min_interval=0, cooldown=0.2)
    limiter.penalize()

    start = time.monotonic()
    with limiter.slot():
        waited = time.monotonic() - start

    assert waited >= 0.15, f"cooldown was not honoured ({waited:.3f}s)"
