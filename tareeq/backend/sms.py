"""Compact replies for SMS / WhatsApp (target <= 160 chars).

Arabic by default. The web simulator asks for English (LANG="en") so the demo can be presented in English;
real SMS (Twilio) always gets Arabic.
"""
from contextvars import ContextVar

LANG = ContextVar("lang", default="ar")
NAME_EN = {}   # Arabic place / stand name -> English, filled by main from the gazetteer


def en():
    return LANG.get() == "en"


def tr(a, e):
    return e if en() else a


AR_DIGITS = str.maketrans("0123456789.", "٠١٢٣٤٥٦٧٨٩٫")
STATE_AR = {"open": "مفتوح", "degraded": "صعب", "foot_only": "مشاة فقط", "blocked": "مغلق",
            "unsafe": "خطر", "unknown": "غير معروف"}
CONF_AR = {"high": "ثقة عالية", "medium": "ثقة متوسطة", "low": "ثقة ضعيفة"}
STATE_EN = {"open": "open", "degraded": "degraded", "foot_only": "foot only", "blocked": "blocked",
            "unsafe": "unsafe", "unknown": "unknown"}
CONF_EN = {"high": "high confidence", "medium": "medium confidence", "low": "low confidence"}
MODE_EN = {"foot": "walk", "cart": "cart", "tuktuk": "tuk-tuk", "trailer": "trailer"}


def state_name(s):
    return (STATE_EN if en() else STATE_AR)[s]
MAX = 160


def ar(x):
    return str(x) if en() else str(x).translate(AR_DIGITS)


def short(name):
    if en():
        return NAME_EN.get(name, name)
    for pre in ("موقف ", "مستشفى ", "مجمع "):
        name = name.replace(pre, "")
    return name


def age_ar(minutes):
    if minutes is None:
        return tr("لا بلاغات حديثة", "no recent reports")
    if minutes < 60:
        return tr(f"قبل {ar(minutes)}د", f"{minutes}m ago")
    return tr(f"قبل {ar(round(minutes / 60))}س", f"{round(minutes / 60)}h ago")


def route_reply(a, b, r):
    if r is None:
        return fit(f"{short(a['name'])}{arrow()}{short(b['name'])}: " + tr(
            "لا يوجد طريق آمن معروف الآن. لا تخاطر، اتبع أوامر الإخلاء الرسمية.",
            "no known safe route right now. Don't take risks; follow official evacuation orders."))
    modes = []
    for l in r["legs"]:
        m = MODE_EN[l["mode"]] if en() else l["mode_ar"]
        if not modes or modes[-1] != m:
            modes.append(m)
    first_ride = next((l for l in r["legs"] if l["stand"]), None)
    dominant = max(r["state_share"].items(), key=lambda kv: kv[1])
    parts = [
        f"{short(a['name'])}{arrow()}{short(b['name'])}:",
        "+".join(modes),
        f"{ar(r['p50_min'])}–{ar(r['p85_min'])}" + tr("د", "min"),
    ]
    if r["fare_ils"]:
        parts.append(f"~{ar(r['fare_ils'])}₪")
    if first_ride:
        parts.append(tr(f"اركب من {short(first_ride['stand'])}", f"board at {short(first_ride['stand'])}"))
    if r["walk_km"] >= 0.3:
        parts.append(tr(f"مشي {ar(r['walk_km'])}كم", f"walk {r['walk_km']}km"))
    conf = (CONF_EN if en() else CONF_AR)[r["confidence_label"]]
    tail = f"· {state_name(dominant[0])} {ar(round(dominant[1]*100))}{tr('٪', '%')} · {conf} · {age_ar(r['newest_report_min'])}"
    if r.get("provisional_share"):
        tail += tr(" · بلاغ إغلاق غير مؤكد", " · unconfirmed closure report")
    elif r["unknown_share"] >= 0.2:
        tail += tr(" · أجزاء غير مؤكدة", " · parts unconfirmed")
    if r.get("demo"):
        tail += tr(" · تجريبي", " · demo")
    msg = " ".join(parts) + " " + tail
    return fit(msg)


