"""Tareeq API: one data layer, served to the web map, the chat/SMS simulator and Twilio."""
import base64
import hashlib
import hmac
import os
import random
from pathlib import Path
from xml.sax.saxutils import escape

import networkx as nx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import sms
from graph import Corridor
from nlu import Gazetteer, parse
from alerts import Alerts
from conversation import Conversation
from router import Router
from state import STATES, StateStore

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Tareeq — Gaza Transit Info")
app.add_middleware(GZipMiddleware, minimum_size=1000)   # segments.geojson is ~2.3 MB raw; small payloads matter on bad links
C = Corridor()
GAZ = Gazetteer(C.places)
SIGNALS = {"fuel_index": 1.0}
WORLD = {}
DEMO = os.environ.get("TAREEQ_DEMO", "1") == "1"
TRUSTED_REPORTERS = set(filter(None, os.environ.get("TAREEQ_TRUSTED", "").split(",")))
if DEMO:
    SUBSCRIPTIONS = {}   # phone -> saved route; demo keeps it in memory
else:
    from subscriptions import PersistentSubscriptions
    if not os.environ.get("TAREEQ_SUBS_KEY"):
        raise RuntimeError("TAREEQ_DEMO=0 requires TAREEQ_SUBS_KEY (encrypts subscriber phone numbers)")
    SUBSCRIPTIONS = PersistentSubscriptions(str(Path(__file__).resolve().parent.parent / "data/subscriptions.db"),
                                            os.environ["TAREEQ_SUBS_KEY"], lambda name: GAZ.match(name, 1.0)[0])


def active_nogo():
    return [k for k, f in C.nogo.items() if f["properties"].get("active")]


def seed_reports(store, router):
    """Simulate a morning of driver + rider reports along the corridors drivers actually run.
    Demo data only: illustrates the pipeline; replaced by real reports in a pilot."""
    rng = random.Random(42)
    now = store.now()
    G = nx.Graph()
    for sid, p in C.seg.items():
        G.add_edge(p["u"], p["v"], seg=sid, len=p["len"] * (0.6 if p["major"] else 1.0))
    stands = C.stands
    driven = set()
    for i, a in enumerate(stands):
        for b in stands[i + 1:]:
            try:
                path = nx.shortest_path(G, a["node"], b["node"], weight="len")
            except nx.NetworkXNoPath:
                continue
            driven.update(G.edges[u, v]["seg"] for u, v in zip(path, path[1:]))
    drivers = [f"driver{i}" for i in range(600)]
    trusted = [f"trusted{i}" for i in range(8)]
    for sid in driven:
        if sid in store.official_unsafe:
            continue
        prior = C.seg[sid]["prior"]
        r = rng.random()
        if prior == "foot_only":
            state = "degraded" if r < 0.6 else "foot_only"   # many cleared since 2024
        elif prior == "degraded":
            state = "degraded" if r < 0.7 else "open"
        else:
            state = "open" if r < 0.85 else "degraded"
        if r > 0.985:
            state = "blocked"
        n = rng.choice([2, 2, 3, 4])
        age_h = rng.uniform(0.2, 3.0)
        for k, d in enumerate(rng.sample(drivers, n)):
            store.add_report(sid, state, d, channel="seed", ts=now - (age_h + k * rng.uniform(0, 0.5)) * 3600)
        if rng.random() < 0.05:
            store.add_report(sid, state, rng.choice(trusted), trusted=True, channel="seed",
                             ts=now - rng.uniform(1, 6) * 3600)
    # second pass: drivers also report the roads they actually choose today
    for i, a in enumerate(stands):
        for b in stands[i + 1:]:
            r = router.route(a["node"], b["node"], ["tuktuk"])
            for leg in (r or {}).get("legs", []):
                for sid in leg["segments"]:
                    if store.get(sid).source in ("verified", "official"):
                        continue
                    driven.add(sid)
                    state = "degraded" if C.seg[sid]["prior"] != "unknown" else "open"
                    age_h = rng.uniform(0.3, 3)
                    for k, d in enumerate(rng.sample(drivers, rng.choice([2, 3]))):
                        store.add_report(sid, state, d, channel="seed", ts=now - (age_h + k * 0.2) * 3600)
    store.events.clear()
    return len(driven)


DB_PATH = Path(__file__).resolve().parent.parent / "data/tareeq.db"


