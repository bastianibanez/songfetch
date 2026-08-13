"""Head-offset decisions, driven by recorded ffmpeg output."""

from __future__ import annotations

import pytest

from ytmp3.trim import (
    MAX_TRIM,
    head_offset,
    intro_chapter_end,
    leading_silence,
    parse_silences,
)

from conftest import FIXTURES

SILENCE = FIXTURES / "silencedetect"


def stderr(name: str) -> str:
    return (SILENCE / f"{name}.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def test_parse_pairs():
    assert parse_silences(stderr("mid_track_only")) == [(61.5, 63.25), (180.0, 181.5)]


def test_unterminated_silence_at_eof_is_ignored():
    """`clean_start` ends with a silence_start and no silence_end."""
    assert parse_silences(stderr("clean_start")) == []


def test_parse_empty():
    assert parse_silences(stderr("empty")) == []


# --------------------------------------------------------------------------
# leading silence
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("clean_start", 0.0),
        ("short_lead_silence", 0.412375),
        ("long_talking_head", 22.615),
        ("negligible_lead", 0.04),
        ("mid_track_only", 0.0),
        ("empty", 0.0),
    ],
)
def test_leading_silence(name, expected):
    assert leading_silence(stderr(name)) == pytest.approx(expected)


# --------------------------------------------------------------------------
# chapters
# --------------------------------------------------------------------------

def test_intro_chapter_detected():
    chapters = [
        {"start_time": 0.0, "end_time": 12.0, "title": "Intro"},
        {"start_time": 12.0, "end_time": 200.0, "title": "Song"},
    ]
    assert intro_chapter_end(chapters) == 12.0


@pytest.mark.parametrize(
    "title", ["Intro", "intro", "Talking", "Spoken word", "Skit", "Interlude", "Label ident"]
)
def test_intro_chapter_synonyms(title):
    assert intro_chapter_end([{"start_time": 0, "end_time": 8, "title": title}]) == 8.0


def test_music_first_chapter_is_not_an_intro():
    assert intro_chapter_end([{"start_time": 0, "end_time": 90, "title": "Verse 1"}]) == 0.0


def test_intro_chapter_not_at_the_head_is_ignored():
    assert intro_chapter_end([{"start_time": 60, "end_time": 70, "title": "Intro"}]) == 0.0


def test_no_chapters():
    assert intro_chapter_end(None) == 0.0
    assert intro_chapter_end([]) == 0.0


# --------------------------------------------------------------------------
# combined decision
# --------------------------------------------------------------------------

def test_chapter_beats_silence():
    """An uploader-declared intro wins: it may not be silent at all."""
    d = head_offset(
        chapters=[{"start_time": 0, "end_time": 9.0, "title": "Intro"}],
        silence_stderr=stderr("short_lead_silence"),
    )
    assert d.offset == 9.0
    assert "intro chapter" in d.reason


def test_silence_used_when_no_chapters():
    d = head_offset(silence_stderr=stderr("short_lead_silence"))
    assert d.offset == pytest.approx(0.412375)
    assert d.should_trim


def test_negligible_silence_is_left_alone():
    d = head_offset(silence_stderr=stderr("negligible_lead"))
    assert d.offset == 0.0
    assert not d.should_trim
    assert d.reason == "no leading silence"


def test_clean_track_untouched():
    d = head_offset(silence_stderr=stderr("clean_start"))
    assert d.offset == 0.0


def test_long_silence_is_capped_not_applied():
    """22.6s of 'silence' is more likely a bad detection than dead air."""
    d = head_offset(silence_stderr=stderr("long_talking_head"))
    assert d.offset == 0.0
    assert "exceeds" in d.reason


def test_cap_overridable_with_force():
    d = head_offset(silence_stderr=stderr("long_talking_head"), force=True)
    assert d.offset == pytest.approx(22.615)


def test_long_intro_chapter_also_capped():
    chapters = [{"start_time": 0, "end_time": MAX_TRIM + 5, "title": "Intro"}]
    assert head_offset(chapters=chapters).offset == 0.0
    assert head_offset(chapters=chapters, force=True).offset == MAX_TRIM + 5


def test_nothing_at_all():
    d = head_offset()
    assert d.offset == 0.0
    assert not d.should_trim
