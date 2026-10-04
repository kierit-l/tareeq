"""Operations layer: operator console (spec 4.6), field reporting for aid workers, strike hazards.

Mounted by main.py with `ops.install(app, globals())`. Everything here plugs into the existing data layer
through two seams only:
  - hazards and approved zones are features in C.nogo (kind + buffer_m), applied with
    S().set_official_unsafe(C.unsafe_segments(active_nogo()), reason)
  - field reports are ordinary S().add_report(..., trusted=True, channel="field")

Rules:
  - Two-person rule (S2): adding or removing a no-go zone, dismissing a strike hazard and broadcasting
    to subscribers each need a second, different operator to approve.
  - Strike hazards only make roads *less* safe, so they apply without approval and expire on their own.
  - Field reporters are aid workers holding an access code issued in the console. Codes are stored
    hashed; a report's GPS fix is used once to find the road and never stored.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from graph import to_utm

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from shapely.geometry import Point, shape

import strikes as strikes_mod

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
FIELD_MAX_AGE_S = 24 * 3600          # queued offline reports older than this are dropped
NOTE_RETENTION_S = 14 * 86400        # same as raw reports (spec section 5)
ZONE_KINDS = {"yellow_line", "evacuation", "military", "uxo", "other"}

# Field report kinds (what an aid worker sees) -> segment state + radius of road affected around the pin.
FIELD_KINDS = {
    "open":       ("open", 0),
    "slow":       ("degraded", 0),     # crowds, carts, queues
    "rubble":     ("degraded", 0),
    "crater":     ("degraded", 0),
    "flood":      ("degraded", 0),
    "foot_only":  ("foot_only", 0),
    "blocked":    ("blocked", 0),
    "checkpoint": ("unsafe", 100),
    "uxo":        ("unsafe", 150),     # unexploded ordnance
    "strike":     ("unsafe", 200),
}

router = APIRouter()
W = {}      # main.py namespace: C, S, R, DEMO, active_nogo, ALERTS, SUBSCRIPTIONS, reset
LOCK = threading.RLock()
DB = None
STRIKES = None


def now_real():
    return time.time()


def sim_now():
    """Store clock (the demo can jump forward); used for anything compared with report ages."""
    return W["S"]().now()


# ---------------- storage ----------------
def open_db(path):
    db = sqlite3.connect(path, check_same_thread=False)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS zones (
        id TEXT PRIMARY KEY, name TEXT, kind TEXT, geometry TEXT, buffer_m REAL, source TEXT,
        status TEXT,            -- pending | active | rejected | pending_removal | removed
        proposed_by TEXT, proposed_at REAL, approved_by TEXT, approved_at REAL,
        removal_by TEXT, removal_at REAL, note TEXT);
    CREATE TABLE IF NOT EXISTS reporters (
        id INTEGER PRIMARY KEY, label TEXT, org TEXT, code_hash TEXT UNIQUE,
        created_by TEXT, created_at REAL, active INTEGER);
    CREATE TABLE IF NOT EXISTS field_notes (
        id INTEGER PRIMARY KEY, seg INTEGER, kind TEXT, note TEXT, reporter TEXT, ts REAL);
    CREATE TABLE IF NOT EXISTS broadcasts (
        id INTEGER PRIMARY KEY, text TEXT, geometry TEXT, status TEXT,
        proposed_by TEXT, proposed_at REAL, approved_by TEXT, approved_at REAL, recipients INTEGER);
    CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts REAL, actor TEXT, action TEXT, detail TEXT);
    """)
    return db


def audit(actor, action, detail=""):
    with LOCK:
        DB.execute("INSERT INTO audit(ts,actor,action,detail) VALUES (?,?,?,?)",
                   (now_real(), actor, action, json.dumps(detail, ensure_ascii=False) if not isinstance(detail, str) else detail))
        DB.commit()


def code_hash(code):
    return hashlib.sha256(("tareeq-field:" + code.strip().upper()).encode()).hexdigest()


def new_code():
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"     # no 0/O/1/I/L: read over a bad phone line
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


# ---------------- hazards in C.nogo ----------------
def apply_hazards(reason):
    """Recompute official-unsafe segments from every active feature in C.nogo."""
    C, S = W["C"], W["S"]
    with LOCK:
        return S().set_official_unsafe(C.unsafe_segments(W["active_nogo"]()), reason)


