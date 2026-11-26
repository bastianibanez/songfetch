"""Review screen for a bulk run: see every match before anything downloads.

The searching is the cheap part of a bulk run; the expensive part is finding
out afterwards that six of the twenty tracks grabbed the wrong upload. So the
results land in a table first, ranking's confident picks pre-approved and its
doubtful ones not, and nothing touches the network again until `ctrl+r`.
"""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Static

from .app import SONGFETCH_THEME, TABLE_CSS, CandidatePicker, _fmt_duration, _fmt_views
from .bulk import SKIP, TAKE, BulkResult, bulk_search
from .catalog import add_tracks, resolve_catalog
from .models import DownloadJob, JobState
from .pipeline import Options, run_all

# Row glyphs. Deliberately the same vocabulary as the main TUI's status column.
GLYPH_SEARCHING = "⋯"
GLYPH_TAKE = "✓"
GLYPH_SKIP = "✗"
GLYPH_ASK = "?"


class BulkReviewApp(App[None]):
    """One row per query: the match, the score, and whether it is going to download."""

    TITLE = "songfetch · Wishlist"
    CSS = TABLE_CSS

    # Deliberately none of these are `priority`, unlike the main TUI's. Priority
    # bindings outrank the focused widget *and* any modal on top, so a priority
    # `enter` here would fire this screen's action instead of letting the
    # candidate picker accept the highlighted option -- it would stack a second
    # picker on the first. The main TUI needs priority because its search Input
    # eats every keystroke; this screen has no Input to lose keys to.
    BINDINGS = [
        Binding("space", "toggle", "Take / skip"),
        Binding("ctrl+a", "take_all", "Take all"),
        Binding("ctrl+d", "skip_all", "Skip all"),
        Binding("ctrl+r", "download", "Download taken"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, queries: list[str], opts: Options) -> None:
        super().__init__()
        self.register_theme(SONGFETCH_THEME)
        self.theme = SONGFETCH_THEME.name
        self.opts = opts
        self.queries = queries
        # One slot per query, filled in as searches land. Index is the row key,
        # so the table keeps the file's order however the searches complete.
        self.results: list[BulkResult | None] = [None] * len(queries)
        self.jobs: dict[str, DownloadJob] = {}
        self.downloading = False
        self.finished = False
        self._updates: queue.Queue[int] = queue.Queue()

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("♫  songfetch  /  Wishlist", classes="app-banner")
        yield Static("↑/↓  Move   ·   Space  Take or skip   ·   Enter  Choose an upload",
                     classes="help-line")
        yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
        yield Static("", id="wishlist-detail", classes="row-detail", markup=False)
        yield Static("", id="status", classes="status-strip")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column(" ", key="state", width=3)
        table.add_column("Query", key="query", width=25)
        table.add_column("Match", key="match", width=32)
        table.add_column("Note", key="note", width=20)
        for i, query in enumerate(self.queries):
            table.add_row(GLYPH_SEARCHING, query, "", "searching…", key=str(i))
        table.focus()
        self.update_status()
        # Searches report from worker threads; the table is only ever touched
        # from the event loop, so results arrive through a queue like the main
        # TUI's progress updates do.
        self.set_interval(0.1, self._drain_updates)
        self.search_all()

    # -- searching ---------------------------------------------------------

    @work(thread=True, exclusive=True, group="bulk")
    def search_all(self) -> None:
        """Blocking: runs off the event loop, one thread for the whole fan-out."""
        order = {q: i for i, q in enumerate(self.queries)}

        def landed(result: BulkResult) -> None:
            index = order.get(result.query)
            if index is None:  # pragma: no cover - queries are the only source
                return
            self.results[index] = result
            self._updates.put(index)

        bulk_search(
            self.queries,
            limit=self.opts.search_limit,
            cookies_from_browser=self.opts.cookies_from_browser,
            cookies_file=self.opts.cookies_file,
            progress=landed,
        )
        self._updates.put(-1)

    def _drain_updates(self) -> None:
        touched = False
        while True:
            try:
                index = self._updates.get_nowait()
            except queue.Empty:
                break
            touched = True
            if index < 0:
                self.finished = True
            else:
                self._refresh_row(index)
        if touched:
            self.update_status()

    # -- rows --------------------------------------------------------------

    def _refresh_row(self, index: int) -> None:
        table = self.query_one(DataTable)
        result = self.results[index]
        if result is None:
            return
        glyph, match, note = self._describe(result)
        table.update_cell(str(index), "state", glyph)
        table.update_cell(str(index), "match", match)
        table.update_cell(str(index), "note", note)
        if index == self._current():
            self._show_detail()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_detail()

    def _show_detail(self) -> None:
        index = self._current()
        if index is None:
            return
        row = self.query_one(DataTable).get_row_at(index)
        self.query_one("#wishlist-detail", Static).update(
            " · ".join(str(value) for value in row[1:] if value)
        )

    @staticmethod
    def _describe(result: BulkResult) -> tuple[str, str, str]:
        if result.error:
            return GLYPH_SKIP, "—", f"search failed: {result.error[:60]}"

        picked = result.selection
        if picked is None:
            return GLYPH_SKIP, "—", result.ambiguity or "no results"

        c = picked.candidate
        match = f"{c.channel or c.uploader} · {c.title}"
        note = (
            f"{_fmt_duration(c.duration)} · {_fmt_views(c.view_count)} views · "
            f"score {picked.score:.0f}"
        )
        if result.chosen is not None:
            note = f"picked · {note}"
        elif result.ambiguity:
            note = f"{result.ambiguity} · {len(result.alternatives)} alts · {note}"

        if result.wanted:
            glyph = GLYPH_TAKE
        elif result.decision == SKIP:
            glyph = GLYPH_SKIP
        else:
            glyph = GLYPH_ASK
        return glyph, match, note

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens its alternatives.

        Handled as the table's event rather than as an `enter` binding: the
        DataTable claims that key for row selection, so a binding would never
        see it.
        """
        self.action_alternatives()

    def _current(self) -> int | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self.results):
            return None
        return row

    # -- decisions ---------------------------------------------------------

    def action_toggle(self) -> None:
        index = self._current()
        if index is None:
            return
        result = self.results[index]
        if result is None or result.selection is None:
            return
        result.decision = SKIP if result.wanted else TAKE
        self._refresh_row(index)
        self.update_status()

    def action_take_all(self) -> None:
        self._decide_all(TAKE)

    def action_skip_all(self) -> None:
        self._decide_all(SKIP)

    def _decide_all(self, decision: str) -> None:
        for i, result in enumerate(self.results):
            if result is None or result.selection is None:
                continue
            result.decision = decision
            self._refresh_row(i)
        self.update_status()

    @work
    async def action_alternatives(self) -> None:
        """Open the ranked list for the row under the cursor."""
        index = self._current()
        if index is None:
            return
        result = self.results[index]
        if result is None or not result.ranked:
            return

        picked = await self.push_screen_wait(
            CandidatePicker(
                result.ranked,
                result.query,
                result.ambiguity or "pick a different upload",
            )
        )
        if picked is None:
            return
        result.chosen = next(
            (s for s in result.ranked if s.candidate.id == picked.id), result.chosen
        )
        # Choosing is an act of confirmation: nobody opens this list to pick
        # the upload they then leave out of the download.
        result.decision = TAKE
        self._refresh_row(index)
        self.update_status()

    # -- status ------------------------------------------------------------

    def update_status(self) -> None:
        taken = sum(1 for r in self.results if r is not None and r.wanted)
        done = sum(1 for r in self.results if r is not None)
        if self.downloading:
            finished = sum(
                1 for j in self.jobs.values() if j.state in (JobState.DONE, JobState.ON_DISK)
            )
            failed = sum(1 for j in self.jobs.values() if j.state is JobState.FAILED)
            text = f"downloading — {finished}/{len(self.jobs)} done, {failed} failed"
        elif not self.finished:
            text = f"searching — {done}/{len(self.queries)} back, {taken} taken"
        else:
            text = (
                f"{taken}/{len(self.queries)} taken   "
                f"[dim]space = take/skip · enter = alternatives · "
                f"ctrl+a/ctrl+d = all · ctrl+r = download[/dim]"
            )
        self.query_one("#status", Static).update(text)

    # -- downloading -------------------------------------------------------

    def action_download(self) -> None:
        if self.downloading:
            return
        wanted = [r for r in self.results if r is not None and r.wanted]
        if not wanted:
            return
        self.downloading = True
        self.download(wanted)

    @work(exclusive=True, group="download")
    async def download(self, wanted: list[BulkResult]) -> None:
        jobs: list[DownloadJob] = []
        rows: dict[str, int] = {}
        for index, result in enumerate(self.results):
            if result is None or result not in wanted:
                continue
            track = result.track()
            picked = result.selection
            if track is None or picked is None:  # pragma: no cover - `wanted` implies both
                continue
            job = DownloadJob(track=track, chosen=picked.candidate, candidates=[picked])
            if (Path(self.opts.music_dir) / f"{track.filename_stem}.mp3").exists():
                job.state = JobState.ON_DISK
            self.jobs[track.id] = job
            rows[track.id] = index
            jobs.append(job)

        def progress(job: DownloadJob) -> None:
            index = rows.get(job.track.id)
            if index is not None:
                self._updates.put(index)

        self.set_interval(0.2, self._drain_download_updates)
        await run_all(jobs, self.opts, progress)

        # A bulk result that made it to disk is part of the library now, so
        # it belongs in the catalog exactly like a web-search result does.
        kept = [j.track for j in jobs if j.state in (JobState.DONE, JobState.ON_DISK)]
        if kept:
            catalog = await asyncio.to_thread(resolve_catalog, self.opts.music_dir)
            await asyncio.to_thread(add_tracks, self.opts.music_dir, catalog, kept)

        self.downloading = False
        self._render_jobs(rows)
        self.update_status()

    def _drain_download_updates(self) -> None:
        if not self.downloading:
            return
        self.update_status()

    def _render_jobs(self, rows: dict[str, int]) -> None:
        table = self.query_one(DataTable)
        for track_id, index in rows.items():
            job = self.jobs.get(track_id)
            if job is None:
                continue
            table.update_cell(str(index), "state", job.glyph)
            if job.message:
                table.update_cell(str(index), "note", job.message)
        self._show_detail()


def review(queries: list[str], opts: Options) -> None:
    """Run the review screen. Blocks until the user quits."""
    BulkReviewApp(queries, opts).run()
