"""Ask yt-dlp what exists. The only module that performs a search request.

Search used to ask yt-dlp for full metadata on every result of both sources in
one shot. One query measured that way took 281 seconds and came back with 67
entries: the YouTube Music results page is sections, not a list, and the art
tracks are buried inside album and playlist entries which yt-dlp expands one
video at a time while ignoring `--playlist-end` entirely.

So it happens in two phases now.

Phase one *lists*: one flat request per source, both in flight at once, with YT
Music restricted to its Songs tab (`MUSIC_SONGS_FILTER`) so the page really is
a list of recordings. A flat listing is about a hundred times cheaper than a
full one -- 2.7 seconds against 281 -- because it never resolves formats.

Phase two *hydrates*: every surviving id is looked up in full, in parallel, for
the fields ranking scores on that a flat entry omits. Each lookup is a fixed
~2 second round-trip no amount of extractor tuning shortens, which is precisely
why they go wide instead of sequential.

Same query, ten seconds.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from .models import Candidate

YTDLP = shutil.which("yt-dlp") or "yt-dlp"

SEARCH_TIMEOUT = 90

# Listing is one cheap request; a full lookup is one video's formats. Neither
# should be allowed to hold the whole search hostage for the 90s budget.
LIST_TIMEOUT = 60
DETAIL_TIMEOUT = 60

# Detail lookups are latency, not CPU: eight in flight keeps a twelve-result
# search inside two round-trips of wall time without hammering YouTube.
DETAIL_WORKERS = 8

# The opaque `sp=` parameter YouTube Music itself puts on the URL when you
# click the "Songs" tab. Filtering there is what turns the results page from a
# nest of albums and playlists into a flat list of recordings -- see
# `search_youtube_music` for why that is the whole performance fix.
MUSIC_SONGS_FILTER = "EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D"

# Multiplier on `limit` when listing the songs tab. One, not two: the songs
# list arrives ordered best-first and pads its tail with unrelated chart filler
# ("Hotline Bling" turns up for a Ninajirachi query), so over-fetching only
# buys full lookups for results that rank below anything we would display.
MUSIC_OVERFETCH = 1

# Flat entries for a channel, a playlist or an album are not something that can
# be downloaded as a track. They come back with an `ie_key` naming the tab
# extractor rather than the video one.
_NOT_A_VIDEO = {"YoutubeTab", "YoutubeChannel", "YoutubePlaylist", "YoutubeUser"}


class ProbeError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    """Raised inside a search whose caller has already moved on."""


class Cancel:
    """Cooperative cancellation token that also kills in-flight yt-dlp runs.

    A search is a fan-out of subprocesses. Abandoning it without killing them
    leaves eight yt-dlp processes competing for bandwidth with the search the
    user actually wants -- which is what "unbearably slow" feels like from the
    outside even after the search itself got fast.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._procs: set[subprocess.Popen] = set()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        """Flag the search and kill whatever it is currently running."""
        with self._lock:
            self._cancelled = True
            procs, self._procs = self._procs, set()
        for proc in procs:
            try:
                proc.kill()
            except OSError:  # pragma: no cover - already gone
                pass

    def check(self) -> None:
        """Raise if cancelled. Called before anything expensive is started."""
        if self.cancelled:
            raise Cancelled("search cancelled")

    def _register(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if self._cancelled:
                already_dead = True
            else:
                self._procs.add(proc)
                already_dead = False
        if already_dead:
            # Cancelled between `check` and spawn: kill it here rather than
            # letting an orphan run to completion unwatched.
            try:
                proc.kill()
            except OSError:  # pragma: no cover
                pass

    def _unregister(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)


def _run(args: list[str], timeout: int, cancel: Cancel | None = None) -> str:
    if cancel is not None:
        cancel.check()

    try:
        proc = subprocess.Popen(
            [YTDLP, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment problem
        raise ProbeError(f"yt-dlp not found on PATH ({YTDLP})") from exc

    if cancel is not None:
        cancel._register(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise ProbeError(f"yt-dlp search timed out after {timeout}s") from exc
    finally:
        if cancel is not None:
            cancel._unregister(proc)

    # A killed process exits non-zero with nothing useful on stderr; report it
    # as cancellation rather than as a yt-dlp failure the user should read.
    if cancel is not None and cancel.cancelled:
        raise Cancelled("search cancelled")

    # --ignore-errors means a non-zero exit can still come with perfectly good
    # results for the other entries, so the exit code alone is not the signal.
    # Only a run that produced no JSON at all counts as a failure -- but that
    # failure must be reported, not silently returned as "no results found".
    if proc.returncode != 0 and "{" not in stdout:
        tail = (stderr or "").strip().splitlines()[-2:]
        raise ProbeError(f"yt-dlp search failed (exit {proc.returncode}): {' / '.join(tail)}")
    return stdout


def parse_dump_json(stdout: str) -> list[Candidate]:
    """Parse `--dump-json` output: one JSON object per line.

    Tolerant by design -- a single malformed line from a private or
    region-blocked video must not lose the rest of the results.
    """
    out: list[Candidate] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("_type") == "playlist":
            continue
        # Flat listings surface channels and playlists as siblings of the
        # videos; full extraction never did, which is why this check is new.
        if entry.get("ie_key") in _NOT_A_VIDEO:
            continue
        if not entry.get("id"):
            continue
        out.append(Candidate.from_ytdlp(entry))
    return out


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """First occurrence of each video id wins."""
    seen: set[str] = set()
    out: list[Candidate] = []
    for c in candidates:
        if c.id and c.id not in seen:
            seen.add(c.id)
            out.append(c)
    return out


def _cookie_args(cookies_from_browser: str = "", cookies_file: str = "") -> list[str]:
    from .encode import cookie_args

    return cookie_args(cookies_from_browser, cookies_file)


def _list_args(cookies_from_browser: str = "", cookies_file: str = "") -> list[str]:
    """Flags for phase one: name the results, resolve nothing.

    `--flat-playlist` is the whole point. Without it yt-dlp resolves every
    result's format list -- thirty-odd entries each, with signature decoding --
    at search time, for results the user will never download.
    """
    return [
        *_cookie_args(cookies_from_browser, cookies_file),
        "--dump-json",
        "--no-warnings",
        "--ignore-errors",
        "--skip-download",
        "--flat-playlist",
    ]


def _detail_args(cookies_from_browser: str = "", cookies_file: str = "") -> list[str]:
    """Flags for phase two: everything known about one video."""
    return [
        *_cookie_args(cookies_from_browser, cookies_file),
        "--dump-json",
        "--no-warnings",
        "--ignore-errors",
        "--skip-download",
        "--no-playlist",
        # --dump-json still performs format selection, which can fail on an
        # authenticated client for entries we are only reading metadata from.
        # Without this one such entry aborts the whole lookup.
        "--ignore-no-formats-error",
        "--no-flat-playlist",
    ]


def _music_page(query: str, songs_only: bool = True) -> str:
    """YouTube Music search URL, optionally pinned to the Songs tab."""
    url = f"https://music.youtube.com/search?q={quote(query)}"
    if songs_only:
        url = f"{url}&sp={MUSIC_SONGS_FILTER}"
    return url


def search_youtube(
    query: str,
    limit: int = 6,
    timeout: int = LIST_TIMEOUT,
    cookies_from_browser: str = "",
    cookies_file: str = "",
    cancel: Cancel | None = None,
) -> list[Candidate]:
    """Plain YouTube search, listed flat. Finds channel rips, flips and bootlegs."""
    args = _list_args(cookies_from_browser, cookies_file)
    return parse_dump_json(_run([*args, f"ytsearch{limit}:{query}"], timeout, cancel))


def search_youtube_music(
    query: str,
    limit: int = 6,
    timeout: int = LIST_TIMEOUT,
    cookies_from_browser: str = "",
    cookies_file: str = "",
    cancel: Cancel | None = None,
) -> list[Candidate]:
    """YouTube Music search, listed flat. Surfaces the Topic/art-track uploads.

    These are audio-only by construction and start on the downbeat, which is
    exactly what we want -- but they only exist for commercially released
    tracks, hence the plain-YouTube pass alongside.

    Asks the Songs tab specifically. The unfiltered page mixes top-result
    cards, videos, albums, playlists and related artists, and yt-dlp walks into
    each album a video at a time: 67 entries and 281 seconds for one query.
    The songs tab is a list, comes back best-first, and honours --playlist-end.

    `sp` is an opaque YouTube parameter, so it is allowed to stop working. When
    the songs tab yields nothing we fall back to the unfiltered page: slow
    beats broken.
    """
    args = _list_args(cookies_from_browser, cookies_file)
    end = max(1, limit * MUSIC_OVERFETCH)

    songs = parse_dump_json(
        _run([*args, "--playlist-end", str(end), _music_page(query, True)], timeout, cancel)
    )
    if songs:
        return songs
    return parse_dump_json(
        _run([*args, "--playlist-end", str(end), _music_page(query, False)], timeout, cancel)
    )


def hydrate(
    candidates: list[Candidate],
    timeout: int = DETAIL_TIMEOUT,
    cookies_from_browser: str = "",
    cookies_file: str = "",
    cancel: Cancel | None = None,
    workers: int = DETAIL_WORKERS,
) -> list[Candidate]:
    """Full metadata for listed ids, fetched in parallel, order preserved.

    A flat entry already carries title, duration, channel, description and view
    count. What it lacks is `categories` and the structured artist/track/album
    that YT Music attaches to art tracks -- worth 130 points of ranking between
    them, which is the difference between the official upload and a fan rip.

    A lookup that fails is not fatal: the flat candidate is kept as-is and just
    ranks without those signals.
    """
    if not candidates:
        return []

    args = _detail_args(cookies_from_browser, cookies_file)

    def one(c: Candidate) -> Candidate:
        try:
            full = parse_dump_json(_run([*args, c.url], timeout, cancel))
        except ProbeError:
            return c
        return full[0] if full else c

    if cancel is not None:
        cancel.check()

    with ThreadPoolExecutor(max_workers=min(workers, len(candidates))) as pool:
        return list(pool.map(one, candidates))


def find_candidates(
    query: str,
    limit: int = 6,
    timeout: int = SEARCH_TIMEOUT,
    music_first: bool = True,
    cookies_from_browser: str = "",
    cookies_file: str = "",
    cancel: Cancel | None = None,
) -> list[Candidate]:
    """Merged YT Music + YouTube results, deduplicated and hydrated.

    Both sources are listed concurrently -- they are independent requests and
    waiting for one before starting the other doubled the cheap half of the
    search for nothing.

    Neither source failing is fatal: YT Music search is the flakier of the two
    and a plain-YouTube-only result set is still perfectly rankable.
    """
    sources = [search_youtube_music, search_youtube]
    if not music_first:
        sources.reverse()

    results: list[Candidate] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        futures = [
            pool.submit(
                fn,
                query,
                limit=limit,
                timeout=min(timeout, LIST_TIMEOUT),
                cookies_from_browser=cookies_from_browser,
                cookies_file=cookies_file,
                cancel=cancel,
            )
            for fn in sources
        ]
        # Collected in submission order, not completion order: dedupe keeps the
        # first occurrence of an id, and "first" has to mean the preferred
        # source rather than whichever request happened to answer sooner.
        for fut in futures:
            try:
                results.extend(fut.result())
            except ProbeError as exc:
                errors.append(str(exc))

    listed = dedupe(results)
    if not listed:
        if errors:
            raise ProbeError("; ".join(errors))
        return []

    return hydrate(
        listed,
        timeout=min(timeout, DETAIL_TIMEOUT),
        cookies_from_browser=cookies_from_browser,
        cookies_file=cookies_file,
        cancel=cancel,
    )
