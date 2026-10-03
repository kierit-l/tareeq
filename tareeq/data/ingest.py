"""Build the corridor road graph for Tareeq.

Inputs (data/raw/):
  osm_roads.json    Overpass `out geom` dump of highways in the corridor bbox
  osm_places.json   Overpass dump of places, hospitals, clinics, roundabouts
  unosat/*.gdb      UNOSAT/UN-Habitat road damage assessment (imagery 2024-05-29)

Outputs (data/build/):
  segments.geojson  ~100-250 m road segments with UNOSAT damage prior
  places.json       gazetteer: Arabic/English names + resident aliases, snapped to graph nodes
  stands.json       informal-transport stands (where trailers/tuk-tuks/carts gather)
"""
import glob
import json
import math
from collections import Counter
from pathlib import Path

import numpy as np
import pyogrio
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import LineString, Point
from shapely.ops import transform

ROOT = Path(__file__).parent
RAW = ROOT / "raw"
OUT = ROOT / "build"
BBOX = (34.24, 31.30, 34.36, 31.43)  # lon_min, lat_min, lon_max, lat_max
MAX_SEG_M = 250

to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32636", always_xy=True).transform
merc_to_utm = Transformer.from_crs("EPSG:3857", "EPSG:32636", always_xy=True).transform

# UNOSAT Classification domain (read from the GDB coded-value domain)
DAMAGE = {1: "crater", 2: "debris_moderate", 3: "debris_severe", 4: "bulldozed", 5: "destroyed", 6: "obstacles"}
DAMAGE_RANK = {"destroyed": 6, "debris_severe": 5, "obstacles": 4, "bulldozed": 3, "crater": 2, "debris_moderate": 1}
DAMAGE_PRIOR = {
    "destroyed": "foot_only", "debris_severe": "foot_only", "obstacles": "foot_only",
    "bulldozed": "degraded", "crater": "degraded", "debris_moderate": "degraded",
}


def load_unosat():
    gdb = glob.glob(str(RAW / "unosat" / "*.gdb"))[0]
    df = pyogrio.read_dataframe(gdb, columns=["Classification", "RoadType"])
    df = df.to_crs("EPSG:4326").cx[BBOX[0]:BBOX[2], BBOX[1]:BBOX[3]]
    geoms, classes = [], []
    for g, c in zip(df.geometry, df.Classification):
        if g is None:
            continue
        g2 = transform(to_utm, g)
        geoms.append(g2)
        classes.append(DAMAGE.get(int(c)) if c == c and c is not None else None)
    print(f"UNOSAT features in bbox: {len(geoms)}  {Counter(classes).most_common()}")
    return geoms, classes


def build_segments():
    ways = [e for e in json.load(open(RAW / "osm_roads.json"))["elements"] if e["type"] == "way"]
    use = Counter(n for w in ways for n in w["nodes"])
    node_xy = {}
    segs = []
    synth = 0
    for w in ways:
        pts = [(g["lon"], g["lat"]) for g in w["geometry"]]
        for nid, p in zip(w["nodes"], pts):
            node_xy[nid] = p
        # split at intersections (nodes shared by >1 way) and way ends
        cut = [0] + [i for i, n in enumerate(w["nodes"][1:-1], 1) if use[n] > 1] + [len(pts) - 1]
        tags = w.get("tags", {})
        for a, b in zip(cut, cut[1:]):
            piece = pts[a:b + 1]
            if len(piece) < 2:
                continue
            line = LineString(piece)
            line_m = transform(to_utm, line)
            n_sub = max(1, math.ceil(line_m.length / MAX_SEG_M))
            ends = [w["nodes"][a]]
            for k in range(1, n_sub):
                synth += 1
                ends.append(-synth)
            ends.append(w["nodes"][b])
            for k in range(n_sub):
                sub = substring(line, line_m.length, k / n_sub, (k + 1) / n_sub)
                for end, pt in ((ends[k], sub.coords[0]), (ends[k + 1], sub.coords[-1])):
                    node_xy.setdefault(end, pt)
                segs.append({
                    "u": ends[k], "v": ends[k + 1], "geom": sub,
                    "len": transform(to_utm, sub).length,
                    "highway": tags.get("highway"),
                    "name": tags.get("name:ar") or tags.get("name"),
                    "name_en": tags.get("name:en"),
                    "osm_way": w["id"],
                })
    return segs, node_xy


