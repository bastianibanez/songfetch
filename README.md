# songfetch

The installed command is `ytmp3` (the Python package kept its original name).

Search a track catalog in a TUI, pick tracks, get MP3s that are actually the
song — not the music video, not a remix you didn't ask for, and not thirty
seconds of label ident before the drums come in.

```
uv sync --extra dev
make                                  # TUI, with the auth flags already on
make check                            # are yt-dlp / ffmpeg / deno installed?
```

`make help` lists the rest. Override anything on the command line:
`make tui BROWSER=safari MUSIC_DIR=~/Music`. The bare
`uv run python -m ytmp3.cli` still works, it just leaves the flags to you.

## What it does

| Problem | How it's handled |
|---|---|
| Search results are music videos | Candidates are scored; `- Topic` / art-track uploads win, `(Official Music Video)` / lyric video / visualizer are penalised |
| Bootlegs and flips have no Topic upload | Regular YouTube results stay eligible and are scored on their own merits |
| A remix outranks the original | A *precision* term penalises title words the query never asked for |
| ...but sometimes you want the remix | Every penalty is suppressed when the query itself contains the trigger word |
| Audio doesn't start at the music | SponsorBlock `music_offtopic`, named intro chapters, then leading-silence detection |
| Transcoding twice | The source stream is downloaded unconverted; exactly one ffmpeg pass trims, encodes and tags |

### On "max quality"

YouTube serves already-lossy Opus/AAC. MP3 V0 (`-q:a 0`, the default here) is a
transparent transcode *of that source* — it cannot recover what the source
already threw away. `--cbr320` gives constant 320kbps if a player needs it;
`--keep-source` also saves the untouched original next to the MP3.

## Commands

```bash
ytmp3                        # TUI (default)
ytmp3 search four tet        # print catalog matches
ytmp3 web bicep glue         # search YouTube for anything, ranked (no download)
ytmp3 bulk wishlist.txt      # search a file of queries, review, then download
ytmp3 status                 # what's downloaded, what's missing
ytmp3 get csirac             # download the best catalog match
ytmp3 sync                   # download everything not already on disk
```

Useful flags: `--music-dir`, `--cbr320`, `--keep-source`, `--force-trim`,
`--concurrency`, `--overwrite`, `--no-web-fallback`, `--cookies-from-browser`.

## TUI keys

| Key | Action |
|---|---|
| type | filter the catalog (title / artist / album) |
| `↓` | move from the search box into the results |
| `space` | toggle selection on the highlighted row |
| `enter` | download selection (or the highlighted row) |
| `esc` | back to the search box |
| `ctrl+a` / `ctrl+d` | select all shown / clear selection |
| `ctrl+f` | search the web for what's in the box |
| `ctrl+r` | download selection from anywhere |
| `ctrl+q` | quit |

## Searching beyond the catalog

The catalog is a wishlist, not a limit. When a query matches nothing in it, the
TUI waits for the typing to stop and then searches YouTube Music + YouTube for
the query itself; `ctrl+f` forces that search even when the catalog did have an
answer. Results appear under a `web ⌕` header, ranked by the same brain that
picks uploads for catalog downloads:

```
── catalog (0)
── web ⌕ (7)
 ·  Bicep     Glue                       BICEP · 4:29 · 29.9M views · score 335
 ·  Bicep     Glue (Original Mix)        Filippos Sgouros · 4:30 · 8.7M · score 130
 ·  BICEP     BICEP | GLUE               BICEP · 4:45 · 70.2M · score 75
```

Selecting a web row downloads *that exact upload* -- the pick is already made,
so the search-and-rank step is skipped. Artist, title and album are parsed off
the upload (`(Official Video)` and friends are stripped, `(Extended Mix)` and
`(Effy Remix)` are not, because those are part of what the recording is), and
once the MP3 is on disk the track is appended to `catalog.json` so it is an
ordinary catalog entry from then on.

Automatic fallback is what `--no-web-fallback` turns off; `ctrl+f` still works.

A web search takes about ten seconds. Both sources are listed in one request
each, concurrently, and every result is then looked up in full — in parallel —
for the fields ranking needs. Repeating a query inside the same session is
free, and typing over an in-flight search kills its yt-dlp processes rather
than leaving them running against the query you already abandoned.

