"""Search plumbing: what gets asked for, and what comes back parsed.

Every test here fakes `probe._run`, so nothing touches the network. The point
is the *shape* of the yt-dlp invocations -- which flags, which URLs, in which
order -- because that shape is the whole performance fix.
"""

from __future__ import annotations

import json

import pytest

from ytmp3 import probe
from ytmp3.probe import (
    MUSIC_OVERFETCH,
    MUSIC_SONGS_FILTER,
    Cancel,
    Cancelled,
    dedupe,
    find_candidates,
    parse_dump_json,
)


def _entry(vid: str, title: str = "", **extra) -> str:
    return json.dumps({"id": vid, "title": title or vid, **extra})


def _lines(*entries: str) -> str:
    return "\n".join(entries) + "\n"


class Calls(list):
    """The recorded yt-dlp invocations, plus the canned replies to give back."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: dict[str, str] = {}


@pytest.fixture
def calls(monkeypatch):
    """Record every yt-dlp invocation and reply with a canned dump.

    Replies are keyed by a substring of the joined args, first match wins;
    anything unmatched comes back empty, which is how "the songs tab found
    nothing" is expressed.
    """
    recorded = Calls()

    def fake_run(args, timeout, cancel=None):
        # Order matters and mirrors the real `_run`: a cancelled search must
        # not be recorded as work done.
        if cancel is not None:
            cancel.check()
        recorded.append(list(args))
        joined = " ".join(args)
        for needle, out in recorded.replies.items():
            if needle in joined:
                return out
        return ""

    monkeypatch.setattr(probe, "_run", fake_run)
    return recorded


# -- parsing ---------------------------------------------------------------


def test_channels_and_playlists_are_not_candidates():
    """Flat listings put channels and playlists next to the videos.

    Full extraction never surfaced them, so nothing filtered them before; a
    flat listing does, and a channel is not something you can download.
    """
    out = parse_dump_json(
        _lines(
            _entry("vid1", "A Song", ie_key="Youtube"),
            _entry("chan", "Some Artist", ie_key="YoutubeTab"),
            _entry("list", "Best Of", ie_key="YoutubePlaylist"),
            json.dumps({"_type": "playlist", "id": "pl", "title": "Album"}),
        )
    )
    assert [c.id for c in out] == ["vid1"]


def test_a_malformed_line_does_not_lose_the_rest():
    out = parse_dump_json(_lines(_entry("a"), "{not json", "", _entry("b")))
    assert [c.id for c in out] == ["a", "b"]


def test_dedupe_keeps_the_first_occurrence():
    got = parse_dump_json(_lines(_entry("a", "music"), _entry("a", "plain"), _entry("b")))
    assert [c.title for c in dedupe(got)] == ["music", "b"]


# -- the songs-tab fix -----------------------------------------------------


def test_yt_music_asks_the_songs_tab(calls):
    """The unfiltered YT Music page is a page of sections, not a list.

    yt-dlp walks into every album and playlist on it one video at a time and
    ignores --playlist-end while doing so: 67 entries in 281 seconds for one
    query. The Songs tab is an actual list. This is the fix.
    """
    calls.replies[MUSIC_SONGS_FILTER] = _lines(_entry("m1"))
    calls.replies["ytsearch"] = _lines(_entry("y1"))

    find_candidates("bicep glue", limit=4)

    music = [c for c in calls if any("music.youtube.com" in a for a in c)]
    assert music, "no YT Music request was made"
    assert any(MUSIC_SONGS_FILTER in a for a in music[0])


def test_listing_is_flat_and_hydration_is_not(calls):
    """Two phases, two flag sets: name them cheaply, then look them up."""
    calls.replies[MUSIC_SONGS_FILTER] = _lines(_entry("m1"))
    calls.replies["watch?v=m1"] = _lines(_entry("m1", "M1", categories=["Music"]))

    find_candidates("bicep glue", limit=2, music_first=True)

    listing = [c for c in calls if any("music.youtube.com" in a or "ytsearch" in a for a in c)]
    detail = [c for c in calls if any("watch?v=" in a for a in c)]
    assert listing and all("--flat-playlist" in c for c in listing)
    assert detail and all("--no-flat-playlist" in c for c in detail)


def test_the_songs_tab_is_asked_for_limit_times_overfetch(calls):
    calls.replies[MUSIC_SONGS_FILTER] = _lines(_entry("m1"))

    find_candidates("bicep glue", limit=6)

    music = next(c for c in calls if any("music.youtube.com" in a for a in c))
    end = music[music.index("--playlist-end") + 1]
    assert end == str(6 * MUSIC_OVERFETCH)


def test_an_empty_songs_tab_falls_back_to_the_unfiltered_page(calls):
    """`sp` is an opaque YouTube parameter, so it is allowed to stop working.

    Slow beats broken: if the songs tab yields nothing we go back to the page
    that costs 281 seconds rather than reporting no results.
    """
    calls.replies["music.youtube.com"] = ""  # songs tab and fallback both empty

    find_candidates("bicep glue", limit=3)

    music = [" ".join(c) for c in calls if "music.youtube.com" in " ".join(c)]
    assert len(music) == 2, music
    assert MUSIC_SONGS_FILTER in music[0]
    assert MUSIC_SONGS_FILTER not in music[1]


def test_a_populated_songs_tab_does_not_hit_the_unfiltered_page(calls):
    calls.replies[MUSIC_SONGS_FILTER] = _lines(_entry("m1"))

    find_candidates("bicep glue", limit=3)

    music = [" ".join(c) for c in calls if "music.youtube.com" in " ".join(c)]
    assert len(music) == 1


# -- merging ---------------------------------------------------------------


def test_music_results_win_the_dedupe(calls):
    """Both sources are listed concurrently, but preference is not a race."""
    calls.replies[MUSIC_SONGS_FILTER] = _lines(_entry("same", "art track"))
    calls.replies["ytsearch"] = _lines(_entry("same", "fan rip"), _entry("other"))

    got = find_candidates("bicep glue", limit=3, music_first=True)

    assert [c.id for c in got] == ["same", "other"]
    assert got[0].title == "art track"


def test_hydration_failure_keeps_the_flat_candidate(monkeypatch):
    """A lookup that fails costs ranking signal, not the result."""
    listed = _lines(_entry("m1", "Flat Title"))

    def fake_run(args, timeout, cancel=None):
        joined = " ".join(args)
        if "watch?v=" in joined:
            raise probe.ProbeError("boom")
        return listed if MUSIC_SONGS_FILTER in joined else ""

    monkeypatch.setattr(probe, "_run", fake_run)

    got = find_candidates("bicep glue", limit=2)
    assert [c.title for c in got] == ["Flat Title"]


def test_both_sources_failing_is_an_error(monkeypatch):
    def fake_run(args, timeout, cancel=None):
        raise probe.ProbeError("yt-dlp search failed (exit 1): nope")

    monkeypatch.setattr(probe, "_run", fake_run)

    with pytest.raises(probe.ProbeError):
        find_candidates("bicep glue", limit=2)


# -- cancellation ----------------------------------------------------------


def test_a_pre_cancelled_search_spends_nothing(calls):
    cancel = Cancel()
    cancel.cancel()

    with pytest.raises(Cancelled):
        find_candidates("bicep glue", limit=3, cancel=cancel)

    assert calls == []
