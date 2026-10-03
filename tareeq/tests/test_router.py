"""Router invariants on the real corridor graph."""
import pytest

from router import Router
from state import StateStore

PAIRS = [("المواصي", "مستشفى ناصر"), ("دير البلح", "خان يونس"), ("المواصي", "دير البلح"),
         ("القرارة", "مستشفى غزة الأوروبي"), ("مدينة حمد", "مستشفى شهداء الأقصى")]


def make(corridor, unsafe=frozenset()):
    store = StateStore(corridor.seg, set(unsafe))
    return store, Router(corridor, store, {"fuel_index": 1.0})


def all_segments(r):
    return [s for leg in r["legs"] for s in leg["segments"]]


@pytest.mark.parametrize("a,b", PAIRS)
def test_route_never_enters_unsafe_buffer(corridor, gaz, a, b):
    unsafe = corridor.unsafe_segments([k for k, f in corridor.nogo.items() if f["properties"].get("active")])
    assert unsafe, "expected at least one active no-go polygon in nogo.geojson"
    _, router = make(corridor, unsafe)
    pa, pb = gaz.match(a)[0], gaz.match(b)[0]
    r = router.route(pa["node"], pb["node"])
    if r is not None:
        assert not set(all_segments(r)) & unsafe


def test_route_avoids_crowd_unsafe_segment(corridor, gaz):
    store, router = make(corridor)
    a, b = gaz.match("المواصي")[0], gaz.match("مستشفى ناصر")[0]
    first = router.route(a["node"], b["node"])
    victim = first["legs"][0]["segments"][len(first["legs"][0]["segments"]) // 2]
    store.add_report(victim, "unsafe", "someone")
    second = router.route(a["node"], b["node"])
    assert second is None or victim not in all_segments(second)


def test_no_route_when_destination_cut_off(corridor, gaz):
    """Cut every segment touching the destination node: router must say 'no route', not cross it."""
    b = gaz.match("مستشفى ناصر")[0]
    cut = {sid for sid, p in corridor.seg.items() if b["node"] in (p["u"], p["v"])}
    _, router = make(corridor, cut)
    a = gaz.match("المواصي")[0]
    assert router.route(a["node"], b["node"]) is None


def test_range_is_ordered_and_rounded(corridor, gaz):
    _, router = make(corridor)
    r = router.route(gaz.match("دير البلح")[0]["node"], gaz.match("خان يونس")[0]["node"])
    assert r["p50_min"] < r["p85_min"] and r["p50_min"] % 5 == 0 and r["p85_min"] % 5 == 0


def test_trailer_only_on_major_roads(corridor, gaz):
    _, router = make(corridor)
    r = router.route(gaz.match("المواصي")[0]["node"], gaz.match("دير البلح")[0]["node"], ["trailer"])
    for leg in (r or {}).get("legs", []):
        if leg["mode"] == "trailer":
            assert all(corridor.seg[s]["major"] for s in leg["segments"])


def test_weak_evidence_cannot_be_high_confidence(corridor, gaz):
    """Fresh store = only satellite prior / Unknown: spec A6 caps confidence below high."""
    _, router = make(corridor)
    r = router.route(gaz.match("المواصي")[0]["node"], gaz.match("مستشفى ناصر")[0]["node"])
    assert r["confidence_label"] != "high"
