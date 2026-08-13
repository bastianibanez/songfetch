"""Work out where the music actually starts.

Pure functions over ffmpeg's `silencedetect` stderr and yt-dlp's chapter list.
Running ffmpeg is `encode.py`'s job; deciding what to cut is this module's, so
the decision is testable against recorded output with no subprocess involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Anything longer than this at the head of a track is more likely to be a
# mis-detection (or a genuinely quiet intro the user wants) than dead air.
MAX_TRIM = 15.0
# Leading silence below this is not worth a trim.
MIN_TRIM = 0.15
# A "silence_start" this close to zero counts as the head of the file.
HEAD_EPSILON = 0.05

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")

_INTRO_CHAPTER = re.compile(
    r"\b(intro|introduction|talking|speech|spoken|skit|dialogue|interlude|ident|announcement)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TrimDecision:
    offset: float
    reason: str

    @property
    def should_trim(self) -> bool:
        return self.offset > 0.0


def parse_silences(stderr: str) -> list[tuple[float, float]]:
    """Extract (start, end) pairs from ffmpeg `silencedetect` stderr.

    A trailing `silence_start` with no matching `silence_end` (silence running
    to EOF) is ignored -- it is not a leading-silence signal.
    """
    starts = [float(m.group(1)) for m in _SILENCE_START.finditer(stderr)]
    ends = [float(m.group(1)) for m in _SILENCE_END.finditer(stderr)]
    return [(s, e) for s, e in zip(starts, ends)]


def leading_silence(stderr: str) -> float:
    """Seconds of silence at the very start of the file, 0.0 if none."""
    for start, end in parse_silences(stderr):
        if start <= HEAD_EPSILON:
            return max(0.0, end)
    return 0.0


def intro_chapter_end(chapters: list[dict] | None) -> float:
    """End time of a leading non-music chapter, 0.0 if there isn't one."""
    if not chapters:
        return 0.0
    first = chapters[0]
    start = float(first.get("start_time") or 0.0)
    if start > 0.5:
        return 0.0
    if not _INTRO_CHAPTER.search(str(first.get("title") or "")):
        return 0.0
    return max(0.0, float(first.get("end_time") or 0.0))


def head_offset(
    chapters: list[dict] | None = None,
    silence_stderr: str = "",
    *,
    max_trim: float = MAX_TRIM,
    force: bool = False,
) -> TrimDecision:
    """Decide how many seconds to skip so playback starts on the music.

    A named intro chapter wins over silence detection: it is an explicit
    statement by the uploader, and the intro may not be silent at all.
    """
    chapter = intro_chapter_end(chapters)
    if chapter > 0.0:
        if chapter > max_trim and not force:
            return TrimDecision(
                0.0, f"intro chapter {chapter:.2f}s exceeds {max_trim:.0f}s cap, not trimmed"
            )
        return TrimDecision(chapter, f"intro chapter ends at {chapter:.2f}s")

    silence = leading_silence(silence_stderr)
    if silence < MIN_TRIM:
        return TrimDecision(0.0, "no leading silence")
    if silence > max_trim and not force:
        return TrimDecision(
            0.0, f"leading silence {silence:.2f}s exceeds {max_trim:.0f}s cap, not trimmed"
        )
    return TrimDecision(silence, f"leading silence {silence:.2f}s")
