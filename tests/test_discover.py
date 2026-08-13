"""Web-search parsing. Pure: no network, no yt-dlp."""

from __future__ import annotations

import pytest

from ytmp3.catalog import add_tracks, load_catalog
from ytmp3.discover import (
    WEB_ID_PREFIX,
    clean_title,
    is_web_track,
    split_artist_title,
    track_from_candidate,
)
from ytmp3.models import Candidate, Track


def make(**kw) -> Candidate:
    base = dict(id="vid123", title="Something", url="https://youtu.be/vid123")
    return Candidate(**{**base, **kw})


# --- clean_title ----------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("TAKA (Official Music Video)", "TAKA"),
        ("TAKA [Official Video]", "TAKA"),
        ("Move (Official Audio)", "Move"),
        ("Down [Lyric Video]", "Down"),
        ("CSIRAC (HD)", "CSIRAC"),
        ("Bass Dhol [4K]", "Bass Dhol"),
        ("For the Sky [NCS Release]", "For the Sky"),
        ("Told You (Free Download)", "Told You"),
        ("Something (Visualizer)", "Something"),
    ],
)
def test_upload_noise_is_stripped(raw, expected):
    assert clean_title(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "CSIRAC (Effy Remix)",
        "Talk To Me (Extended Mix)",
        "Move (KARAN! Flip)",
        "Down [VIP]",
        "Pink Countach (Radio Edit)",
    ],
)
def test_variant_brackets_survive(raw):
    """The bracket is part of what the recording IS -- stripping it would make
    the remix indistinguishable from the original in the catalog."""
    assert clean_title(raw) == raw


def test_noise_and_variant_together():
    assert clean_title("CSIRAC (Effy Remix) [HD]") == "CSIRAC (Effy Remix)"


def test_empty_title_survives():
    assert clean_title("") == ""


def test_all_noise_falls_back_to_the_original():
    # Stripping everything would leave an unlabelled row; the raw title is
    # worse but still identifies something.
    assert clean_title("(Official Video)") == "(Official Video)"


# --- split_artist_title ---------------------------------------------------

@pytest.mark.parametrize(
    "raw,artist,title",
    [
        ("LNRT - Move", "LNRT", "Move"),
        ("Ahadadream, Priya Ragu & Skrillex - TAKA", "Ahadadream, Priya Ragu & Skrillex", "TAKA"),
        ("Travmkind – Mein Körper", "Travmkind", "Mein Körper"),
        ("Levity — Blue", "Levity", "Blue"),
    ],
)
def test_artist_title_split(raw, artist, title):
    assert split_artist_title(raw) == (artist, title)


def test_only_the_first_separator_splits():
    assert split_artist_title("A & B - Song - Live") == ("A & B", "Song - Live")


@pytest.mark.parametrize("raw", ["Sub-Zero", "CSIRAC", "hi-fi-lo"])
def test_bare_hyphens_do_not_split(raw):
    """A hyphen without surrounding spaces is inside a word, not a separator."""
    assert split_artist_title(raw) == ("", raw)


# --- track_from_candidate -------------------------------------------------

def test_music_metadata_is_trusted_over_the_title():
    c = make(
        title="TAKA (Official Music Video)",
        music_artist="Ahadadream",
        music_track="TAKA",
        music_album="TAKA - Single",
        duration=192,
    )
    track = track_from_candidate(c)
    assert (track.artist, track.title, track.album) == ("Ahadadream", "TAKA", "TAKA - Single")
    assert track.duration == 192


def test_title_is_parsed_when_there_is_no_music_metadata():
    track = track_from_candidate(make(title="LNRT - Move (Official Video)", channel="LNRT"))
    assert (track.artist, track.title) == ("LNRT", "Move")


def test_channel_is_the_artist_when_the_title_has_no_separator():
    track = track_from_candidate(make(title="Move", channel="LNRT"))
    assert (track.artist, track.title) == ("LNRT", "Move")


def test_topic_suffix_is_dropped_from_the_channel_artist():
    track = track_from_candidate(make(title="Move", channel="LNRT - Topic"))
    assert track.artist == "LNRT"


def test_uploader_is_the_fallback_when_there_is_no_channel():
    track = track_from_candidate(make(title="Move", uploader="LNRT"))
    assert track.artist == "LNRT"


def test_id_is_namespaced_by_video_id():
    track = track_from_candidate(make(id="abc123"))
    assert track.id == f"{WEB_ID_PREFIX}abc123"
    assert is_web_track(track)


def test_catalog_tracks_are_not_web_tracks(catalog):
    assert not any(is_web_track(t) for t in catalog)


def test_a_track_that_parses_to_nothing_keeps_the_raw_title():
    track = track_from_candidate(make(title="(Official Video)"))
    assert track.title == "(Official Video)"


# --- keeping a downloaded web track --------------------------------------

def test_add_tracks_appends_and_persists(tmp_path, catalog):
    live = tmp_path / "catalog.json"
    new = Track(id="yt:abc", title="TAKA", artist="Ahadadream", duration=192)

    updated = add_tracks(tmp_path, list(catalog), [new])

    assert len(updated) == len(catalog) + 1
    assert updated[-1] == new
    assert live.exists()
    assert load_catalog(live)[-1] == new


def test_add_tracks_is_idempotent(tmp_path, catalog):
    new = Track(id="yt:abc", title="TAKA", artist="Ahadadream")
    once = add_tracks(tmp_path, list(catalog), [new])
    twice = add_tracks(tmp_path, once, [new])
    assert len(twice) == len(once)


def test_add_tracks_dedupes_within_one_call(tmp_path, catalog):
    new = Track(id="yt:abc", title="TAKA", artist="Ahadadream")
    updated = add_tracks(tmp_path, list(catalog), [new, new])
    assert sum(1 for t in updated if t.id == "yt:abc") == 1


def test_add_tracks_leaves_an_existing_id_alone(tmp_path, catalog):
    clash = Track(id=catalog[0].id, title="Wrong", artist="Wrong")
    updated = add_tracks(tmp_path, list(catalog), [clash])
    assert updated == list(catalog)


def test_add_tracks_survives_an_unwritable_music_dir(tmp_path, catalog):
    """Losing the download because the flash drive went away would be worse
    than a catalog file that is briefly behind."""
    blocked = tmp_path / "catalog.json"
    blocked.mkdir()  # a directory where the file should be -> OSError on write
    new = Track(id="yt:abc", title="TAKA", artist="Ahadadream")
    assert add_tracks(tmp_path, list(catalog), [new])[-1] == new