def zone_feature(row):
    zid, name, kind, geom, buffer_m, source = row[:6]
    props = {"id": zid, "name": name, "name_en": name, "kind": kind, "source": source, "active": True,
             "approved": True}
    if buffer_m:
        props["buffer_m"] = buffer_m
    return {"type": "Feature", "properties": props, "geometry": json.loads(geom)}


def load_active_zones():
    for row in DB.execute("SELECT id,name,kind,geometry,buffer_m,source FROM zones WHERE status IN ('active','pending_removal')"):
        W["C"].nogo[row[0]] = zone_feature(row)


def sync_strikes():
    """Put live strike hazards into C.nogo, expire old ones. Returns True if the hazard set changed."""
    C = W["C"]
    now = sim_now()
    live = {h["properties"]["id"]: h for h in STRIKES.hazards(now)}
    changed = False
    with LOCK:
        for hid, f in list(C.nogo.items()):
            if f["properties"].get("kind") != "strike":
                continue
            if hid not in live:
                del C.nogo[hid]
                changed = True
        for hid, h in live.items():
            old = C.nogo.get(hid)
            if not old or old["properties"].get("active") != h["properties"]["active"]:
                changed = True
            C.nogo[hid] = h
    return changed


def strike_loop():
    interval = max(60, int(os.environ.get("TAREEQ_STRIKES_REFRESH_S", "900")))
    last_fetch = 0
    while True:
        try:
            if os.environ.get("TAREEQ_STRIKES_FETCH", "1") != "0" and now_real() - last_fetch >= interval:
                STRIKES.refresh()
                last_fetch = now_real()
            if sync_strikes():
                apply_hazards("strikes")
                W["ALERTS"].dispatch()
        except Exception as e:      # a feed outage must never take the service down
            print("strike loop:", repr(e))
        time.sleep(60)


# ---------------- auth ----------------
def operators():
    """Operator names (no passwords: the console has no login). Two names are needed for the two-person rule."""
    raw = os.environ.get("TAREEQ_OPERATORS") or "amal,omar"
    return [x.split(":", 1)[0].strip() for x in raw.split(",") if x.strip()]


def operator(x_operator: str = Header("")):
    ops = operators()
    name = x_operator.strip() or ops[0]
    if name not in ops:
        raise HTTPException(401, "unknown operator")
    return name


def field_reporter(code):
    if not code:
        raise HTTPException(401, "access code required")
    row = DB.execute("SELECT id,label,org,active FROM reporters WHERE code_hash=?", (code_hash(code),)).fetchone()
    if not row or not row[3]:
        raise HTTPException(401, "unknown or revoked access code")
    return {"id": row[0], "label": row[1], "org": row[2], "hash": code_hash(code)}


# ---------------- zones (S1/S2) ----------------
class ZoneIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    kind: str
    geometry: dict
    buffer_m: float | None = Field(None, ge=0, le=2000)
    source: str = Field(min_length=2, max_length=200)    # e.g. "OCHA evacuation order 2026-10-03 #123"
    note: str = ""


@router.get("/api/ops/admin/zones")
def zones(op: str = Depends(operator)):
    rows = DB.execute("SELECT id,name,kind,geometry,buffer_m,source,status,proposed_by,proposed_at,approved_by,"
                      "approved_at,removal_by,removal_at,note FROM zones ORDER BY proposed_at DESC").fetchall()
    keys = ["id", "name", "kind", "geometry", "buffer_m", "source", "status", "proposed_by", "proposed_at",
            "approved_by", "approved_at", "removal_by", "removal_at", "note"]
    recorded = [dict(zip(keys, r)) | {"geometry": json.loads(r[3])} for r in rows]
    known = {z["id"] for z in recorded}
    baseline = [{"id": k, "name": f["properties"].get("name"), "kind": f["properties"].get("kind", "other"),
                 "geometry": f["geometry"], "buffer_m": f["properties"].get("buffer_m"),
                 "source": f["properties"].get("source", "nogo.geojson"),
                 "status": "active" if f["properties"].get("active") else "inactive", "baseline": True}
                for k, f in W["C"].nogo.items() if k not in known and f["properties"].get("kind") != "strike"]
    return {"zones": recorded + baseline, "me": op}