Status column: `·` pending · `✓` already on disk · `⋯` searching · `?` needs a
manual pick · `↓` downloading · `✂` trimming · `♪` done · `✗` failed.

When ranking can't confidently choose, a modal lists the candidates with their
scores and the reasons behind them; `esc` skips the track.

## A whole wishlist at once

`ytmp3 bulk wishlist.txt` searches a file of queries and shows you every match
before anything downloads. One query per line; blank lines and lines starting
with `#` are ignored, repeats are dropped, and the file's order is kept.

```
── bulk ⌕ 5 queries ─────────────────────────────────────────────────────
 ✓  four tet baby        Four Tet · Baby              4:04 · 1.2M · score 335
 ?  bicep glue           BICEP · Glue                 top two within 40 · 7 alts
 ✓  Ninajirachi - CSIRAC Ninajirachi · CSIRAC         4:21 · 890K · score 335
 ✗  aphex twin xtal      —                            no results
```

Ranking's confident picks arrive already ticked and its doubtful ones do not,
because the work is confirming the handful it was unsure about rather than
re-approving the obvious. `space` takes or skips the row under the cursor,
`enter` opens the ranked alternatives, `ctrl+a` / `ctrl+d` take or skip
everything, and `ctrl+r` downloads what is ticked -- straight into the same
pipeline `sync` uses, catalog entry included.

Searches run concurrently, but every yt-dlp request in the process -- one
search's or a bulk run's hundredth -- passes through one global limiter: at
most four in flight, spaced by at least a quarter second, and widened
automatically if a 429 slips through. YouTube answers 429 for a while once it
starts, so the fastest bulk run is the one that never triggers it. Five queries
take about a minute.

## Authentication (required for downloads)

YouTube answers anonymous download requests with *"Sign in to confirm you're
not a bot"*. Pass your own browser session:

```bash
ytmp3 --cookies-from-browser chrome sync
```

**Known environment requirement:** yt-dlp also needs a JavaScript runtime plus
its EJS solver bundle to resolve YouTube's signature challenge. Without one,
format selection fails with *"Only images are available"* and no audio can be
downloaded — searching and ranking still work fine. `yt-dlp-ejs` is already a
dependency here; the runtime is not, and Deno is the one yt-dlp recommends:

```bash
brew install deno
```

Verify with `yt-dlp -v <url> 2>&1 | grep "JS runtimes"` — it should not say `none`.

## Testing

```bash
uv run python -m pytest -q          # 216 tests, no network
uv run python -m pytest -q -m network   # live YouTube (~10s a query)
```

Use `python -m pytest`, not `uv run pytest` — the bare command can resolve to a
globally installed pytest that cannot import this package.

The interesting logic is pure and fixture-driven:

- `tests/fixtures/tracks.json` — 77 tracks transcribed from the source screenshots
- `tests/fixtures/search_cases.json` — 29 query → expected-ordering cases
- `tests/fixtures/candidates/` — recorded yt-dlp result sets, one per ranking scenario
- `tests/fixtures/silencedetect/` — recorded ffmpeg stderr

`search.py`, `ranking.py` and `trim.py` never touch the network or the disk, so
every scoring decision is reproducible offline.

## Layout

```
src/ytmp3/
  textutil.py   normalize / squash / safe filenames   (pure)
  search.py     catalog text search                   (pure)
  ranking.py    candidate scoring, ambiguity gate     (pure)
  trim.py       head-offset decision                  (pure)
  discover.py   free-text web search -> Track         (parsing is pure)
  catalog.py    catalog JSON + already-on-disk scan
  cache.py      session memo for repeated web searches (pure)
  ratelimit.py  one global throttle for every yt-dlp request (pure)
  bulk.py       a file of queries -> per-query verdicts (parsing is pure)
  probe.py      yt-dlp search (YT Music + YouTube): list flat, hydrate wide
  encode.py     fetch, silence probe, single transcode
  pipeline.py   orchestration, 3 tracks at a time
  app.py        Textual TUI
  bulkapp.py    Textual review screen for a bulk run
  cli.py        argparse entry point
```

MP3s land in `/Volumes/Flash/music/songs/` by default; override with
`--music-dir`. The catalog lives at `<music-dir>/catalog.json`, seeded from the
fixtures on first run. Add tracks by editing that file, or by downloading them
out of a web search -- those arrive with a `yt:<videoid>` id, which is why they
can never collide with a hand-written entry.
