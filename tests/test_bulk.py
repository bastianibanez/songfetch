"""Bulk parsing, per-query verdicts, and what the review screen would download.

The searching is faked throughout: `find_candidates` is the seam, so nothing
here spawns yt-dlp.
"""

from __future__ import annotations

import pytest

from ytmp3 import bulk
from ytmp3.bulk import SKIP, TAKE, BulkResult, bulk_search, parse_queries, search_one
from ytmp3.models import Candidate, ScoredCandidate
from ytmp3.probe import ProbeError


def _candidate(vid: str, title: str, **extra) -> Candidate:
    fields = {
        "id": vid,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "channel": "BICEP",
        "duration": 269,
        "categories": ("Music",),
        "view_count": 1_000_000,
    }
    fields.update(extra)
    return Candidate(**fields)


# -- parsing ---------------------------------------------------------------


def test_blank_lines_and_comments_are_dropped():
    got = parse_queries("# wishlist\n\nbicep glue\n\n  four tet baby  \n# end\n")
    assert got == ["bicep glue", "four tet baby"]


def test_a_trailing_hash_is_part_of_the_query():
    """"Glue #1" is a plausible title; guessing it is a comment corrupts it."""
    assert parse_queries("bicep glue #1\n") == ["bicep glue #1"]


def test_repeats_are_dropped_but_order_is_kept():
    got = parse_queries("bicep glue\nfour tet baby\nBicep  Glue\n")
    assert got == ["bicep glue", "four tet baby"]


def test_an_empty_file_yields_nothing():
    assert parse_queries("\n\n#  \n") == []


def test_read_query_file(tmp_path):
    path = tmp_path / "wishlist.txt"
    path.write_text("# mine\nbicep glue\n", encoding="utf-8")
    assert bulk.read_query_file(path) == ["bicep glue"]


# -- one query -------------------------------------------------------------


@pytest.fixture
def fake_search(monkeypatch):
    """Replace the network with a per-query canned candidate list."""
    answers: dict[str, list[Candidate] | Exception] = {}

    def fake(query, limit=6, cookies_from_browser="", cookies_file="", cancel=None):
        answer = answers.get(query, [])
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(bulk, "find_candidates", fake)
    return answers


def test_a_clear_winner_is_confident(fake_search):
    fake_search["bicep glue"] = [
        _candidate("a", "Glue", music_artist="Bicep", music_track="Glue"),
        _candidate("b", "Glue (fan upload)", channel="Some Guy", view_count=900),
    ]

    result = search_one("bicep glue")

    assert result.confident
    assert result.best is not None and result.best.candidate.id == "a"
    assert result.wanted, "a confident row is opted in without being touched"


def test_a_close_pair_is_ambiguous_and_not_opted_in(fake_search):
    """Two uploads that score alike are exactly what the review screen is for."""
    # Same upload twice under different ids: nothing distinguishes them, so
    # the scores tie and the margin rule refuses to call it.
    fake_search["csirac effy remix"] = [
        _candidate("a", "CSIRAC (Effy Remix)", channel="Ninajirachi"),
        _candidate("b", "CSIRAC (Effy Remix)", channel="Ninajirachi"),
    ]

    result = search_one("csirac effy remix")

    assert not result.confident
    assert result.ambiguity
    assert not result.wanted
    assert result.alternatives, "the runner-up is offered as an alternative"


def test_a_search_failure_is_recorded_not_raised(fake_search):
    fake_search["aphex twin xtal"] = ProbeError("yt-dlp search failed (exit 1): nope")

    result = search_one("aphex twin xtal")

    assert result.error
    assert result.best is None
    assert not result.wanted


def test_no_results_is_not_an_error(fake_search):
    result = search_one("nothing at all")
    assert not result.error
    assert result.best is None
    assert not result.wanted


# -- decisions -------------------------------------------------------------


def _result(query: str, confident: bool) -> BulkResult:
    ranked = [
        ScoredCandidate(candidate=_candidate("a", "Glue"), score=335.0),
        ScoredCandidate(candidate=_candidate("b", "Glue (live)"), score=90.0),
    ]
    return BulkResult(query=query, ranked=ranked, ambiguity="" if confident else "too close")


def test_skipping_a_confident_row_takes_it_out():
    result = _result("bicep glue", confident=True)
    assert result.wanted
    result.decision = SKIP
    assert not result.wanted


def test_taking_an_ambiguous_row_puts_it_in():
    result = _result("csirac", confident=False)
    assert not result.wanted
    result.decision = TAKE
    assert result.wanted


def test_an_explicit_alternative_becomes_the_selection():
    result = _result("bicep glue", confident=True)
    result.chosen = result.ranked[1]
    assert result.selection is not None
    assert result.selection.candidate.id == "b"


def test_a_row_with_nothing_to_download_is_never_wanted():
    """Even ctrl+a cannot take a query that came back empty."""
    result = BulkResult(query="nothing", decision=TAKE)
    assert not result.wanted


def test_the_track_falls_back_to_the_query_for_an_artist():
    """An upload titled just "Glue" still belongs to Bicep if the query said so."""
    result = BulkResult(
        query="Bicep - Glue",
        ranked=[
            ScoredCandidate(
                candidate=_candidate("a", "Glue", channel="", uploader=""), score=335.0
            )
        ],
    )
    track = result.track()
    assert track is not None
    assert track.artist == "Bicep"
    assert track.title == "Glue"


# -- the whole file --------------------------------------------------------

def test_bulk_search_keeps_the_file_order(fake_search):
    """Results come back in the order written, not the order they completed."""
    for i, query in enumerate(["one", "two", "three", "four"]):
        fake_search[query] = [_candidate(f"v{i}", f"Title {i}", music_track=f"Title {i}")]

    got = bulk_search(["one", "two", "three", "four"], workers=4)

    assert [r.query for r in got] == ["one", "two", "three", "four"]


def test_bulk_search_reports_each_result_as_it_lands(fake_search):
    fake_search["one"] = [_candidate("v1", "One")]
    seen: list[str] = []

    bulk_search(["one", "two"], progress=lambda r: seen.append(r.query))

    assert sorted(seen) == ["one", "two"]


def test_an_empty_query_list_does_no_work(fake_search):
    assert bulk_search([]) == []
