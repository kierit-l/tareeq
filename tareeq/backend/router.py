"""Multi-modal router over a layered graph.

Layers: foot, cart, tuktuk, trailer. You can only *board* a vehicle at a stand (where informal
transport gathers); you can step off anywhere. Edge passability and speed depend on the
segment's current state; low confidence and Unknown add routing cost (not time) so the
router prefers well-evidenced roads but still finds a way when nothing else exists.

Trip time = wait + in-vehicle + walking, reported as a P50-P85 range from a Monte Carlo over
per-leg speed and wait uncertainty (wider when evidence is weak).
"""
import math
import random
from datetime import datetime, timedelta, timezone

import networkx as nx
import numpy as np

MODES = ["foot", "cart", "tuktuk", "trailer"]
VEHICLES = MODES[1:]
# km/h by mode and state; None = impassable
SPEED = {
    "foot":    {"open": 4.5, "degraded": 3.5, "foot_only": 2.8, "unknown": 4.0},
    "cart":    {"open": 8.0, "degraded": 6.0, "unknown": 7.0},
    "tuktuk":  {"open": 18.0, "degraded": 9.0, "unknown": 12.0},
    "trailer": {"open": 22.0, "degraded": 9.0, "unknown": 14.0},
}
SIGMA = {"open": 0.15, "degraded": 0.35, "foot_only": 0.3, "unknown": 0.5}  # lognormal spread
UNKNOWN_COST = 2.5
PROVISIONAL_COST = 5.0      # one unconfirmed Blocked report: only if there is no alternative
WEAK_SHARE_MAX_HIGH = 0.2   # >20 % of length on satellite prior / Unknown can't be "high" confidence
OUTLINE_MIN_RUN_M = 150    # shorter stretches (a roundabout, a jog between two streets) fold into a neighbour
ALIGHT_MIN = 0.5
TRANSFER_PENALTY_MIN = 4
GAZA_TZ = timezone(timedelta(hours=3))
MODE_AR = {"foot": "مشي", "cart": "عربة", "tuktuk": "توكتوك", "trailer": "مقطورة"}


def hour_factor(mode, hour):
    if mode == "foot":
        return 1.0
    if 8 <= hour < 14:
        return 1.25       # crowded roads around aid distribution / markets
    if hour >= 18 or hour < 6:
        return 1.1
    return 1.0


