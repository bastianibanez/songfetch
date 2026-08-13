"""Catalog persistence and the already-on-disk scan."""

from __future__ import annotations

from ytmp3.catalog import CATALOG_NAME, load_catalog, resolve_catalog, save_catalog, scan_existing
from ytmp3.models import Track


def touch(dirpath, name: str) -> None:
    (dirpath / name).write_bytes(b"\x00")


def test_roundtrip(tmp_path, catalog):
    path = tmp_path / "c.json"
    save_catalog(path, catalog)
    assert load_catalog(path) == catalog


def test_resolve_seeds_on_first_run(tmp_path):
    tracks = resolve_catalog(tmp_path)
    assert (tmp_path / CATALOG_NAME).exists()
    assert len(tracks) == 77


def test_resolve_reuses_existing(tmp_path):
    save_catalog(tmp_path / CATALOG_NAME, [Track(id="x", title="X", artist="Y")])
    assert [t.id for t in resolve_catalog(tmp_path)] == ["x"]


def test_scan_matches_artist_dash_title(tmp_path, catalog):
    touch(tmp_path, "AYYBO - RIZZ.mp3")
    assert scan_existing(tmp_path, catalog) == {"rizz"}


def test_scan_is_punctuation_and_case_blind(tmp_path, catalog):
    touch(tmp_path, "ayybo_dont want none.mp3")
    assert "dont-want-none" in scan_existing(tmp_path, catalog)


def test_scan_folds_diacritics(tmp_path, catalog):
    touch(tmp_path, "Travmkind - Mein Korper gehort nicht mehr mir.mp3")
    assert "mein-korper" in scan_existing(tmp_path, catalog)


def test_short_title_needs_the_artist_too(tmp_path, catalog):
    """'Cap', 'Raw', '100' would otherwise match half the directory."""
    touch(tmp_path, "Some Other Band - Capsize.mp3")
    assert "cap" not in scan_existing(tmp_path, catalog)

    touch(tmp_path, "Enzo is Burning - Cap.mp3")
    assert "cap" in scan_existing(tmp_path, catalog)


def test_appledouble_sidecars_ignored(tmp_path, catalog):
    touch(tmp_path, "._AYYBO - RIZZ.mp3")
    assert scan_existing(tmp_path, catalog) == set()


def test_scan_ignores_non_mp3(tmp_path, catalog):
    touch(tmp_path, "AYYBO - RIZZ.opus")
    assert scan_existing(tmp_path, catalog) == set()


def test_scan_missing_dir(tmp_path, catalog):
    assert scan_existing(tmp_path / "nope", catalog) == set()


def test_real_music_dir_shape(tmp_path, catalog):
    """The user's existing dump: only RIZZ is in the catalog."""
    for name in [
        "AYYBO - RIZZ.mp3",
        "B de Bellako ( Matt. Edit ).mp3",
        "SKRILLEX - REDLINE DASH (SEFARO EDIT).mp3",
        "Sub Focus - Original Don (Segan Remix).mp3",
        "matt.mp3",
        "lovelanguage?.mp3",
    ]:
        touch(tmp_path, name)
    assert scan_existing(tmp_path, catalog) == {"rizz"}