def reset():
    """Demo: in-memory store reseeded with simulated reports. Production (TAREEQ_DEMO=0): on-disk
    store, never seeded, and a fixed TAREEQ_SALT so reporter hashes survive restarts."""
    if DEMO:
        store = StateStore(C.seg, C.unsafe_segments(active_nogo()))
    else:
        if not os.environ.get("TAREEQ_SALT"):
            raise RuntimeError("TAREEQ_DEMO=0 requires TAREEQ_SALT")
        store = StateStore(C.seg, C.unsafe_segments(active_nogo()), db_path=str(DB_PATH))
    router = Router(C, store, SIGNALS, demo=DEMO)
    WORLD.update(store=store, router=router)
    if DEMO:
        n = seed_reports(store, router)
        print(f"seeded reports on {n} segments (simulated)")


SIGNALS["fuel_index"] = 1.0
reset()
CONV = Conversation(lambda: WORLD["store"], lambda: WORLD["router"], C, GAZ,
                    lambda node: segments_at(node), SUBSCRIPTIONS)
ALERTS = Alerts(lambda: WORLD["store"], lambda: WORLD["router"], SUBSCRIPTIONS)
CONV.on_unsubscribe = ALERTS.unsubscribe


@app.middleware("http")
async def push_alerts(request, call_next):
    """Any write may change a saved route: push SMS alerts (capped, background send)."""
    response = await call_next(request)
    if request.method == "POST" and request.url.path.startswith("/api/"):
        ALERTS.dispatch()
    return response


def S():
    return WORLD["store"]


def R():
    return WORLD["router"]


# ---------------- API ----------------
@app.get("/api/segments")
def segments():
    return FileResponse(Path(__file__).resolve().parent.parent / "data/build/segments.geojson",
                        media_type="application/geo+json")


@app.get("/api/states")
def states():
    """Compact state table: id -> [state_idx, conf%, age_min|-1, n_reports, source, pending]."""
    now = S().now()
    out = {}
    for sid in C.seg:
        st = S().get(sid)
        out[sid] = [STATES.index(st.state), round(st.conf * 100),
                    round((now - st.updated) / 60) if st.updated else -1,
                    st.n_reports, st.source[0], sum(st.pending.values())]
    return {"now": now, "states": STATES, "data": out}


@app.get("/api/places")
def places():
    return {"places": [{k: p[k] for k in ("name", "en", "lon", "lat", "kind", "node", "aliases")} for p in C.places],
            "stands": C.stands}


@app.get("/api/nogo")
def nogo():
    return {"type": "FeatureCollection", "features": list(C.nogo.values())}


def find_place(q):
    p, _ = GAZ.match(q)
    if not p:
        raise HTTPException(404, f"unknown place: {q}")
    return p


@app.get("/api/route")
def route(frm: str, to: str, modes: str = "", accessible: bool = False):
    a, b = find_place(frm), find_place(to)
    r = R().route(a["node"], b["node"], modes.split(",") if modes else None, accessible)
    return {"from": a, "to": b, "route": r, "sms": sms.route_reply(a, b, r), "signals": SIGNALS}


class ReportIn(BaseModel):
    segment: int | None = None
    place: str | None = None
    lon: float | None = None
    lat: float | None = None
    state: str
    reporter: str = "web-anon"
    trusted: bool = False


@app.post("/api/report")
def report(r: ReportIn):
    segs = []
    if r.segment is not None:
        segs = [r.segment]
    elif r.lon is not None:
        s = C.nearest_segment(r.lon, r.lat)
        segs = [s] if s is not None else []
    elif r.place:
        segs = segments_at(find_place(r.place)["node"])
    if not segs:
        raise HTTPException(400, "no road segment found for report")
    # trusted status is granted server-side (registered reporter ids); the demo toggle only works in DEMO mode
    trusted = r.trusted and (DEMO or r.reporter in TRUSTED_REPORTERS)
    results = [S().add_report(s, r.state, r.reporter, trusted, channel="app") for s in segs]
    return {"segments": segs, "results": results}


def segments_at(node):
    """Segments meeting at a place's node. Prefer the main roads there; a junction report covers them all."""
    touching = sorted((sid for sid, p in C.seg.items() if node in (p["u"], p["v"])),
                      key=lambda sid: (not C.seg[sid]["major"], -C.seg[sid]["len"], sid))
    major = [sid for sid in touching if C.seg[sid]["major"]]
    return (major or touching)[:4]