@router.post("/api/ops/admin/zones")
def propose_zone(z: ZoneIn, op: str = Depends(operator)):
    if z.kind not in ZONE_KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(ZONE_KINDS)}")
    try:
        g = shape(z.geometry)
        assert g.is_valid and not g.is_empty and g.geom_type in ("Polygon", "MultiPolygon")
    except Exception:
        raise HTTPException(400, "geometry must be a valid GeoJSON Polygon")
    zid = "zone_" + secrets.token_hex(4)
    with LOCK:
        DB.execute("INSERT INTO zones(id,name,kind,geometry,buffer_m,source,status,proposed_by,proposed_at,note) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (zid, z.name, z.kind, json.dumps(z.geometry), z.buffer_m, z.source, "pending", op, now_real(), z.note))
        DB.commit()
    audit(op, "zone_proposed", {"id": zid, "name": z.name, "source": z.source})
    preview = len(W["C"].segments_in({"geometry": z.geometry, "properties": {"kind": z.kind, "buffer_m": z.buffer_m}}))
    return {"id": zid, "status": "pending", "segments_affected": preview,
            "next": "a second operator must approve before it affects routing"}


def _zone(zid):
    r = DB.execute("SELECT id,name,kind,geometry,buffer_m,source,status,proposed_by,removal_by FROM zones WHERE id=?",
                   (zid,)).fetchone()
    if not r:
        raise HTTPException(404, "zone not found")
    return r


@router.post("/api/ops/admin/zones/{zid}/approve")
def approve_zone(zid: str, op: str = Depends(operator)):
    r = _zone(zid)
    status, proposer, remover = r[6], r[7], r[8]
    if status == "pending":
        if proposer == op:
            raise HTTPException(403, "two-person rule: someone other than the proposer must approve")
        with LOCK:
            DB.execute("UPDATE zones SET status='active', approved_by=?, approved_at=? WHERE id=?", (op, now_real(), zid))
            DB.commit()
            W["C"].nogo[zid] = zone_feature(r)
        added, removed = apply_hazards(f"nogo:{zid}")
        audit(op, "zone_approved", {"id": zid, "segments_added": added})
        return {"id": zid, "status": "active", "segments_added": added}
    if status == "pending_removal":
        if remover == op:
            raise HTTPException(403, "two-person rule: someone other than the requester must approve removal")
        with LOCK:
            DB.execute("UPDATE zones SET status='removed' WHERE id=?", (zid,))
            DB.commit()
            W["C"].nogo.pop(zid, None)
        added, removed = apply_hazards(f"nogo:{zid}")
        audit(op, "zone_removed", {"id": zid, "segments_removed": removed})
        return {"id": zid, "status": "removed", "segments_removed": removed}
    raise HTTPException(409, f"nothing to approve (status {status})")


@router.post("/api/ops/admin/zones/{zid}/reject")
def reject_zone(zid: str, op: str = Depends(operator)):
    r = _zone(zid)
    if r[6] == "pending":
        new = "rejected"
    elif r[6] == "pending_removal":
        new = "active"           # removal request withdrawn/rejected: zone stays in force
    else:
        raise HTTPException(409, f"nothing to reject (status {r[6]})")
    with LOCK:
        DB.execute("UPDATE zones SET status=? WHERE id=?", (new, zid))
        DB.commit()
    audit(op, "zone_rejected", {"id": zid, "status": new})
    return {"id": zid, "status": new}


@router.post("/api/ops/admin/zones/{zid}/remove")
def request_removal(zid: str, op: str = Depends(operator)):
    """Lifting a zone makes routes *less* safe, so it needs a second operator, like adding one."""
    if zid in W["C"].nogo and not DB.execute("SELECT 1 FROM zones WHERE id=?", (zid,)).fetchone():
        # a baseline zone from nogo.geojson: record it so the removal has an audit trail
        f = W["C"].nogo[zid]
        with LOCK:
            DB.execute("INSERT INTO zones(id,name,kind,geometry,buffer_m,source,status,proposed_by,proposed_at,"
                       "approved_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (zid, f["properties"].get("name"), f["properties"].get("kind", "other"),
                        json.dumps(f["geometry"]), f["properties"].get("buffer_m"),
                        f["properties"].get("source", "nogo.geojson"), "active", "baseline", now_real(), "baseline"))
            DB.commit()
    r = _zone(zid)
    if r[6] != "active":
        raise HTTPException(409, f"only active zones can be removed (status {r[6]})")
    with LOCK:
        DB.execute("UPDATE zones SET status='pending_removal', removal_by=?, removal_at=? WHERE id=?",
                   (op, now_real(), zid))
        DB.commit()
    audit(op, "zone_removal_requested", {"id": zid})
    return {"id": zid, "status": "pending_removal"}