SIDE_ST = {"track": ("طريق ترابي", "dirt track"), "service": ("طريق خدمة", "service rd")}
HEADING = {"N": "شمالاً", "S": "جنوباً", "E": "شرقاً", "W": "غرباً"}


def walk_reply(a, b, r):
    """Walking route as a street outline: 'Walk A→B: 70–85min, 4.4km. side st N 0.8km › Gush Katif 1.2km › …'"""
    if r is None:
        return route_reply(a, b, None)
    od = f"{short(a['name'])}{arrow()}{short(b['name'])}"
    head = tr(f"مشي {od}: {ar(r['p50_min'])}–{ar(r['p85_min'])}د، {ar(r['total_km'])}كم.",
              f"Walk {od}: {r['p50_min']}–{r['p85_min']}min, {r['total_km']}km.")
    steps = [street_step(s) for s in r["outline"]]
    tail = [(CONF_EN if en() else CONF_AR)[r["confidence_label"]], age_ar(r["newest_report_min"])]
    if r.get("provisional_share"):
        tail.append(tr("بلاغ إغلاق غير مؤكد", "unconfirmed closure report"))
    # the outline matters more than the trailer: drop tail items first, then middle streets (first/last stay)
    cut = 0
    while True:
        shown = steps if not cut else steps[:1] + ["…"] + steps[1 + cut:]
        msg = head + " " + tr(" ← ", " › ").join(shown) + "".join(" · " + t for t in tail)
        if len(msg) <= MAX:
            return msg
        if tail:
            tail.pop()
        elif len(steps) - cut > 2:
            cut += 1
        else:
            return fit(msg)


def street_step(s):
    if s["name"]:
        name = s["name_en"] if en() and s["name_en"] else s["name"]   # router already dropped the شارع prefix
        name = name[7:] + " St" if name.startswith("Sharia ") else name
        name = name.split(" (")[0][:22]
    else:
        ar_lbl, en_lbl = SIDE_ST.get(s["highway"], ("شارع فرعي", "side st"))
        d = heading(s["from"], s["to"])
        name = f"{ar_lbl} {HEADING[d]}" if not en() else f"{en_lbl} {d}"
    step = f"{name} {ar(round(s['m'] / 1000, 1))}" + tr("كم", "km")
    rough = s["states"].get("degraded", 0) + s["states"].get("foot_only", 0)
    if s["states"].get("unknown", 0) >= 0.5:
        step += tr(" (غير مؤكد)", " (unconfirmed)")
    elif rough >= 0.5:
        step += tr(" (ركام)", " (rubble)")
    return step


def heading(frm, to):
    dx, dy = (to[0] - frm[0]) * 0.85, to[1] - frm[1]   # lon degrees are ~0.85 lat degrees at Gaza
    if abs(dx) > abs(dy):
        return "E" if dx > 0 else "W"
    return "N" if dy > 0 else "S"


def arrow():
    return tr("←", "→")


def report_reply(res, place, state):
    name = short(place["name"])
    if res["ignored_reason"] == "contradicts_official_nogo":
        return fit(tr(f"شكراً. {name} داخل منطقة خطر رسمية؛ لا يمكن تعليمها كآمنة.",
                      f"Thanks. {name} is inside an official danger zone; it can't be marked safe."))
    if res["ignored_reason"]:
        return tr("شكراً. تم استلام البلاغ للمراجعة.", "Thanks. Your report was received for review.")
    if res["changed"]:
        return fit(tr(f"شكراً. تأكد: {name} الآن {STATE_AR[res['after']]}. سيتم تنبيه المشتركين.",
                      f"Thanks. Confirmed: {name} is now {STATE_EN[res['after']]}. Subscribers will be alerted."))
    n = res["pending"].get(state, 0)
    return fit(tr(f"شكراً. سُجل '{STATE_AR[state]}' عند {name} ({ar(n)} بلاغ). ننتظر تأكيداً ثانياً.",
                  f"Thanks. Logged '{STATE_EN[state]}' at {name} ({n} report{'s' if n != 1 else ''}). Waiting for a second confirmation."))


