"""Textual TUI: type to search the catalog or the web, select tracks, watch them download."""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import discover, probe
from .catalog import add_tracks, resolve_catalog, scan_existing
from .models import Candidate, DownloadJob, JobState, ScoredCandidate, Track
from .pipeline import Options, jobs_for, run_all
from .search import search_tracks

# Row keys for the section separators. The \x00 prefix cannot collide with a
# catalog id or a `yt:<videoid>`, which is what makes "is this a real row?"
# a substring check rather than a lookup.
HEADER_PREFIX = "\x00"
HDR_CATALOG = f"{HEADER_PREFIX}catalog"
HDR_WEB = f"{HEADER_PREFIX}web"

# Typing fires a yt-dlp subprocess, so the auto-fallback waits for the typing
# to stop and only runs when the catalog itself had nothing to offer.
WEB_DEBOUNCE = 0.6
WEB_MIN_QUERY = 3


def _fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "?"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _fmt_views(n: int) -> str:
    for unit, size in (("M", 1_000_000), ("K", 1_000)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(n)


class CandidatePicker(ModalScreen[Candidate | None]):
    """Shown when ranking cannot confidently choose. Esc skips the track."""

    BINDINGS = [Binding("escape", "skip", "Skip this track")]

    def __init__(self, job: DownloadJob) -> None:
        super().__init__()
        self.job = job

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(
                f"[b]{self.job.track.title}[/b] — {self.job.track.artist}\n"
                f"[dim]{self.job.message or 'pick the right upload'}[/dim]",
                id="picker-title",
            )
            options = [
                Option(self._describe(s), id=str(i))
                for i, s in enumerate(self.job.candidates)
            ]
            yield OptionList(*options, id="picker-list")
            yield Static("[dim]enter = pick   esc = skip this track[/dim]")

    @staticmethod
    def _describe(s: ScoredCandidate) -> str:
        c = s.candidate
        head = f"[b]{c.title}[/b]"
        meta = f"[dim]{c.channel or c.uploader} · {_fmt_duration(c.duration)} · {c.view_count:,} views · score {s.score:.0f}[/dim]"
        return f"{head}\n{meta}\n[dim]{s.explain()}[/dim]"

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = int(event.option.id or 0)
        self.dismiss(self.job.candidates[index].candidate)

    def action_skip(self) -> None:
        self.dismiss(None)


class YtMp3App(App[None]):
    """Search box on top, results table below, live status per track."""

    CSS = """
    Screen { layout: vertical; }
    #search { dock: top; }
    #status { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    DataTable { height: 1fr; }
    #picker {
        width: 90%; height: 80%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #picker-list { height: 1fr; }
    """

    # priority=True: the search Input binds ctrl+a to its own text cursor, so
    # without it these never reach the app while the user is typing.
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+a", "select_all", "Select all shown", priority=True),
        Binding("ctrl+d", "clear_selection", "Clear selection", priority=True),
        Binding("ctrl+r", "enqueue", "Download selected", priority=True),
        Binding("ctrl+f", "web_search", "Search the web", priority=True),
    ]

    def __init__(self, opts: Options) -> None:
        super().__init__()
        self.opts = opts
        self.catalog = resolve_catalog(opts.music_dir)
        on_disk = scan_existing(opts.music_dir, self.catalog)
        self.jobs: dict[str, DownloadJob] = {
            j.track.id: j for j in jobs_for(self.catalog, on_disk)
        }
        self.selected: set[str] = set()
        self.visible_ids: list[str] = []
        self._updates: queue.Queue[str] = queue.Queue()
        # Web results belong to exactly one query; anything typed after that
        # invalidates them, which is also how a late worker knows to shut up.
        self.web_query: str = ""
        self.web_status: str = ""
        self.web_tracks: list[Track] = []
        self._web_timer = None
        # Textual's `exclusive` worker stops awaiting the old search but cannot
        # reach into its yt-dlp processes; this token can, and a search that is
        # a dozen subprocesses wide has to be killed, not just ignored.
        self._web_cancel: probe.Cancel | None = None

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Input(placeholder="search title / artist / album…", id="search")
        yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column(" ", key="sel", width=3)
        table.add_column("St", key="state", width=3)
        table.add_column("Title", key="title", width=42)
        table.add_column("Artist", key="artist", width=32)
        table.add_column("Album", key="album", width=28)
        table.add_column("Note", key="note")
        self.refresh_rows("")
        self.query_one(Input).focus()
        # Worker threads report progress through a queue; the UI drains it here
        # so no widget is ever touched from off the event loop.
        self.set_interval(0.2, self._drain_updates)

    # -- rows --------------------------------------------------------------

    def refresh_rows(self, query: str) -> None:
        table = self.query_one(DataTable)
        table.clear()
        tracks = search_tracks(self.catalog, query)
        self.visible_ids = [t.id for t in tracks]

        # Web rows only belong to the query that produced them. A track that
        # has since been added to the catalog is already rendered above, and
        # re-adding it here would collide on the row key.
        show_web = bool(self.web_query) and self.web_query == query.strip()
        catalog_ids = set(self.visible_ids)
        web = [t for t in self.web_tracks if t.id not in catalog_ids] if show_web else []

        if show_web:
            table.add_row(*self._header(f"catalog ({len(tracks)})"), key=HDR_CATALOG)
        for track in tracks:
            table.add_row(*self._row(track.id), key=track.id)
        if show_web:
            table.add_row(*self._header(f"web ⌕ {self.web_status}"), key=HDR_WEB)
            for track in web:
                table.add_row(*self._row(track.id), key=track.id)
            self.visible_ids += [t.id for t in web]

        self.update_status()

    @staticmethod
    def _header(label: str) -> tuple[str, str, str, str, str, str]:
        return ("", "", f"[b $accent]── {label}[/]", "", "", "")

    def _row(self, track_id: str) -> tuple[str, str, str, str, str, str]:
        job = self.jobs[track_id]
        t = job.track
        mark = "[b]▸[/b]" if track_id in self.selected else " "
        # A pending catalog row has nothing worth saying; a pending web row
        # carries the channel/duration/views/score that justify its position.
        note = job.message
        if job.state is JobState.PENDING and not discover.is_web_track(t):
            note = ""
        if job.state is JobState.DOWNLOADING and job.percent:
            note = f"{job.percent:.0f}%"
        return (mark, job.glyph, t.title, t.artist, t.album, note)

    def _refresh_row(self, track_id: str) -> None:
        if track_id not in self.visible_ids:
            return
        table = self.query_one(DataTable)
        values = self._row(track_id)
        for column, value in zip(("sel", "state", "title", "artist", "album", "note"), values):
            table.update_cell(track_id, column, value)

    def update_status(self) -> None:
        done = sum(1 for j in self.jobs.values() if j.state is JobState.DONE)
        disk = sum(1 for j in self.jobs.values() if j.state is JobState.ON_DISK)
        failed = sum(1 for j in self.jobs.values() if j.state is JobState.FAILED)
        web = sum(1 for i in self.visible_ids if i.startswith(discover.WEB_ID_PREFIX))
        shown = f"{len(self.visible_ids) - web}/{len(self.catalog)} shown"
        if web:
            shown += f" + {web} web"
        self.query_one("#status", Static).update(
            f"{shown} · "
            f"{len(self.selected)} selected · {done} downloaded · {disk} on disk · {failed} failed"
            f"   [dim]ctrl+f = search the web → {self.opts.music_dir}[/dim]"
        )

    def _drain_updates(self) -> None:
        touched = False
        while True:
            try:
                track_id = self._updates.get_nowait()
            except queue.Empty:
                break
            self._refresh_row(track_id)
            touched = True
        if touched:
            self.update_status()

    # -- input -------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        if self.web_query and self.web_query != query:
            self._forget_web()
        self.refresh_rows(event.value)
        self._schedule_web(query)

    def _forget_web(self) -> None:
        """Drop the web section. The jobs stay: a track already downloaded
        should still read `♪` if the same search is run again."""
        self.selected.difference_update({t.id for t in self.web_tracks})
        self.web_query = ""
        self.web_status = ""
        self.web_tracks = []

    def _schedule_web(self, query: str) -> None:
        """Auto-fallback: search the web once typing settles, but only when the
        catalog had no answer at all. `ctrl+f` is the way to force it."""
        if self._web_timer is not None:
            self._web_timer.stop()
            self._web_timer = None
        if not self.opts.web_fallback or len(query) < WEB_MIN_QUERY:
            return
        if search_tracks(self.catalog, query):
            return
        self._web_timer = self.set_timer(WEB_DEBOUNCE, lambda: self.search_web(query))

    def on_input_submitted(self) -> None:
        self.query_one(DataTable).focus()

    def on_key(self, event) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            if event.key == "down":
                self.query_one(DataTable).focus()
                event.stop()
            return
        if isinstance(focused, DataTable):
            if event.key == "space":
                self.action_toggle()
                event.stop()
            elif event.key == "escape":
                self.query_one(Input).focus()
                event.stop()
            elif event.key == "enter":
                self.action_enqueue()
                event.stop()

    # -- actions -----------------------------------------------------------

    def _cursor_track(self) -> str | None:
        table = self.query_one(DataTable)
        if not self.visible_ids or table.cursor_row < 0:
            return None
        try:
            key = str(table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value)
        except Exception:
            return None
        # Section separators are rows too, but they are not tracks.
        return None if key.startswith(HEADER_PREFIX) else key

    def action_toggle(self) -> None:
        track_id = self._cursor_track()
        if track_id is None:
            return
        self.selected.symmetric_difference_update({track_id})
        self._refresh_row(track_id)
        self.update_status()

    def action_select_all(self) -> None:
        self.selected.update(self.visible_ids)
        for track_id in self.visible_ids:
            self._refresh_row(track_id)
        self.update_status()

    def action_clear_selection(self) -> None:
        cleared = list(self.selected)
        self.selected.clear()
        for track_id in cleared:
            self._refresh_row(track_id)
        self.update_status()

    def action_enqueue(self) -> None:
        chosen = list(self.selected) or ([t] if (t := self._cursor_track()) else [])
        pending = [
            self.jobs[i]
            for i in chosen
            if self.jobs[i].state
            in (JobState.PENDING, JobState.FAILED, JobState.AMBIGUOUS, JobState.ON_DISK)
        ]
        if not pending:
            return
        self.selected.clear()
        for job in pending:
            job.state = JobState.SEARCHING
            job.message = "queued"
            self._refresh_row(job.track.id)
        self.update_status()
        self.download(pending)

    def action_web_search(self) -> None:
        query = self.query_one(Input).value.strip()
        if len(query) >= WEB_MIN_QUERY:
            self.search_web(query)

    # -- work --------------------------------------------------------------

    @work(exclusive=True, group="websearch")
    async def search_web(self, query: str) -> None:
        """Free-text YouTube search, rendered under the catalog matches.

        `exclusive` means a newer query cancels this one, but a cancellation
        can land after yt-dlp has already returned, so every write back to the
        UI re-checks that `query` is still the one on screen.

        Cancelling also kills the search's yt-dlp processes. Leaving them to
        finish would have the abandoned query competing for bandwidth with the
        one the user is actually waiting on.
        """
        if self._web_timer is not None:
            self._web_timer.stop()
            self._web_timer = None

        if self._web_cancel is not None:
            self._web_cancel.cancel()
        cancel = self._web_cancel = probe.Cancel()

        self.web_query = query
        self.web_tracks = []
        self.web_status = "searching…"
        self._render()

        try:
            ranked = await asyncio.to_thread(
                discover.web_search,
                query,
                self.opts.search_limit,
                self.opts.cookies_from_browser,
                self.opts.cookies_file,
                cancel,
            )
        except probe.Cancelled:
            # Superseded by a newer query, which owns the header from here.
            return
        except Exception as exc:  # noqa: BLE001 - shown in the header, not swallowed
            if self.web_query == query:
                self.web_status = f"failed — {type(exc).__name__}: {exc}"
                self._render()
            return

        if self.web_query != query:
            return

        self.web_tracks = [self._track_for(s) for s in ranked]
        self.web_status = f"({len(self.web_tracks)})" if self.web_tracks else "(no results)"
        self._render()

    def _track_for(self, scored: ScoredCandidate) -> Track:
        """Give a search result a job, pre-resolved to the candidate it came from.

        `chosen` being set is what makes the download skip the search-and-rank
        step: the user already picked this exact upload off the list.
        """
        track = discover.track_from_candidate(scored.candidate)
        job = self.jobs.get(track.id)
        if job is None:
            job = DownloadJob(track=track)
            self.jobs[track.id] = job
            if (Path(self.opts.music_dir) / f"{track.filename_stem}.mp3").exists():
                job.state = JobState.ON_DISK
        job.chosen = scored.candidate
        job.candidates = [scored]
        if job.state is JobState.PENDING:
            c = scored.candidate
            job.message = (
                f"{c.channel or c.uploader} · {_fmt_duration(c.duration)} · "
                f"{_fmt_views(c.view_count)} views · score {scored.score:.0f}"
            )
        return job.track

    def _render(self) -> None:
        """Redraw against whatever is in the search box right now."""
        self.refresh_rows(self.query_one(Input).value)

    @work(exclusive=False)
    async def download(self, jobs: list[DownloadJob]) -> None:
        def progress(job: DownloadJob) -> None:
            self._updates.put(job.track.id)

        async def chooser(job: DownloadJob):
            if not job.candidates:
                return None
            return await self.push_screen_wait(CandidatePicker(job))

        await run_all(jobs, self.opts, progress, chooser)
        for job in jobs:
            self._updates.put(job.track.id)

        # A web result that made it to disk is no longer a search result --
        # it is part of the library, and belongs in the catalog file.
        keep = [
            j.track
            for j in jobs
            if discover.is_web_track(j.track)
            and j.state in (JobState.DONE, JobState.ON_DISK)
        ]
        if keep:
            self.catalog = await asyncio.to_thread(
                add_tracks, self.opts.music_dir, self.catalog, keep
            )
            self._render()


def run(music_dir: Path, **kwargs) -> None:
    YtMp3App(Options(music_dir=Path(music_dir), **kwargs)).run()
