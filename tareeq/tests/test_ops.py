"""Operator console, field reporting and strike hazards (backend/ops.py, backend/strikes.py)."""
import os
import time

import pytest

os.environ.setdefault("TAREEQ_DEMO", "1")
os.environ["TAREEQ_STRIKES_FETCH"] = "0"     # never hit the network from tests


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)
    c.post("/api/reset")
    return c, main


def login(c, name):
    return {"X-Operator": name}


def route_segments(c):
    r = c.get("/api/route", params={"frm": "المواصي", "to": "مستشفى ناصر"}).json()["route"]
    return [s for leg in r["legs"] for s in leg["segments"]] if r else []


def square(lon, lat, d=0.001):
    return {"type": "Polygon", "coordinates": [[[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]]}


def test_admin_rejects_unknown_operator(client):
    c, _ = client
    assert c.get("/api/ops/admin/zones").status_code == 200
    assert c.get("/api/ops/admin/zones", headers={"X-Operator": "mallory"}).status_code == 401


def test_zone_two_person_rule_and_routing(client):
    c, main = client
    a, o = login(c, "amal"), login(c, "omar")
    segs = route_segments(c)
    mid = segs[len(segs) // 2]
    lon, lat = main.C.node_ll[main.C.seg[mid]["u"]]
    z = c.post("/api/ops/admin/zones", headers=a, json={"name": "test", "kind": "evacuation", "source": "OCHA test",
                                                        "geometry": square(lon, lat)}).json()
    assert z["status"] == "pending"
    assert mid not in main.S().official_unsafe, "pending zone must not affect routing"
    assert c.post(f"/api/ops/admin/zones/{z['id']}/approve", headers=a).status_code == 403
    assert c.post(f"/api/ops/admin/zones/{z['id']}/approve", headers=o).json()["status"] == "active"
    assert mid in main.S().official_unsafe
    assert mid not in route_segments(c)
    # removal also needs two people
    c.post(f"/api/ops/admin/zones/{z['id']}/remove", headers=o)
    assert c.post(f"/api/ops/admin/zones/{z['id']}/approve", headers=o).status_code == 403
    assert c.post(f"/api/ops/admin/zones/{z['id']}/approve", headers=a).json()["status"] == "removed"


def test_zone_rejects_invalid_geometry(client):
    c, _ = client
    a = login(c, "amal")
    r = c.post("/api/ops/admin/zones", headers=a, json={"name": "bad", "kind": "evacuation", "source": "x y",
                                                        "geometry": {"type": "Point", "coordinates": [34.3, 31.35]}})
    assert r.status_code == 400


def test_strike_hazard_reroutes_and_survives_reset(client):
    c, main = client
    a, o = login(c, "amal"), login(c, "omar")
    segs = route_segments(c)
    mid = segs[len(segs) // 2]
    lon, lat = main.C.node_ll[main.C.seg[mid]["u"]]
    assert c.post("/api/ops/admin/strikes/simulate", headers=a, json={"lon": lon, "lat": lat}).json()["segments_added"] > 0
    assert mid in main.S().official_unsafe and mid not in route_segments(c)
    c.post("/api/reset")
    assert mid in main.S().official_unsafe, "demo reset must keep live strike hazards"
    # dismissal: same operator twice is refused, a second operator completes it
    sid = c.get("/api/ops/strikes").json()["features"][0]["properties"]["id"]
    assert c.post(f"/api/ops/admin/strikes/{sid}/dismiss", headers=a).json()["status"] == "requested"
    assert c.post(f"/api/ops/admin/strikes/{sid}/dismiss", headers=a).status_code == 403
    assert c.post(f"/api/ops/admin/strikes/{sid}/dismiss", headers=o).json()["status"] == "dismissed"
    assert mid not in main.S().official_unsafe


def test_strike_hazard_expires(client):
    c, main = client
    a = login(c, "amal")
    c.post("/api/ops/admin/strikes/simulate", headers=a, json={"lon": 34.30, "lat": 31.35})
    n = len(main.S().official_unsafe)
    main.S().advance(25)            # demo strikes live 24 h
    main.ops.sync_strikes()
    main.ops.apply_hazards("test")
    assert len(main.S().official_unsafe) < n


def test_field_report_flow(client):
    c, main = client
    a = login(c, "amal")
    code = c.post("/api/ops/admin/reporters", headers=a, json={"label": "Nurse A", "org": "NGO"}).json()["code"]
    assert c.get("/api/ops/field/me", params={"code": code}).json()["label"] == "Nurse A"
    r = c.post("/api/ops/field/report", json={"code": code, "kind": "blocked", "lon": 34.2922, "lat": 31.3470}).json()
    assert r["accepted"] and main.S().get(r["segments"][0]).state == "blocked", "field reporters are trusted"
    r = c.post("/api/ops/field/report", json={"code": code, "kind": "strike", "lon": 34.32, "lat": 31.343}).json()
    assert len(r["segments"]) > 1 and all(main.S().get(s).state == "unsafe" for s in r["segments"])
    assert not c.post("/api/ops/field/report", json={"code": code, "kind": "open", "lon": 34.29, "lat": 31.34,
                                                     "age_s": 2 * 86400}).json()["accepted"]
    rid = [x for x in c.get("/api/ops/admin/reporters", headers=a).json()["reporters"] if x["label"] == "Nurse A"][0]["id"]
    c.post(f"/api/ops/admin/reporters/{rid}/revoke", headers=a)
    assert c.post("/api/ops/field/report", json={"code": code, "kind": "open", "lon": 34.29, "lat": 31.34}).status_code == 401


def test_field_report_never_stores_coordinates(client):
    c, main = client
    c.post("/api/ops/field/report", json={"code": "DEMO-0000", "kind": "rubble", "lon": 34.2922, "lat": 31.3470})
    cols = [r[1] for r in main.ops.DB.execute("PRAGMA table_info(field_notes)")]
    assert not {"lon", "lat"} & set(cols)


def test_broadcast_two_person(client):
    c, _ = client
    a, o = login(c, "amal"), login(c, "omar")
    b = c.post("/api/ops/admin/broadcasts", headers=a, json={"text": "اختبار البث للمشتركين"}).json()
    assert c.post(f"/api/ops/admin/broadcasts/{b['id']}/approve", headers=a).status_code == 403
    assert c.post(f"/api/ops/admin/broadcasts/{b['id']}/approve", headers=o).json()["status"] == "sent"


# ---------------- strikes.py feed parsing (no network) ----------------
class FakeResp:
    def __init__(self, text):
        self.text = text
    def raise_for_status(self):
        pass
    def iter_lines(self):
        return iter(self.text.splitlines())
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass


class FakeHttp:
    def __init__(self, text):
        self.text = text
    def stream(self, method, url):
        return FakeResp(self.text)
    def get(self, url):
        return FakeResp(self.text)


FIRMS_CSV = """latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight
31.3470,34.2922,330.1,0.4,0.4,2026-10-03,0948,N20,nominal,2.0NRT,290,5.2,D
31.3480,34.2930,320.1,0.4,0.4,2026-10-03,0948,N20,low,2.0NRT,290,1.2,D
31.2404,34.24105,310.1,0.4,0.4,2026-09-30,1053,N20,nominal,2.0NRT,290,3.1,D
"""


def test_firms_filters_low_confidence_and_outside_strip(tmp_path):
    import strikes
    f = strikes.StrikeFeed(None, tmp_path, http=FakeHttp(FIRMS_CSV))
    assert f.fetch_firms() == 3 * 1      # same CSV for each of the 3 satellites, deduped by id
    assert len(f.events) == 1
    e = next(iter(f.events.values()))
    assert (round(e["lat"], 3), round(e["lon"], 3)) == (31.347, 34.292)
    h = f.hazards(e["time"] + 3600)[0]["properties"]
    assert h["active"] and h["buffer_m"] >= 375
    assert not f.hazards(e["time"] + 49 * 3600)[0]["properties"]["active"], "FIRMS hazards expire after 48 h"


def test_demo_strikes_not_persisted(tmp_path):
    import strikes
    f = strikes.StrikeFeed(None, tmp_path, http=FakeHttp(""))
    f.add_manual(34.3, 31.35, time.time(), source="demo")
    assert strikes.StrikeFeed(None, tmp_path, http=FakeHttp("")).events == {}


def test_gdelt_centroid_is_not_a_hazard():
    """GDELT's 'Gaza (general)' point sits inside the corridor; it must never become a road hazard."""
    import strikes
    assert "gdelt" not in strikes.SOURCES
