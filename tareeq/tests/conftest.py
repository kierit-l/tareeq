"""Shared fixtures. The real corridor (OSM + UNOSAT build) loads in ~1 s, so it is built once per session."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


@pytest.fixture(scope="session")
def corridor():
    from graph import Corridor
    return Corridor()


@pytest.fixture(scope="session")
def gaz(corridor):
    from nlu import Gazetteer
    return Gazetteer(corridor.places)


@pytest.fixture
def toy_segments():
    """Three segments 100 m apart along a line, no satellite prior."""
    return {i: {"id": i, "len": 100.0, "prior": "unknown", "xy": (i * 100.0, 0.0)} for i in range(3)}


@pytest.fixture
def store(toy_segments):
    from state import StateStore
    return StateStore(toy_segments, unsafe_segments=set())
