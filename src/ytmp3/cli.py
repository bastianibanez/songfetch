"""Command line entry point. `ytmp3` with no arguments opens the TUI."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .catalog import resolve_catalog, scan_existing
from .models import DownloadJob, JobState, Track
from .pipeline import Options, jobs_for, run_all
from .search import search

DEFAULT_MUSIC_DIR = Path("/Volumes/Flash/music/songs")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ytmp3", description=__doc__)
    p.add_argument("--music-dir", type=Path, default=DEFAULT_MUSIC_DIR,
                   help=f"where MP3s land (default: {DEFAULT_MUSIC_DIR})")
    p.add_argument("--cbr320", action="store_true",
                   help="constant 320kbps instead of V0 VBR")
    p.add_argument("--keep-source", action="store_true",
                   help="also keep the untranscoded source stream")
    p.add_argument("--force-trim", action="store_true",
                   help="apply head trims longer than the 15s safety cap")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--limit", type=int, default=6, help="candidates fetched per source")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-web-fallback", dest="web_fallback", action="store_false",
                   help="in the TUI, do not search the web automatically when the "
                        "catalog has no match (ctrl+f still does)")
    p.add_argument(
        "--cookies-from-browser", default="", metavar="BROWSER",
        help="authenticate as yourself, e.g. chrome / safari / firefox. "
             "YouTube often answers anonymous downloads with 'Sign in to confirm "
             "you're not a bot'; this is yt-dlp's documented remedy.",
    )
    p.add_argument("--cookies", dest="cookies_file", default="", metavar="FILE",
                   help="Netscape-format cookie file (alternative to --cookies-from-browser)")

    sub = p.add_subparsers(dest="command")
    sub.add_parser("tui", help="interactive search (default)")

    s = sub.add_parser("search", help="print catalog matches and exit")
    s.add_argument("query", nargs="+")

    w = sub.add_parser("web", help="search YouTube for anything, ranked (no download)")
    w.add_argument("query", nargs="+")

    b = sub.add_parser(
        "bulk",
        help="search a file of queries, review every match, then download the ones you keep",
    )
    b.add_argument("file", type=Path, metavar="FILE",
                   help="one query per line; blank lines and #-comments ignored")

    g = sub.add_parser("get", help="download the catalog tracks matching a query")
    g.add_argument("query", nargs="+")
    g.add_argument("--top", type=int, default=1, help="how many matches to take")

    sub.add_parser("sync", help="download every catalog track not already on disk")
    sub.add_parser("status", help="show what is downloaded and what is missing")
    return p


def _options(args) -> Options:
    return Options(
        music_dir=args.music_dir,
        cbr320=args.cbr320,
        keep_source=args.keep_source,
        force_trim=args.force_trim,
        concurrency=args.concurrency,
        search_limit=args.limit,
        overwrite=args.overwrite,
        web_fallback=args.web_fallback,
        cookies_from_browser=args.cookies_from_browser,
        cookies_file=args.cookies_file,
    )


def _report(job: DownloadJob) -> None:
    line = f"  {job.glyph} {job.track.artist} - {job.track.title}"
    if job.message:
        line += f"  [{job.message}]"
    print(line, flush=True)


def _run_jobs(jobs: list[DownloadJob], opts: Options) -> int:
    if not jobs:
        print("nothing to do")
        return 0
    print(f"{len(jobs)} track(s) -> {opts.music_dir}")
    asyncio.run(run_all(jobs, opts, _report))

    failed = [j for j in jobs if j.state is JobState.FAILED]
    stuck = [j for j in jobs if j.state is JobState.AMBIGUOUS]
    done = [j for j in jobs if j.state is JobState.DONE]
    print(f"\ndone {len(done)}  ambiguous {len(stuck)}  failed {len(failed)}")
    for job in stuck:
        print(f"  ? {job.track.artist} - {job.track.title} (run the TUI to choose)")
    for job in failed:
        print(f"  ✗ {job.track.artist} - {job.track.title}: {job.message}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    opts = _options(args)
    command = args.command or "tui"

    if command == "tui":
        from .app import YtMp3App

        YtMp3App(opts).run()
        return 0

    if command == "bulk":
        # Before the catalog check, like `web`: a bulk list is by definition a
        # set of things the catalog does not have yet.
        from .bulk import read_query_file
        from .bulkapp import review

        try:
            queries = read_query_file(args.file)
        except OSError as exc:
            print(f"cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
        if not queries:
            print(f"no queries in {args.file}", file=sys.stderr)
            return 1
        review(queries, opts)
        return 0

    if command == "web":
        # Deliberately before the catalog check: a web search is exactly the
        # case where the track is not in the catalog.
        from .discover import track_from_candidate, web_search

        query = " ".join(args.query)
        try:
            ranked = web_search(
                query,
                limit=opts.search_limit,
                cookies_from_browser=opts.cookies_from_browser,
                cookies_file=opts.cookies_file,
            )
        except Exception as exc:  # noqa: BLE001 - a CLI reports, it does not traceback
            print(f"web search failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if not ranked:
            print("no results")
            return 1
        for scored in ranked:
            c = scored.candidate
            track = track_from_candidate(c)
            print(f"{scored.score:7.1f}  {track.artist} - {track.title}")
            print(f"           {c.channel or c.uploader} · {c.duration or '?'}s · "
                  f"{c.view_count:,} views · {c.url}")
            print(f"           {scored.explain()}")
        return 0

    catalog: list[Track] = resolve_catalog(opts.music_dir)
    if not catalog:
        print(f"no catalog at {opts.music_dir}/catalog.json", file=sys.stderr)
        return 2
    on_disk = scan_existing(opts.music_dir, catalog)

    if command == "search":
        query = " ".join(args.query)
        hits = search(catalog, query)
        if not hits:
            print("no matches")
            return 1
        for score_value, track in hits:
            mark = "✓" if track.id in on_disk else " "
            print(f"{mark} {score_value:7.1f}  {track.title}  —  {track.artist}"
                  + (f"  ({track.album})" if track.album else ""))
        return 0

    if command == "status":
        missing = [t for t in catalog if t.id not in on_disk]
        print(f"{len(on_disk)}/{len(catalog)} on disk, {len(missing)} missing")
        for track in missing:
            print(f"  · {track.artist} - {track.title}")
        return 0

    if command == "get":
        query = " ".join(args.query)
        hits = [t for _, t in search(catalog, query)][: max(1, args.top)]
        if not hits:
            print("no catalog match", file=sys.stderr)
            return 1
        return _run_jobs(jobs_for(hits, set()), opts)

    if command == "sync":
        missing = [t for t in catalog if t.id not in on_disk]
        return _run_jobs(jobs_for(missing, set()), opts)

    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
