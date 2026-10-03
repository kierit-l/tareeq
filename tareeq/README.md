# طريق Tareeq — Gaza road status & honest trip times

Arabic-first, SMS-first service that answers **"Can I get there safely today, and how long will it really take?"**
Built for the Deir al-Balah ↔ Khan Younis ↔ Al-Mawasi pilot corridor (see `../Gaza Transit Info — Product Spec.md`).

> Demo build. Driver/rider reports, fares and wait times are **simulated**. No-go polygons are **illustrative, not real safety data**.

## Run

```bash
cd tareeq
uv venv -p 3.11 .venv && uv pip install -p .venv -r requirements.txt
.venv/bin/python data/ingest.py          # only if data/build/ is missing (needs data/raw/)
cd backend && ../.venv/bin/uvicorn main:app --port 8765
# open http://localhost:8765
```

Optional: `export ANTHROPIC_API_KEY=...` turns on Claude parsing of free-form Arabic SMS
(e.g. "الطريق عند دوار بني سهيلا مسكر بالركام"). Without it, a deterministic keyword grammar runs offline.

## What's real

| Layer | Source |
|---|---|
| Road graph | OpenStreetMap via Overpass (Oct 2026), split into 7,325 segments of ≤250 m |
| Damage prior | UNOSAT/UN-Habitat road damage assessment (imagery 2024-05-29): crater, debris, destroyed… mapped to Degraded / Foot-only priors at low confidence |
| Places | OSM hospitals, clinics, camps, roundabouts + residents' aliases ("ناصر", "مواصي", "الدير") |

## How it works

```
OSM + UNOSAT + no-go polygons ─► ingest.py ─► segments + gazetteer + stands
reports (SMS / map / bot) ─► state.py  verify (1 trusted or ≥2 independent; 1 for Unsafe) · provisional · decay · safety
   conversation.py  rate limit · last quoted route · trips · numbered clarifications
                                └► router.py  layered graph foot/cart/tuk-tuk/trailer, board only at stands
                                    └► P50–P85 Monte Carlo trip time, fare, confidence, freshness
channels: web map (RTL) · SMS simulator · /api/twilio webhook (SMS/WhatsApp)
```

- **Segment states:** Open · Degraded · Foot only · Blocked · Unsafe · Unknown. Shown with words + colour + line pattern.
- **Decay:** τ = 6 h open, 24 h degraded, 72 h foot-only, 168 h blocked, 8 h unsafe. Stale Open → **Unknown**, never Open.
- **Safety:** official no-go polygons + 300 m buffer are excluded and can't be overridden; reports claiming they're passable are rejected. Crowd reports can only make roads *less* safe.
- **Misinformation:** one reporter at places implying >80 km/h travel is ignored; daily-rotating salted reporter hashes count independence without tracking anyone.
- **Privacy:** no GPS trails, no device IDs, timestamps floored to 5 min, no crowd counts at stands, raw reports purged after 14 days.
- **Trip time:** wait (stand × hour × fuel) + ride (mode × state × hour) + walk, 400-sample Monte Carlo with per-leg correlated noise → P50–P85 range, wider when evidence is weak.

## SMS grammar

| Send | Meaning |
|---|---|
| `طريق المواصي مستشفى ناصر` / `من دير البلح الى خانيونس` | route |
| `مغلق دوار بني سهيلا` · `مفتوح …` · `صعب …` · `مشي …` · `خطر …` | report blocked / open / degraded / foot-only / unsafe |
| `تنبيه المواصي ناصر` | alert me when my saved route changes |
| `وصلت ٤٥` / `وصلت بعد ساعة` | I arrived: scores the last quoted range (`GET /api/metrics`) |
| `ما قدرت اوصل عند دوار الأقصى` | couldn't get through: failed trip + provisional Blocked on the quoted leg nearest that place |
| `إلغاء` · `خصوصية` | unsubscribe · short privacy policy |
| `١` `٢` `٣` | answer a numbered question ("did you mean…", "which street?") |
| `مساعدة` | help |

Unknown places get the 3 closest matches; a missing origin or destination gets "من وين لوين؟" ("from where to where?"). The service never guesses.
20 messages per sender per hour.

## 3-minute demo script

1. **Problem (20 s):** 68–85 % of roads damaged, no timetables, blackouts. Google Maps assumes the roads exist.
2. **SMS:** tap chip *طريق المواصي مستشفى ناصر* → "توكتوك ٤٠–٥٠د ~٩₪ اركب من المواصي · ثقة عالية · قبل ٢٣د" (85 chars).
3. **Verification:** click a segment on the blue route → *أبلغ: مغلق*. Nothing changes ("waiting for a second independent report"). Click again → **Blocked**, route detours, range widens, "What changed" logs it.
4. **Alerts:** send *تنبيه المواصي ناصر*, then block a segment on the route → 🔔 alert arrives in the phone.
5. **Safety:** toggle *أمر إخلاء جديد* → zone appears, routes avoid it with a 300 m buffer. Try reporting a road inside it as open → rejected.
6. **Honesty:** press *+٦ ساعات* → evidence ages, confidence drops to low, range widens, "أجزاء غير مؤكدة".
7. **Fuel:** slide fuel scarcity to 1.8× → waits and fares rise.
8. **Do-no-harm (20 s):** privacy bullets above. Roadmap: pilot corridor → strip-wide offline PWA → open API for agencies.

## API

`GET /api/route?frm=&to=&modes=tuktuk,cart&accessible=true` · `POST /api/sms {text,sender}` · `POST /api/report {segment|place|lon,lat, state, reporter, trusted}` ·
`GET /api/states` (compact delta-friendly table) · `GET /api/alerts?sender=` · `GET /api/changes` · `POST /api/clock {hours}` · `POST /api/signals {fuel_index}` ·
`POST /api/nogo/{id} {active}` · `POST /api/reset` · `POST /api/twilio` (form: Body, From)

## Demo vs production

`TAREEQ_DEMO=1` (default): in-memory store, simulated seed reports, routes carry `"demo": true` (SMS ends "· تجريبي"), and the clock, no-go toggle, reset and client-side trusted flag are enabled.
`TAREEQ_DEMO=0`: requires `TAREEQ_SALT` and `TAREEQ_SUBS_KEY`; reports go to `data/tareeq.db`, alert subscriptions to a separate `data/subscriptions.db` (phone numbers Fernet-encrypted, looked up by HMAC, row deleted on إلغاء), never seeds, and disables those demo endpoints. Trusted reporters come from `TAREEQ_TRUSTED`. Set `TWILIO_AUTH_TOKEN` to enforce webhook signatures; add `TWILIO_ACCOUNT_SID` + `TWILIO_FROM` to push route-change alerts to SMS subscribers (max 3/day; web subscribers pull `/api/alerts`).

## Known simplifications

- One state per segment + a mode passability matrix (instead of fully independent per-mode states).
- Speeds, waits and fares are seeded priors, not learned from confirmed trips yet.
- SMS target is 160 chars per the spec; note Arabic SMS is UCS-2 (70 chars/segment), so replies use 2–3 segments.

Data licences: OSM © contributors (ODbL); UNOSAT road assessment (CC BY-SA).
