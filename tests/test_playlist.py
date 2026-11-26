import json

from ytmp3 import encode, probe
from ytmp3.cli import main
from ytmp3.trim import TrimDecision


def test_playlist_preserves_positions_and_retries_only_missing_files(
    tmp_path, monkeypatch, capsys
):
    first = {"id": "firstVideo1", "title": "Artist - Song", "url": "firstVideo1"}
    second = {**first, "id": "otherVideo2"}
    entries = [first, None, second, first]
    watch = "https://www.youtube.com/watch?v="
    download_urls = []

    def list_entries(args, timeout):
        assert args[-1] == "https://music.youtube.com/playlist?list=PL_example"
        assert "--flat-playlist" in args
        assert "--dump-single-json" in args
        assert "--playlist-end" not in args
        assert args[args.index("--cookies") + 1] == "cookies.txt"
        return json.dumps({"entries": entries})

    def fetch(url, directory, **kwargs):
        assert kwargs["cookies_file"] == "cookies.txt"
        download_urls.append(url)
        if url == watch + second["id"]:
            raise encode.EncodeError("video unavailable")
        audio = directory / "audio.webm"
        audio.write_bytes(b"source")
        return encode.Fetched(audio, {}, None)

    def encode_mp3(fetched, dest, track, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp3")

    monkeypatch.setattr(probe, "_run", list_entries)
    monkeypatch.setattr(encode, "fetch_audio", fetch)
    monkeypatch.setattr(encode, "decide_trim", lambda *a, **kw: TrimDecision(0, "clean"))
    monkeypatch.setattr(encode, "encode_mp3", encode_mp3)
    args = [
        "--music-dir", str(tmp_path), "--cookies", "cookies.txt",
        "--keep-source", "--concurrency", "1", "playlist",
        "https://music.youtube.com/watch?v=firstVideo1&list=PL_example&si=share",
    ]

    assert main(args) == 1
    assert download_urls == [watch + first["id"], watch + second["id"], watch + first["id"]]
    dest = tmp_path / "playlists" / "PL_example"
    assert sorted(p.name for p in dest.glob("*.mp3")) == [
        "0001 - Artist - Song [firstVideo1].mp3",
        "0004 - Artist - Song [firstVideo1].mp3",
    ]
    assert (dest / "0004 - Artist - Song [firstVideo1].webm").read_bytes() == b"source"
    assert "playlist entry 2 is unavailable" in capsys.readouterr().err

    download_urls.clear()
    assert main(args) == 1
    assert download_urls == [watch + second["id"]]
    assert "on disk 2" in capsys.readouterr().out


def test_playlist_rejects_urls_without_a_youtube_playlist(capsys):
    assert main(["playlist", "https://music.youtube.com/watch?v=firstVideo1"]) == 2
    assert main(["playlist", "https://example.com/playlist?list=PL_example"]) == 2
    assert "expected a YouTube Music or YouTube playlist URL" in capsys.readouterr().err
