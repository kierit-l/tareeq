"""Walking routes over SMS: foot-only routing and the street-by-street outline reply."""
import os

import pytest

os.environ.setdefault("TAREEQ_DEMO", "1")
os.environ["TAREEQ_STRIKES_FETCH"] = "0"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)
    c.post("/api/reset")
    return c


@pytest.mark.parametrize("text,lang", [("walk Mawasi to Nasser Hospital", "en"), ("مشي من المواصي الى ناصر", "ar")])
def test_walk_sms_is_foot_only_with_street_outline(client, text, lang):
    out = client.post("/api/sms", json={"text": text, "sender": "+970-walk-" + lang, "lang": lang}).json()
    r = out["route"]
    assert r and all(leg["mode"] == "foot" for leg in r["legs"])
    assert r["fare_ils"] == 0
    assert out["reply"].startswith("Walk " if lang == "en" else "مشي ")
    assert len(out["reply"]) <= 160
    sep = " › " if lang == "en" else " ← "
    assert out["reply"].count(sep) >= 1, out["reply"]   # at least two streets named


def test_outline_covers_the_whole_route(client):
    from main import GAZ, R
    a, b = GAZ.match("المواصي")[0], GAZ.match("مستشفى ناصر")[0]
    r = R().route(a["node"], b["node"], ["foot"])
    total = sum(s["m"] for s in r["outline"])
    assert abs(total / 1000 - r["total_km"]) < 0.1
    assert all(s["m"] >= 150 for s in r["outline"])                       # no roundabout-sized noise
    assert all(x["name"] != y["name"] for x, y in zip(r["outline"], r["outline"][1:]))   # runs merged


def test_vehicle_route_reply_unchanged(client):
    out = client.post("/api/sms", json={"text": "route Mawasi to Nasser Hospital", "sender": "+970-ride", "lang": "en"}).json()
    assert not out["reply"].startswith("Walk ")
