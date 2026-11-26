"""Textual TUI: type to search the catalog or the web, select tracks, watch them download."""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button, DataTable, Footer, Input, OptionList, ProgressBar, Static, TabbedContent, TabPane,
)
from textual.widgets.option_list import Option

from . import discover, probe
from .catalog import add_tracks, resolve_catalog, scan_existing
from .models import Candidate, DownloadJob, JobState, ScoredCandidate, Track
from .pipeline import Options, jobs_for, run_all
from .playlist import PlaylistLink, playlist_jobs
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

SONGFETCH_THEME = Theme(
    name="songfetch", primary="#5be0be", secondary="#7299e7", accent="#5be0be",
    foreground="#e1eaf6", background="#101822", surface="#172330", panel="#223447",
    success="#5be0be", warning="#ffd18a", error="#ff929e",
    variables={"button-color-foreground": "#101822", "footer-key-foreground": "#5be0be"},
)

TABLE_CSS = """
DataTable { height: 1fr; background: $surface; }
DataTable > .datatable--header { background: $panel; color: $text; text-style: bold; }
DataTable > .datatable--cursor { background: $primary 25%; color: $text; }
.app-banner { height: 1; padding: 0 2; background: $panel; color: $accent; text-style: bold; }
.help-line { height: auto; max-height: 2; color: $text-muted; }
.status-strip { height: auto; max-height: 2; background: $panel; padding: 0 1; }
.row-detail { height: auto; min-height: 1; max-height: 3; padding: 0 1; }
Footer { background: $surface; }
"""

STATE_LABELS = {
    JobState.PENDING: ("Ready", "dim"),
    JobState.ON_DISK: ("On disk", "cyan"),
    JobState.SEARCHING: ("Finding", "blue"),
    JobState.AMBIGUOUS: ("Pick", "yellow"),
    JobState.DOWNLOADING: ("Downloading", "blue"),
    JobState.TRIMMING: ("Trimming", "blue"),
    JobState.ENCODING: ("Encoding", "blue"),
    JobState.DONE: ("Saved", "green"),
    JobState.FAILED: ("Failed", "bold red"),
}


