"""Download-side plumbing: cookie handling and failure diagnosis.

Nothing here spawns yt-dlp. The subject is what the module does with what
yt-dlp said, which is where the useful sentence kept getting thrown away.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from ytmp3 import encode
from ytmp3.encode import EncodeError, _diagnose, cookie_args

# The real tail of a failed download against a rotated cookie jar. The cause is
# a warning near the top; the error at the bottom says only "403".
ROTTEN_COOKIES = """\
WARNING: [youtube] The provided YouTube account cookies are no longer valid. \
They have likely been rotated in the browser as a security measure.
[youtube] lXc8EHmFXOM: Downloading webpage
[info] lXc8EHmFXOM: Downloading 1 format(s): 251
ERROR: unable to download video data: HTTP Error 403: Forbidden
"""


# -- diagnosis -------------------------------------------------------------


def test_a_rotated_cookie_jar_is_named_not_just_the_403():
    assert "rotated" in _diagnose(ROTTEN_COOKIES)


def test_the_bot_check_is_recognised():
    hint = _diagnose("ERROR: Sign in to confirm you’re not a bot. Use --cookies-from-browser")
    assert "bot check" in hint


def test_rate_limiting_is_recognised():
    hint = _diagnose("WARNING: Unable to download webpage: HTTP Error 429: Too Many Requests")
    assert "rate-limit" in hint


def test_an_unremarkable_failure_gets_no_hint():
    assert _diagnose("ERROR: something nobody has seen before") == ""


def test_the_hint_reaches_the_raised_error(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(cmd, 1, "", ROTTEN_COOKIES)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(EncodeError) as caught:
        encode._run(["yt-dlp"], 10, "yt-dlp download")

    message = str(caught.value)
    assert "403" in message, "the symptom is still reported"
    assert "rotated" in message, "and now so is the cause"


# -- cookie flags ----------------------------------------------------------


def test_an_explicit_cookie_file_is_passed_through():
    assert cookie_args(cookies_file="/tmp/c.txt") == ["--cookies", "/tmp/c.txt"]


def test_no_credentials_means_no_flags():
    assert cookie_args() == []


def test_the_browser_export_is_memoised_and_reused(monkeypatch, tmp_path):
    """Exporting re-decrypts the browser store, so it happens once a process.

    One search is a listing per source plus a lookup per result; paying the
    export on each of those invocations was most of a search.
    """
    exported = tmp_path / "cookies.txt"
    exported.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\ta\tb\n")
    calls: list[str] = []

    def fake_export(browser: str) -> str:
        calls.append(browser)
        return str(exported)

    monkeypatch.setattr(encode, "_COOKIE_EXPORTS", {})
    monkeypatch.setattr(encode, "_export_browser_cookies", fake_export)

    assert cookie_args(cookies_from_browser="chrome") == ["--cookies", str(exported)]
    assert cookie_args(cookies_from_browser="chrome") == ["--cookies", str(exported)]
    assert len(calls) == 2, "cookie_args delegates; the memo lives in the export"


def test_a_failed_export_falls_back_to_the_slow_flag(monkeypatch):
    """Slower beats unauthenticated: a bot check is worse than a lost second."""
    monkeypatch.setattr(encode, "_COOKIE_EXPORTS", {})
    monkeypatch.setattr(encode, "_export_browser_cookies", lambda browser: "")

    assert cookie_args(cookies_from_browser="chrome") == ["--cookies-from-browser", "chrome"]


def test_the_export_seeds_a_netscape_header_and_stays_private(monkeypatch):
    """`--cookies` is an input as much as an output.

    yt-dlp loads the file before it writes it and refuses one that does not
    look like a cookie jar, so an empty file fails the export outright. The
    file holds live session cookies, hence 0600.
    """
    seen: dict[str, str] = {}

    def fake_run(cmd, capture_output, text, timeout, check):
        target = cmd[cmd.index("--cookies") + 1]
        seen["header"] = open(target, encoding="utf-8").read()
        seen["mode"] = oct(os.stat(target).st_mode)[-3:]
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(encode, "_COOKIE_EXPORTS", {})
    monkeypatch.setattr(subprocess, "run", fake_run)

    path = encode._export_browser_cookies("chrome")

    assert path, "a jar with a real cookie in it counts as exported"
    assert seen["header"].startswith("# Netscape HTTP Cookie File")
    assert seen["mode"] == "600"


def test_an_export_that_wrote_no_cookies_is_a_failure(monkeypatch):
    """The seeded header is always there, so file size proves nothing."""

    def fake_run(cmd, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(cmd, 1, "", "ERROR: could not copy chrome cookie db")

    monkeypatch.setattr(encode, "_COOKIE_EXPORTS", {})
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert encode._export_browser_cookies("chrome") == ""
