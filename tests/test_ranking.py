"""Candidate ranking: the 'don't download the music video' rules."""

from __future__ import annotations

import json

import pytest

from ytmp3.models import Candidate
from ytmp3.ranking import Ambiguous, MIN_ACCEPT, pick, rank, score_candidate

from conftest import FIXTURES

# `._*` are macOS AppleDouble sidecars, which appear whenever this repo lives on
# a FAT/exFAT volume (the flash drive it ships on). They are binary, not JSON.
CANDIDATE_FILES = sorted(
    p for p in (FIXTURES / "candidates").glob("*.json") if not p.name.startswith("._")
)
assert CANDIDATE_FILES, "no candidate fixtures found"


def _load(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["candidates"] = [Candidate.from_ytdlp(e) for e in data["entries"]]
    return data


@pytest.mark.parametrize("path", CANDIDATE_FILES, ids=lambda p: p.stem)
def test_candidate_fixture(path):
    case = _load(path)
    query = case["query"]
    expected = case.get("expected_duration")

    ranked = rank(case["candidates"], query, expected)
    detail = "\n".join(
        f"    {s.candidate.id:14} {s.score:8.1f}  {s.explain()}" for s in ranked
    )

    if case["expect_ambiguous"]:
        with pytest.raises(Ambiguous) as exc:
            pick(case["candidates"], query, expected)
        if "expect_reason_contains" in case:
            assert case["expect_reason_contains"] in exc.value.reason, detail
    else:
        chosen = pick(case["candidates"], query, expected)
        assert chosen.candidate.id == case["expect_winner"], (
            f"{case['note']}\n{detail}"
        )
        if "expect_reason_contains" in case:
            assert case["expect_reason_contains"] in chosen.explain(), detail


@pytest.mark.parametrize("path", CANDIDATE_FILES, ids=lambda p: p.stem)
def test_ranking_is_stable(path):
    case = _load(path)
    a = [s.candidate.id for s in rank(case["candidates"], case["query"], case.get("expected_duration"))]
    b = [s.candidate.id for s in rank(list(reversed(case["candidates"])), case["query"], case.get("expected_duration"))]
    assert a == b, "ranking must not depend on input order"


# --------------------------------------------------------------------------
# targeted rule checks
# --------------------------------------------------------------------------

def _cand(**kw) -> Candidate:
    base = {
        "id": "x",
        "title": "Some Track",
        "channel": "Some Artist",
        "uploader": "Some Artist",
        "description": "",
        "duration": 200,
        "categories": ["Music"],
        "view_count": 1000,
        "thumbnails": [{"width": 1280, "height": 720}],
    }
    base.update(kw)
    return Candidate.from_ytdlp(base)


def test_topic_channel_detected():
    topic = _cand(channel="Some Artist - Topic", uploader="Some Artist - Topic")
    plain = _cand()
    q = "Some Artist Some Track"
    assert score_candidate(topic, q).score > score_candidate(plain, q).score


def test_music_metadata_counts_as_topic_even_off_topic_channel():
    """YT Music art tracks carry artist+track even when the channel is branded."""
    art = _cand(artist="Some Artist", track="Some Track")
    assert "topic/art-track" in score_candidate(art, "Some Artist Some Track").explain()


def test_official_video_is_penalised():
    mv = _cand(title="Some Artist - Some Track (Official Music Video)")
    plain = _cand()
    q = "Some Artist Some Track"
    assert score_candidate(mv, q).score < score_candidate(plain, q).score - 70


def test_penalty_suppressed_when_query_asks_for_it():
    live = _cand(title="Some Track (Live at EDC)")
    unsuppressed = score_candidate(live, "Some Artist Some Track")
    suppressed = score_candidate(live, "Some Artist Some Track Live at EDC")
    assert "live suppressed" in suppressed.explain()
    assert suppressed.score > unsuppressed.score


def test_wrong_song_topic_upload_loses_to_right_song_rip():
    """Regression: source quality must never outweigh actually being the track."""
    wrong_topic = _cand(
        id="wrong",
        title="Something Else",
        channel="Other Artist - Topic",
        uploader="Other Artist - Topic",
        description="Provided to YouTube by A Label\n\nSomething Else",
        artist="Other Artist",
        track="Something Else",
        thumbnails=[{"width": 544, "height": 544}],
    )
    right_rip = _cand(id="right", title="Some Artist - Some Track (VIP Edit)", channel="RipChannel")
    best = pick([wrong_topic, right_rip], "Some Artist Some Track VIP Edit")
    assert best.candidate.id == "right"


def test_remix_loses_to_the_original():
    """Regression from live results: both have full coverage, so only the
    precision/variant terms can separate 'CSIRAC' from 'CSIRAC (Effy Remix)'."""
    topic = {
        "channel": "Ninajirachi",
        "uploader": "Ninajirachi",
        "description": "Provided to YouTube by a label\n\nCSIRAC",
        "artist": "Ninajirachi",
        "categories": ["Music"],
    }
    original = _cand(id="orig", title="CSIRAC", track="CSIRAC", duration=202, **topic)
    remix = _cand(id="remix", title="CSIRAC (Effy Remix)", track="CSIRAC (Effy Remix)", duration=274, **topic)
    best = pick([remix, original], "Ninajirachi CSIRAC")
    assert best.candidate.id == "orig"


def test_remix_wins_when_the_query_asks_for_it():
    topic = {
        "channel": "Ninajirachi",
        "uploader": "Ninajirachi",
        "description": "Provided to YouTube by a label\n\nCSIRAC",
        "artist": "Ninajirachi",
        "categories": ["Music"],
    }
    original = _cand(id="orig", title="CSIRAC", track="CSIRAC", duration=202, **topic)
    remix = _cand(id="remix", title="CSIRAC (Effy Remix)", track="CSIRAC (Effy Remix)", duration=274, **topic)
    best = pick([original, remix], "Ninajirachi CSIRAC Effy Remix")
    assert best.candidate.id == "remix"


@pytest.mark.parametrize(
    "title", ["Track (VIP)", "Track (Bootleg)", "Track - Extended Mix", "Track (Acoustic)", "Track (Instrumental)"]
)
def test_variant_markers_penalised(title):
    plain = score_candidate(_cand(title="Track"), "Some Artist Track")
    variant = score_candidate(_cand(title=title), "Some Artist Track")
    assert variant.score < plain.score


def test_live_flag_penalised():
    q = "Some Artist Some Track"
    assert score_candidate(_cand(is_live=True), q).score < score_candidate(_cand(), q).score


def test_duration_mismatch_penalised():
    q = "Some Artist Some Track"
    good = score_candidate(_cand(duration=200), q, expected_duration=200)
    bad = score_candidate(_cand(duration=400), q, expected_duration=200)
    assert good.score > bad.score


def test_too_short_rejected_when_duration_unknown():
    q = "Some Artist Some Track"
    assert score_candidate(_cand(duration=30), q).score < score_candidate(_cand(duration=200), q).score


def test_long_upload_allowed_when_query_says_extended():
    """Isolate the duration rule: compare reasons, not totals.

    Adding words to the query also changes coverage, so comparing raw scores
    across two different queries would measure two things at once.
    """
    long_c = _cand(title="Some Track (Extended Mix)", duration=900)
    assert "too long" not in score_candidate(long_c, "Some Artist Some Track Extended Mix").explain()
    assert "too long" in score_candidate(_cand(duration=900), "Some Artist Some Track").explain()


def test_empty_candidate_list_is_ambiguous():
    with pytest.raises(Ambiguous):
        pick([], "anything")


def test_min_accept_is_enforced():
    junk = _cand(title="totally unrelated upload", channel="Nobody", categories=[])
    with pytest.raises(Ambiguous) as exc:
        pick([junk], "Some Artist Some Track")
    assert "below" in exc.value.reason
    assert score_candidate(junk, "Some Artist Some Track").score < MIN_ACCEPT
