"""TUI smoke tests. The pipeline is stubbed: no network, no ffmpeg."""

from __future__ import annotations

import asyncio

import pytest

from ytmp3 import pipeline, probe
from ytmp3.app import WEB_DEBOUNCE, YtMp3App
from ytmp3.catalog import load_catalog
from ytmp3.models import Candidate, JobState, ScoredCandidate
from ytmp3.pipeline import Options
from ytmp3.probe import ProbeError


@pytest.fixture
def opts(tmp_path):
    # web_fallback off by default: these tests are about the catalog, and an
    # automatic web search would put a real yt-dlp subprocess in the suite.
    return Options(music_dir=tmp_path, web_fallback=False)


async def test_starts_with_the_whole_catalog(opts):
    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.visible_ids) == 77


async def test_typing_filters_the_table(opts):
    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"ninajirachi")
        await pilot.pause()
        assert app.visible_ids == ["csirac", "ipod-touch", "london-song"]
        assert app.query_one("#results").row_count == 3


async def test_clearing_the_query_restores_everything(opts):
    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"levity")
        await pilot.pause()
        assert len(app.visible_ids) == 2
        for _ in range(len("levity")):
            await pilot.press("backspace")
        await pilot.pause()
        assert len(app.visible_ids) == 77


async def test_space_toggles_selection(opts):
    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"tlalnepantla")
        await pilot.press("down")          # focus moves to the table
        await pilot.press("space")
        await pilot.pause()
        assert app.selected == {"tlalnepantla"}
        await pilot.press("space")
        await pilot.pause()
        assert app.selected == set()


async def test_select_all_shown_only(opts):
    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"ninajirachi")
        await pilot.pause()
        await pilot.press("ctrl+a")
        await pilot.pause()
        assert app.selected == {"csirac", "ipod-touch", "london-song"}


async def test_enqueue_runs_the_pipeline(opts, monkeypatch):
    """Enter on a selected row must hand exactly that track to the pipeline."""
    seen: list[list[str]] = []

    async def fake_run_all(jobs, options, progress=None, chooser=None):
        seen.append([j.track.id for j in jobs])
        for job in jobs:
            job.state = JobState.DONE
            job.message = "stubbed"
            if progress:
                progress(job)
        return jobs

    monkeypatch.setattr("ytmp3.app.run_all", fake_run_all)

    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"csirac")
        await pilot.press("down")
        await pilot.press("space")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

    assert seen == [["csirac"]]
    assert app.jobs["csirac"].state is JobState.DONE


async def test_submitting_playlist_url_downloads_from_the_tui(tmp_path, monkeypatch):
    opts = Options(music_dir=tmp_path, cookies_from_browser="chrome")
    candidate = Candidate("firstVideo1", "Artist - Song", "https://www.youtube.com/watch?v=firstVideo1")
    requested = []
    downloaded = []
    started = asyncio.Event()
    finish = asyncio.Event()

    def list_playlist(url, browser, cookies_file):
        requested.append((url, browser, cookies_file))
        return [candidate, None, candidate]

    async def run_all(jobs, options, progress):
        assert options is opts
        jobs[0].state = JobState.DOWNLOADING
        progress(jobs[0])
        started.set()
        await finish.wait()
        for job in jobs:
            assert job.chosen == candidate
            downloaded.append(job.destination.relative_to(tmp_path))
            job.state = JobState.DONE
            progress(job)

    monkeypatch.setattr(probe, "list_playlist", list_playlist)
    monkeypatch.setattr("ytmp3.app.run_all", run_all)
    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        app.query_one("#search").value = "csirac"
        await pilot.press("ctrl+t")
        assert app.focused.id == "playlist-url"
        app.query_one("#playlist-url").value = "https://example.com/list"
        await pilot.press("enter")
        assert "playlist ID" in str(app.query_one("#playlist-status").content)
        app.query_one("#playlist-url").value = (
            "https://music.youtube.com/playlist?list=PL_example&si=share"
        )
        await pilot.pause()
        assert app._web_timer is None
        await pilot.click("#download-playlist")
        await asyncio.wait_for(started.wait(), timeout=2)
        assert app.query_one("#download-playlist").disabled
        app.query_one("#playlist-url").focus()
        await pilot.press("ctrl+t")
        assert app.focused.id == "search"
        assert app.query_one("#search").value == "csirac"
        assert app.visible_ids == ["csirac"]
        finish.set()
        await app.workers.wait_for_complete()
        await pilot.click("#--content-tab-playlists")
        assert app.focused.id == "playlist-url"
        status = str(app.query_one("#playlist-status").content)
        assert "Playlist 3/3" in status
        assert "1 unavailable" in status
        table = app.query_one("#playlist-results")
        assert table.row_count == 3
        assert str(table.get_cell("1", "state")) == "Saved"
        assert str(table.get_cell("2", "state")) == "Unavailable"
        await pilot.press("down", "down")
        assert "playable track" in str(app.query_one("#playlist-detail").content)
        await pilot.press("escape")
        assert app.focused.id == "playlist-url"

    assert requested == [("https://music.youtube.com/playlist?list=PL_example", "chrome", "")]
    assert [path.as_posix() for path in downloaded] == [
        "playlists/PL_example/0001 - Artist - Song [firstVideo1].mp3",
        "playlists/PL_example/0003 - Artist - Song [firstVideo1].mp3",
    ]


