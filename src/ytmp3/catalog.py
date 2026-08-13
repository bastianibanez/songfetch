"""Loading the wishlist and noticing what is already downloaded."""

from __future__ import annotations

import json
from pathlib import Path

from .models import Track
from .textutil import normalize, squash

# Shipped with the package so a fresh checkout has something to search.
SEED = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "tracks.json"

CATALOG_NAME = "catalog.json"


def load_catalog(path: Path) -> list[Track]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Track.from_dict(d) for d in data]


def save_catalog(path: Path, tracks: list[Track]) -> None:
    payload = [
        {k: v for k, v in t.to_dict().items() if v not in ("", None)} for t in tracks
    ]
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def catalog_path(music_dir: Path) -> Path:
    return Path(music_dir) / CATALOG_NAME


def resolve_catalog(music_dir: Path) -> list[Track]:
    """Load `<music_dir>/catalog.json`, seeding it from the fixtures on first run."""
    live = catalog_path(music_dir)
    if not live.exists():
        if not SEED.exists():
            return []
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text(SEED.read_text(encoding="utf-8"), encoding="utf-8")
    return load_catalog(live)


def add_tracks(music_dir: Path, catalog: list[Track], new: list[Track]) -> list[Track]:
    """Append tracks the catalog does not already have and persist the result.

    Used when a web-search result is downloaded: the track was not in the
    wishlist, but now that it is on disk it belongs there. Ids already present
    are left untouched, so re-downloading never duplicates a row.

    Returns the updated catalog. Writing is best-effort -- a read-only or
    unmounted music dir must not lose the download that just succeeded -- so
    the returned list can be ahead of the file on disk.
    """
    have = {t.id for t in catalog}
    fresh: list[Track] = []
    for track in new:
        if track.id in have:
            continue
        have.add(track.id)
        fresh.append(track)
    if not fresh:
        return catalog

    updated = [*catalog, *fresh]
    try:
        save_catalog(catalog_path(music_dir), updated)
    except OSError:
        pass
    return updated


def _artist_matches(track: Track, stem_squashed: str, stem_tokens: set[str]) -> bool:
    artist_sq = squash(track.artist)
    if artist_sq and artist_sq in stem_squashed:
        return True
    # Collaboration filenames rarely list every artist; one distinctive token is
    # enough. Short tokens ("&", "dj") are too collision-prone to count.
    return any(len(tok) >= 4 and tok in stem_tokens for tok in normalize(track.artist).split())


def scan_existing(music_dir: Path, catalog: list[Track]) -> set[str]:
    """Ids of catalog tracks that already have a file in `music_dir`.

    Matching is normalized (case, punctuation, diacritics) so
    ``AYYBO - RIZZ.mp3`` is recognised as the catalog's ``RIZZ`` by AYYBO.
    A short title alone is not enough -- ``Cap``, ``Raw`` and ``100`` would
    match half the directory -- so short titles must also match the artist.
    """
    music_dir = Path(music_dir)
    if not music_dir.is_dir():
        return set()

    stems = []
    for f in music_dir.glob("*.mp3"):
        # macOS AppleDouble sidecars are not real audio.
        if f.name.startswith("._"):
            continue
        stems.append((squash(f.stem), set(normalize(f.stem).split())))

    found: set[str] = set()
    for track in catalog:
        title_sq = squash(track.title)
        if not title_sq:
            continue
        for stem_sq, stem_tokens in stems:
            if title_sq not in stem_sq:
                continue
            if len(title_sq) >= 8 or _artist_matches(track, stem_sq, stem_tokens):
                found.add(track.id)
                break
    return found
