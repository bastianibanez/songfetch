"""Core data types. Plain, frozen, JSON-friendly."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Track:
    """A wanted track, as it appears in the user's catalog."""

    id: str
    title: str
    artist: str
    album: str = ""
    duration: int | None = None  # seconds, if known

    @property
    def query(self) -> str:
        """The text handed to yt-dlp search."""
        return f"{self.artist} {self.title}".strip()

    @property
    def filename_stem(self) -> str:
        from .textutil import safe_filename

        return safe_filename(f"{self.artist} - {self.title}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Track:
        return cls(
            id=d["id"],
            title=d["title"],
            artist=d.get("artist", ""),
            album=d.get("album", ""),
            duration=d.get("duration"),
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    """One yt-dlp search result, reduced to the fields ranking cares about."""

    id: str
    title: str
    url: str
    uploader: str = ""
    channel: str = ""
    description: str = ""
    duration: int | None = None
    categories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    view_count: int = 0
    is_live: bool = False
    was_live: bool = False
    # YT Music / Topic uploads populate these; regular uploads do not.
    music_artist: str = ""
    music_track: str = ""
    music_album: str = ""
    release_year: int | None = None
    thumbnail_width: int | None = None
    thumbnail_height: int | None = None

    @classmethod
    def from_ytdlp(cls, e: dict[str, Any]) -> Candidate:
        """Build from a raw `yt-dlp --dump-json` entry."""
        thumbs = e.get("thumbnails") or []
        best = max(
            (t for t in thumbs if t.get("width") and t.get("height")),
            key=lambda t: t["width"] * t["height"],
            default=None,
        )
        vid = e.get("id", "")
        return cls(
            id=vid,
            title=e.get("title", ""),
            url=e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={vid}",
            uploader=e.get("uploader") or "",
            channel=e.get("channel") or "",
            description=e.get("description") or "",
            duration=e.get("duration"),
            categories=tuple(e.get("categories") or ()),
            tags=tuple(e.get("tags") or ()),
            view_count=e.get("view_count") or 0,
            is_live=bool(e.get("is_live")),
            was_live=bool(e.get("was_live")),
            music_artist=e.get("artist") or "",
            music_track=e.get("track") or "",
            music_album=e.get("album") or "",
            release_year=e.get("release_year"),
            thumbnail_width=(best or {}).get("width"),
            thumbnail_height=(best or {}).get("height"),
        )


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: Candidate
    score: float
    reasons: tuple[str, ...] = ()

    def explain(self) -> str:
        return ", ".join(self.reasons)


class JobState(str, Enum):
    PENDING = "pending"
    ON_DISK = "on_disk"
    SEARCHING = "searching"
    AMBIGUOUS = "ambiguous"
    DOWNLOADING = "downloading"
    TRIMMING = "trimming"
    ENCODING = "encoding"
    DONE = "done"
    FAILED = "failed"


GLYPHS: dict[JobState, str] = {
    JobState.PENDING: "·",
    JobState.ON_DISK: "✓",
    JobState.SEARCHING: "⋯",
    JobState.AMBIGUOUS: "?",
    JobState.DOWNLOADING: "↓",
    JobState.TRIMMING: "✂",
    JobState.ENCODING: "♪",
    JobState.DONE: "♪",
    JobState.FAILED: "✗",
}


@dataclass
class DownloadJob:
    """Mutable per-track progress record."""

    track: Track
    state: JobState = JobState.PENDING
    percent: float = 0.0
    message: str = ""
    chosen: Candidate | None = None
    candidates: list[ScoredCandidate] = field(default_factory=list)
    output_path: str = ""
    trim_offset: float = 0.0
    destination: Path | None = None

    @property
    def glyph(self) -> str:
        return GLYPHS[self.state]
