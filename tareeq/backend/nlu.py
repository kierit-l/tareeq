"""Understanding SMS / chat messages.

1. Deterministic keyword grammar (works offline, no cost): طريق A B / مغلق A / مفتوح A ...
2. If that fails and ANTHROPIC_API_KEY is set, Claude parses free Arabic text into the same
   structure. Claude only *parses*; verification rules in state.py decide what changes.
"""
import difflib
import json
import os
import re

AR_DIAC = re.compile(r"[ً-ْـ]")


def norm(s: str) -> str:
    s = AR_DIAC.sub("", s or "").strip().lower()
    s = re.sub("[أإآ]", "ا", s)
    s = s.replace("ة", "ه").replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    s = re.sub(r"\s+", " ", s)
    return s


GENERIC = {"مستشفي", "مستشفى", "مجمع", "عياده", "مركز", "مخيم", "معسكر", "دوار", "مدرسه", "جامعه", "سوق",
           "صحي", "طبي", "الطبي", "الطبى", "hospital", "clinic", "camp", "roundabout"}


def core(q):
    """Distinctive part of a place name: 'مستشفى الشفا' -> 'شفا', so generic words can't carry a match."""
    words = [strip_al(w) for w in q.split() if w not in GENERIC]
    return " ".join(words) or q


def strip_al(w):
    return w[2:] if w.startswith("ال") and len(w) > 4 else w


REPORT_WORDS = {
    "blocked": ["مغلق", "مسكر", "مسدود", "مقفل", "سكر", "blocked", "closed"],
    "open": ["مفتوح", "سالك", "فاتح", "open"],
    "degraded": ["صعب", "بطيء", "بطيء", "ركام", "حفر", "حفره", "زحمه", "مزدحم", "slow", "degraded"],
    "foot_only": ["مشي", "مشاه", "للمشاه", "مشي فقط", "foot"],
    "unsafe": ["خطر", "خطير", "قصف", "استهداف", "danger", "unsafe"],
}
ROUTE_WORDS = ["طريق", "route", "من", "كيف اروح", "بدي اروح"]
# walking route: "مشي من الدير الى ناصر", "walk Mawasi to Nasser", "طريق المواصي ناصر مشي". A single place after
# مشي stays a foot_only report ("مشي دوار البحر").
WALK_WORDS = ["مشي", "ماشي", "مشيا", "على الاقدام", "عالاقدام", "walk", "walking", "on foot", "by foot"]
SEPARATORS = r"\s+(?:الى|الي|إلى|لعند|ل|to|->|←|→)\s+|\s*[-،,]\s*"
ALERT_WORDS = ["تنبيه", "نبهني", "alert", "اشتراك"]
HELP_WORDS = ["مساعده", "help", "?", "؟"]
ARRIVED_WORDS = ["وصلت", "arrived"]
FAILED_WORDS = ["ما قدرت", "ماقدرت", "ما زبط", "مازبط", "couldnt", "failed"]
UNSUB_WORDS = ["الغاء", "stop", "وقف"]
PRIVACY_WORDS = ["خصوصيه", "privacy"]


# Known places outside the pilot corridor: say so instead of guessing or asking "from where to where?"
OUT_OF_COVERAGE = {
    "مستشفى الشفاء": ["الشفاء", "الشفا", "مستشفى الشفا", "مجمع الشفاء", "Shifa"],
    "مدينة غزة": ["غزة", "غزه", "مدينة غزه", "Gaza City"],
    "رفح": ["Rafah"], "جباليا": ["مخيم جباليا", "Jabalia"], "بيت لاهيا": ["Beit Lahia"],
    "بيت حانون": ["Beit Hanoun"], "النصيرات": ["مخيم النصيرات", "Nuseirat"], "البريج": ["مخيم البريج", "Bureij"],
    "المغازي": ["مخيم المغازي", "Maghazi"], "الزوايدة": ["Zawaida"], "مستشفى العودة": ["العودة"],
    "المستشفى المعمداني": ["المعمداني", "الأهلي العربي"],
}
_OOC = [(norm(a), name) for name, al in OUT_OF_COVERAGE.items() for a in [name] + al]


def out_of_coverage(text):
    t = " " + norm(text) + " "
    for a, name in sorted(_OOC, key=lambda x: -len(x[0])):
        if f" {a} " in t or f" ال{a} " in t:
            return name
    return None


