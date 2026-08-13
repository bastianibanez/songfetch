"""Search behaviour, driven by tests/fixtures/search_cases.json."""

from __future__ import annotations

import pytest

from ytmp3.search import search, search_tracks
from ytmp3.textutil import normalize, safe_filename, squash, tokens


# --------------------------------------------------------------------------
# normalize()
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Mein Körper gehört nicht mehr mir", "mein korper gehort nicht mehr mir"),
        ("geods sorèd", "geods sored"),
        ("swedm®", "swedm"),
        ("Don't Want None", "dont want none"),
        ("Beat Keep Rockin'", "beat keep rockin"),
        ("A.F.T.E.R", "a f t e r"),
        ("FISHER & AR/CO", "fisher ar co"),
        ("Noizu, Westend & No/Me", "noizu westend no me"),
        ("BUZSQUEZ & qwerty_lxd", "buzsquez qwerty lxd"),
        ("Gecko (Overdrive)", "gecko overdrive"),
        ("Kris R., GeezyDee & Flame 火炎", "kris r geezydee flame 火炎"),
        ("F*CK U SKRILLEX YOU THINK UR ANDY WARHOL BUT UR NOT!! <3", "f ck u skrillex you think ur andy warhol but ur not 3"),
        ("Skrillex & ISOxo - Quest Vault [ISOxo Played @ EDC Mexico 2024]",
         "skrillex isoxo quest vault isoxo played edc mexico 2024"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_squash_removes_spaces():
    assert squash("Talk To Me") == "talktome"
    assert squash("A.F.T.E.R") == "after"


def test_tokens():
    assert tokens("Chris Lake & Aluna") == ["chris", "lake", "aluna"]
    assert tokens("") == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("FISHER & AR/CO - Ocean", "FISHER & AR_CO - Ocean"),
        ("Ninajirachi - CSIRAC", "Ninajirachi - CSIRAC"),
        ("a" * 300, "a" * 180),
        ("...", "untitled"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


# --------------------------------------------------------------------------
# search(), fixture-driven
# --------------------------------------------------------------------------

def _ids(catalog, query):
    return [t.id for t in search_tracks(catalog, query)]


def test_every_fixture_case(catalog, search_cases):
    """One assertion loop so a failure names the case that broke."""
    failures = []
    for case in search_cases:
        got = _ids(catalog, case["query"])
        label = f"{case['query']!r} ({case['note']})"

        if "expect_count" in case and len(got) != case["expect_count"]:
            failures.append(f"{label}: expected {case['expect_count']} results, got {len(got)}: {got[:10]}")

        top = case.get("expect_top", [])
        if top and got[: len(top)] != top:
            failures.append(f"{label}: expected head {top}, got {got[: len(top) + 2]}")

        for want in case.get("expect_contains", []):
            if want not in got:
                failures.append(f"{label}: expected {want!r} in results, got {got}")

        for unwanted in case.get("expect_absent", []):
            if unwanted in got:
                failures.append(f"{label}: expected {unwanted!r} absent, got {got}")

    assert not failures, "\n".join(failures)


def test_catalog_ids_are_unique(catalog):
    ids = [t.id for t in catalog]
    assert len(ids) == len(set(ids))


def test_scores_descend(catalog):
    scores = [s for s, _ in search(catalog, "move")]
    assert scores == sorted(scores, reverse=True)


def test_limit_truncates(catalog):
    assert len(search(catalog, "", limit=5)) == 5


def test_search_is_deterministic(catalog):
    a = _ids(catalog, "skrillex")
    b = _ids(catalog, "skrillex")
    assert a == b


def test_every_track_is_findable_by_its_own_title(catalog):
    """Sanity: typing a track's exact title must put it first."""
    misses = []
    for track in catalog:
        got = _ids(catalog, track.title)
        if not got or got[0] != track.id:
            misses.append(f"{track.id}: {track.title!r} -> {got[:3]}")
    assert not misses, "\n".join(misses)