async def test_on_disk_tracks_are_marked(tmp_path):
    (tmp_path / "AYYBO - RIZZ.mp3").write_bytes(b"\x00")
    app = YtMp3App(Options(music_dir=tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.jobs["rizz"].state is JobState.ON_DISK
        assert app.jobs["csirac"].state is JobState.PENDING


async def test_no_match_empties_the_table(opts):
    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"zzqqxxvv")
        await pilot.pause()
        assert app.visible_ids == []
        assert app.query_one("#results").row_count == 0


def test_options_defaults(tmp_path):
    o = Options(music_dir=tmp_path)
    assert o.concurrency == pipeline.DEFAULT_CONCURRENCY
    assert o.cbr320 is False
    assert o.web_fallback is True


# --- web search -----------------------------------------------------------
#
# `discover.web_search` is the single network call, so stubbing it is enough to
# keep the whole web path offline.

def fake_result(vid: str, title: str, score: float, **kw) -> ScoredCandidate:
    c = Candidate(id=vid, title=title, url=f"https://youtu.be/{vid}", **kw)
    return ScoredCandidate(candidate=c, score=score, reasons=("stub",))


RESULTS = [
    fake_result("aaa", "Bicep - Glue", 300.0, channel="Bicep", duration=330, view_count=1_200_000),
    fake_result("bbb", "Glue (Official Video)", 120.0, channel="Ninja Tune", duration=331),
]


@pytest.fixture
def stub_web(monkeypatch):
    calls: list[str] = []

    def fake(query, limit=6, cookies_from_browser="", cookies_file="", cancel=None):
        calls.append(query)
        return list(RESULTS)

    monkeypatch.setattr("ytmp3.discover.web_search", fake)
    return calls


async def test_ctrl_f_adds_a_web_section(opts, stub_web):
    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"bicep glue")
        await pilot.pause()
        assert app.visible_ids == []          # nothing in the catalog

        await pilot.press("ctrl+f")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.visible_ids == ["yt:aaa", "yt:bbb"]
        assert app.web_status == "(2)"
        # two section headers on top of the two results
        assert app.query_one("#results").row_count == 4

    assert stub_web == ["bicep glue"]


async def test_web_rows_carry_the_parsed_track(opts, stub_web):
    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"bicep glue")
        await pilot.press("ctrl+f")
        await app.workers.wait_for_complete()
        await pilot.pause()

    job = app.jobs["yt:aaa"]
    assert (job.track.artist, job.track.title) == ("Bicep", "Glue")
    assert job.chosen is not None and job.chosen.id == "aaa"
    assert "1.2M views" in job.message


async def test_typing_on_invalidates_the_web_section(opts, stub_web):
    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"bicep glue")
        await pilot.press("ctrl+f")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.visible_ids == ["yt:aaa", "yt:bbb"]

        await pilot.press("x")
        await pilot.pause()

        assert app.web_tracks == []
        assert app.visible_ids == []
        assert app.query_one("#results").row_count == 0


async def test_a_failed_web_search_is_reported_not_raised(opts, monkeypatch):
    def boom(*a, **kw):
        raise ProbeError("yt-dlp not found on PATH")

    monkeypatch.setattr("ytmp3.discover.web_search", boom)

    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"bicep glue")
        await pilot.press("ctrl+f")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert "yt-dlp not found" in app.web_status
    assert app.visible_ids == []


