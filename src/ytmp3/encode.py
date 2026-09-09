"""Fetch the source audio and turn it into one MP3, transcoding exactly once."""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .models import Track
from .trim import TrimDecision, head_offset

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
YTDLP = shutil.which("yt-dlp") or "yt-dlp"

DOWNLOAD_TIMEOUT = 900
FFMPEG_TIMEOUT = 900

# SponsorBlock publishes community-marked segment boundaries. `music_offtopic`
# is precisely "the non-music part of a music upload" -- intros, outros, talking
# -- so removing it does most of the start-on-the-downbeat work for free.
SPONSORBLOCK_CATEGORIES = "music_offtopic,intro"


class EncodeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Fetched:
    audio: Path
    info: dict
    thumbnail: Path | None


def _run(cmd: list[str], timeout: int, what: str) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise EncodeError(f"{cmd[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise EncodeError(f"{what} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        raise EncodeError(f"{what} failed (exit {proc.returncode}): {' / '.join(tail)}")
    return proc


_COOKIE_LOCK = threading.Lock()
_COOKIE_EXPORTS: dict[str, str] = {}


def _export_browser_cookies(browser: str) -> str:
    """Dump a browser's cookie jar to a private file once per process.

    `--cookies-from-browser` re-reads and decrypts the browser's cookie store
    on *every* yt-dlp invocation -- about a second each. Search used to be one
    invocation, so that second was invisible; it is now a listing per source
    plus a lookup per result, and paying it a dozen times is most of a search.

    Exporting once and passing the resulting file costs the same second on the
    first call and nothing afterwards. `ytsearch0:` makes yt-dlp do the cookie
    work and then no network work at all -- it writes the jar out on exit.

    Returns "" if the export fails, which sends the caller back to the
    per-invocation flag: slower, but never worse than not authenticating.

    The file holds live session cookies. It is written 0600 inside a private
    temp directory and removed at exit; nothing else is done with it.
    """
    with _COOKIE_LOCK:
        cached = _COOKIE_EXPORTS.get(browser)
        if cached is not None:
            return cached

        path = ""
        try:
            tmpdir = tempfile.mkdtemp(prefix="ytmp3-cookies-")
            os.chmod(tmpdir, 0o700)
            target = Path(tmpdir) / "cookies.txt"
            # Create it 0600 before yt-dlp writes, so it is never briefly
            # world-readable on a permissive umask.
            os.close(os.open(target, os.O_CREAT | os.O_WRONLY, 0o600))
            proc = subprocess.run(
                [
                    YTDLP,
                    "--cookies-from-browser", browser,
                    "--cookies", str(target),
                    "--skip-download",
                    "--no-warnings",
                    "--ignore-errors",
                    "ytsearch0:cookie-export",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if target.stat().st_size > 0:
                path = str(target)
                atexit.register(shutil.rmtree, tmpdir, True)
            else:
                shutil.rmtree(tmpdir, ignore_errors=True)
                tail = (proc.stderr or "").strip().splitlines()[-1:]
                if tail:
                    print(f"cookie export failed, falling back: {tail[0]}", file=sys.stderr)
        except (OSError, subprocess.SubprocessError):
            path = ""

        _COOKIE_EXPORTS[browser] = path
        return path


def cookie_args(cookies_from_browser: str = "", cookies_file: str = "") -> list[str]:
    """yt-dlp authentication flags.

    YouTube increasingly answers anonymous download requests with "Sign in to
    confirm you're not a bot". Passing your own browser session is yt-dlp's
    documented remedy; nothing here bypasses anything, it just authenticates
    as you.
    """
    if cookies_file:
        return ["--cookies", cookies_file]
    if cookies_from_browser:
        exported = _export_browser_cookies(cookies_from_browser)
        if exported:
            return ["--cookies", exported]
        return ["--cookies-from-browser", cookies_from_browser]
    return []


def fetch_audio(
    url: str,
    workdir: Path,
    timeout: int = DOWNLOAD_TIMEOUT,
    cookies_from_browser: str = "",
    cookies_file: str = "",
) -> Fetched:
    """Download the best audio stream *without converting it*.

    Deliberately no `-x --audio-format mp3` here: converting now and trimming
    later would mean two lossy generations. We keep the original stream and let
    `encode_mp3` do the one and only transcode.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    stem = workdir / "source"

    _run(
        [
            YTDLP,
            "-f", "bestaudio/best",
            "--no-playlist",
            "--no-warnings",
            "--write-info-json",
            "--write-thumbnail",
            "--sponsorblock-remove", SPONSORBLOCK_CATEGORIES,
            "--retries", "10",
            "--fragment-retries", "10",
            "--concurrent-fragments", "4",
            *cookie_args(cookies_from_browser, cookies_file),
            "-o", f"{stem}.%(ext)s",
            url,
        ],
        timeout,
        "yt-dlp download",
    )

    info_path = stem.with_suffix(".info.json")
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}

    audio = next(
        (
            p
            for p in sorted(workdir.iterdir())
            if p.is_file()
            and p.stem == "source"
            and p.suffix.lower() not in {".json", ".jpg", ".jpeg", ".png", ".webp"}
        ),
        None,
    )
    if audio is None:
        raise EncodeError("yt-dlp produced no audio file")

    thumb = next(
        (p for p in sorted(workdir.iterdir()) if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}),
        None,
    )
    return Fetched(audio=audio, info=info, thumbnail=thumb)


def detect_silence(audio: Path, noise_db: int = -50, min_dur: float = 0.3, timeout: int = FFMPEG_TIMEOUT) -> str:
    """Return ffmpeg's `silencedetect` stderr. Parsing lives in `trim.py`."""
    proc = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-nostdin",
            "-i", str(audio),
            "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.stderr or ""


def decide_trim(fetched: Fetched, *, force: bool = False) -> TrimDecision:
    return head_offset(
        chapters=fetched.info.get("chapters"),
        silence_stderr=detect_silence(fetched.audio),
        force=force,
    )


def _cover_args(thumb: Path | None) -> tuple[list[str], list[str]]:
    """(input args, mapping args) for embedding cover art, cropped square."""
    if thumb is None or not thumb.exists():
        return [], []
    inputs = ["-i", str(thumb)]
    mapping = [
        "-map", "0:a",
        "-map", "1:v",
        "-c:v", "mjpeg",
        # Centre-crop to a square so players that expect album art do not get
        # a 16:9 video thumbnail letterboxed into the tag.
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=600:600",
        "-disposition:v", "attached_pic",
        "-metadata:s:v", "title=Album cover",
        "-metadata:s:v", "comment=Cover (front)",
    ]
    return inputs, mapping


def encode_mp3(
    fetched: Fetched,
    dest: Path,
    track: Track,
    *,
    offset: float = 0.0,
    cbr320: bool = False,
    timeout: int = FFMPEG_TIMEOUT,
) -> Path:
    """The single transcode: trim the head, encode MP3, write tags and cover."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part.mp3")

    info = fetched.info
    year = info.get("release_year") or (str(info.get("upload_date") or "")[:4] or None)
    cover_in, cover_map = _cover_args(fetched.thumbnail)

    quality = ["-b:a", "320k"] if cbr320 else ["-q:a", "0"]

    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y"]
    if offset > 0:
        # Before -i so ffmpeg seeks rather than decodes-and-discards.
        cmd += ["-ss", f"{offset:.3f}"]
    cmd += ["-i", str(fetched.audio), *cover_in]
    cmd += cover_map or ["-map", "0:a"]
    cmd += [
        "-c:a", "libmp3lame",
        *quality,
        "-id3v2_version", "3",
        "-write_id3v1", "1",
        "-metadata", f"title={track.title}",
        "-metadata", f"artist={track.artist}",
        "-metadata", f"album={track.album or track.title}",
        "-metadata", f"comment=source: {info.get('webpage_url', '')}",
    ]
    if year:
        cmd += ["-metadata", f"date={year}"]
    cmd += [str(tmp)]

    _run(cmd, timeout, "ffmpeg encode")
    tmp.replace(dest)
    return dest
