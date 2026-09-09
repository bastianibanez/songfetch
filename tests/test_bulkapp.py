"""Bulk review screen. Searching and downloading are both stubbed out."""

from __future__ import annotations

import pytest

from ytmp3 import bulkapp
from ytmp3.bulk import SKIP, TAKE, BulkResult
from ytmp3.bulkapp import GLYPH_ASK, GLYPH_SKIP, GLYPH_TAKE, BulkReviewApp
from ytmp3.models import Candidate, JobState, ScoredCandidate
from ytmp3.pipeline import Options

QUERIES = ["bicep glue", "csirac effy remix", "aphex twin xtal"]


def _scored(vid: str, title: str, score: float, channel: str = "BICEP") -> ScoredCandidate:
    return ScoredCandidate(
        candidate=Candidate(
            id=vid,
            title=title,
            url=f"https://www.youtube.com/watch?v={vid}",
            channel=channel,
            duration=269,
            view_count=1_000_000,
            music_artist=channel,
            music_track=title,
        ),
        score=score,
        reasons=("+100 topic upload",),
    )


def _results() -> list[BulkResult]:
    """One confident row, one ambiguous row, one that found nothing."""
    return [
        BulkResult(query=QUERIES[0], ranked=[_scored("a", "Glue", 335.0)]),
        BulkResult(
            query=QUERIES[1],
            ranked=[
                _scored("b", "CSIRAC (Effy Remix)", 180.0, "Ninajirachi"),
                _scored("c", "CSIRAC (Effy Rework)", 175.0, "Effy"),
            ],
            ambiguity="top two within 40",
        ),
        BulkResult(query=QUERIES[2], error="yt-dlp search failed (exit 1): nope"),
    ]


@pytest.fixture
def opts(tmp_path):
    return Options(music_dir=tmp_path)


@pytest.fixture
def stub_search(monkeypatch):
    """Answer the whole fan-out at once, without threads or network."""
    canned = _results()

    def fake(queries, limit=6, cookies_from_browser="", cookies_file="",
             progress=None, cancel=None, workers=4):
        for result in canned:
            if progress is not None:
                progress(result)
        return canned

    monkeypatch.setattr(bulkapp, "bulk_search", fake)
    return canned


async def test_every_query_gets_a_row(opts, stub_search):
    app = BulkReviewApp(QUERIES, opts)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#results").row_count == 3


async def test_results_fill_in_and_keep_the_file_order(opts, stub_search):
    app = BulkReviewApp(QUERIES, opts)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)
        assert [r.query for r in app.results if r] == QUERIES


async def test_confident_rows_are_taken_and_doubtful_ones_are_not(opts, stub_search):
    """The review screen's whole job: confirm the few, not re-approve the many."""
    app = BulkReviewApp(QUERIES, opts)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

        table = app.query_one("#results")
        assert table.get_cell("0", "state") == GLYPH_TAKE
        assert table.get_cell("1", "state") == GLYPH_ASK
        assert table.get_cell("2", "state") == GLYPH_SKIP


async def test_a_failed_search_says_why(opts, stub_search):
    app = BulkReviewApp(QUERIES, opts)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)
        assert "search failed" in app.query_one("#results").get_cell("2", "note")


async def test_space_toggles_the_row_under_the_cursor(opts, stub_search):
    app = BulkReviewApp(QUERIES, opts)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

        await pilot.press("space")          # cursor starts on row 0, a taken row
        await pilot.pause()
        assert app.results[0].decision == SKIP
        assert app.query_one("#results").get_cell("0", "state") == GLYPH_SKIP

        await pilot.press("space")
        await pilot.pause()
        assert app.results[0].decision == TAKE


async def test_space_opts_an_ambiguous_row_in(opts, stub_search):
    app = BulkReviewApp(QUERIES, opts)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

        await pilot.press("down")
        await pilot.press("space")
        await pilot.pause()
        assert app.results[1].wanted


async def test_a_row_with_no_results_cannot_be_taken(opts, stub_search):
    app = BulkReviewApp(QUERIES, opts)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

        await pilot.press("down", "down", "space")
        await pilot.pause()
        assert not app.results[2].wanted


async def test_take_all_and_skip_all(opts, stub_search):
    app = BulkReviewApp(QUERIES, opts)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

        await pilot.press("ctrl+a")
        await pilot.pause()
        assert [r.wanted for r in app.results] == [True, True, False]

        await pilot.press("ctrl+d")
        await pilot.pause()
        assert not any(r.wanted for r in app.results)


async def test_downloading_takes_only_the_wanted_rows(opts, stub_search, monkeypatch):
    seen: list[str] = []

    async def fake_run_all(jobs, options, progress=None, chooser=None):
        for job in jobs:
            seen.append(job.track.id)
            job.state = JobState.DONE
        return jobs

    monkeypatch.setattr(bulkapp, "run_all", fake_run_all)
    monkeypatch.setattr(bulkapp, "add_tracks", lambda music_dir, catalog, new: catalog + new)

    app = BulkReviewApp(QUERIES, opts)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

        await pilot.press("ctrl+r")
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

    # Only the confident row was opted in, so only it downloads.
    assert seen == ["yt:a"]


async def test_downloading_nothing_is_a_no_op(opts, stub_search, monkeypatch):
    called = False

    async def fake_run_all(jobs, options, progress=None, chooser=None):
        nonlocal called
        called = True
        return jobs

    monkeypatch.setattr(bulkapp, "run_all", fake_run_all)

    app = BulkReviewApp(QUERIES, opts)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

        await pilot.press("ctrl+d")     # skip everything
        await pilot.press("ctrl+r")
        await pilot.pause(0.2)

    assert not called


async def test_picking_an_alternative_selects_it_and_takes_the_row(opts, stub_search):
    """Nobody opens the alternatives list to pick an upload they then leave out."""
    app = BulkReviewApp(QUERIES, opts)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

        await pilot.press("down")       # the ambiguous row
        await pilot.press("enter")
        await pilot.pause()

        # The list opens with the top option highlighted, so one `down` reaches
        # the alternative.
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause(0.2)

        result = app.results[1]
        assert result.selection is not None
        assert result.selection.candidate.id == "c"
        assert result.decision == TAKE
        assert "picked" in app.query_one("#results").get_cell("1", "note")
