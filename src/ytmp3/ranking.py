"""Pure scoring of yt-dlp search results.

This is the "don't download the music video" brain. It never touches the
network: `rank()` takes already-fetched candidates and returns them ordered.

The hybrid rule: auto-generated YouTube Music uploads ("<Artist> - Topic"
channels, art tracks, "Provided to YouTube by ..." descriptions) are strongly
preferred because they are audio-only and start on the downbeat. But plenty of
wanted tracks -- channel rips, flips, bootlegs -- have no Topic upload at all,
so regular uploads stay eligible and are scored on their own merits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Candidate, ScoredCandidate
from .textutil import normalize

# --- positive weights -----------------------------------------------------
W_TOPIC = 100.0
W_PROVIDED_BY = 60.0
W_MUSIC_CATEGORY = 30.0
W_SQUARE_ART = 25.0
W_UPLOADER_IS_ARTIST = 25.0
W_DURATION_EXACT = 40.0

# Query coverage is a gate, not a garnish. A "- Topic" upload of the *wrong*
# song collects +215 from the source-quality signals alone, so a small additive
# coverage bonus would let it outrank the correct non-Topic rip. Instead,
# coverage pays a bonus when full and charges a penalty when partial:
#   points = W_COVERAGE_FULL * ratio - W_COVERAGE_MISS * (1 - ratio)
# ratio 1.0 -> +120,  0.8 -> +36,  0.5 -> -90,  0.1 -> -258.
# The miss weight has to exceed the full source-quality stack (215) so that
# being the wrong song can never be rescued by being a nice upload.
W_COVERAGE_FULL = 120.0
W_COVERAGE_MISS = 300.0

# Coverage asks "does the candidate have everything the query wanted?".
# Precision asks the mirror question: "what did the candidate add that nobody
# asked for?" Without it "CSIRAC" and "CSIRAC (Effy Remix)" score identically,
# because both contain every query token.
W_PRECISION = 40.0

# --- acceptance thresholds ------------------------------------------------
MIN_ACCEPT = 60.0
MARGIN = 25.0

_PROVIDED_BY = re.compile(r"^\s*provided to youtube by", re.IGNORECASE)
_TOPIC_CHANNEL = re.compile(r"\s-\stopic\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Penalty:
    name: str
    points: float
    pattern: re.Pattern[str]


# Order matters only for readability; every rule is evaluated.
PENALTIES: tuple[_Penalty, ...] = (
    _Penalty("official_video", 80.0, re.compile(r"official\s*(music\s*)?video|\bm/?v\b", re.I)),
    _Penalty(
        "live",
        60.0,
        re.compile(r"\b(live|liveset|dj\s*set|festival|boiler\s*room|tomorrowland|edc|ultra|rampage|@)\b", re.I),
    ),
    _Penalty("video_edit", 40.0, re.compile(r"\b(lyric[s]?\s*video|visuali[sz]er|reaction|interview|trailer|teaser|behind\s*the\s*scenes)\b", re.I)),
    _Penalty("pitch_edit", 40.0, re.compile(r"\b(sped\s*up|speed\s*up|slowed|reverb|8d|nightcore|daycore|chipmunk)\b", re.I)),
    # An unrequested remix/edit/VIP is a different recording, not this track.
    # Suppression makes this self-cancelling: a query that says "KARAN! Flip"
    # or "(Extended Mix)" is asking for exactly that variant.
    _Penalty(
        "variant",
        70.0,
        re.compile(
            r"\b(remix|rmx|edit|vip|bootleg|flip|mashup|mash-up|cover|instrumental|"
            r"acapella|a\s?capella|acoustic|demo|reprise|extended|club\s*mix|dub|version|refix)\b",
            re.I,
        ),
    ),
    _Penalty("compilation", 30.0, re.compile(r"\b(full\s*album|megamix|compilation|mixtape|best\s*of|top\s*\d+|\d+\s*hours?)\b", re.I)),
)

# yt-dlp categories that are outright wrong for a music download.
_BAD_CATEGORIES = {"Gaming", "News & Politics", "Sports"}


def _is_topic(c: Candidate) -> bool:
    channel = c.channel or c.uploader
    if _TOPIC_CHANNEL.search(channel or ""):
        return True
    # YT Music art tracks carry structured music metadata even off Topic channels.
    return bool(c.music_artist and c.music_track)


def _is_square_art(c: Candidate) -> bool:
    if not (c.thumbnail_width and c.thumbnail_height):
        return False
    ratio = c.thumbnail_width / c.thumbnail_height
    return 0.9 <= ratio <= 1.1


def _duration_delta(c: Candidate, expected: int | None, query: str) -> tuple[float, str]:
    """Return (points, reason). Positive points for a duration that matches."""
    d = c.duration
    if d is None:
        return 0.0, ""
    if expected:
        diff = abs(d - expected)
        if diff <= 3:
            return W_DURATION_EXACT, f"+{W_DURATION_EXACT:.0f} duration matches ({d}s)"
        if diff <= 20:
            return 0.0, ""
        return -min(50.0, (diff - 20) * 1.5), f"-duration off by {diff}s"
    # No expected duration: only reject the obviously-wrong shapes.
    if d < 60:
        return -50.0, f"-50 too short ({d}s)"
    long_ok = bool(re.search(r"\b(extended|mix|set|continuous)\b", query, re.I))
    if d > 720 and not long_ok:
        return -40.0, f"-40 too long ({d}s)"
    return 0.0, ""


def _coverage(c: Candidate, query: str) -> tuple[float, str]:
    qt = normalize(query).split()
    if not qt:
        return 0.0, ""
    hay = set(
        normalize(
            f"{c.title} {c.uploader} {c.channel} {c.music_artist} {c.music_track} {c.music_album}"
        ).split()
    )
    hit = sum(1 for t in qt if any(h == t or h.startswith(t) for h in hay))
    ratio = hit / len(qt)
    pts = W_COVERAGE_FULL * ratio - W_COVERAGE_MISS * (1.0 - ratio)
    return pts, f"{pts:+.0f} query coverage {hit}/{len(qt)}"


def _precision(c: Candidate, query: str) -> tuple[float, str]:
    """Penalise title words the query never asked for.

    Only the title counts: channel names and uploader boilerplate legitimately
    contain words outside the query.
    """
    title_tokens = normalize(c.title).split()
    if not title_tokens:
        return 0.0, ""
    qt = set(normalize(query).split())
    if not qt:
        return 0.0, ""
    extra = [t for t in title_tokens if not any(q == t or t.startswith(q) for q in qt)]
    if not extra:
        return 0.0, ""
    pts = -W_PRECISION * len(extra) / len(title_tokens)
    return pts, f"{pts:+.0f} unasked-for title words ({', '.join(extra[:3])})"


def score_candidate(c: Candidate, query: str, expected_duration: int | None = None) -> ScoredCandidate:
    """Score one candidate. Deterministic, no I/O."""
    nquery = normalize(query)
    total = 0.0
    reasons: list[str] = []

    if _is_topic(c):
        total += W_TOPIC
        reasons.append(f"+{W_TOPIC:.0f} topic/art-track")
    if _PROVIDED_BY.match(c.description or ""):
        total += W_PROVIDED_BY
        reasons.append(f"+{W_PROVIDED_BY:.0f} provided-to-youtube-by")
    if "Music" in c.categories:
        total += W_MUSIC_CATEGORY
        reasons.append(f"+{W_MUSIC_CATEGORY:.0f} music category")
    if _BAD_CATEGORIES.intersection(c.categories):
        total -= 60.0
        reasons.append("-60 non-music category")
    if _is_square_art(c):
        total += W_SQUARE_ART
        reasons.append(f"+{W_SQUARE_ART:.0f} square art")

    uploader_norm = normalize(c.channel or c.uploader)
    if uploader_norm and uploader_norm.removesuffix(" topic").strip() in nquery:
        total += W_UPLOADER_IS_ARTIST
        reasons.append(f"+{W_UPLOADER_IS_ARTIST:.0f} uploader is the artist")

    pts, why = _coverage(c, query)
    total += pts
    if why:
        reasons.append(why)

    pts, why = _precision(c, query)
    total += pts
    if why:
        reasons.append(why)

    if c.is_live or c.was_live:
        total -= 70.0
        reasons.append("-70 live stream")

    haystack = f"{c.title} {c.channel or c.uploader}"
    for rule in PENALTIES:
        matches = [m.group(0) for m in rule.pattern.finditer(haystack)]
        if not matches:
            continue
        # Suppression: if the user asked for the thing the rule punishes,
        # the rule does not apply. "Quest Vault [... @ EDC Mexico 2024]" is a
        # wanted track when the query itself says EDC.
        if any(normalize(m) and normalize(m) in nquery for m in matches):
            reasons.append(f"~{rule.name} suppressed (in query)")
            continue
        total -= rule.points
        reasons.append(f"-{rule.points:.0f} {rule.name} ({matches[0].strip()!r})")

    pts, why = _duration_delta(c, expected_duration, query)
    total += pts
    if why:
        reasons.append(why)

    return ScoredCandidate(candidate=c, score=total, reasons=tuple(reasons))


def rank(
    candidates: list[Candidate], query: str, expected_duration: int | None = None
) -> list[ScoredCandidate]:
    """Best first. Ties break on view count, then video id, so it is stable."""
    scored = [score_candidate(c, query, expected_duration) for c in candidates]
    return sorted(scored, key=lambda s: (-s.score, -s.candidate.view_count, s.candidate.id))


class Ambiguous(Exception):
    """No candidate is a confident winner; a human should choose."""

    def __init__(self, candidates: list[ScoredCandidate], reason: str):
        super().__init__(reason)
        self.candidates = candidates
        self.reason = reason


def pick(
    candidates: list[Candidate], query: str, expected_duration: int | None = None
) -> ScoredCandidate:
    """Return the single best candidate, or raise `Ambiguous`."""
    ranked = rank(candidates, query, expected_duration)
    if not ranked:
        raise Ambiguous([], "no candidates")
    best = ranked[0]
    if best.score < MIN_ACCEPT:
        raise Ambiguous(ranked, f"best score {best.score:.0f} below {MIN_ACCEPT:.0f}")
    if len(ranked) > 1 and (best.score - ranked[1].score) < MARGIN:
        raise Ambiguous(
            ranked, f"top two within {MARGIN:.0f} ({best.score:.0f} vs {ranked[1].score:.0f})"
        )
    return best