def _state_label(state: JobState) -> Text:
    label, style = STATE_LABELS[state]
    return Text(label, style=style)


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
    DEFAULT_CSS = """
    CandidatePicker { align: center middle; background: $background 75%; }
    #picker { width: 90%; max-width: 100; height: 80%; border: round $accent;
              background: $surface; padding: 1 2; }
    #picker-title { height: auto; padding-bottom: 1; }
    #picker-list { height: 1fr; }
    """

    def __init__(
        self,
        candidates: list[ScoredCandidate],
        title: str = "",
        subtitle: str = "",
    ) -> None:
        super().__init__()
        self.candidates = candidates
        self.title_text = title
        self.subtitle = subtitle or "pick the right upload"

    @classmethod
    def for_job(cls, job: DownloadJob) -> CandidatePicker:
        return cls(
            job.candidates,
            f"{job.track.title} — {job.track.artist}",
            job.message,
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            heading = Text("Choose an upload\n", style="bold")
            heading.append(f"{self.title_text}\n")
            heading.append(self.subtitle, style="dim")
            yield Static(heading, id="picker-title")
            options = [
                Option(self._describe(s), id=str(i))
                for i, s in enumerate(self.candidates)
            ]
            yield OptionList(*options, id="picker-list")
            yield Static("↑/↓  Move   ·   Enter  Use upload   ·   Esc  Skip", classes="help-line")

    @staticmethod
    def _describe(s: ScoredCandidate) -> Text:
        c = s.candidate
        text = Text(c.title, style="bold")
        text.append(
            f"\n{c.channel or c.uploader} · {_fmt_duration(c.duration)} · "
            f"{c.view_count:,} views · score {s.score:.0f}\n{s.explain()}",
            style="dim",
        )
        return text

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = int(event.option.id or 0)
        self.dismiss(self.candidates[index].candidate)

    def action_skip(self) -> None:
        self.dismiss(None)


class YtMp3App(App[None]):
    """Search box on top, results table below, live status per track."""

    TITLE = "songfetch"
    CSS = TABLE_CSS + """
    #modes { height: 1fr; }
    #modes ContentTabs { background: $surface; }
    #modes Tab { padding: 0 2; }
    #modes Tab.-active { text-style: bold; color: $accent; }
    #modes DataTable { overflow-x: hidden; }
    TabPane { height: 1fr; padding: 0 1; }
    .toolbar { height: 3; }
    .toolbar Input { width: 1fr; min-width: 8; }
    .toolbar Button { margin-left: 1; min-width: 10; }
    .empty-table { height: 1fr; background: $surface; color: $text-muted;
                   content-align: center middle; padding: 1 2; }
    #playlist-meter { height: 1; margin: 0 1; }
    #playlist-meter Bar { width: 1fr; }
    """

    # priority=True: the search Input binds ctrl+a to its own text cursor, so
    # without it these never reach the app while the user is typing.
    BINDINGS = [
        Binding("ctrl+t", "switch_tab", "Switch tab", priority=True),
        Binding("f1", "show_mode('songs')", show=False, priority=True),
        Binding("f2", "show_mode('playlists')", show=False, priority=True),
        Binding("ctrl+r", "enqueue", "Download", priority=True),
        Binding("ctrl+f", "web_search", "Web search", priority=True),
        Binding("ctrl+a", "select_all", "Select all shown", show=False, priority=True),
        Binding("ctrl+d", "clear_selection", "Clear selection", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, opts: Options) -> None:
        super().__init__()
        self.register_theme(SONGFETCH_THEME)
        self.theme = SONGFETCH_THEME.name
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
        self._playlist_running = False
        self._playlist_jobs: list[DownloadJob] | None = None
        self._playlist_unavailable = 0
        self._playlist_error = ""
        self._playlist_rows: dict[str, DownloadJob] = {}
        self._playlist_updates: queue.Queue[str] = queue.Queue()

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("♫  songfetch", classes="app-banner")
        with TabbedContent(initial="songs", id="modes"):
            with TabPane("Songs", id="songs"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Search title, artist, or album", id="search")
                    yield Button("Search web", id="search-web")
                    yield Button("Download", id="download-songs", variant="primary")
                yield Static("↓ Results · Space Select · Esc Search",
                             classes="help-line")
                yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
                yield Static("", id="songs-empty", classes="empty-table", markup=False)
                yield Static("", id="song-detail", classes="row-detail", markup=False)
                yield Static("", id="status", classes="status-strip", markup=False)
            with TabPane("Playlists", id="playlists"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="YouTube Music playlist URL", id="playlist-url")
                    yield Button("Download", id="download-playlist", variant="primary")
                playlist_help = "Enter Download all · ↓ Tracks · Esc URL"
                yield Static(playlist_help, classes="help-line")
                yield Static("", id="playlist-destination", classes="help-line", markup=False)
                yield ProgressBar(show_eta=False, id="playlist-meter")
                yield DataTable(id="playlist-results", cursor_type="row", zebra_stripes=True)
                yield Static(
                    "Paste a playlist link from YouTube Music or YouTube.",
                    id="playlist-empty",
                    classes="empty-table",
                    markup=False,
                )
                yield Static("", id="playlist-detail", classes="row-detail", markup=False)
                yield Static("", id="playlist-status", classes="status-strip", markup=False)
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_column(" ", key="sel", width=1)
        table.add_column("Status", key="state", width=11)
        table.add_column("Title", key="title", width=31)
        table.add_column("Artist", key="artist", width=22)
        playlist = self.query_one("#playlist-results", DataTable)
        playlist.add_column("#", key="position", width=4)
        playlist.add_column("Status", key="state", width=11)
        playlist.add_column("Title", key="title", width=30)
        playlist.add_column("Artist", key="artist", width=21)
        self.refresh_rows("")
        self._update_playlist_status()
        self.query_one("#search", Input).focus()
        # Worker threads report progress through a queue; the UI drains it here
        # so no widget is ever touched from off the event loop.
        self.set_interval(0.2, self._drain_updates)

    # -- rows --------------------------------------------------------------

    def refresh_rows(self, query: str) -> None:
        table = self.query_one("#results", DataTable)
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

        empty = self.query_one("#songs-empty", Static)
        empty.display = not table.row_count
        table.display = bool(table.row_count)
        if empty.display:
            message = (
                "Press Enter to open Playlists and download this link."
                if PlaylistLink.parse(query) is not None
                else "No matching songs. Try another query or select Search web."
            )
            empty.update(message)
        self.update_status()

    @staticmethod
    def _header(label: str) -> tuple[str, str, Text, str]:
        return ("", "", Text(label, style="bold cyan", no_wrap=True, overflow="ellipsis"), "")

    def _row(self, track_id: str) -> tuple[Text, Text, Text, Text]:
        job = self.jobs[track_id]
        t = job.track
        mark = Text("✓", style="bold green") if track_id in self.selected else Text(" ")
        return mark, _state_label(job.state), Text(t.title), Text(t.artist)

    def _refresh_row(self, track_id: str) -> None:
        if track_id not in self.visible_ids:
            return
        table = self.query_one("#results", DataTable)
        values = self._row(track_id)
        for column, value in zip(("sel", "state", "title", "artist"), values):
            table.update_cell(track_id, column, value)

    def update_status(self) -> None:
        status = self.query_one("#status", Static)
        done = sum(1 for j in self.jobs.values() if j.state is JobState.DONE)
        disk = sum(1 for j in self.jobs.values() if j.state is JobState.ON_DISK)
        failed = sum(1 for j in self.jobs.values() if j.state is JobState.FAILED)
        if not self.visible_ids and self.web_query:
            status.update(f"Web search {self.web_status}")
        else:
            status.update(
                f"{len(self.visible_ids)} shown · {len(self.selected)} selected · "
                f"{done} saved · {disk} on disk · {failed} failed"
            )
        self._show_song_detail()

    def _show_song_detail(self) -> None:
        job = self.jobs.get(self._cursor_track())
        parts = (
            (part for part in (job.track.title, job.track.artist, job.track.album, job.message) if part)
            if job else ()
        )
        self.query_one("#song-detail", Static).update(" · ".join(parts))

    def _playlist_progress(self) -> str:
        if self._playlist_error:
            return self._playlist_error
        jobs = self._playlist_jobs
        if jobs is None:
            return "Loading tracks…" if self._playlist_running else ""
        done = sum(j.state is JobState.DONE for j in jobs)
        disk = sum(j.state is JobState.ON_DISK for j in jobs)
        failed = sum(j.state is JobState.FAILED for j in jobs)
        total = len(jobs) + self._playlist_unavailable
        return (
            f"Playlist {done + disk + failed + self._playlist_unavailable}/{total}"
            f" · {done} downloaded · {disk} on disk · {failed} failed"
            f" · {self._playlist_unavailable} unavailable"
        )

    def _update_playlist_status(self) -> None:
        self.query_one("#playlist-status", Static).update(self._playlist_progress())
        meter = self.query_one("#playlist-meter", ProgressBar)
        empty = self.query_one("#playlist-empty", Static)
        empty.display = self._playlist_jobs is None
        self.query_one("#playlist-results", DataTable).display = not empty.display
        meter.display = self._playlist_running or self._playlist_jobs is not None
        if self._playlist_jobs is None:
            meter.update(total=None, progress=0)
            message = "Paste a playlist link from YouTube Music or YouTube."
            if self._playlist_running:
                message = ""
            elif self._playlist_error:
                message = "Check the URL and your access to the playlist, then try again."
            empty.update(message)
            return
        processed = sum(
            j.state in (JobState.DONE, JobState.ON_DISK, JobState.FAILED)
            for j in self._playlist_jobs
        ) + self._playlist_unavailable
        meter.update(total=len(self._playlist_jobs) + self._playlist_unavailable, progress=processed)

    def _show_playlist_detail(self) -> None:
        table = self.query_one("#playlist-results", DataTable)
        if table.row_count:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            job = self._playlist_rows.get(key)
            detail = " · ".join(
                part for part in (job.track.title, job.track.artist, job.message) if part
            ) if job else "YouTube did not return a playable track at this position."
        else:
            detail = ""
        self.query_one("#playlist-detail", Static).update(detail)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "results":
            self._show_song_detail()
        elif event.data_table.id == "playlist-results":
            self._show_playlist_detail()

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
        self._drain_playlist_updates()

    def _drain_playlist_updates(self) -> None:
        keys = set()
        while True:
            try:
                keys.add(self._playlist_updates.get_nowait())
            except queue.Empty:
                break
        if keys:
            table = self.query_one("#playlist-results", DataTable)
            for key in keys:
                table.update_cell(key, "state", _state_label(self._playlist_rows[key].state))
            self._show_playlist_detail()
            self._update_playlist_status()

    # -- input -------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search":
            return
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
        if PlaylistLink.parse(query) is not None:
            if self._web_cancel is not None:
                self._web_cancel.cancel()
            return
        if not self.opts.web_fallback or len(query) < WEB_MIN_QUERY:
            return
        if search_tracks(self.catalog, query):
            return
        self._web_timer = self.set_timer(WEB_DEBOUNCE, lambda: self.search_web(query))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "playlist-url":
            self._start_playlist()
        elif PlaylistLink.parse(event.value) is not None:
            if not self._playlist_running:
                self.query_one("#playlist-url", Input).value = event.value
            self.action_show_mode("playlists")
            self._start_playlist()
        else:
            self.query_one("#results", DataTable).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "search-web":
            self.action_web_search()
        elif event.button.id == "download-songs":
            self.action_enqueue()
        elif event.button.id == "download-playlist":
            self._start_playlist()

    def action_switch_tab(self) -> None:
        tabs = self.query_one("#modes", TabbedContent)
        self.action_show_mode("playlists" if tabs.active == "songs" else "songs")

    def action_show_mode(self, mode: str) -> None:
        tabs = self.query_one("#modes", TabbedContent)
        if tabs.active == mode:
            tabs.get_pane(mode).query_one(Input).focus()
        else:
            tabs.active = mode

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        event.pane.query_one(Input).focus()

    def on_key(self, event) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            if event.key == "down":
                table = "#results" if focused.id == "search" else "#playlist-results"
                self.query_one(table, DataTable).focus()
                event.stop()
            return
        if isinstance(focused, DataTable):
            if event.key == "escape":
                field = "#search" if focused.id == "results" else "#playlist-url"
                self.query_one(field, Input).focus()
                event.stop()
            elif focused.id == "results" and event.key == "space":
                self.action_toggle()
                event.stop()
            elif focused.id == "results" and event.key == "enter":
                self.action_enqueue()
                event.stop()

    # -- actions -----------------------------------------------------------

    def _cursor_track(self) -> str | None:
        table = self.query_one("#results", DataTable)
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
        if self.query_one("#modes", TabbedContent).active == "playlists":
            self._start_playlist()
            return
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
        query = self.query_one("#search", Input).value.strip()
        if len(query) >= WEB_MIN_QUERY and PlaylistLink.parse(query) is None:
            self.search_web(query)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.screen, CandidatePicker) and action in {
            "switch_tab", "show_mode", "select_all", "clear_selection", "web_search", "enqueue",
        }:
            return False
        if action in {"select_all", "clear_selection", "web_search"}:
            return self.query_one("#modes", TabbedContent).active == "songs"
        return super().check_action(action, parameters)

    def _start_playlist(self) -> None:
        if self._playlist_running:
            return
        link = PlaylistLink.parse(self.query_one("#playlist-url", Input).value)
        if link is None:
            self.query_one("#playlist-status", Static).update(
                "Paste a YouTube Music or YouTube URL that contains a playlist ID."
            )
            self.query_one("#playlist-url", Input).focus()
            return

        self._playlist_running = True
        self._playlist_jobs = None
        self._playlist_unavailable = 0
        self._playlist_error = ""
        self._playlist_rows.clear()
        self.query_one("#playlist-results", DataTable).clear()
        self.query_one("#playlist-detail", Static).update("")
        self.query_one("#download-playlist", Button).disabled = True
        self.query_one("#playlist-destination", Static).update(
            Text(f"Save to {link.directory(self.opts.music_dir)}", no_wrap=True, overflow="ellipsis")
        )
        self._update_playlist_status()
        self.download_playlist(link)

    # -- work --------------------------------------------------------------

    @work(group="playlist")
    async def download_playlist(self, link: PlaylistLink) -> None:
        try:
            jobs, unavailable = await asyncio.to_thread(playlist_jobs, link, self.opts)
            self._playlist_jobs = jobs
            self._playlist_unavailable = len(unavailable)
            self._populate_playlist(jobs, unavailable)
            self._update_playlist_status()
            keys = {id(job): key for key, job in self._playlist_rows.items()}

            def progress(job: DownloadJob) -> None:
                self._playlist_updates.put(keys[id(job)])

            await run_all(jobs, self.opts, progress)
            for key in keys.values():
                self._playlist_updates.put(key)
            self._drain_playlist_updates()
        except probe.ProbeError as exc:
            self._playlist_error = f"Could not load playlist · {exc}"
        finally:
            self._playlist_running = False
            self.query_one("#download-playlist", Button).disabled = False
            self._update_playlist_status()
        if self.query_one("#modes", TabbedContent).active != "playlists":
            self.notify(escape(self._playlist_progress()))

    def _populate_playlist(self, jobs: list[DownloadJob], unavailable: list[int]) -> None:
        table = self.query_one("#playlist-results", DataTable)
        missing = set(unavailable)
        tracks = iter(jobs)
        for position in range(1, len(jobs) + len(missing) + 1):
            key = str(position)
            if position in missing:
                table.add_row(key, Text("Unavailable", style="yellow"), "—", "", key=key)
                continue
            job = next(tracks)
            self._playlist_rows[key] = job
            table.add_row(
                key, _state_label(job.state), Text(job.track.title), Text(job.track.artist), key=key
            )

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
        self.refresh_rows(self.query_one("#search", Input).value)

    @work(exclusive=False)
    async def download(self, jobs: list[DownloadJob]) -> None:
        def progress(job: DownloadJob) -> None:
            self._updates.put(job.track.id)

        async def chooser(job: DownloadJob):
            if not job.candidates:
                return None
            return await self.push_screen_wait(CandidatePicker.for_job(job))

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