class SmsIn(BaseModel):
    text: str
    sender: str = "+970-demo"


def handle_sms(text, sender, channel="web"):
    return CONV.handle(text, sender, lambda t: parse(t, GAZ), channel)


def slim(p):
    return {k: (v["name"] if isinstance(v, dict) else v) for k, v in p.items()}


@app.post("/api/sms")
def sms_in(m: SmsIn):
    out = handle_sms(m.text, m.sender)
    out["chars"] = len(out["reply"])
    return out


@app.post("/api/twilio")
async def twilio(request: Request):
    """Twilio SMS/WhatsApp webhook. Phone numbers are only used as an input to the daily hash."""
    form = dict(await request.form())
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if token:  # https://www.twilio.com/docs/usage/security#validating-requests
        payload = str(request.url) + "".join(k + form[k] for k in sorted(form))
        expected = base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
        if not hmac.compare_digest(expected, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(403, "bad signature")
    reply = handle_sms(form.get("Body", ""), form.get("From", ""), channel="twilio")["reply"]
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(reply)}</Message></Response>'
    return Response(xml, media_type="application/xml")


@app.get("/api/alerts")
def alerts(sender: str = "+970-demo"):
    """'What changed' for a subscriber (pull; SMS subscribers are pushed by ALERTS.dispatch)."""
    return ALERTS.collect(sender) or {"alerts": []}


@app.get("/api/changes")
def changes(since: float = 0):
    return {"events": [e for e in S().events if e["ts"] > since][-200:], "now": S().now()}


class ClockIn(BaseModel):
    hours: float


@app.post("/api/clock")
def clock(c: ClockIn):
    if not DEMO:
        raise HTTPException(403, "clock is demo-only")
    S().advance(c.hours)
    return {"now": S().now(), "offset_h": S().clock_offset / 3600}


class SignalsIn(BaseModel):
    fuel_index: float


@app.post("/api/signals")
def signals(s: SignalsIn):
    SIGNALS["fuel_index"] = max(0.5, min(3.0, s.fuel_index))
    return SIGNALS


class NogoIn(BaseModel):
    active: bool


@app.post("/api/nogo/{nid}")
def toggle_nogo(nid: str, n: NogoIn):
    if not DEMO:
        raise HTTPException(403, "no-go polygons come from the OCHA/UN feed in production")
    if nid not in C.nogo:
        raise HTTPException(404)
    C.nogo[nid]["properties"]["active"] = n.active
    added, removed = S().set_official_unsafe(C.unsafe_segments(active_nogo()), f"nogo:{nid}")
    return {"active": active_nogo(), "segments_added": added, "segments_removed": removed}


@app.get("/api/metrics")
def metrics():
    """Are we accurate? Share of confirmed trips within the quoted P85 (target 80 %) and failed trips (target < 5 %)."""
    return S().trip_stats() | {"demo": DEMO}


@app.post("/api/reset")
def reset_api():
    if not DEMO:
        raise HTTPException(403, "reset is demo-only")
    CONV.reset()
    for k, f in C.nogo.items():
        f["properties"]["active"] = k == "eastern_zone"
    SIGNALS["fuel_index"] = 1.0
    SUBSCRIPTIONS.clear()
    reset()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/sw.js")
def service_worker():
    """Served from the root so the worker's scope covers the page, the API and the tiles."""
    return FileResponse(FRONTEND / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


# ---------------- basemap ----------------
# One self-hosted vector basemap for the whole Strip (Protomaps OSM build, z0-14, ~4.4 MB, ODbL), so phones never
# hit a third-party tile server and the service worker can keep the entire map offline. Rebuild with:
#   pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles data/build/gaza.pmtiles \
#     --bbox=34.20,31.21,34.58,31.60 --maxzoom=14
BASEMAP = Path(__file__).resolve().parent.parent / "data/build/gaza.pmtiles"


@app.get("/map/gaza.pmtiles")
def basemap():
    return FileResponse(BASEMAP, media_type="application/vnd.pmtiles")   # FileResponse handles Range requests


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

import ops  # noqa: E402  operator console, field reports, strike hazards (see ops.py)
ops.install(app, globals())
