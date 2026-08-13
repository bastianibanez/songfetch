"""Live network tests. Deselected by default; run with `-m network`.

    uv run python -m pytest -m network -q

These hit YouTube for real, so they are slow, occasionally flaky, and may
require `--cookies-from-browser` style authentication (see README).
"""

from __future__ import annotations

import os
import shutil

import pytest

from ytmp3.models import Track
from ytmp3.pipeline import Options, download_one, jobs_for, resolve_candidate
from ytmp3.probe import find_candidates
from ytmp3.ranking import rank

pytestmark = pytest.mark.network

COOKIES = os.environ.get("YTMP3_TEST_COOKIES_BROWSER", "")

TRACK = Track(id="csirac", title="CSIRAC", artist="Ninajirachi", album="I Love My Computer")


def _opts(tmp_path):
    return Options(music_dir=tmp_path, concurrency=1, cookies_from_browser=COOKIES)


def test_search_returns_candidates():
    got = find_candidates(TRACK.query, limit=4, cookies_from_browser=COOKIES)
    assert got, "no candidates came back from either source"


def test_ranking_prefers_the_plain_track_over_remixes():
    """The live regression that motivated the precision/variant rules."""
    got = find_candidates(TRACK.query, limit=5, cookies_from_browser=COOKIES)
    ranked = rank(got, TRACK.query, TRACK.duration)
    assert ranked
    winner = ranked[0].candidate
    assert "remix" not in winner.title.lower()


def test_resolves_without_ambiguity(tmp_path):
    job = jobs_for([TRACK], set())[0]
    chosen = resolve_candidate(job, _opts(tmp_path))
    assert chosen is not None, [
        (s.score, s.candidate.title) for s in job.candidates[:4]
    ]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_end_to_end_download(tmp_path):
    """Full pipeline: fetch -> trim decision -> single transcode -> tagged MP3."""
    job = jobs_for([TRACK], set())[0]
    opts = _opts(tmp_path)
    job.chosen = resolve_candidate(job, opts)
    if job.chosen is None:
        pytest.skip("ranking was ambiguous; nothing to download unattended")

    dest = download_one(job, opts)
    assert dest.exists()
    assert dest.stat().st_size > 500_000, "suspiciously small for a 3 minute track"

    probe = shutil.which("ffprobe")
    if probe:
        import json
        import subprocess

        out = subprocess.run(
            [probe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(dest)],
            capture_output=True, text=True, check=True,
        ).stdout
        data = json.loads(out)
        audio = next(s for s in data["streams"] if s["codec_type"] == "audio")
        assert audio["codec_name"] == "mp3"
        tags = {k.lower(): v for k, v in data["format"].get("tags", {}).items()}
        assert tags.get("title") == TRACK.title
        assert tags.get("artist") == TRACK.artist
