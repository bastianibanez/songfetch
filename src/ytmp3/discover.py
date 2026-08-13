"""Free-text web search: a query the catalog has never heard of.

`search.py` answers "which of my tracks is this?". This module answers "what
is out there?" -- it hands an arbitrary query to yt-dlp, ranks the results with
the same brain used for catalog downloads, and turns the ones the user picks
into `Track`s that can be downloaded and kept.

The parsing half (`clean_title`, `split_artist_title`, `track_from_candidate`)
is pure and fixture-tested; only `web_search` touches the network.
"""

from __future__ import annotations

import re

from .models import Candidate, ScoredCandidate, Track
from .probe import find_candidates
from .ranking import rank

# Ids of tracks discovered on the web are namespaced by video id: a slug built
# from artist/title could collide with a hand-written catalog entry, and the
# video id is the one thing guaranteed unique and stable.
WEB_ID_PREFIX = "yt:"

# Bracketed segments that describe the *upload* rather than the *recording*.
# Deliberately not a general "strip all brackets": "(Extended Mix)", "(KARAN!
# Flip)" and "[VIP]" are part of what the track IS and must survive.
_NOISE_INSIDE_BRACKETS = re.compile(
    r"""^\s*(
        official(\s+(music\s+)?(video|audio|visuali[sz]er|lyric[s]?\s*video))?
        | (music\s+)?video
        | audio | hq | hd | uhd | \d+k | \d+p
        | lyric[s]?(\s*video)?
        | visuali[sz]er
        | full\s+stream | out\s+now | free\s+(download|dl)
        | ncs\s+release | premiere | monstercat\s+release
        | m/?v
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
_BRACKETED = re.compile(r"[\(\[]([^\(\)\[\]]*)[\)\]]")

# " - ", " – ", " — " with whitespace on both sides. A bare hyphen is left
# alone: "Sub-Zero" is one word, not an artist and a title.
_ARTIST_SPLIT = re.compile(r"\s+[-–—]\s+")

_TOPIC_SUFFIX = re.compile(r"\s*-\s*topic\s*$", re.IGNORECASE)
_TRAILING_JUNK = re.compile(r"[\s|·•,\-–—]+$")


def clean_title(raw: str) -> str:
    """Drop upload-describing bracket noise, keep everything meaningful.

    ``TAKA (Official Music Video)`` -> ``TAKA``
    ``CSIRAC (Effy Remix) [HD]``    -> ``CSIRAC (Effy Remix)``
    """
    if not raw:
        return ""
    out = _BRACKETED.sub(lambda m: "" if _NOISE_INSIDE_BRACKETS.match(m.group(1)) else m.group(0), raw)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return _TRAILING_JUNK.sub("", out).strip() or raw.strip()


def split_artist_title(raw: str) -> tuple[str, str]:
    """Split ``"Artist - Title"``. Returns ``("", raw)`` when there is no split.

    Only the first separator counts, so ``"A & B - Song - Live"`` keeps
    ``"Song - Live"`` as the title rather than losing the tail.
    """
    parts = _ARTIST_SPLIT.split(raw, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return "", raw.strip()


def track_from_candidate(c: Candidate) -> Track:
    """Best-effort `Track` for a search result, so it can be tagged and kept.

    YT Music entries carry structured artist/track/album fields and are trusted
    outright. Everything else is parsed out of the upload title, which is the
    only place the information exists.
    """
    title = clean_title(c.title)
    artist = ""

    if c.music_track:
        title = c.music_track
        artist = c.music_artist
    else:
        parsed_artist, parsed_title = split_artist_title(title)
        title = parsed_title
        artist = parsed_artist or _TOPIC_SUFFIX.sub("", c.channel or c.uploader or "").strip()

    return Track(
        id=f"{WEB_ID_PREFIX}{c.id}",
        title=title or c.title,
        artist=artist,
        album=c.music_album,
        duration=c.duration,
    )


def is_web_track(track: Track) -> bool:
    return track.id.startswith(WEB_ID_PREFIX)


def web_search(
    query: str,
    limit: int = 6,
    cookies_from_browser: str = "",
    cookies_file: str = "",
) -> list[ScoredCandidate]:
    """Search YouTube Music + YouTube for `query`, best first.

    Blocking (yt-dlp is a subprocess); callers on the event loop must push it
    through a thread. Raises `probe.ProbeError` when both sources fail.
    """
    candidates: list[Candidate] = find_candidates(
        query,
        limit=limit,
        cookies_from_browser=cookies_from_browser,
        cookies_file=cookies_file,
    )
    # No expected duration: a free-text query carries no reference length, so
    # `rank` falls back to rejecting only the obviously-wrong shapes.
    return rank(candidates, query, None)
