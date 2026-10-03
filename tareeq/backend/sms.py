"""Compact Arabic replies for SMS / WhatsApp (target <= 160 chars)."""

AR_DIGITS = str.maketrans("0123456789.", "٠١٢٣٤٥٦٧٨٩٫")
STATE_AR = {"open": "مفتوح", "degraded": "صعب", "foot_only": "مشاة فقط", "blocked": "مغلق",
            "unsafe": "خطر", "unknown": "غير معروف"}
CONF_AR = {"high": "ثقة عالية", "medium": "ثقة متوسطة", "low": "ثقة ضعيفة"}
MAX = 160


def ar(x):
    return str(x).translate(AR_DIGITS)


def short(name):
    for pre in ("موقف ", "مستشفى ", "مجمع "):
        name = name.replace(pre, "")
    return name


def age_ar(minutes):
    if minutes is None:
        return "لا بلاغات حديثة"
    if minutes < 60:
        return f"قبل {ar(minutes)}د"
    return f"قبل {ar(round(minutes / 60))}س"


def route_reply(a, b, r):
    if r is None:
        return f"{short(a['name'])}←{short(b['name'])}: لا يوجد طريق آمن معروف الآن. لا تخاطر، اتبع أوامر الإخلاء الرسمية."
    modes = []
    for l in r["legs"]:
        if not modes or modes[-1] != l["mode_ar"]:
            modes.append(l["mode_ar"])
    first_ride = next((l for l in r["legs"] if l["stand"]), None)
    dominant = max(r["state_share"].items(), key=lambda kv: kv[1])
    parts = [
        f"{short(a['name'])}←{short(b['name'])}:",
        "+".join(modes),
        f"{ar(r['p50_min'])}–{ar(r['p85_min'])}د",
    ]
    if r["fare_ils"]:
        parts.append(f"~{ar(r['fare_ils'])}₪")
    if first_ride:
        parts.append(f"اركب من {short(first_ride['stand'])}")
    if r["walk_km"] >= 0.3:
        parts.append(f"مشي {ar(r['walk_km'])}كم")
    tail = f"· {STATE_AR[dominant[0]]} {ar(round(dominant[1]*100))}٪ · {CONF_AR[r['confidence_label']]} · {age_ar(r['newest_report_min'])}"
    if r.get("provisional_share"):
        tail += " · بلاغ إغلاق غير مؤكد"
    elif r["unknown_share"] >= 0.2:
        tail += " · أجزاء غير مؤكدة"
    if r.get("demo"):
        tail += " · تجريبي"
    msg = " ".join(parts) + " " + tail
    return fit(msg)


def report_reply(res, place, state):
    if res["ignored_reason"] == "contradicts_official_nogo":
        return fit(f"شكراً. {short(place['name'])} داخل منطقة خطر رسمية؛ لا يمكن تعليمها كآمنة.")
    if res["ignored_reason"]:
        return "شكراً. تم استلام البلاغ للمراجعة."
    if res["changed"]:
        return fit(f"شكراً. تأكد: {short(place['name'])} الآن {STATE_AR[res['after']]}. سيتم تنبيه المشتركين.")
    n = res["pending"].get(state, 0)
    return fit(f"شكراً. سُجل '{STATE_AR[state]}' عند {short(place['name'])} ({ar(n)} بلاغ). ننتظر تأكيداً ثانياً.")


HELP = "أرسل: طريق [من] [إلى] — مثال: طريق المواصي ناصر. للإبلاغ: مغلق [مكان] / مفتوح [مكان] / خطر [مكان]. تنبيه [من] [إلى] للتنبيهات."


PRIVACY = ("خصوصيتك: لا نحفظ موقعك. لا نحفظ رقمك إلا إذا اشتركت بالتنبيهات، ويُحذف عند الإلغاء. "
           "البلاغات مجهولة وتُحذف بعد ١٤ يوماً. لا نشارك البيانات مع أي طرف.")
UNSUB = "تم إلغاء التنبيهات. أرسل تنبيه [من] [إلى] للاشتراك مجدداً."
RATE_LIMITED = "رسائل كثيرة خلال ساعة. حاول لاحقاً. في الطوارئ اتبع التعليمات الرسمية."
ASK_OD = "من وين لوين؟ أرسل: طريق [من] الى [إلى] — مثال: طريق المواصي الى ناصر"
NEED_ROUTE = "لم نجد رحلة سابقة لك. أرسل طريق [من] [إلى] أولاً."


def numbered(question, names):
    opts = " ".join(f"{ar(i + 1)}) {short(n)}" for i, n in enumerate(names))
    return fit(f"{question} {opts} — أرسل الرقم")


def arrived_reply(minutes, p50, p85):
    if minutes is None:
        return "كم دقيقة استغرقت؟ أرسل: وصلت [دقائق] — مثال: وصلت ٤٥"
    verdict = "ضمن توقعنا ✓" if minutes <= p85 else "أطول من توقعنا — سنحسّن التقدير"
    return fit(f"شكراً! سجلنا {ar(minutes)}د (توقعنا {ar(p50)}–{ar(p85)}د): {verdict}.")


def failed_reply(place_name, res):
    where = f" عند {short(place_name)}" if place_name else ""
    state = "تأكد الإغلاق" if res and res.get("changed") else "سجلنا بلاغ إغلاق مؤقت"
    return fit(f"آسفون. {state}{where}، ونتجنبه في الطرق القادمة. أرسل طريق [من] [إلى] لطريق بديل.")


def fit(msg):
    return msg if len(msg) <= MAX else msg[: MAX - 1] + "…"
