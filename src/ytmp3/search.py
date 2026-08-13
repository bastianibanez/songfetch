"""Pure text search over the track catalog.

No I/O, no network, no state. `search()` is a total function of
(catalog, query), which is what makes the fixture suite in
`tests/test_search.py` meaningful.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from .models import Track
from .textutil import normalize, squash

# Score bands. Kept far apart so a title hit can never be outranked by an
# album hit, and so tests can assert on ordering rather than exact numbers.
EXACT_TITLE = 1000.0
PREFIX_TITLE = 600.0
SQUASH_PREFIX_TITLE = 580.0
CONTAINS_TITLE = 400.0
SQUASH_CONTAINS_TITLE = 380.0
EXACT_ARTIST = 350.0
CONTAINS_ARTIST = 300.0
ALL_TOKENS = 250.0
EXACT_ALBUM = 250.0
CONTAINS_ALBUM = 200.0
TYPO_MAX = 150.0

# A shorter field containing the query is a tighter match than a longer one.
_TIGHTNESS = 50.0
# Per-token similarity floor for the typo fallback. 0.8 accepts
# "skrilex"->"skrillex" (0.93) while rejecting unrelated words.
TYPO_FLOOR = 0.8
# Tokens shorter than this are matched literally only. A 2-3 character token
# carries too few characters for an edit ratio to mean anything: "925" vs "95"
# scores exactly 0.8, which would drag "Starjunk 95" into a search for "925".
TYPO_MIN_TOKEN = 4
# Tokens shorter than this are matched literally only. A 2-3 character token
# has too few characters for an edit ratio to be meaningful: "925" vs "95"
# scores exactly 0.8, which would drag "Starjunk 95" into a search for "925".
TYPO_MIN_TOKEN = 4


def _tightness(query: str, field: str) -> float:
    return _TIGHTNESS * len(query) / max(len(field), 1)


def score(track: Track, query: str) -> float:
    """Relevance of `track` for `query`. 0.0 means "no match at all"."""
    q = normalize(query)
    if not q:
        return 0.0

    nt = normalize(track.title)
    na = normalize(track.artist)
    nal = normalize(track.album)
    best = 0.0

    # --- title, literal ---
    if nt == q:
        best = max(best, EXACT_TITLE)
    elif nt.startswith(q):
        best = max(best, PREFIX_TITLE + _tightness(q, nt))
    elif q in nt:
        best = max(best, CONTAINS_TITLE + _tightness(q, nt))

    # --- title, punctuation-blind ("aftr" vs "A.F.T.E.R", "talktome") ---
    sq = squash(query)
    snt = squash(track.title)
    if sq and snt:
        if snt == sq:
            best = max(best, EXACT_TITLE)
        elif snt.startswith(sq):
            best = max(best, SQUASH_PREFIX_TITLE)
        elif sq in snt:
            best = max(best, SQUASH_CONTAINS_TITLE)

    # --- artist ---
    if na and na == q:
        best = max(best, EXACT_ARTIST)
    elif na and q in na:
        best = max(best, CONTAINS_ARTIST + _tightness(q, na))

    # --- album ---
    if nal and nal == q:
        best = max(best, EXACT_ALBUM)
    elif nal and q in nal:
        best = max(best, CONTAINS_ALBUM + _tightness(q, nal))

    # --- every query token appears somewhere, in any order/field ---
    qt = q.split()
    hay_tokens = set(f"{nt} {na} {nal}".split())
    if qt and hay_tokens:
        if all(any(h == t or h.startswith(t) for h in hay_tokens) for t in qt):
            best = max(best, ALL_TOKENS + 10.0 * len(qt))

        # --- typo fallback: every token must be close to *some* token ---
        if all(len(t) >= TYPO_MIN_TOKEN for t in qt):
            ratios = [
                max((SequenceMatcher(None, t, h).ratio() for h in hay_tokens), default=0.0)
                for t in qt
            ]
            if ratios and min(ratios) >= TYPO_FLOOR:
                best = max(best, TYPO_MAX * (sum(ratios) / len(ratios)))

    return best


def _sortkey(track: Track) -> tuple[str, str, str]:
    """Deterministic tie-break so identical scores always order the same way."""
    return (normalize(track.title), normalize(track.artist), track.id)


def search(
    catalog: list[Track], query: str, limit: int | None = None
) -> list[tuple[float, Track]]:
    """Rank `catalog` against `query`, best first.

    An empty query returns the whole catalog in stable alphabetical order
    (score 0.0). A query nothing matches returns an empty list.
    """
    if not normalize(query):
        ranked = [(0.0, t) for t in sorted(catalog, key=_sortkey)]
    else:
        scored = [(score(t, query), t) for t in catalog]
        ranked = sorted(
            (pair for pair in scored if pair[0] > 0.0),
            key=lambda pair: (-pair[0], _sortkey(pair[1])),
        )
    return ranked[:limit] if limit is not None else ranked


def search_tracks(catalog: list[Track], query: str, limit: int | None = None) -> list[Track]:
    """`search()` without the scores, for callers that only want the order."""
    return [t for _, t in search(catalog, query, limit)]