# ---------------- strikes ----------------
@router.get("/api/ops/strikes")
def strikes_public():
    """Live strike hazards (what routing avoids) + feed status. Public: it's the same data the map shows."""
    return {"type": "FeatureCollection", "features": STRIKES.hazards(sim_now(), include_expired=False),
            "status": STRIKES.status(), "now": sim_now()}


@router.post("/api/ops/admin/strikes/refresh")
def strikes_refresh(op: str = Depends(operator)):
    STRIKES.refresh(force=True)
    if sync_strikes():
        apply_hazards("strikes")
    audit(op, "strikes_refreshed", STRIKES.status())
    return STRIKES.status()


class SimStrikeIn(BaseModel):
    lon: float
    lat: float


@router.post("/api/ops/admin/strikes/simulate")
def strikes_simulate(b: SimStrikeIn, op: str = Depends(operator)):
    if not W["DEMO"]:
        raise HTTPException(403, "simulated strikes are demo-only")
    STRIKES.add_manual(b.lon, b.lat, sim_now(), source="demo", label="ضربة محاكاة (توضيحية)")
    sync_strikes()
    added, _ = apply_hazards("strikes")
    audit(op, "strike_simulated", {"lon": round(b.lon, 3), "lat": round(b.lat, 3)})
    return {"segments_added": added}


@router.post("/api/ops/admin/strikes/{sid}/dismiss")
def strike_dismiss(sid: str, op: str = Depends(operator)):
    """Dismissing a hazard makes routes less safe: first operator requests, a second one confirms."""
    state = STRIKES.dismiss(sid, op)
    if state == "unknown":
        raise HTTPException(404, "no such strike hazard")
    if state == "same_operator":
        raise HTTPException(403, "two-person rule: a different operator must confirm the dismissal")
    if state == "dismissed" and sync_strikes():
        apply_hazards("strikes")
    audit(op, "strike_dismiss_" + state, {"id": sid})
    return {"id": sid, "status": state}


# ---------------- field reporters (R1) ----------------
class ReporterIn(BaseModel):
    label: str = Field(min_length=2, max_length=80)
    org: str = Field("", max_length=80)


@router.get("/api/ops/admin/reporters")
def reporters(op: str = Depends(operator)):
    rows = DB.execute("SELECT id,label,org,created_by,created_at,active FROM reporters ORDER BY id DESC").fetchall()
    counts = dict(DB.execute("SELECT reporter, COUNT(*) FROM field_notes GROUP BY reporter").fetchall())
    hashes = dict(DB.execute("SELECT id, code_hash FROM reporters").fetchall())
    return {"reporters": [{"id": r[0], "label": r[1], "org": r[2], "created_by": r[3], "created_at": r[4],
                           "active": bool(r[5]), "reports": counts.get(hashes[r[0]], 0)} for r in rows]}


@router.post("/api/ops/admin/reporters")
def add_reporter(b: ReporterIn, op: str = Depends(operator)):
    code = new_code()
    with LOCK:
        cur = DB.execute("INSERT INTO reporters(label,org,code_hash,created_by,created_at,active) VALUES (?,?,?,?,?,1)",
                         (b.label, b.org, code_hash(code), op, now_real()))
        DB.commit()
    audit(op, "reporter_added", {"id": cur.lastrowid, "label": b.label, "org": b.org})
    return {"id": cur.lastrowid, "code": code, "note": "shown once; share it privately"}


@router.post("/api/ops/admin/reporters/{rid}/revoke")
def revoke_reporter(rid: int, op: str = Depends(operator)):
    with LOCK:
        n = DB.execute("UPDATE reporters SET active=0 WHERE id=?", (rid,)).rowcount
        DB.commit()
    if not n:
        raise HTTPException(404)
    audit(op, "reporter_revoked", {"id": rid})
    return {"id": rid, "active": False}