class Gazetteer:
    def __init__(self, places):
        self.places = places
        self.index = []
        for p in places:
            for n in [p["name"], p.get("en")] + p.get("aliases", []):
                if n:
                    self.index.append((norm(n), p))

    def match(self, text, min_score=0.8):
        q = norm(text)
        if not q:
            return None, 0
        best, score = None, 0.0
        for n, p in self.index:
            if q == n:
                return p, 1.0
            sc = self.score(q, n)
            if sc > score:
                best, score = p, sc
        return (best, score) if score >= min_score else (None, score)

    @staticmethod
    def score(q, n):
        return difflib.SequenceMatcher(None, core(q), core(n)).ratio()

    def top(self, text, k=3):
        """Closest k distinct places, for "did you mean" replies."""
        q = norm(text)
        scored = {}
        for n, p in self.index:
            sc = self.score(q, n)
            if sc > scored.get(p["name"], (0, None))[0]:
                scored[p["name"]] = (sc, p)
        return [p for _, p in sorted(scored.values(), key=lambda x: -x[0])[:k]]

    def any_place(self, text):
        words = text.split()
        for i in range(len(words)):
            for j in range(len(words), i, -1):
                p, _ = self.match(" ".join(words[i:j]), min_score=0.85)
                if p:
                    return p
        return None

    def split_two(self, text):
        """'المواصي مستشفى ناصر' -> (Mawasi, Nasser): try separators, then every split point."""
        parts = [x for x in re.split(SEPARATORS, text.strip()) if x]
        if len(parts) == 2:
            a, sa = self.match(parts[0])
            b, sb = self.match(parts[1])
            if a and b:
                return a, b
        words = text.split()
        best, best_s = None, 0
        for i in range(1, len(words)):
            a, sa = self.match(" ".join(words[:i]))
            b, sb = self.match(" ".join(words[i:]))
            if a and b and sa + sb > best_s:
                best, best_s = (a, b), sa + sb
        return best or (None, None)


def dialect(t):
    """Levantine contractions: عالمستشفى -> المستشفى, للمواصي -> المواصي, ع المواصي -> المواصي."""
    t = re.sub(r"(^|\s)عال", r"\1ال", t)
    t = re.sub(r"(^|\s)لل", r"\1ال", t)
    t = re.sub(r"^(?:على|علي|لعند|عند)\s+", "", t)   # "على ناصر من الدير" -> "ناصر من الدير"
    return re.sub(r"(^|\s)ع\s+", r"\1", t)


def rule_parse(text, gaz: Gazetteer):
    t = norm(text)
    words = t.split()
    if not words:
        return {"intent": "help"}
    if words[0] in HELP_WORDS or t in HELP_WORDS:
        return {"intent": "help"}
    if re.fullmatch(r"\d{1,2}", t):
        return {"intent": "choice", "n": int(t)}
    if words[0] in [norm(w) for w in UNSUB_WORDS]:
        return {"intent": "unsubscribe"}
    if words[0] in [norm(w) for w in PRIVACY_WORDS]:
        return {"intent": "privacy"}
    if words[0] in [norm(w) for w in ARRIVED_WORDS]:
        m = re.search(r"\d{1,3}", t)
        mins = int(m.group()) if m else None
        if re.search(r"ساعه|ساعات|hour", t):
            mins = (mins or 1) * 60
            if "ونص" in t:
                mins += 30
        return {"intent": "arrived", "minutes": mins}
    for kw in FAILED_WORDS:
        k = norm(kw)
        if t.startswith(k):
            rest = re.sub(r"^(اوصل|امر|اعدي|اقطع)\s*", "", t[len(k):].strip())
            rest = re.sub(r"^(من|عند|ع|على)\s+", "", dialect(rest))
            place = gaz.any_place(rest) if rest else None
            return {"intent": "failed", "place": place}
    if words[0] in [norm(w) for w in ALERT_WORDS]:
        a, b = gaz.split_two(" ".join(words[1:]))
        if a and b:
            return {"intent": "alert", "from": a, "to": b}
    rest, walk = strip_walk(t, trailing=False)
    if walk:
        a, b = gaz.split_two(re.sub(r"^من\s+", "", dialect(rest)))
        if a and b:
            return {"intent": "route", "from": a, "to": b, "modes": ["foot"]}
    for state, kws in REPORT_WORDS.items():
        for kw in kws:
            k = norm(kw)
            if t.startswith(k + " "):
                frag = dialect(t[len(k) + 1:]).replace("عند ", "")
                place, s = gaz.match(frag)
                if place:
                    return {"intent": "report", "state": state, "place": place}
                ooc = out_of_coverage(frag)
                if ooc:
                    return {"intent": "out_of_coverage", "clarify": True, "name": ooc}
                return {"intent": "suggest", "clarify": True, "template": k + " {}", "choices": gaz.top(frag)}
    for kw in ROUTE_WORDS:
        k = norm(kw)
        if t.startswith(k + " "):
            rest, walk = strip_walk(dialect(t[len(k) + 1:]))
            foot = {"modes": ["foot"]} if walk else {}
            # "<dest> من <origin>" (e.g. "بدي اروح عالأوروبي من المواصي"): a mid-sentence من marks the origin
            m = re.match(r"^(?:الى\s+|لعند\s+)?(.+?)\s+من\s+(.+)$", rest)
            if m:
                b, sb = gaz.match(m.group(1))
                a, sa = gaz.match(m.group(2))
                if a and b:
                    return {"intent": "route", "from": a, "to": b} | foot
            rest = re.sub(r"^من\s+", "", rest)
            a, b = gaz.split_two(rest)
            if a and b:
                return {"intent": "route", "from": a, "to": b} | foot
            return clarify_route(rest, gaz)
    return None