def substring(line, length_m, f0, f1):
    from shapely.ops import substring as sub
    return sub(line, f0, f1, normalized=True)


def join_damage(segs):
    geoms, classes = load_unosat()
    tree = STRtree(geoms)
    for s in segs:
        gm = transform(to_utm, s["geom"])
        buf = gm.buffer(12)
        best, assessed = None, False
        for i in tree.query(buf):
            ov = geoms[i].buffer(12).intersection(gm).length / max(gm.length, 1)
            if ov < 0.3:
                continue
            assessed = True
            c = classes[i]
            if c and (best is None or DAMAGE_RANK[c] > DAMAGE_RANK[best]):
                best = c
        s["damage"] = best if best else ("none" if assessed else "not_assessed")
        s["prior"] = DAMAGE_PRIOR.get(best, "unknown")


def largest_component(segs):
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s in segs:
        parent[find(s["u"])] = find(s["v"])
    root = Counter(find(s["u"]) for s in segs).most_common(1)[0][0]
    return [s for s in segs if find(s["u"]) == root]


# Residents' names for places: camps, landmarks, colloquial spellings.
ALIASES = {
    "مجمع ناصر الطبى": ["مستشفى ناصر", "ناصر", "مجمع ناصر", "Nasser"],
    "مستشفى شهداء الأقصى": ["مستشفى الأقصى", "شهداء الأقصى", "الأقصى", "Al-Aqsa Hospital"],
    "مستشفى غزة الأوروبي": ["الأوروبي", "المستشفى الأوروبي", "European Hospital"],
    "المواصي": ["مواصي", "مواصي خانيونس", "Mawasi"],
    "دير البلح": ["الدير", "دير البلح البلد", "Deir al-Balah"],
    "مخيم خان يونس": ["مخيم خانيونس", "معسكر خانيونس", "Khan Younis Camp"],
    "دوار بني سهيلا": ["بني سهيلا", "Bani Suheila roundabout"],
    "دوار السنية": ["السنية"],
    "القرارة": ["قرارة", "Qarara"],
    "جامعة الأقصى": ["الجامعة", "Al-Aqsa University"],
    "مخيّم دير البلح": ["مخيم الدير", "مخيم دير البلح", "معسكر الدير"],
    "مدينة حمد": ["حمد", "أبراج حمد", "Hamad City"],
}
EXTRA_PLACES = [  # landmarks residents use that are missing from OSM's place layer
    {"name": "خان يونس", "en": "Khan Younis", "lon": 34.3063, "lat": 31.3440, "kind": "town", "aliases": ["خانيونس", "خان يونس البلد", "Khan Younis"]},
    {"name": "مستشفى الصليب الأحمر الميداني", "en": "ICRC field hospital (Rafah/Mawasi)", "lon": 34.2556, "lat": 31.3190, "kind": "hospital", "aliases": ["الصليب الأحمر", "ICRC"]},
]