class Router:
    def __init__(self, corridor, store, signals, demo=False):
        self.demo = demo
        self.c = corridor
        self.store = store
        self.signals = signals    # {"fuel_index": 1.0, ...}
        self.G = nx.DiGraph()
        self.name_en = {street(p["name"]): p["name_en"] for p in corridor.seg.values() if p["name"] and p["name_en"]}
        self.stand_at = {}
        for s in corridor.stands:
            self.stand_at[s["node"]] = s
        for sid, p in corridor.seg.items():
            u, v = p["u"], p["v"]
            for mode in MODES:
                if mode == "trailer" and not p["major"]:
                    continue  # car-towed trailers need wider roads
                for a, b in ((u, v), (v, u)):
                    key = ((a, mode), (b, mode))
                    if not self.G.has_edge(*key) or self.G.edges[key]["len"] > p["len"]:
                        self.G.add_edge(*key, seg=sid, len=p["len"], mode=mode)
        nodes = {n for n, _ in list(self.G.nodes)}
        for n in nodes:
            for mode in VEHICLES:
                if self.G.has_node((n, mode)):
                    self.G.add_edge((n, mode), (n, "foot"), alight=True, mode="foot")
        for node, s in self.stand_at.items():
            for mode in s["modes"]:
                if self.G.has_node((node, mode)):
                    self.G.add_edge((node, "foot"), (node, mode), board=s["id"], mode=mode)

    # ---- costs ----
    def _hour(self):
        return datetime.fromtimestamp(self.store.now(), GAZA_TZ).hour

    def wait_median(self, stand, mode, hour):
        w = stand["wait_median_min"] * self.signals["fuel_index"] ** 1.2
        if mode == "trailer":
            w *= 1.3   # trailers leave when full
        if 6 <= hour < 9 or 14 <= hour < 17:
            w *= 0.8
        if hour >= 19 or hour < 6:
            w *= 2.0
        return w

    def _edge_minutes(self, d, hour, accessible=False):
        if d.get("alight"):
            return ALIGHT_MIN, ALIGHT_MIN, None
        if d.get("board"):
            s = next(x for x in self.c.stands if x["id"] == d["board"])
            w = self.wait_median(s, d["mode"], hour)
            return w, w + TRANSFER_PENALTY_MIN, None
        st = self.store.get(d["seg"])
        sp = SPEED[d["mode"]].get(st.state)
        if sp is None:
            return None, None, st
        minutes = d["len"] / 1000 / sp * 60 * hour_factor(d["mode"], hour)
        cost = minutes * (1 + 0.6 * (1 - min(st.conf, 1.0)))
        if st.state == "unknown":
            cost *= UNKNOWN_COST
        if st.provisional:
            cost *= PROVISIONAL_COST
        if accessible and d["mode"] == "foot" and st.state in ("degraded", "foot_only"):
            cost *= 6
        return minutes, cost, st

    def route(self, from_node, to_node, modes=None, accessible=False):
        modes = set(modes or MODES) | {"foot"}
        hour = self._hour()

        def weight(a, b, d):
            if d["mode"] not in modes:
                return None
            _, cost, _ = self._edge_minutes(d, hour, accessible)
            return cost

        src, dst = (from_node, "foot"), (to_node, "foot")
        try:
            path = nx.dijkstra_path(self.G, src, dst, weight=weight)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        return self._describe(path, hour)

    # ---- explain ----
    def _describe(self, path, hour):
        legs, cur, steps = [], None, []
        for a, b in zip(path, path[1:]):
            d = self.G.edges[a, b]
            if d.get("alight"):
                cur = None
                continue
            if d.get("board"):
                s = next(x for x in self.c.stands if x["id"] == d["board"])
                cur = {"mode": d["mode"], "stand": s, "wait": self.wait_median(s, d["mode"], hour),
                       "segs": [], "minutes": 0.0, "len": 0.0, "from_node": a[0]}
                legs.append(cur)
                continue
            if cur is None or cur["mode"] != d["mode"]:
                cur = {"mode": d["mode"], "stand": None, "wait": 0.0, "segs": [], "minutes": 0.0,
                       "len": 0.0, "from_node": a[0]}
                legs.append(cur)
            minutes, _, st = self._edge_minutes(d, hour)
            steps.append((d["seg"], st, a[0], b[0]))
            cur["segs"].append((d["seg"], st))
            cur["minutes"] += minutes
            cur["len"] += d["len"]
            cur["to_node"] = b[0]
        legs = [l for l in legs if l["segs"]]
        p50, p85 = self._monte_carlo(legs, hour)

        all_states = [(sid, st) for l in legs for sid, st in l["segs"]]
        total_len = sum(self.c.seg[s]["len"] for s, _ in all_states) or 1
        conf = sum(self.c.seg[s]["len"] * min(st.conf, 1) for s, st in all_states) / total_len
        unknown_m = sum(self.c.seg[s]["len"] for s, st in all_states if st.state == "unknown")
        weak_m = sum(self.c.seg[s]["len"] for s, st in all_states if st.source in ("prior", "none"))
        prov_m = sum(self.c.seg[s]["len"] for s, st in all_states if st.provisional)
        label = "high" if conf >= 0.6 else ("medium" if conf >= 0.35 else "low")
        if label == "high" and weak_m / total_len > WEAK_SHARE_MAX_HIGH:
            label = "medium"
        verified = [st for _, st in all_states if st.source == "verified"]
        now = self.store.now()
        newest = max((st.updated for st in verified), default=None)
        oldest_main = min((st.updated for st in verified), default=None)
        reporters = max((st.n_reports for st in verified), default=0)
        counts = {}
        for s, st in all_states:
            counts[st.state] = counts.get(st.state, 0) + self.c.seg[s]["len"]

        out_legs = []
        for l in legs:
            fare = None
            if l["mode"] != "foot":
                km = l["len"] / 1000
                fare = max(1, round(l["stand"]["base_fare_ils"] * (1 + km / 6) * self.signals["fuel_index"]))
            out_legs.append({
                "mode": l["mode"], "mode_ar": MODE_AR[l["mode"]],
                "stand": l["stand"]["name"] if l["stand"] else None,
                "wait_min": round(l["wait"]), "ride_min": round(l["minutes"]),
                "km": round(l["len"] / 1000, 1), "fare_ils": fare,
                "from": self.node_ll(l["from_node"]), "to": self.node_ll(l["to_node"]),
                "segments": [s for s, _ in l["segs"]],
            })
        return {
            "p50_min": p50, "p85_min": p85,
            "range_text": f"{p50}–{p85}",
            "total_km": round(total_len / 1000, 1),
            "walk_km": round(sum(l["len"] for l in legs if l["mode"] == "foot") / 1000, 1),
            "fare_ils": sum(l["fare_ils"] or 0 for l in out_legs),
            "confidence": round(conf, 2),
            "confidence_label": label,
            "unknown_share": round(unknown_m / total_len, 2),
            "weak_evidence_share": round(weak_m / total_len, 2),
            "provisional_share": round(prov_m / total_len, 2),
            "demo": self.demo,
            "state_share": {k: round(v / total_len, 2) for k, v in counts.items()},
            "newest_report_min": round((now - newest) / 60) if newest else None,
            "oldest_report_min": round((now - oldest_main) / 60) if oldest_main else None,
            "max_reporters": reporters,
            "legs": out_legs,
            "outline": self._outline(steps),
            "hour": hour,
        }

    def _outline(self, steps):
        """Street-by-street summary for SMS: consecutive segments on the same street become one run;
        short runs fold into the previous one so the outline names only the streets a walker follows."""
        runs = []
        for sid, st, a, b in steps:
            p = self.c.seg[sid]
            name = street(p["name"])
            if not runs or runs[-1]["name"] != name:
                runs.append({"name": name, "name_en": self.name_en.get(name), "highway": p["highway"],
                             "m": 0.0, "states": {}, "from": self.node_ll(a)})
            r = runs[-1]
            r["m"] += p["len"]
            r["states"][st.state] = r["states"].get(st.state, 0) + p["len"]
            r["to"] = self.node_ll(b)
        merged = []
        for i, r in enumerate(runs):
            short = r["m"] < OUTLINE_MIN_RUN_M and len(runs) > 1
            if merged and (short or merged[-1]["name"] == r["name"]):
                _absorb(merged[-1], r)
            elif short and i + 1 < len(runs):
                _absorb(runs[i + 1], r, before=True)
            else:
                merged.append(r)
        for r in merged:
            r["m"] = round(r["m"])
            r["states"] = {k: round(v / r["m"], 2) for k, v in r["states"].items()} if r["m"] else {}
        return merged

    def node_ll(self, n):
        return self.c.node_ll[n]

    def _monte_carlo(self, legs, hour, n=400):
        rng = np.random.default_rng(7)
        total = np.zeros(n)
        for l in legs:
            # one shared factor per leg: delays on a corridor are correlated
            ride = np.zeros(n)
            if l["minutes"]:
                weights = {}
                for sid, st in l["segs"]:
                    k = st.state if st.state in SIGMA else "unknown"
                    weights[k] = weights.get(k, 0) + self.c.seg[sid]["len"]
                tot = sum(weights.values())
                sigma = sum(SIGMA[k] * w for k, w in weights.items()) / tot
                ride = l["minutes"] * rng.lognormal(0.05, sigma, n)
            wait = l["wait"] * rng.lognormal(0, 0.6, n) if l["wait"] else 0
            total += ride + wait
        p50, p85 = np.percentile(total, [50, 85])
        r5 = lambda x: int(5 * math.ceil(x / 5))
        return r5(p50), max(r5(p85), r5(p50) + 5)


def street(name):
    """'شارع صلاح الدين' and 'صلاح الدين' are the same street; OSM tags both."""
    return name.split(" (")[0].removeprefix("شارع ").strip() if name else None


def _absorb(into, r, before=False):
    into["m"] += r["m"]
    for k, v in r["states"].items():
        into["states"][k] = into["states"].get(k, 0) + v
    if before:
        into["from"] = r["from"]
    else:
        into["to"] = r["to"]
