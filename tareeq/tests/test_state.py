"""Segment-state rules from the spec (verification, decay, safety asymmetry, official override)."""
from state import MIN_CONF, PROVISIONAL_H, StateStore


def test_single_report_does_not_verify(store):
    r = store.add_report(0, "open", "a")
    assert not r["changed"] and store.get(0).state == "unknown"
    assert store.get(0).pending == {"open": 1}


def test_two_independent_reports_verify(store):
    store.add_report(0, "degraded", "a")
    r = store.add_report(0, "degraded", "b")
    assert r["changed"] and store.get(0).state == "degraded" and store.get(0).source == "verified"


def test_same_reporter_twice_is_one_report(store):
    store.add_report(0, "open", "a")
    store.add_report(0, "open", "a")
    assert store.get(0).state == "unknown"


def test_trusted_reporter_verifies_alone(store):
    store.add_report(0, "blocked", "t", trusted=True)
    assert store.get(0).state == "blocked"


def test_single_unsafe_report_verifies(store):
    """Crowd reports may always make a road less safe."""
    store.add_report(1, "unsafe", "a")
    assert store.get(1).state == "unsafe"


def test_single_blocked_report_is_provisional(store):
    store.add_report(1, "blocked", "a")
    st = store.get(1)
    assert st.state != "blocked" and st.provisional
    store.advance(PROVISIONAL_H + 0.5)
    assert not store.get(1).provisional


def test_stale_open_decays_to_unknown_not_open(store):
    store.add_report(0, "open", "a")
    store.add_report(0, "open", "b")
    assert store.get(0).state == "open"
    store.advance(24)   # tau(open)=6 h -> conf far below MIN_CONF
    st = store.get(0)
    assert st.state == "unknown" and st.conf < MIN_CONF


def test_official_unsafe_overrides_and_rejects_safer_reports(toy_segments):
    s = StateStore(toy_segments, unsafe_segments={2})
    r = s.add_report(2, "open", "t", trusted=True)
    assert not r["accepted"] and r["ignored_reason"] == "contradicts_official_nogo"
    assert s.get(2).state == "unsafe" and s.get(2).source == "official"


def test_lifting_official_zone_emits_event(toy_segments):
    s = StateStore(toy_segments, unsafe_segments={2})
    s.set_official_unsafe(set(), "nogo:test")
    assert s.get(2).state == "unknown"
    assert s.events[-1]["seg"] == 2 and s.events[-1]["to"] == "unknown"


def test_newer_trusted_report_overturns_unsafe(store):
    store.add_report(1, "unsafe", "a")
    store.advance(0.2)
    store.add_report(1, "open", "t", trusted=True)
    assert store.get(1).state == "open"


def test_reporter_ids_are_hashed(store):
    store.add_report(0, "open", "+970599123456")
    rows = store.db.execute("SELECT reporter FROM reports").fetchall()
    assert all("970599" not in r[0] for r in rows)
