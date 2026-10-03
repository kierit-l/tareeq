"""SMS / WhatsApp conversation logic shared by /api/sms and /api/twilio.

Per sender we keep only short-lived conversational context, keyed by the daily reporter hash:
the last quoted route (to score "وصلت" and to place "ما قدرت" / "مغلق" reports on the road the
person was actually told to use), and a pending numbered question. Nothing is written to disk.
"""
import math
import time

import sms
from graph import to_utm
from nlu import norm
from state import reporter_hash

RATE_LIMIT_PER_H = 20
ON_ROUTE_M = 600          # a report this close to the sender's quoted route refers to that route
CONTEXT_TTL_S = 6 * 3600


class Conversation:
    def __init__(self, store, router, corridor, gaz, segments_at, subscriptions):
        self.store, self.router = store, router      # callables: the world can be reset
        self.c, self.gaz = corridor, gaz
        self.segments_at = segments_at
        self.subs = subscriptions
        self.last = {}      # sender hash -> {"from", "to", "route", "ts"}
        self.pending = {}   # sender hash -> numbered question
        self.hits = {}      # sender hash -> recent message timestamps
        self.on_unsubscribe = None
        self.channel = "web"

    def reset(self):
        self.last.clear(); self.pending.clear(); self.hits.clear()

    # ---------- entry ----------
    def handle(self, text, sender, parse, channel="web"):
        self.channel = channel
        now = time.time()
        key = reporter_hash(sender, now)
        hits = [t for t in self.hits.get(key, []) if now - t < 3600]
        hits.append(now)
        self.hits[key] = hits
        if len(hits) > RATE_LIMIT_PER_H:
            return {"reply": sms.RATE_LIMITED, "parsed": {"intent": "rate_limited"}}

        p = parse(text)
        pend = self.pending.pop(key, None)
        if p["intent"] == "choice" and pend:
            return self._answer_choice(p["n"], pend, sender, key, parse)

        fn = getattr(self, "_" + p["intent"], None) or self._unknown
        out = fn(p, sender, key)
        out.setdefault("parsed", slim(p))
        return out

    # ---------- intents ----------
    def _route(self, p, sender, key):
        r = self.router().route(p["from"]["node"], p["to"]["node"])
        if r:
            self.last[key] = {"from": p["from"], "to": p["to"], "route": r, "ts": self.store().now()}
        return {"reply": sms.route_reply(p["from"], p["to"], r), "route": r}

    def _alert(self, p, sender, key):
        r = self.router().route(p["from"]["node"], p["to"]["node"])
        segs = {s for l in (r or {}).get("legs", []) for s in l["segments"]}
        self.subs[sender] = {"from": p["from"], "to": p["to"], "segs": segs, "since": self.store().now(),
                             "channel": self.channel}
        return {"reply": sms.fit(f"تم. سننبهك عند تغيّر الطريق {sms.short(p['from']['name'])}←{sms.short(p['to']['name'])}. "
                                 "أرسل إلغاء للإيقاف."), "route": r}

    def _report(self, p, sender, key):
        segs, question = self._report_segments(key, p["place"], p["state"])
        if question:
            return question
        return self._file(segs, p["state"], sender, p["place"])

    def _arrived(self, p, sender, key):
        last = self._last(key)
        if not last:
            return {"reply": sms.NEED_ROUTE}
        r = last["route"]
        if p["minutes"] is None:
            return {"reply": sms.arrived_reply(None, 0, 0)}
        self.store().record_trip(r["p50_min"], r["p85_min"], p["minutes"], route_segs(r), ok=True)
        self.last.pop(key, None)
        return {"reply": sms.arrived_reply(p["minutes"], r["p50_min"], r["p85_min"]), "trip": "ok"}

    def _failed(self, p, sender, key):
        last = self._last(key)
        place = p.get("place")
        res = None
        if last:
            r = last["route"]
            self.store().record_trip(r["p50_min"], r["p85_min"], None, route_segs(r), ok=False)
            self.last.pop(key, None)
            if place:  # A2: a failed trip is also a Blocked report on the quoted leg nearest that place
                seg = self._nearest_on_route(r, place)
                res = self.store().add_report(seg, "blocked", sender, channel="sms")
        elif place:
            segs, question = self._report_segments(key, place, "blocked")
            if question:
                return question
            return self._file(segs, "blocked", sender, place)
        else:
            return {"reply": sms.NEED_ROUTE}
        return {"reply": sms.failed_reply(place["name"] if place else None, res), "trip": "failed"}

    def _unsubscribe(self, p, sender, key):
        if self.on_unsubscribe:
            self.on_unsubscribe(sender)
        self.subs.pop(sender, None)
        return {"reply": sms.UNSUB}

    def _privacy(self, p, sender, key):
        return {"reply": sms.fit(sms.PRIVACY)}

    def _help(self, p, sender, key):
        return {"reply": sms.HELP}

    def _suggest(self, p, sender, key):
        names = [c["name"] for c in p["choices"]]
        self.pending[key] = {"type": "place", "template": p["template"], "options": names}
        return {"reply": sms.numbered("لم نعرف المكان. هل تقصد:", names)}

    def _out_of_coverage(self, p, sender, key):
        return {"reply": sms.fit(f"{p['name']} خارج منطقة الخدمة حالياً (دير البلح – خان يونس – المواصي). "
                                 "نعمل على التوسع.")}

    def _ambiguous_od(self, p, sender, key):
        return {"reply": sms.ASK_OD}

    def _unknown(self, p, sender, key):
        return {"reply": "لم نفهم الرسالة. " + sms.HELP[:110]}

    # ---------- helpers ----------
    def _answer_choice(self, n, pend, sender, key, parse):
        if not 1 <= n <= len(pend["options"]):
            self.pending[key] = pend
            return {"reply": sms.numbered("اختر رقماً:", [o if isinstance(o, str) else o[0] for o in pend["options"]]),
                    "parsed": {"intent": "choice"}}
        if pend["type"] == "place":
            return self.handle(pend["template"].format(pend["options"][n - 1]), sender, parse, self.channel)
        name, segs = pend["options"][n - 1]
        return self._file(segs, pend["state"], sender, pend["place"]) | {"parsed": {"intent": "choice", "street": name}}

    def _report_segments(self, key, place, state):
        """R2: which road does a report at a place mean?"""
        last = self._last(key)
        if last:
            seg = self._nearest_on_route(last["route"], place)
            if self._dist(seg, place) <= ON_ROUTE_M:
                return [seg], None
        self._ring_nodes = None
        touching = self._roundabout(place) or self.segments_at(place["node"])
        streets = {}
        for sid in touching:
            streets.setdefault(self._street_label(sid, place), []).append(sid)
        ring = [k for k in streets if norm(k) == norm(place["name"])]   # the roundabout itself isn't a "street"
        if ring and len(streets) > 1:
            streets["الدوار نفسه"] = streets.pop(ring[0])   # offered last as its own option
        if len(streets) > 1:
            options = sorted(streets.items(), key=lambda kv: (kv[0] == "الدوار نفسه", kv[0]))
            self.pending[key] = {"type": "street", "options": options, "state": state, "place": place}
            return None, {"reply": sms.numbered(f"أي شارع عند {sms.short(place['name'])}؟", [o[0] for o in options])}
        return touching, None

    def _roundabout(self, place):
        """Whole ring (segments named like the place, within 150 m) plus every road leaving it."""
        if place.get("kind") != "roundabout":
            return None
        ring = [sid for sid, p in self.c.seg.items()
                if p["name"] and norm(p["name"]) == norm(place["name"]) and self._dist(sid, place) < 150]
        if not ring:
            return None
        nodes = {n for sid in ring for n in (self.c.seg[sid]["u"], self.c.seg[sid]["v"])}
        exits = [sid for sid, p in self.c.seg.items() if sid not in ring and (p["u"] in nodes or p["v"] in nodes)]
        self._ring_nodes = nodes
        return ring + exits

    def _street_label(self, sid, place):
        p = self.c.seg[sid]
        nodes = getattr(self, "_ring_nodes", None) or {place["node"]}
        near = p["u"] if p["u"] in nodes else (p["v"] if p["v"] in nodes else place["node"])
        far = p["v"] if p["u"] == near else p["u"]
        (x0, y0), (x1, y1) = (place["lon"], place["lat"]), self.c.node_ll[far]
        heading = compass(x1 - x0, y1 - y0)
        if p["name"]:
            return p["name"] if norm(p["name"]) == norm(place["name"]) else f"{p['name']} ({heading})"
        return f"{HIGHWAY_AR.get(p['highway'], 'طريق')} باتجاه {heading}"

    def _file(self, segs, state, sender, place):
        results = [self.store().add_report(s, state, sender, channel="sms") for s in segs]
        res = next((x for x in results if x["changed"]), results[0])
        return {"reply": sms.report_reply(res, place, state), "segments": segs, "results": results}

    def _last(self, key):
        last = self.last.get(key)
        if last and self.store().now() - last["ts"] < CONTEXT_TTL_S:
            return last
        return None

    def _dist(self, seg, place):
        return math.dist(self.c.seg[seg]["xy"], to_utm(place["lon"], place["lat"]))

    def _nearest_on_route(self, r, place):
        return min(route_segs(r), key=lambda s: self._dist(s, place))


HIGHWAY_AR = {"trunk": "طريق رئيسي", "primary": "طريق رئيسي", "secondary": "طريق رئيسي", "tertiary": "طريق",
              "residential": "شارع فرعي", "unclassified": "طريق", "service": "طريق خدمة", "track": "طريق ترابي"}


def compass(dx, dy):
    if abs(dx) > abs(dy):
        return "الشرق" if dx > 0 else "الغرب"
    return "الشمال" if dy > 0 else "الجنوب"


def route_segs(r):
    return [s for l in r["legs"] for s in l["segments"]]


def slim(p):
    out = {}
    for k, v in p.items():
        if isinstance(v, dict):
            out[k] = v["name"]
        elif isinstance(v, list):
            out[k] = [x["name"] if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out
