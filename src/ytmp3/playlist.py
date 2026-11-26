"""Shared URL and download job handling for playlists."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import probe
from .discover import track_from_candidate
from .models import DownloadJob
from .pipeline import Options

YOUTUBE_HOSTS = {"music.youtube.com", "www.youtube.com", "youtube.com", "m.youtube.com"}


@dataclass(frozen=True, slots=True)
class PlaylistLink:
    id: str
    url: str

    @classmethod
    def parse(cls, value: str) -> PlaylistLink | None:
        try:
            parsed = urlsplit(value.strip())
        except ValueError:
            return None
        host = parsed.netloc.lower()
        if (parsed.scheme not in {"http", "https"} or host not in YOUTUBE_HOSTS
                or parsed.path not in {"/playlist", "/watch"}):
            return None
        ids = parse_qs(parsed.query).get("list", [])
        if len(ids) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", ids[0]):
            return None
        playlist_id = ids[0]
        return cls(playlist_id, f"https://{host}/playlist?list={playlist_id}")

    def directory(self, music_dir: Path) -> Path:
        return music_dir / "playlists" / self.id


def playlist_jobs(link: PlaylistLink, opts: Options) -> tuple[list[DownloadJob], list[int]]:
    entries = probe.list_playlist(link.url, opts.cookies_from_browser, opts.cookies_file)
    if not entries:
        raise probe.ProbeError("playlist is empty or has no visible tracks")

    directory = link.directory(opts.music_dir)
    jobs = []
    unavailable = []
    for position, candidate in enumerate(entries, start=1):
        if candidate is None:
            unavailable.append(position)
            continue
        track = track_from_candidate(candidate)
        stem = track.filename_stem.encode("utf-8")[:200].decode("utf-8", errors="ignore")
        filename = f"{position:04d} - {stem} [{candidate.id}].mp3"
        jobs.append(DownloadJob(track, chosen=candidate, destination=directory / filename))
    return jobs, unavailable