# ---------------- review queue (R5) + metrics ----------------
@router.get("/api/ops/admin/queue")
def queue(op: str = Depends(operator)):
    C, S = W["C"], W["S"]
    now = sim_now()
    items = []
    for sid, p in C.seg.items():
        st = S().get(sid)
        reasons = []
        if st.provisional:
            reasons.append("provisional_blocked")
        contested = {k: n for k, n in st.pending.items() if k != st.state}
        if contested and st.source == "verified":
            reasons.append("contested")
        elif st.pending and st.source != "verified":
            reasons.append("awaiting_confirmation")
        if reasons:
            items.append({"seg": sid, "name": p.get("name"), "state": st.state, "conf": round(st.conf, 2),
                          "pending": st.pending, "reasons": reasons, "major": p["major"],
                          "ll": C.node_ll[p["u"]],
                          "age_min": round((now - st.updated) / 60) if st.updated else None})
    if hasattr(S(), "contested"):     # conflicting verified states within 6 h (state.py)
        listed = {x["seg"] for x in items}
        for c in S().contested(hours=24):
            if c["seg"] in listed:
                next(x for x in items if x["seg"] == c["seg"])["conflict"] = c.get("conflict")
                continue
            p = C.seg[c["seg"]]
            items.append({"seg": c["seg"], "name": p.get("name"), "state": c["state"], "conf": round(c["conf"], 2),
                          "pending": c.get("pending", {}), "reasons": ["contested"], "major": p["major"],
                          "conflict": c.get("conflict"), "ll": C.node_ll[p["u"]],
                          "age_min": round((now - c["updated"]) / 60) if c.get("updated") else None})
    order = {"provisional_blocked": 0, "contested": 1, "awaiting_confirmation": 2}
    items.sort(key=lambda x: (min(order[r] for r in x["reasons"]), not x["major"]))
    notes = DB.execute("SELECT seg,kind,note,ts FROM field_notes WHERE ts>? ORDER BY ts DESC LIMIT 50",
                       (now - 24 * 3600,)).fetchall()
    return {"items": items[:200], "total": len(items),
            "field_notes": [{"seg": s, "kind": k, "note": n, "age_min": round((now - t) / 60),
                             "ll": C.node_ll[C.seg[s]["u"]] if s in C.seg else None} for s, k, n, t in notes]}


@router.get("/api/ops/admin/overview")
def overview(op: str = Depends(operator)):
    S = W["S"]
    pending_zones = DB.execute("SELECT COUNT(*) FROM zones WHERE status IN ('pending','pending_removal')").fetchone()[0]
    pending_bc = DB.execute("SELECT COUNT(*) FROM broadcasts WHERE status='pending'").fetchone()[0]
    reporters_n = DB.execute("SELECT COUNT(*) FROM reporters WHERE active=1").fetchone()[0]
    return {"me": op, "operators": operators(), "demo": W["DEMO"], "metrics": S().trip_stats(), "pending_zones": pending_zones,
            "pending_broadcasts": pending_bc, "active_reporters": reporters_n,
            "subscribers": len(W["SUBSCRIPTIONS"]), "official_unsafe_segments": len(S().official_unsafe),
            "strikes": STRIKES.status()}


@router.get("/api/ops/admin/audit")
def audit_log(op: str = Depends(operator)):
    rows = DB.execute("SELECT ts,actor,action,detail FROM audit ORDER BY id DESC LIMIT 200").fetchall()
    return {"audit": [{"ts": t, "actor": a, "action": x, "detail": d} for t, a, x, d in rows]}


# ---------------- broadcasts ----------------
class BroadcastIn(BaseModel):
    text: str = Field(min_length=5, max_length=160)
    geometry: dict | None = None        # only subscribers whose saved route crosses this area; None = everyone


@router.get("/api/ops/admin/broadcasts")
def broadcasts(op: str = Depends(operator)):
    rows = DB.execute("SELECT id,text,status,proposed_by,proposed_at,approved_by,recipients,geometry IS NOT NULL "
                      "FROM broadcasts ORDER BY id DESC LIMIT 50").fetchall()
    keys = ["id", "text", "status", "proposed_by", "proposed_at", "approved_by", "recipients", "has_area"]
    return {"broadcasts": [dict(zip(keys, r)) for r in rows]}


