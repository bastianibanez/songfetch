"""One global throttle for every yt-dlp request the process makes.

A single web search is already a dozen round-trips. A bulk run is that times
the length of the file, and the naive version of "in parallel" -- one search
per worker, each with its own pool of detail lookups -- multiplies out to a
hundred concurrent requests and a 429 within seconds. Once YouTube starts
answering 429 it keeps doing so for a while, so the fastest bulk run is the one
that never triggers it rather than the one that recovers well.

Hence a limiter that is global rather than per-search: a cap on requests in
flight, a floor on the gap between starts, and a penalty box that widens that
floor when a 429 does slip through and lets it decay back down when it stops.

Everything here is thread-based, because that is what `probe` runs in.
"""

from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager
from typing import Iterator

# Four in flight is comfortably under what triggers throttling and still keeps
# a twelve-lookup hydration inside three round-trips of wall time.
MAX_CONCURRENT = 4

# Floor on the gap between two request starts. Concurrency alone is not enough:
# four workers finishing together fire four requests in the same millisecond.
MIN_INTERVAL = 0.25

# What a 429 costs. The interval is multiplied by this and the whole limiter
# stops issuing for COOLDOWN seconds.
PENALTY_FACTOR = 2.0
COOLDOWN = 20.0
MAX_INTERVAL = 8.0

# How fast the penalty wears off once requests are succeeding again.
RECOVERY_FACTOR = 0.9
RECOVERY_AFTER = 30.0


class RateLimiter:
    """Concurrency cap plus a spacing floor, shared by every caller.

    Not a general-purpose token bucket: the thing being protected is a remote
    service with an opaque budget, so the policy is deliberately conservative
    and reacts to being told off rather than trying to model the limit.
    """

    def __init__(
        self,
        max_concurrent: int = MAX_CONCURRENT,
        min_interval: float = MIN_INTERVAL,
        cooldown: float = COOLDOWN,
    ) -> None:
        self.base_interval = min_interval
        self.cooldown = cooldown
        self._sem = threading.BoundedSemaphore(max(1, max_concurrent))
        self._lock = threading.Lock()
        self._interval = min_interval
        self._next_start = 0.0
        self._blocked_until = 0.0
        self._last_penalty = 0.0
        self.penalties = 0

    # -- policy ------------------------------------------------------------

    def penalize(self) -> None:
        """Called when a response smells like 429. Widens the gap for everyone."""
        with self._lock:
            now = time.monotonic()
            self.penalties += 1
            self._last_penalty = now
            self._interval = min(MAX_INTERVAL, self._interval * PENALTY_FACTOR)
            self._blocked_until = max(self._blocked_until, now + self.cooldown)

    def _recover(self, now: float) -> None:
        """Walk the interval back down after a clean stretch. Caller holds the lock."""
        if self._interval <= self.base_interval:
            return
        if now - self._last_penalty < RECOVERY_AFTER:
            return
        self._interval = max(self.base_interval, self._interval * RECOVERY_FACTOR)
        self._last_penalty = now

    @property
    def interval(self) -> float:
        with self._lock:
            return self._interval

    # -- acquisition -------------------------------------------------------

    def _reserve(self) -> float:
        """Claim the next start slot and report how long to wait for it."""
        with self._lock:
            now = time.monotonic()
            self._recover(now)
            # Jitter keeps workers released from a cooldown together from
            # re-converging into the same burst that earned the penalty.
            start = max(now, self._next_start, self._blocked_until)
            start += random.uniform(0, self._interval * 0.25)
            self._next_start = start + self._interval
            return start - now

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold one request's worth of budget for the duration of the block."""
        self._sem.acquire()
        try:
            delay = self._reserve()
            if delay > 0:
                time.sleep(delay)
            yield
        finally:
            self._sem.release()


# The process-wide instance. A limiter that is not shared is not a limiter:
# every search, bulk or otherwise, spends from this one budget.
LIMITER = RateLimiter()


# yt-dlp reports throttling as a warning on a retry and as an error when it
# gives up, so the text is the only reliable signal in either case.
_THROTTLED = ("http error 429", "too many requests")


def looks_throttled(output: str) -> bool:
    lowered = output.lower()
    return any(needle in lowered for needle in _THROTTLED)