def strip_walk(t, trailing=True):
    """('walk mawasi to nasser') -> ('mawasi to nasser', True); a walk word may lead or trail."""
    for w in sorted((norm(w) for w in WALK_WORDS), key=len, reverse=True):
        if t.startswith(w + " "):
            return t[len(w) + 1:], True
        if trailing and t.endswith(" " + w):
            return t[: -len(w) - 1], True
    return t, False


def clarify_route(rest, gaz):
    """Never guess O/D: outside coverage -> say so; else offer the closest places, or ask من وين لوين؟"""
    ooc = out_of_coverage(rest)
    if ooc:
        return {"intent": "out_of_coverage", "clarify": True, "name": ooc}
    m = re.match(r"^(.+?)\s+من\s+(.+)$", rest)
    parts = [m.group(2), m.group(1)] if m else [x for x in re.split(SEPARATORS, rest) if x]
    if len(parts) == 2:
        a, _ = gaz.match(parts[0])
        b, _ = gaz.match(parts[1])
        if a and not b:
            return {"intent": "suggest", "clarify": True, "template": f"طريق {a['name']} الى {{}}", "choices": gaz.top(parts[1])}
        if b and not a:
            return {"intent": "suggest", "clarify": True, "template": f"طريق {{}} الى {b['name']}", "choices": gaz.top(parts[0])}
    return {"intent": "ambiguous_od", "clarify": True, "known": gaz.any_place(rest)}


SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["route", "report", "alert", "help", "unknown"]},
        "from_place": {"type": "string", "description": "Exact canonical place name from the list, or empty"},
        "to_place": {"type": "string", "description": "Exact canonical place name from the list, or empty"},
        "report_place": {"type": "string", "description": "Exact canonical place name from the list, or empty"},
        "state": {"type": "string", "enum": ["open", "degraded", "foot_only", "blocked", "unsafe", "none"]},
    },
    "required": ["intent", "from_place", "to_place", "report_place", "state"],
    "additionalProperties": False,
}

SYSTEM = """You parse short SMS messages from residents of Gaza (Palestinian Arabic dialect, sometimes \
English or Arabizi) for a road-status service. Classify the intent:
- route: they want to travel from one place to another
- report: they are reporting a road condition near a place (open, degraded = slow/rubble/craters/crowded, \
foot_only = vehicles cannot pass but walking can, blocked = impassable, unsafe = strike/danger)
- alert: they want notifications about a route
- help / unknown
Map every place to the closest canonical name from this list (residents use nicknames, landmarks, \
misspellings); leave it empty if nothing fits. Never invent places.
Canonical places:
"""


def claude_parse(text, gaz: Gazetteer):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    client = anthropic.Anthropic()
    names = "\n".join(sorted({p["name"] + (f" ({p['en']})" if p.get("en") else "") for p in gaz.places}))
    try:
        resp = client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM + names,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.APIConnectionError:
        return None
    except anthropic.APIStatusError:
        return None
    if resp.stop_reason == "refusal":
        return None
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))

    def place(name):
        if not name:
            return None
        p, _ = gaz.match(re.sub(r"\s*\(.*\)$", "", name), min_score=0.9)
        return p

    out = {"intent": data["intent"], "via": "claude"}
    if data["intent"] in ("route", "alert"):
        out["from"], out["to"] = place(data["from_place"]), place(data["to_place"])
        if not (out["from"] and out["to"]):
            return None
    elif data["intent"] == "report":
        out["place"], out["state"] = place(data["report_place"]), data["state"]
        if not out["place"] or out["state"] == "none":
            return None
    return out


def parse(text, gaz):
    r = rule_parse(text, gaz)
    if r and not r.get("clarify"):
        r["via"] = "rules"
        return r
    c = claude_parse(text, gaz)   # free text the grammar couldn't fully resolve
    if c:
        return c
    if r:
        r["via"] = "rules"
        return r
    return {"intent": "unknown", "via": "none"}