@router.post("/api/ops/admin/broadcasts")
def propose_broadcast(b: BroadcastIn, op: str = Depends(operator)):
    with LOCK:
        cur = DB.execute("INSERT INTO broadcasts(text,geometry,status,proposed_by,proposed_at) VALUES (?,?,?,?,?)",
                         (b.text, json.dumps(b.geometry) if b.geometry else None, "pending", op, now_real()))
        DB.commit()
    audit(op, "broadcast_proposed", {"id": cur.lastrowid, "text": b.text})
    return {"id": cur.lastrowid, "status": "pending"}


@router.post("/api/ops/admin/broadcasts/{bid}/approve")
def approve_broadcast(bid: int, op: str = Depends(operator)):
    r = DB.execute("SELECT text,geometry,status,proposed_by FROM broadcasts WHERE id=?", (bid,)).fetchone()
    if not r:
        raise HTTPException(404)
    text, geom, status, proposer = r
    if status != "pending":
        raise HTTPException(409, f"status {status}")
    if proposer == op:
        raise HTTPException(403, "two-person rule: someone other than the proposer must approve")
    area_segs = set(W["C"].segments_in({"geometry": json.loads(geom), "properties": {"buffer_m": 0}})) if geom else None
    sent = 0
    for sender, sub in list(W["SUBSCRIPTIONS"].items()):
        if sub.get("channel") != "twilio":
            continue
        if area_segs is not None and not (set(sub.get("segs", ())) & area_segs):
            continue
        threading.Thread(target=W["ALERTS"].send, args=(sender, text), daemon=True).start()
        sent += 1
    with LOCK:
        DB.execute("UPDATE broadcasts SET status='sent', approved_by=?, approved_at=?, recipients=? WHERE id=?",
                   (op, now_real(), sent, bid))
        DB.commit()
    audit(op, "broadcast_sent", {"id": bid, "recipients": sent})
    return {"id": bid, "status": "sent", "recipients": sent}


@router.post("/api/ops/admin/broadcasts/{bid}/reject")
def reject_broadcast(bid: int, op: str = Depends(operator)):
    with LOCK:
        n = DB.execute("UPDATE broadcasts SET status='rejected', approved_by=? WHERE id=? AND status='pending'",
                       (op, bid)).rowcount
        DB.commit()
    if not n:
        raise HTTPException(409, "not pending")
    audit(op, "broadcast_rejected", {"id": bid})
    return {"id": bid, "status": "rejected"}


# ---------------- field reporting (aid workers) ----------------
class FieldReportIn(BaseModel):
    code: str
    kind: str
    lon: float | None = None
    lat: float | None = None
    segment: int | None = None
    note: str = Field("", max_length=140)
    age_s: float = Field(0, ge=0)      # seconds since the report was made (offline queue); avoids phone-clock skew


@router.get("/api/ops/field/me")
def field_me(code: str):
    r = field_reporter(code)
    return {"label": r["label"], "org": r["org"], "kinds": list(FIELD_KINDS), "demo": W["DEMO"]}


