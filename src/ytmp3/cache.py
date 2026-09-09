"""Session-scoped memo for web searches.

A web search costs about ten seconds and a dozen yt-dlp round-trips. The same
query typed twice in one sitting -- which is what happens every time someone
backspaces over a result list and types it again -- should cost nothing.

Deliberately process-local and time-boxed: YouTube's result set does move, just
not inside the few minutes a user spends picking a track.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Hashable

TTL_SECONDS = 900

# 128 queries is far more than one session produces; the bound exists so a
# long-running TUI cannot grow without limit, not to manage scarcity.
MAX_ENTRIES = 128

_WHITESPACE = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    """Cache key for a free-text query.

    ``"Bicep  GLUE"`` and ``"bicep glue"`` are the same search. Casing and
    stray spacing are exactly the two ways a user retypes a query they already
    ran, so both have to collapse or the cache never hits.
    """
    return _WHITESPACE.sub(" ", query.strip()).casefold()


class TTLCache:
    """Small thread-safe cache with a per-entry expiry.

    Written to from worker threads and read from the event loop, hence the
    lock. Expiry is checked on read rather than swept: entries are few and a
    background sweeper would be more machinery than the problem deserves.
    """

    def __init__(self, ttl: float = TTL_SECONDS, max_entries: int = MAX_ENTRIES) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[Hashable, tuple[float, Any]] = {}

    def get(self, key: Hashable) -> Any | None:
        """Value for `key`, or None when absent or stale.

        A cached `None` is indistinguishable from a miss; callers cache result
        lists, and an empty list is falsy but still a hit, so they must test
        `is not None` rather than truthiness.
        """
        now = time.monotonic()
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            expires_at, value = hit
            if expires_at <= now:
                del self._entries[key]
                return None
            return value

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            if len(self._entries) >= self.max_entries and key not in self._entries:
                # Oldest insertion first: dicts preserve insertion order, and
                # the eviction policy matters far less than having one.
                self._entries.pop(next(iter(self._entries)), None)
            self._entries[key] = (time.monotonic() + self.ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
