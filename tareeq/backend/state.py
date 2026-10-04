"""Segment state store: verified reports + confidence decay + safety overrides.

Rules (from the product spec):
- A crowd report changes a segment only after verification: 1 trusted reporter, or >=2
  independent reporters agreeing within the decay window.
- Confidence decays exponentially; fast for volatile states (open, unsafe), slow for
  physical ones (blocked, foot_only). Stale Open drifts to Unknown, never stays Open.
- Official no-go polygons (OCHA/UN) always win. Reports claiming a road inside them is
  passable are discarded. Crowd reports may make a road *less* safe, never more.
- Burst outliers (one reporter, many far-apart segments in minutes) are ignored.
- Privacy: reporter ids are salted daily-rotating hashes; timestamps rounded to 5 min;
  raw reports purged after 14 days.
"""
import hashlib
import math
import os
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

STATES = ["open", "degraded", "foot_only", "blocked", "unsafe", "unknown"]
TAU_H = {"open": 6, "degraded": 24, "foot_only": 72, "blocked": 168, "unsafe": 8}
MIN_CONF = 0.25          # below this a verified state lapses
PRIOR_CONF = 0.2         # UNOSAT satellite prior (imagery 2024) - weak evidence
RETENTION_S = 14 * 86400
OUTLIER_WINDOW_S = 600
OUTLIER_SPREAD_M = 1500
MAX_KMH = 80
PROVISIONAL_H = 6
CONFLICT_H = 6
EVENT_KEEP_S = 14 * 86400
SALT = os.environ.get("TAREEQ_SALT") or os.urandom(16).hex()  # production requires TAREEQ_SALT (see main.py)


