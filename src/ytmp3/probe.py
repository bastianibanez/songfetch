"""Ask yt-dlp what exists. The only module that performs a search request."""

from __future__ import annotations

import json
import shutil
import subprocess
from urllib.parse import quote

from .models import Candidate

YTDLP = shutil.which("yt-dlp") or "yt-dlp"

SEARCH_TIMEOUT = 90


class ProbeError(RuntimeError):
    pass


def _run(args: list[str], timeout: int) -> str:
    try:
        proc = subprocess.run(
            [YTDLP, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment problem
        raise ProbeError(f"yt-dlp not found on PATH ({YTDLP})") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"yt-dlp search timed out after {timeout}s") from exc

    # --ignore-errors means a non-zero exit can still come with perfectly good
    # results for the other entries, so the exit code alone is not the signal.
    # Only a run that produced no JSON at all counts as a failure -- but that
    # failure must be reported, not silently returned as "no results found".
    if proc.returncode != 0 and "{" not in proc.stdout:
        tail = (proc.stderr or "").strip().splitlines()[-2:]
        raise ProbeError(f"yt-dlp search failed (exit {proc.returncode}): {' / '.join(tail)}")
    return proc.stdout


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


def _common_args(cookies_from_browser: str = "", cookies_file: str = "") -> list[str]:
    from .encode import cookie_args

    return [
        *cookie_args(cookies_from_browser, cookies_file),
        "--dump-json",
        "--no-warnings",
        "--ignore-errors",
        "--skip-download",
        "--no-playlist",
        # --dump-json still performs format selection, which can fail on an
        # authenticated client for entries we are only reading metadata from.
        # Without this one such entry aborts the whole search.
        "--ignore-no-formats-error",
        # Flat entries omit description/categories/duration, which is most of
        # what ranking.py scores on, so full extraction is required here.
        "--no-flat-playlist",
    ]


def search_youtube(
    query: str,
    limit: int = 6,
    timeout: int = SEARCH_TIMEOUT,
    cookies_from_browser: str = "",
    cookies_file: str = "",
) -> list[Candidate]:
    """Plain YouTube search. Finds channel rips, flips and bootlegs."""
    args = _common_args(cookies_from_browser, cookies_file)
    return parse_dump_json(_run([*args, f"ytsearch{limit}:{query}"], timeout))


def search_youtube_music(
    query: str,
    limit: int = 6,
    timeout: int = SEARCH_TIMEOUT,
    cookies_from_browser: str = "",
    cookies_file: str = "",
) -> list[Candidate]:
    """YouTube Music search. Surfaces the Topic/art-track uploads.

    These are audio-only by construction and start on the downbeat, which is
    exactly what we want -- but they only exist for commercially released
    tracks, hence the plain-YouTube pass alongside.
    """
    args = _common_args(cookies_from_browser, cookies_file)
    url = f"https://music.youtube.com/search?q={quote(query)}"
    return parse_dump_json(_run([*args, "--playlist-end", str(limit), url], timeout))


def find_candidates(
    query: str,
    limit: int = 6,
    timeout: int = SEARCH_TIMEOUT,
    music_first: bool = True,
    cookies_from_browser: str = "",
    cookies_file: str = "",
) -> list[Candidate]:
    """Merged YT Music + YouTube results, deduplicated.

    Neither source failing is fatal: YT Music search is the flakier of the two
    and a plain-YouTube-only result set is still perfectly rankable.
    """
    results: list[Candidate] = []
    errors: list[str] = []

    sources = [search_youtube_music, search_youtube]
    if not music_first:
        sources.reverse()

    for fn in sources:
        try:
            results.extend(
                fn(
                    query,
                    limit=limit,
                    timeout=timeout,
                    cookies_from_browser=cookies_from_browser,
                    cookies_file=cookies_file,
                )
            )
        except ProbeError as exc:
            errors.append(str(exc))

    if not results and errors:
        raise ProbeError("; ".join(errors))
    return dedupe(results)
