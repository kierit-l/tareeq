"""Recent-strike hazards from public and partner data.

There is no free public feed of precise, recent strike locations in Gaza (researched 3 Oct 2026):
  - NASA FIRMS VIIRS thermal anomalies: real coordinates (375 m pixels), ~3 h latency, free. Only sees
    fires burning at a satellite pass, so it misses most strikes and also catches non-strike fires.
    -> used as HAZARDS (buffered 450 m, 48 h), dismissable by two operators.
  - GDELT 2.0 events: every 15 min, free, but ~92 % of Gaza events sit on one "Gaza (general)" centroid
    inside our corridor. Buffering it would close Deir al-Balah on every headline.
    -> used ONLY as a Strip-wide activity signal, never as a road hazard.
  - ACLED: weekly at best and licence-restricted -> not usable for a 24-72 h window.
  - A partner feed (NGO security unit, ACLED partner export...) as GeoJSON points -> HAZARDS.
Field reports of strikes from aid workers go straight to segment states (see ops.py) and are the
fastest source we have.

Env:
  FIRMS_MAP_KEY             optional; small area API instead of the global 24 h CSVs (no key needed)
  TAREEQ_FIRMS=0            disable FIRMS
  TAREEQ_GDELT=0            disable the GDELT activity signal
  TAREEQ_STRIKES_FETCH=0    no network fetches at all (tests, air-gapped demo)
  TAREEQ_STRIKES_URL        partner GeoJSON FeatureCollection of Points; properties: time (epoch or ISO),
                            precision_m (optional), id (optional), label (optional)
"""
import csv
import io
import json
import os
import threading
import time
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from shapely.geometry import Point, Polygon

# Approximate Gaza Strip outline (lon, lat). Used to drop detections in Egypt/Israel; ~300 m accuracy.
STRIP = Polygon([
    (34.2197, 31.3245), (34.2670, 31.2190), (34.3700, 31.2960), (34.3950, 31.3700),
    (34.4240, 31.4220), (34.4950, 31.4980), (34.5680, 31.5930), (34.4900, 31.5960),
])
BBOX = (34.20, 31.20, 34.58, 31.61)

SOURCES = {   # buffer around the point, how long it stays a hazard
    "firms":   {"buffer_m": 450, "ttl_h": 48, "label": "حريق رصده القمر الصناعي (احتمال قصف)",
                "label_en": "Satellite-detected fire (possible strike)"},
    "partner": {"buffer_m": 250, "ttl_h": 48, "label": "ضربة أبلغ عنها شريك", "label_en": "Strike reported by partner"},
    "manual":  {"buffer_m": 250, "ttl_h": 24, "label": "ضربة", "label_en": "Strike"},
    "demo":    {"buffer_m": 250, "ttl_h": 24, "label": "ضربة محاكاة (توضيحية)", "label_en": "Simulated strike (demo)"},
}
FIRMS_CSV = ("https://firms.modaps.eosdis.nasa.gov/data/active_fire/{d}/csv/{p}_VIIRS_C2_Global_24h.csv")
FIRMS_SATS = [("suomi-npp-viirs-c2", "SUOMI"), ("noaa-20-viirs-c2", "J1"), ("noaa-21-viirs-c2", "J2")]
FIRMS_AREA = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{src}/{w},{s},{e},{n}/2"
FIRMS_AREA_SRCS = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]
GDELT_LAST = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_CENTROIDS = {(31.4167, 34.3333), (31.425074, 34.373398)}   # "Gaza (general)" / country centroid


def in_strip(lon, lat):
    return STRIP.contains(Point(lon, lat))


def parse_time(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e12 else 1)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


