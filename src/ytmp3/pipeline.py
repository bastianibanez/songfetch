"""Orchestration: search -> rank -> fetch -> trim -> encode, N tracks at a time.

The blocking work (yt-dlp, ffmpeg) runs in worker threads so the Textual UI
stays responsive; the decision-making it calls into is all pure.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import encode, probe
from .models import Candidate, DownloadJob, JobState, Track
from .ranking import Ambiguous, pick, rank

# How many tracks are in flight at once. Beyond ~3 YouTube starts throttling
# and the failure modes get harder to read.
DEFAULT_CONCURRENCY = 3

ProgressFn = Callable[[DownloadJob], None]
# Called when the picker cannot decide. Returning None skips the track.
ChooserFn = Callable[[DownloadJob], "asyncio.Future[Candidate | None]"]


@dataclass(frozen=True, slots=True)
class Options:
    music_dir: Path
    cbr320: bool = False
    force_trim: bool = False
    keep_source: bool = False
    search_limit: int = 6
    concurrency: int = DEFAULT_CONCURRENCY
    overwrite: bool = False
    # TUI only: run a web search automatically when the catalog has no match.
    # Off means the web section only appears on ctrl+f.
    web_fallback: bool = True
    # YouTube answers anonymous downloads with a bot check more and more often.
    # e.g. "chrome", "safari", "firefox" -- see `yt-dlp --cookies-from-browser`.
    cookies_from_browser: str = ""
    cookies_file: str = ""


def _noop(_: DownloadJob) -> None:
    return None


def resolve_candidate(job: DownloadJob, opts: Options) -> Candidate | None:
    """Search and pick. Returns None when a human needs to choose.

    Populates `job.candidates` either way so the UI can offer the list.
    """
    candidates = probe.find_candidates(
        job.track.query,
        limit=opts.search_limit,
        cookies_from_browser=opts.cookies_from_browser,
        cookies_file=opts.cookies_file,
    )
    job.candidates = rank(candidates, job.track.query, job.track.duration)
    try:
        return pick(candidates, job.track.query, job.track.duration).candidate
    except Ambiguous:
        return None


def download_one(job: DownloadJob, opts: Options, progress: ProgressFn = _noop) -> Path:
    """Blocking. Fetch the chosen candidate and produce the final MP3."""
    if job.chosen is None:
        raise ValueError("download_one requires job.chosen")

    dest = Path(opts.music_dir) / f"{job.track.filename_stem}.mp3"
    if dest.exists() and not opts.overwrite:
        job.state = JobState.ON_DISK
        job.output_path = str(dest)
        progress(job)
        return dest

    with tempfile.TemporaryDirectory(prefix="ytmp3-") as tmp:
        job.state = JobState.DOWNLOADING
        progress(job)
        fetched = encode.fetch_audio(
            job.chosen.url,
            Path(tmp),
            cookies_from_browser=opts.cookies_from_browser,
            cookies_file=opts.cookies_file,
        )

        job.state = JobState.TRIMMING
        progress(job)
        decision = encode.decide_trim(fetched, force=opts.force_trim)
        job.trim_offset = decision.offset
        job.message = decision.reason
        progress(job)

        job.state = JobState.ENCODING
        progress(job)
        encode.encode_mp3(
            fetched, dest, job.track, offset=decision.offset, cbr320=opts.cbr320
        )

        if opts.keep_source:
            kept = Path(opts.music_dir) / f"{job.track.filename_stem}{fetched.audio.suffix}"
            kept.write_bytes(fetched.audio.read_bytes())

    job.state = JobState.DONE
    job.output_path = str(dest)
    progress(job)
    return dest


async def run_job(
    job: DownloadJob,
    opts: Options,
    progress: ProgressFn = _noop,
    chooser: ChooserFn | None = None,
) -> DownloadJob:
    """One track, end to end. Never raises: failures land in `job.state`."""
    try:
        if job.chosen is None:
            job.state = JobState.SEARCHING
            progress(job)
            job.chosen = await asyncio.to_thread(resolve_candidate, job, opts)

        if job.chosen is None:
            job.state = JobState.AMBIGUOUS
            job.message = "needs a manual pick"
            progress(job)
            if chooser is None:
                return job
            picked = await chooser(job)
            if picked is None:
                job.message = "skipped"
                return job
            job.chosen = picked

        await asyncio.to_thread(download_one, job, opts, progress)
    except Exception as exc:  # noqa: BLE001 - surfaced in the UI, not swallowed
        job.state = JobState.FAILED
        job.message = f"{type(exc).__name__}: {exc}"
        progress(job)
    return job


async def run_all(
    jobs: list[DownloadJob],
    opts: Options,
    progress: ProgressFn = _noop,
    chooser: ChooserFn | None = None,
) -> list[DownloadJob]:
    sem = asyncio.Semaphore(max(1, opts.concurrency))

    async def guarded(job: DownloadJob) -> DownloadJob:
        async with sem:
            return await run_job(job, opts, progress, chooser)

    return list(await asyncio.gather(*(guarded(j) for j in jobs)))


def jobs_for(tracks: list[Track], on_disk: set[str]) -> list[DownloadJob]:
    return [
        DownloadJob(track=t, state=JobState.ON_DISK if t.id in on_disk else JobState.PENDING)
        for t in tracks
    ]
