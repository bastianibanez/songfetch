from __future__ import annotations

import json
from pathlib import Path

import pytest

from ytmp3.models import Track

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(*parts: str):
    return json.loads((FIXTURES.joinpath(*parts)).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def catalog() -> list[Track]:
    return [Track.from_dict(d) for d in load_json("tracks.json")]


@pytest.fixture(scope="session")
def search_cases() -> list[dict]:
    return load_json("search_cases.json")
