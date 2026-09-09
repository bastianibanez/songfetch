"""Many queries from a file, searched at once, reviewed before anything downloads.

A wishlist arrives as a list of lines, not as one query at a time, and running
`ytmp3 web` twenty times means twenty rounds of waiting and no way to see the
whole thing before committing to it.

The parsing half is pure and fixture-free; only `bulk_search` touches the
network, and it does so through the same global limiter every other request
uses -- searches run concurrently, but the *requests* they make are spaced, so
the parallelism buys wall-clock time rather than a 429.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .discover import split_artist_title, track_from_candidate
from .models import ScoredCandidate, Track
from .probe import Cancel, ProbeError, find_candidates
from .ranking import Ambiguous, pick, rank

# How many searches are in flight at once. Each one is already a dozen requests
# wide, and the limiter is what actually meters them; this only decides how
# deep the queue in front of it gets. Four keeps every limiter slot busy
# without building a backlog whose results are stale by the time they land.
BULK_WORKERS = 4

# A line starting with this is a note to the reader, not a track.
COMMENT = "#"


def parse_queries(text: str) -> list[str]:
    """One query per line, minus the blanks, comments and duplicates.

    Trailing comments are deliberately *not* stripped: "Bicep - Glue #1" is a
    plausible track name and guessing wrong silently corrupts the query. Only a
    line that starts with `#` is a comment.

    Order is preserved and repeats are dropped, because a hand-maintained
    wishlist accumulates both.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(COMMENT):
            continue
        key = " ".join(line.split()).casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def read_query_file(path: str | Path) -> list[str]:
    """Queries from a text file. Raises OSError if it cannot be read."""
    return parse_queries(Path(path).read_text(encoding="utf-8"))


# A row the user has not touched follows ranking's own verdict. Once they do
# touch it, their decision outranks the score in both directions.
TAKE = "take"
SKIP = "skip"


@dataclass
class BulkResult:
    """One query's outcome, and what the user decided to do about it."""

    query: str
    ranked: list[ScoredCandidate] = field(default_factory=list)
    # Why ranking would not choose on its own -- empty when it did.
    ambiguity: str = ""
    # Why the search itself failed -- empty when it did not.
    error: str = ""
    # "" until the user says otherwise, which is not the same as `SKIP`: an
    # untouched row still downloads if ranking was confident about it.
    decision: str = ""
    # An alternative the user picked over the top result.
    chosen: ScoredCandidate | None = None

    @property
    def best(self) -> ScoredCandidate | None:
        return self.ranked[0] if self.ranked else None

    @property
    def confident(self) -> bool:
        """Ranking picked a clear winner and no human is strictly needed."""
        return bool(self.ranked) and not self.ambiguity and not self.error

    @property
    def alternatives(self) -> list[ScoredCandidate]:
        return self.ranked[1:]

    @property
    def selection(self) -> ScoredCandidate | None:
        """What would be downloaded: the explicit pick, else the top result."""
        return self.chosen or self.best

    @property
    def wanted(self) -> bool:
        """Whether this row is in the set that `ctrl+r` would download.

        Confident rows are opted in by default and ambiguous ones are not,
        which is the whole point of the review screen: the work is confirming
        the handful ranking was unsure about, not re-approving the obvious.
        """
        if self.selection is None:
            return False
        if self.decision == SKIP:
            return False
        if self.decision == TAKE:
            return True
        return self.confident

    def track(self) -> Track | None:
        """The `Track` this result would produce, parsed off the upload."""
        picked = self.selection
        if picked is None:
            return None
        track = track_from_candidate(picked.candidate)
        if track.artist:
            return track
        # A query the user wrote as "Artist - Title" knows more than an upload
        # title that lost the artist; fall back to it rather than to nothing.
        artist, title = split_artist_title(self.query)
        if artist:
            return Track(
                id=track.id,
                title=track.title or title,
                artist=artist,
                album=track.album,
                duration=track.duration,
            )
        return track


ProgressFn = Callable[[BulkResult], None]


def _noop(_: BulkResult) -> None:
    return None


def search_one(
    query: str,
    limit: int = 6,
    cookies_from_browser: str = "",
    cookies_file: str = "",
    cancel: Cancel | None = None,
) -> BulkResult:
    """One query, end to end. Never raises: failures land in the result.

    Deliberately not `discover.web_search`: this needs the ranking *and* the
    reason ranking would not commit, which `pick` reports and `rank` does not.
    """
    result = BulkResult(query=query)
    try:
        candidates = find_candidates(
            query,
            limit=limit,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
            cancel=cancel,
        )
    except ProbeError as exc:
        result.error = str(exc)
        return result

    # No expected duration: a free-text query carries no reference length, so
    # `rank` falls back to rejecting only the obviously-wrong shapes.
    result.ranked = rank(candidates, query, None)
    try:
        # `pick` returns `ranked[0]` when it is confident, so the value is not
        # worth keeping -- only the reason it refused, when it refuses.
        pick(candidates, query, None)
    except Ambiguous as exc:
        result.ambiguity = exc.reason
    return result


def bulk_search(
    queries: list[str],
    limit: int = 6,
    cookies_from_browser: str = "",
    cookies_file: str = "",
    progress: ProgressFn = _noop,
    cancel: Cancel | None = None,
    workers: int = BULK_WORKERS,
) -> list[BulkResult]:
    """Search every query, concurrently, in the order they were written.

    `progress` is called from worker threads as each result lands, so a UI can
    fill the list in as it goes; callers on an event loop must marshal.
    """
    if not queries:
        return []

    def one(query: str) -> BulkResult:
        result = search_one(
            query,
            limit=limit,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
            cancel=cancel,
        )
        progress(result)
        return result

    with ThreadPoolExecutor(max_workers=min(workers, len(queries))) as pool:
        return list(pool.map(one, queries))
