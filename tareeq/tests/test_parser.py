"""SMS parser regression set. Add every misparse seen in the field here before fixing it."""
import pytest

from nlu import rule_parse

ROUTES = [  # text, from, to  (canonical names or aliases resolved via the gazetteer)
    ("طريق المواصي مستشفى ناصر", "المواصي", "مستشفى ناصر"),
    ("طريق المواصي الى ناصر", "المواصي", "مستشفى ناصر"),
    ("من دير البلح الى خانيونس", "دير البلح", "خان يونس"),
    ("طريق الدير ناصر", "دير البلح", "مستشفى ناصر"),
    ("بدي اروح عالأوروبي من المواصي", "المواصي", "مستشفى غزة الأوروبي"),
    ("بدي اروح على ناصر من الدير", "دير البلح", "مستشفى ناصر"),
    ("كيف اروح لعند ناصر من الدير", "دير البلح", "مستشفى ناصر"),
    ("بدي اروح للمواصي من خانيونس", "خان يونس", "المواصي"),
    ("route Mawasi to Nasser", "المواصي", "مستشفى ناصر"),
    ("طريق القرارة - مستشفى الأقصى", "القرارة", "مستشفى شهداء الأقصى"),
]
REPORTS = [
    ("مغلق دوار بني سهيلا", "blocked", "دوار بني سهيلا"),
    ("مسكر عند دوار بني سهيلا", "blocked", "دوار بني سهيلا"),
    ("مفتوح ناصر", "open", "مستشفى ناصر"),
    ("خطر القرارة", "unsafe", "القرارة"),
    ("ركام الدير", "degraded", "دير البلح"),
    ("مغلق عند دوار البحر", "blocked", "دوار البحر"),
]

# Fixed misparses that must NOT resolve to a place (wrong-place guesses are worse than asking)
CLARIFY = [
    ("طريق المواصي الى مستشفى الشفا", "out_of_coverage"),   # used to match Al-Aqsa hospital
    ("طريق المواصي", "ambiguous_od"),
    ("مغلق مدرسة القدسس", "suggest"),
    ("مغلق جباليا", "out_of_coverage"),
    ("طريق المواصي الى مستشفى نصير", "suggest"),   # misspelt Nasser: offered as a choice, not assumed
]
OTHER = [
    ("وصلت ٤٥", "arrived", {"minutes": 45}),
    ("وصلت بعد ساعة ونص", "arrived", {"minutes": 90}),
    ("ما قدرت اوصل عند دوار الأقصى", "failed", {}),
    ("إلغاء", "unsubscribe", {}),
    ("خصوصية", "privacy", {}),
    ("٢", "choice", {"n": 2}),
]


@pytest.mark.parametrize("text,intent", CLARIFY)
def test_clarify_not_guess(gaz, text, intent):
    p = rule_parse(text, gaz)
    assert p and p["intent"] == intent, p
    if "نصير" in text:
        assert gaz.match("مستشفى ناصر")[0] in p["choices"]


@pytest.mark.parametrize("text,intent,fields", OTHER)
def test_other_intents(gaz, text, intent, fields):
    p = rule_parse(text, gaz)
    assert p and p["intent"] == intent, p
    for k, v in fields.items():
        assert p[k] == v, (k, p[k])


@pytest.mark.parametrize("text,a,b", ROUTES)
def test_route(gaz, text, a, b):
    p = rule_parse(text, gaz)
    assert p and p["intent"] == "route", p
    assert p["from"] is gaz.match(a)[0], (p["from"]["name"], a)
    assert p["to"] is gaz.match(b)[0], (p["to"]["name"], b)


@pytest.mark.parametrize("text,state,place", REPORTS)
def test_report(gaz, text, state, place):
    p = rule_parse(text, gaz)
    assert p and p["intent"] == "report" and p["state"] == state
    assert p["place"] is gaz.match(place)[0]


@pytest.mark.parametrize("text,intent", [
    ("مساعدة", "help"), ("وصلت 45", "arrived"), ("الغاء", "unsubscribe"), ("خصوصية", "privacy"),
    ("ما قدرت اوصل دوار بني سهيلا", "failed"), ("تنبيه المواصي ناصر", "alert"), ("2", "choice"),
])
def test_other_intents(gaz, text, intent):
    assert rule_parse(text, gaz)["intent"] == intent


def test_arrived_hours(gaz):
    assert rule_parse("وصلت بساعة ونص", gaz)["minutes"] == 90


def test_out_of_coverage_is_not_guessed(gaz):
    p = rule_parse("طريق المواصي مستشفى الشفا", gaz)
    assert p["intent"] != "route"


def test_al_shifa_does_not_match_al_aqsa(gaz):
    place, _ = gaz.match("مستشفى الشفا")
    assert place is None or "الأقصى" not in place["name"]


WALKS = [  # text, from, to — walking routes ask the router for the foot layer only
    ("walk Mawasi to Nasser", "المواصي", "مستشفى ناصر"),
    ("مشي من الدير الى ناصر", "دير البلح", "مستشفى ناصر"),
    ("طريق المواصي ناصر مشي", "المواصي", "مستشفى ناصر"),
    ("route Mawasi to Nasser on foot", "المواصي", "مستشفى ناصر"),
    ("بدي اروح على ناصر من الدير مشي", "دير البلح", "مستشفى ناصر"),
]


@pytest.mark.parametrize("text,a,b", WALKS)
def test_walk_route(gaz, text, a, b):
    p = rule_parse(text, gaz)
    assert p and p["intent"] == "route" and p["modes"] == ["foot"], p
    assert p["from"] is gaz.match(a)[0] and p["to"] is gaz.match(b)[0]


def test_walk_word_with_one_place_is_still_a_foot_only_report(gaz):
    p = rule_parse("مشي دوار البحر", gaz)
    assert p["intent"] == "report" and p["state"] == "foot_only"


def test_plain_route_has_no_mode_filter(gaz):
    assert "modes" not in rule_parse("route Mawasi to Nasser", gaz)