HELP_AR = "أرسل: طريق [من] [إلى] — مثال: طريق المواصي ناصر، أو مشي [من] [إلى]. للإبلاغ: مغلق [مكان] / مفتوح [مكان] / خطر [مكان]. تنبيه [من] [إلى] للتنبيهات."
HELP_EN = "Send: route [from] to [to] (e.g. route Mawasi to Nasser) or walk [from] to [to]. Report: blocked / open / unsafe [place]. alert [from] to [to] for alerts."
PRIVACY_AR = ("خصوصيتك: لا نحفظ موقعك. لا نحفظ رقمك إلا إذا اشتركت بالتنبيهات، ويُحذف عند الإلغاء. "
              "البلاغات مجهولة وتُحذف بعد ١٤ يوماً. لا نشارك البيانات مع أي طرف.")
PRIVACY_EN = ("Privacy: we never store your location. Your number is kept only while you're subscribed to alerts. "
              "Reports are anonymous, deleted after 14 days, never shared.")


def help_text():
    return tr(HELP_AR, HELP_EN)


def privacy_text():
    return tr(PRIVACY_AR, PRIVACY_EN)


def unsub_text():
    return tr("تم إلغاء التنبيهات. أرسل تنبيه [من] [إلى] للاشتراك مجدداً.",
              "Alerts stopped. Send alert [from] to [to] to subscribe again.")


def rate_limited_text():
    return tr("رسائل كثيرة خلال ساعة. حاول لاحقاً. في الطوارئ اتبع التعليمات الرسمية.",
              "Too many messages this hour. Try later. In an emergency follow official instructions.")


def ask_od_text():
    return tr("من وين لوين؟ أرسل: طريق [من] الى [إلى] — مثال: طريق المواصي الى ناصر",
              "From where to where? Send: route [from] to [to] — e.g. route Mawasi to Nasser")


def need_route_text():
    return tr("لم نجد رحلة سابقة لك. أرسل طريق [من] [إلى] أولاً.",
              "We have no recent trip for you. Send route [from] to [to] first.")


def numbered(question, names):
    opts = " ".join(f"{ar(i + 1)}) {short(n)}" for i, n in enumerate(names))
    return fit(f"{question} {opts} — " + tr("أرسل الرقم", "send the number"))


def arrived_reply(minutes, p50, p85):
    if minutes is None:
        return tr("كم دقيقة استغرقت؟ أرسل: وصلت [دقائق] — مثال: وصلت ٤٥",
                  "How many minutes did it take? Send: arrived [minutes] — e.g. arrived 45")
    if en():
        verdict = "within our estimate ✓" if minutes <= p85 else "longer than we said — we'll improve the estimate"
        return fit(f"Thanks! Logged {minutes}min (we said {p50}–{p85}min): {verdict}.")
    verdict = "ضمن توقعنا ✓" if minutes <= p85 else "أطول من توقعنا — سنحسّن التقدير"
    return fit(f"شكراً! سجلنا {ar(minutes)}د (توقعنا {ar(p50)}–{ar(p85)}د): {verdict}.")


def failed_reply(place_name, res):
    confirmed = res and res.get("changed")
    if en():
        where = f" at {short(place_name)}" if place_name else ""
        state = "Closure confirmed" if confirmed else "We logged a provisional closure report"
        return fit(f"Sorry. {state}{where}; we'll avoid it in future routes. Send route [from] to [to] for an alternative.")
    where = f" عند {short(place_name)}" if place_name else ""
    state = "تأكد الإغلاق" if confirmed else "سجلنا بلاغ إغلاق مؤقت"
    return fit(f"آسفون. {state}{where}، ونتجنبه في الطرق القادمة. أرسل طريق [من] [إلى] لطريق بديل.")


def fit(msg):
    return msg if len(msg) <= MAX else msg[: MAX - 1] + "…"