async def test_section_headers_are_not_tracks(opts, stub_web):
    """The cursor lands on a header first; selecting there must do nothing."""
    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"bicep glue")
        await pilot.press("ctrl+f")
        await app.workers.wait_for_complete()
        await pilot.press("down")      # into the table, cursor on "catalog (0)"
        await pilot.press("space")
        await pilot.pause()
        assert app.selected == set()

        await pilot.press("ctrl+a")    # selects the rows, never the headers
        await pilot.pause()
        assert app.selected == {"yt:aaa", "yt:bbb"}


async def test_downloading_a_web_result_skips_the_search(opts, stub_web, monkeypatch):
    """`chosen` is already set, so the pipeline must not go looking again."""
    seen: list[tuple[str, str | None]] = []

    async def fake_run_all(jobs, options, progress=None, chooser=None):
        for job in jobs:
            seen.append((job.track.id, job.chosen.id if job.chosen else None))
            job.state = JobState.DONE
            if progress:
                progress(job)
        return jobs

    monkeypatch.setattr("ytmp3.app.run_all", fake_run_all)

    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"bicep glue")
        await pilot.press("ctrl+f")
        await app.workers.wait_for_complete()
        await pilot.pause()

        app.selected = {"yt:aaa"}
        await pilot.press("ctrl+r")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert seen == [("yt:aaa", "aaa")]


async def test_a_downloaded_web_track_joins_the_catalog(opts, stub_web, monkeypatch):
    async def fake_run_all(jobs, options, progress=None, chooser=None):
        for job in jobs:
            job.state = JobState.DONE
        return jobs

    monkeypatch.setattr("ytmp3.app.run_all", fake_run_all)

    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"bicep glue")
        await pilot.press("ctrl+f")
        await app.workers.wait_for_complete()
        await pilot.pause()
        before = len(app.catalog)

        app.selected = {"yt:aaa"}
        await pilot.press("ctrl+r")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(app.catalog) == before + 1
    persisted = load_catalog(opts.music_dir / "catalog.json")
    assert persisted[-1].id == "yt:aaa"
    assert persisted[-1].title == "Glue"


async def test_a_skipped_web_track_is_not_kept(opts, stub_web, monkeypatch):
    """Only a track that reached the disk belongs in the catalog."""
    async def fake_run_all(jobs, options, progress=None, chooser=None):
        for job in jobs:
            job.state = JobState.FAILED
            job.message = "stubbed failure"
        return jobs

    monkeypatch.setattr("ytmp3.app.run_all", fake_run_all)

    app = YtMp3App(opts)
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"bicep glue")
        await pilot.press("ctrl+f")
        await app.workers.wait_for_complete()
        await pilot.pause()
        before = len(app.catalog)

        app.selected = {"yt:aaa"}
        await pilot.press("ctrl+r")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(app.catalog) == before


async def test_the_auto_fallback_waits_for_an_empty_catalog(tmp_path, stub_web):
    """A query the catalog answers must never spend a yt-dlp call."""
    app = YtMp3App(Options(music_dir=tmp_path, web_fallback=True))
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"csirac")
        await pilot.pause(WEB_DEBOUNCE * 2)
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert stub_web == []
    assert app.web_query == ""


async def test_the_auto_fallback_fires_when_nothing_matches(tmp_path, stub_web):
    app = YtMp3App(Options(music_dir=tmp_path, web_fallback=True))
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"bicep glue")
        await pilot.pause(WEB_DEBOUNCE * 2)
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert stub_web == ["bicep glue"]
    assert app.visible_ids == ["yt:aaa", "yt:bbb"]


async def test_the_auto_fallback_debounces(tmp_path, stub_web):
    """One search for a burst of keystrokes, not one per character."""
    app = YtMp3App(Options(music_dir=tmp_path, web_fallback=True))
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"bicep glue")
        await pilot.pause(WEB_DEBOUNCE * 2)
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(stub_web) == 1


async def test_web_fallback_off_means_no_automatic_search(opts, stub_web):
    app = YtMp3App(opts)   # the fixture sets web_fallback=False
    async with app.run_test() as pilot:
        await pilot.click("#search")
        await pilot.press(*"bicep glue")
        await pilot.pause(WEB_DEBOUNCE * 2)
        await pilot.pause()

    assert stub_web == []