def build_places(node_xy, nodes_in_graph):
    els = json.load(open(RAW / "osm_places.json"))["elements"]
    ids = np.array(sorted(nodes_in_graph))
    xy = np.array([to_utm(*node_xy[i]) for i in ids])
    places = []
    seen = set()
    for e in els:
        t = e.get("tags", {})
        name = t.get("name:ar") or t.get("name")
        if not name or name in seen:
            continue
        lat = e.get("lat") or e.get("center", {}).get("lat")
        lon = e.get("lon") or e.get("center", {}).get("lon")
        kind = t.get("amenity") or t.get("place") or ("roundabout" if t.get("junction") else "place")
        seen.add(name)
        places.append({"name": name, "en": t.get("name:en"), "lon": lon, "lat": lat, "kind": kind,
                       "aliases": ALIASES.get(name, [])})
    places += [p for p in EXTRA_PLACES if p["name"] not in seen]
    for p in places:
        x, y = to_utm(p["lon"], p["lat"])
        d = np.hypot(xy[:, 0] - x, xy[:, 1] - y)
        j = int(d.argmin())
        p["node"] = int(ids[j])
        p["snap_m"] = round(float(d[j]))
    return [p for p in places if p["snap_m"] < 1500]


def build_stands(places):
    """Stands are where informal transport gathers: markets, roundabouts, hospitals, camp edges.
    Wait times and fares are ILLUSTRATIVE seeds until driver reports arrive."""
    by = {p["name"]: p for p in places}
    spec = [
        ("دوار بني سهيلا", ["trailer", "tuktuk", "cart"], 12, 4),
        ("مجمع ناصر الطبى", ["trailer", "tuktuk"], 8, 4),
        ("خان يونس", ["trailer", "tuktuk", "cart"], 10, 4),
        ("المواصي", ["trailer", "tuktuk", "cart"], 15, 5),
        ("دير البلح", ["trailer", "tuktuk", "cart"], 10, 4),
        ("مستشفى شهداء الأقصى", ["trailer", "tuktuk"], 9, 4),
        ("القرارة", ["tuktuk", "cart"], 18, 5),
        ("دوار السنية", ["tuktuk", "cart"], 14, 4),
        ("مستشفى غزة الأوروبي", ["tuktuk"], 20, 5),
        ("مخيم خان يونس", ["tuktuk", "cart"], 12, 4),
        ("جامعة الأقصى", ["trailer", "tuktuk"], 12, 4),
        ("مدينة حمد", ["tuktuk", "cart"], 16, 5),
    ]
    stands = []
    for name, modes, wait, fare in spec:
        p = by.get(name)
        if not p:
            print("  stand place missing:", name)
            continue
        stands.append({"id": f"st{len(stands)+1}", "name": f"موقف {name}", "place": name,
                       "node": p["node"], "lon": p["lon"], "lat": p["lat"], "modes": modes,
                       "wait_median_min": wait, "base_fare_ils": fare})
    return stands


def main():
    OUT.mkdir(exist_ok=True)
    segs, node_xy = build_segments()
    print(f"raw segments: {len(segs)}")
    segs = largest_component(segs)
    print(f"segments in main component: {len(segs)}")
    join_damage(segs)
    print("damage:", Counter(s["damage"] for s in segs).most_common())
    feats = []
    for i, s in enumerate(segs):
        feats.append({"type": "Feature", "id": i, "geometry": {
            "type": "LineString", "coordinates": [[round(x, 6), round(y, 6)] for x, y in s["geom"].coords]},
            "properties": {"id": i, "u": s["u"], "v": s["v"], "len": round(s["len"], 1),
                           "highway": s["highway"], "name": s["name"], "name_en": s["name_en"],
                           "damage": s["damage"], "prior": s["prior"]}})
    json.dump({"type": "FeatureCollection", "features": feats}, open(OUT / "segments.geojson", "w"),
              ensure_ascii=False, separators=(",", ":"))
    nodes = {s["u"] for s in segs} | {s["v"] for s in segs}
    places = build_places(node_xy, nodes)
    json.dump(places, open(OUT / "places.json", "w"), ensure_ascii=False, indent=1)
    stands = build_stands(places)
    json.dump(stands, open(OUT / "stands.json", "w"), ensure_ascii=False, indent=1)
    print(f"places: {len(places)}  stands: {len(stands)}")


if __name__ == "__main__":
    main()