class StrikeFeed:
    def __init__(self, corridor, cache_dir: Path, http=None):
        self.c = corridor
        self.cache = Path(cache_dir)
        self.http = http or httpx.Client(timeout=60, follow_redirects=True,
                                         headers={"User-Agent": "Tareeq/0.2 (humanitarian road-safety service)"})
        self.lock = threading.Lock()
        self.events = {}            # id -> {id, source, lon, lat, time, precision_m, confidence, label}
        self.dismiss_req = {}       # id -> operator who asked
        self.dismissed = set()
        self.last = {}              # source -> {ok, at, n, error}
        self.gdelt = deque(maxlen=200)   # (file_ts, n_violent_events_in_strip, sample_urls)
        self.gdelt_seen = set()
        self._load_cache()

    # ---- persistence: hazards survive a restart ----
    def _load_cache(self):
        f = self.cache / "events.json"
        if f.exists():
            d = json.loads(f.read_text())
            self.events = {e["id"]: e for e in d.get("events", [])}
            self.dismissed = set(d.get("dismissed", []))

    def _save_cache(self):
        self.cache.mkdir(parents=True, exist_ok=True)
        tmp = self.cache / "events.json.tmp"
        real = [e for e in self.events.values() if e["source"] != "demo"]    # simulated strikes never persist
        tmp.write_text(json.dumps({"events": real, "dismissed": sorted(self.dismissed)}))
        tmp.replace(self.cache / "events.json")

    def _add(self, e):
        with self.lock:
            self.events[e["id"]] = e

    # ---- sources ----
    def _due(self, name, every_s):
        return time.time() - self.last.get(name, {}).get("at", 0) >= every_s

    def refresh(self, force=False):
        # the keyless global CSVs are several MB each and FIRMS only updates per satellite pass
        firms_every = 1800 if os.environ.get("FIRMS_MAP_KEY") else 3 * 3600
        if os.environ.get("TAREEQ_FIRMS", "1") != "0" and (force or self._due("firms", firms_every)):
            self._run("firms", self.fetch_firms)
        if os.environ.get("TAREEQ_STRIKES_URL"):
            self._run("partner", self.fetch_partner)
        if os.environ.get("TAREEQ_GDELT", "1") != "0":
            self._run("gdelt", self.fetch_gdelt)
        self._prune()
        self._save_cache()

    def _run(self, name, fn):
        try:
            n = fn()
            self.last[name] = {"ok": True, "at": time.time(), "n": n}
        except Exception as ex:
            self.last[name] = {"ok": False, "at": time.time(), "error": repr(ex)[:200]}

    def _firms_rows(self):
        key = os.environ.get("FIRMS_MAP_KEY")
        if key:
            for src in FIRMS_AREA_SRCS:
                url = FIRMS_AREA.format(key=key, src=src, w=BBOX[0], s=BBOX[1], e=BBOX[2], n=BBOX[3])
                r = self.http.get(url)
                r.raise_for_status()
                yield from csv.DictReader(io.StringIO(r.text))
            return
        for d, p in FIRMS_SATS:     # global 24 h files (a few MB each), streamed and filtered
            with self.http.stream("GET", FIRMS_CSV.format(d=d, p=p)) as r:
                r.raise_for_status()
                lines = r.iter_lines()
                header = next(lines).split(",")
                for line in lines:
                    row = dict(zip(header, line.split(",")))
                    try:
                        lat, lon = float(row["latitude"]), float(row["longitude"])
                    except (KeyError, ValueError):
                        continue
                    if BBOX[1] <= lat <= BBOX[3] and BBOX[0] <= lon <= BBOX[2]:
                        yield row

    def fetch_firms(self):
        n = 0
        for row in self._firms_rows():
            lat, lon = float(row["latitude"]), float(row["longitude"])
            conf = (row.get("confidence") or "").lower()[:1]      # VIIRS: l / n / h
            if conf == "l" or not in_strip(lon, lat):
                continue
            hhmm = row["acq_time"].zfill(4)
            t = datetime.strptime(f"{row['acq_date']} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc).timestamp()
            sid = f"strike:firms:{row.get('satellite', '')}:{row['acq_date']}:{hhmm}:{lat:.4f}:{lon:.4f}"
            self._add({"id": sid, "source": "firms", "lon": lon, "lat": lat, "time": t, "precision_m": 375,
                       "confidence": {"n": "nominal", "h": "high"}.get(conf, conf), "frp": row.get("frp")})
            n += 1
        return n

    def fetch_partner(self):
        r = self.http.get(os.environ["TAREEQ_STRIKES_URL"])
        r.raise_for_status()
        n = 0
        for i, f in enumerate(r.json().get("features", [])):
            g, p = f.get("geometry") or {}, f.get("properties") or {}
            if g.get("type") != "Point":
                continue
            lon, lat = g["coordinates"][:2]
            t = parse_time(p.get("time"))
            if t is None or not in_strip(lon, lat):
                continue
            sid = f"strike:partner:{p.get('id', f'{t:.0f}:{lat:.4f}:{lon:.4f}')}"
            self._add({"id": sid, "source": "partner", "lon": lon, "lat": lat, "time": t,
                       "precision_m": p.get("precision_m"), "label": p.get("label")})
            n += 1
        return n

    def fetch_gdelt(self):
        """Count violent events (CAMEO root 18-20) geocoded inside the Strip in the newest 15-min file."""
        last = self.http.get(GDELT_LAST).text.split("\n")[0].split()
        url = last[-1]
        if url in self.gdelt_seen:
            return 0
        self.gdelt_seen.add(url)
        z = zipfile.ZipFile(io.BytesIO(self.http.get(url).content))
        n, urls = 0, []
        for row in csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8", errors="replace"),
                              delimiter="\t"):
            try:
                if row[28] not in ("18", "19", "20"):
                    continue
                lat, lon = float(row[56]), float(row[57])
            except (IndexError, ValueError):
                continue
            if in_strip(lon, lat) or (round(lat, 4), round(lon, 4)) in GDELT_CENTROIDS:
                n += 1
                if len(urls) < 3:
                    urls.append(row[60])
        ts = datetime.strptime(url.rsplit("/", 1)[1][:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
        self.gdelt.append((ts, n, urls))
        return n

    def add_manual(self, lon, lat, t, source="manual", label=None):
        sid = f"strike:{source}:{t:.0f}:{lat:.4f}:{lon:.4f}"
        self._add({"id": sid, "source": source, "lon": lon, "lat": lat, "time": t, "precision_m": 50, "label": label})
        self._save_cache()
        return sid

    def _prune(self, keep_h=7 * 24):
        cutoff = time.time() - keep_h * 3600
        with self.lock:
            for k in [k for k, e in self.events.items() if e["time"] < cutoff]:
                del self.events[k]
                self.dismissed.discard(k)

    # ---- two-person dismissal ----
    def dismiss(self, sid, operator):
        if sid not in self.events:
            return "unknown"
        asked = self.dismiss_req.get(sid)
        if asked is None:
            self.dismiss_req[sid] = operator
            return "requested"
        if asked == operator:
            return "same_operator"
        self.dismissed.add(sid)
        self.dismiss_req.pop(sid, None)
        self._save_cache()
        return "dismissed"

    # ---- output ----
    def hazards(self, now, include_expired=True):
        out = []
        with self.lock:
            evs = list(self.events.values())
        for e in sorted(evs, key=lambda e: -e["time"]):
            cfg = SOURCES.get(e["source"], SOURCES["manual"])
            expires = e["time"] + cfg["ttl_h"] * 3600
            active = e["time"] <= now + 600 and now < expires and e["id"] not in self.dismissed
            if not active and not include_expired:
                continue
            buffer_m = max(cfg["buffer_m"], e.get("precision_m") or 0)
            out.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [e["lon"], e["lat"]]},
                        "properties": {"id": e["id"], "kind": "strike", "source": e["source"], "time": e["time"],
                                       "expires": expires, "active": active, "buffer_m": buffer_m,
                                       "precision_m": e.get("precision_m"), "confidence": e.get("confidence"),
                                       "name": e.get("label") or cfg["label"], "name_en": cfg["label_en"],
                                       "dismiss_requested_by": self.dismiss_req.get(e["id"])}})
        return out

    def status(self):
        now = time.time()
        recent = [(t, n, u) for t, n, u in self.gdelt if now - t < 24 * 3600]
        return {"sources": self.last, "events": len(self.events),
                "gdelt_24h": {"violent_event_reports": sum(n for _, n, _ in recent), "files": len(recent),
                              "sample_urls": [u for _, _, us in recent[-3:] for u in us][:5],
                              "note": "news-derived; Strip-wide signal only, not used for routing"}}