def reporter_hash(raw_id: str, now: float) -> str:
    """Daily-rotating salted hash; enough to count independent reporters, useless for tracking."""
    day = int(now // 86400)
    return hashlib.sha256(f"{SALT}:{day}:{raw_id}".encode()).hexdigest()[:16]


class EventLog(list):
    """State-change log behind 'what changed' and alerts. Write-through to SQLite so a restart
    doesn't drop changes that haven't been alerted yet (only meaningful with an on-disk DB)."""

    def __init__(self, db, lock, since):
        self.db, self.lock = db, lock
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            ts REAL, seg INTEGER, from_state TEXT, to_state TEXT, reason TEXT)""")
        rows = db.execute("SELECT ts, seg, from_state, to_state, reason FROM events WHERE ts >= ? ORDER BY ts",
                          (since,)).fetchall()
        super().__init__({"ts": t, "seg": g, "from": f, "to": to, "reason": r} for t, g, f, to, r in rows)

    def append(self, e):
        super().append(e)
        with self.lock:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (e["ts"], e["seg"], e["from"], e["to"], e["reason"]))
            self.db.commit()

    def clear(self):
        super().clear()
        with self.lock:
            self.db.execute("DELETE FROM events")
            self.db.commit()


@dataclass
class SegState:
    state: str
    conf: float
    source: str            # verified | prior | official | none
    updated: float | None  # ts of newest supporting report
    n_reports: int
    trusted: bool
    pending: dict          # state -> count of unverified reports
    provisional: bool = False  # one fresh, unconfirmed Blocked report: route around it if possible
    conflict: tuple = ()       # other verified states seen within CONFLICT_H of the winning one


class StateStore:
    def __init__(self, segments, unsafe_segments, db_path=":memory:"):
        self.seg = segments                    # id -> properties (len, prior, centroid xy, ...)
        self.official_unsafe = set(unsafe_segments)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY, seg INTEGER, state TEXT, reporter TEXT, trusted INTEGER,
            ts REAL, channel TEXT, ignored TEXT)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS r_seg ON reports(seg, ts)")
        # trip outcomes measure the quoted ranges (target: 80 % inside). No coordinates, no reporter id.
        self.db.execute("""CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY, ts REAL, p50 INTEGER, p85 INTEGER, actual INTEGER, segs TEXT, ok INTEGER)""")
        self.clock_offset = 0.0
        self.events = EventLog(self.db, self.lock, time.time() - EVENT_KEEP_S)
        self._cache = {}
        self._cache_key = None

    # ---- clock (demo can jump forward) ----
    def now(self):
        return time.time() + self.clock_offset

    def advance(self, hours):
        self.clock_offset += hours * 3600
        self._cache_key = None

    # ---- ingest ----
    def add_report(self, seg_id, state, raw_reporter, trusted=False, channel="app", ts=None):
        assert state in STATES[:5], state
        now = self.now() if ts is None else ts
        ts = (now // 300) * 300
        rep = reporter_hash(raw_reporter, now)
        ignored = None
        if seg_id in self.official_unsafe and state != "unsafe":
            ignored = "contradicts_official_nogo"
        elif self._is_burst(rep, seg_id, ts):
            ignored = "outlier_burst"
        before = self.get(seg_id)
        with self.lock:
            self.db.execute("INSERT INTO reports(seg,state,reporter,trusted,ts,channel,ignored) VALUES (?,?,?,?,?,?,?)",
                            (seg_id, state, rep, int(trusted), ts, channel, ignored))
            self.db.commit()
        self._cache.pop(seg_id, None)
        after = self.get(seg_id)
        changed = before.state != after.state
        if changed:
            self.events.append({"ts": self.now(), "seg": seg_id, "from": before.state, "to": after.state,
                                "reason": "verified_reports"})
        return {"accepted": ignored is None, "ignored_reason": ignored, "changed": changed,
                "before": before.state, "after": after.state, "verified": after.source == "verified",
                "pending": after.pending}

    def _is_burst(self, rep, seg_id, ts):
        """Implausible movement: one reporter at places that imply > MAX_KMH within the window."""
        rows = self.db.execute("SELECT seg, ts FROM reports WHERE reporter=? AND ts BETWEEN ? AND ?",
                               (rep, ts - OUTLIER_WINDOW_S, ts + OUTLIER_WINDOW_S)).fetchall()
        x0, y0 = self.seg[seg_id]["xy"]
        jumps = 0
        for s, t in rows:
            d = math.dist(self.seg[s]["xy"], (x0, y0))
            if d > OUTLIER_SPREAD_M and d / max(abs(ts - t), 300) * 3.6 > MAX_KMH:
                jumps += 1
        return jumps >= 2

    def contested(self, hours=24):
        """Segments an operator should look at: an unconfirmed closure, or verified reports that
        disagree within CONFLICT_H. Most recent evidence first."""
        segs = [s for (s,) in self.db.execute(
            "SELECT DISTINCT seg FROM reports WHERE ignored IS NULL AND ts >= ?", (self.now() - hours * 3600,))]
        out = []
        for sid in segs:
            st = self.get(sid)
            if st.provisional or st.conflict:
                out.append({"seg": sid, "state": st.state, "conf": round(st.conf, 2), "provisional": st.provisional,
                            "conflict": list(st.conflict), "pending": st.pending, "updated": st.updated})
        return sorted(out, key=lambda x: -(x["updated"] or 0))

    def purge(self):
        with self.lock:
            self.db.execute("DELETE FROM events WHERE ts < ?", (self.now() - EVENT_KEEP_S,))
            self.db.execute("DELETE FROM reports WHERE ts < ?", (self.now() - RETENTION_S,))
            self.db.commit()

    # ---- evaluate ----
    def get(self, seg_id) -> SegState:
        now = self.now()
        key = int(now // 60)
        if self._cache_key != key:
            self._cache, self._cache_key = {}, key
        if seg_id in self._cache:
            return self._cache[seg_id]
        st = self._evaluate(seg_id, now)
        self._cache[seg_id] = st
        return st

    def _evaluate(self, seg_id, now):
        st, pending_newest = self._evaluate_core(seg_id, now)
        b = pending_newest.get("blocked")
        if b and now - b < PROVISIONAL_H * 3600 and b > (st.updated or 0) and st.state not in ("blocked", "unsafe"):
            st.provisional = True
        return st

    def _evaluate_core(self, seg_id, now):
        if seg_id in self.official_unsafe:
            return SegState("unsafe", 1.0, "official", None, 0, False, {}), {}
        rows = self.db.execute(
            "SELECT state, reporter, trusted, ts FROM reports WHERE seg=? AND ignored IS NULL AND ts<=? AND ts>=?",
            (seg_id, now, now - 3 * TAU_H["blocked"] * 3600)).fetchall()
        by_state = defaultdict(list)
        for state, rep, trusted, ts in rows:
            if now - ts <= 3 * TAU_H[state] * 3600:
                by_state[state].append((rep, trusted, ts))
        best, pending, pending_newest, verified = None, {}, {}, []
        for state, reps in by_state.items():
            # keep each reporter's latest report only
            latest = {}
            for rep, trusted, ts in reps:
                if rep not in latest or ts > latest[rep][1]:
                    latest[rep] = (trusted, ts)
            n = len(latest)
            any_trusted = any(t for t, _ in latest.values())
            newest = max(ts for _, ts in latest.values())
            # crowd reports may always make a road *less* safe: one Unsafe report is enough
            if not (any_trusted or n >= 2 or state == "unsafe"):
                pending[state] = n
                pending_newest[state] = newest
                continue
            conf0 = min(0.95, (0.8 if any_trusted or state == "unsafe" else 0.55) + 0.1 * (n - 1))
            conf = conf0 * math.exp(-(now - newest) / (TAU_H[state] * 3600))
            cand = SegState(state, conf, "verified", newest, n, any_trusted, {})
            verified.append((state, newest))
            # newest verified evidence wins; safety-critical states win ties
            if best is None or newest > best.updated or (
                    newest == best.updated and STATES.index(state) > STATES.index(best.state)):
                best = cand
        if best and best.conf >= MIN_CONF:
            best.pending = pending
            best.conflict = tuple(sorted({st for st, t in verified
                                          if st != best.state and best.updated - t <= CONFLICT_H * 3600}))
            return best, pending_newest
        prior = self.seg[seg_id]["prior"]
        if prior != "unknown":
            return SegState(prior, PRIOR_CONF, "prior", None, 0, False, pending), pending_newest
        # stale or absent evidence -> Unknown (never silently Open)
        return SegState("unknown", 0.0, "none", best.updated if best else None, 0, False, pending), pending_newest

    # ---- trip outcomes (A1/A2) ----
    def record_trip(self, p50, p85, actual, segs, ok):
        with self.lock:
            self.db.execute("INSERT INTO trips(ts,p50,p85,actual,segs,ok) VALUES (?,?,?,?,?,?)",
                            ((self.now() // 300) * 300, p50, p85, actual, ",".join(map(str, segs)), int(ok)))
            self.db.commit()

    def trip_stats(self):
        rows = self.db.execute("SELECT p50, p85, actual, ok FROM trips").fetchall()
        done = [r for r in rows if r[3]]
        le85 = sum(1 for p50, p85, a, _ in done if a is not None and a <= p85)
        le50 = sum(1 for p50, p85, a, _ in done if a is not None and a <= p50)
        share = lambda k: round(k / len(done), 2) if done else None
        # calibration: <= P85 should be 0.80-0.90 and <= P50 0.40-0.60 (catches ranges padded too wide)
        return {"trips": len(rows), "completed": len(done),
                "within_p85_share": share(le85), "within_p50_share": share(le50),
                "failed_share": round((len(rows) - len(done)) / len(rows), 2) if rows else None}  # target < 0.05

    def snapshot(self, ids):
        return {i: self.get(i) for i in ids}

    def set_official_unsafe(self, seg_ids, reason):
        new = set(seg_ids)
        added, removed = new - self.official_unsafe, self.official_unsafe - new
        before = {s: self.get(s).state for s in added | removed}
        self.official_unsafe = new
        self._cache_key = None
        for s in added | removed:
            after = self.get(s).state
            if after != before[s]:
                self.events.append({"ts": self.now(), "seg": s, "from": before[s], "to": after, "reason": reason})
        return len(added), len(removed)