@router.post("/api/ops/field/report")
def field_report(b: FieldReportIn):
    rep = field_reporter(b.code)
    C, S = W["C"], W["S"]
    if b.kind not in FIELD_KINDS:
        raise HTTPException(400, f"kind must be one of {list(FIELD_KINDS)}")
    if b.age_s > FIELD_MAX_AGE_S:
        return {"accepted": False, "reason": "too_old", "segments": []}
    state, radius = FIELD_KINDS[b.kind]
    if b.segment is not None and b.segment in C.seg and not radius:
        segs = [b.segment]
    elif b.lon is not None and b.lat is not None:
        if radius:
            segs = C.segments_in({"geometry": {"type": "Point", "coordinates": [b.lon, b.lat]},
                                  "properties": {"buffer_m": radius}})[:30]
        else:
            s = C.nearest_segment(b.lon, b.lat, max_m=150)
            segs = [s] if s is not None else []
    elif b.segment is not None and b.segment in C.seg:
        p = C.seg[b.segment]
        lon, lat = C.node_ll[p["u"]]
        segs = C.segments_in({"geometry": {"type": "Point", "coordinates": [lon, lat]},
                              "properties": {"buffer_m": radius}})[:30] or [b.segment]
    else:
        raise HTTPException(400, "need a location or a road segment")
    if not segs:
        raise HTTPException(400, "no road within 150 m of that point")
    ts = sim_now() - b.age_s
    reporter = "field:" + rep["hash"][:16]
    results = [S().add_report(s, state, reporter, trusted=True, channel="field", ts=ts) for s in segs]
    with LOCK:
        DB.execute("INSERT INTO field_notes(seg,kind,note,reporter,ts) VALUES (?,?,?,?,?)",
                   (segs[0], b.kind, b.note.strip(), rep["hash"], (ts // 300) * 300))
        DB.execute("DELETE FROM field_notes WHERE ts < ?", (sim_now() - NOTE_RETENTION_S,))
        DB.commit()
    rejected = sum(1 for r in results if r["ignored_reason"] == "contradicts_official_nogo")
    return {"accepted": True, "kind": b.kind, "state": state, "segments": segs,
            "changed": sum(1 for r in results if r["changed"]), "rejected_official": rejected}


@router.get("/api/ops/field/area")
def field_area(lon: float, lat: float, r: float = 1500):
    """Road states, hazards and recent field reports around a point (small payload for phones).
    Coordinates are rounded to ~1 m; nothing about who reported is returned."""
    C, S = W["C"], W["S"]
    r = max(200, min(r, 4000))
    pt = {"geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": {"buffer_m": r}}
    # nearest first, so a zoomed-out view never drops the roads in the middle (where the report pin is)
    cx, cy = to_utm(lon, lat)
    ids = sorted(C.segments_in(pt), key=lambda sid: (C.seg[sid]["xy"][0] - cx) ** 2 + (C.seg[sid]["xy"][1] - cy) ** 2)[:2500]
    now = sim_now()
    feats = W.setdefault("_feat_by_id", {f["properties"]["id"]: f for f in C.geojson["features"]})
    segs = []
    for sid in ids:
        st = S().get(sid)
        segs.append({"id": sid, "c": [[round(x, 5), round(y, 5)] for x, y in feats[sid]["geometry"]["coordinates"]],
                     "s": st.state, "conf": round(st.conf, 2), "src": st.source[0],
                     "age": round((now - st.updated) / 60) if st.updated else None,
                     "n": st.n_reports, "prov": st.provisional, "name": C.seg[sid].get("name")})
    area = Point(lon, lat).buffer(r / 90000)           # ~degrees; coarse filter for hazards
    hazards = [f for f in C.nogo.values() if f["properties"].get("active") and shape(f["geometry"]).intersects(area)]
    idset = set(ids)
    notes = [{"seg": s, "kind": k, "age": round((now - t) / 60)}
             for s, k, t in DB.execute("SELECT seg,kind,ts FROM field_notes WHERE ts>? ORDER BY ts DESC LIMIT 400",
                                       (now - 12 * 3600,)) if s in idset]
    return {"now": now, "segments": segs, "hazards": hazards, "reports": notes[:100]}


# ---------------- pages ----------------
@router.get("/admin")
def admin_page():
    return FileResponse(FRONTEND / "admin.html")


@router.get("/field")
def field_page():
    return FileResponse(FRONTEND / "field.html")


# ---------------- install ----------------
def install(app, ns):
    """Called at the end of main.py. ns is main's globals()."""
    global DB, STRIKES
    W.update({k: ns[k] for k in ("C", "S", "R", "DEMO", "active_nogo", "ALERTS", "SUBSCRIPTIONS")})
    demo = W["DEMO"]
    DB = open_db(":memory:" if demo else str(ROOT / "data/ops.db"))
    STRIKES = strikes_mod.StrikeFeed(W["C"], cache_dir=ROOT / "data/strikes")
    if demo:
        DB.execute("INSERT INTO reporters(label,org,code_hash,created_by,created_at,active) VALUES (?,?,?,?,?,1)",
                   ("Demo aid worker", "Demo NGO", code_hash("DEMO-0000"), "seed", now_real()))
        DB.commit()
    load_active_zones()
    sync_strikes()
    apply_hazards("startup")

    # main.reset() rebuilds the store from C.nogo, and the demo reset switches every zone except the
    # baseline off: re-apply approved zones and live strikes afterwards.
    orig_reset = ns["reset"]

    def reset_with_ops():
        orig_reset()
        load_active_zones()
        sync_strikes()
        apply_hazards("reset")
    ns["reset"] = reset_with_ops

    app.include_router(router)
    threading.Thread(target=strike_loop, daemon=True, name="strikes").start()
