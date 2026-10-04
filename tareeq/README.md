# طريق Tareeq — Gaza road status & honest trip times

Arabic-first, SMS-first service that answers **"Can I get there safely today, and how long will it really take?"**
Built for the Deir al-Balah ↔ Khan Younis ↔ Al-Mawasi pilot corridor (see `../Tareeq — Product Spec v2.md`; the original `../Gaza Transit Info — Product Spec.md` is kept for history).

> Demo build. Driver/rider reports, fares and wait times are **simulated**. No-go polygons are **illustrative, not real safety data**.

## Run

```bash
cd tareeq
uv venv -p 3.11 .venv && uv pip install -p .venv -r requirements.txt
.venv/bin/python data/ingest.py          # only if data/build/ is missing (needs data/raw/)
cd backend && ../.venv/bin/uvicorn main:app --port 8765
# open http://localhost:8765  (operator console: /admin, field reporting: /field)
.venv/bin/python -m pytest -q tests     # from tareeq/
```

Optional: `export ANTHROPIC_API_KEY=...` turns on Claude parsing of free-form Arabic SMS
(e.g. "الطريق عند دوار بني سهيلا مسكر بالركام"). Without it, a deterministic keyword grammar runs offline.

## What's real

| Layer | Source |
|---|---|
| Road graph | OpenStreetMap via Overpass (Oct 2026), split into 7,325 segments of ≤250 m |
| Damage prior | UNOSAT/UN-Habitat road damage assessment (imagery 2024-05-29): crater, debris, destroyed… mapped to Degraded / Foot-only priors at low confidence |
| Places | OSM hospitals, clinics, camps, roundabouts + residents' aliases ("ناصر", "مواصي", "الدير") |
| Basemap | Protomaps extract of Gaza (`data/build/gaza.pmtiles`), served from `/map/gaza.pmtiles`, so no third-party tile server is hit |
| Strike hazards | NASA FIRMS fire detections, GDELT and an optional partner feed (`backend/strikes.py`), turned into expiring no-go zones |

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
- **Offline:** the service worker (`sw.js`, registered by the map and `/field`) caches the page, the basemap and saved trips, so a saved route still opens with no connection.
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
`GET /api/segments` · `GET /api/states` (compact delta-friendly table) · `GET /api/places` · `GET /api/nogo` · `GET /api/alerts?sender=` · `GET /api/changes` · `GET /api/metrics` ·
`POST /api/clock {hours}` · `POST /api/signals {fuel_index}` · `POST /api/nogo/{id} {active}` · `POST /api/reset` · `POST /api/twilio` (form: Body, From)

Operator console and field reporting live under `/api/ops/admin/*` (zones, strikes, reporters, queue, overview, audit, broadcasts) and `/api/ops/field/*` (`me`, `report`, `area`); see `backend/ops.py`.

## Demo vs production

`TAREEQ_DEMO=1` (default): in-memory store, simulated seed reports, routes carry `"demo": true` (SMS ends "· تجريبي"), and the clock, no-go toggle, reset and client-side trusted flag are enabled.
`TAREEQ_DEMO=0`: requires `TAREEQ_SALT` and `TAREEQ_SUBS_KEY`; reports go to `data/tareeq.db`, alert subscriptions to a separate `data/subscriptions.db` (phone numbers Fernet-encrypted, looked up by HMAC, row deleted on إلغاء), never seeds, and disables those demo endpoints. Trusted reporters come from `TAREEQ_TRUSTED`. Set `TWILIO_AUTH_TOKEN` to enforce webhook signatures; add `TWILIO_ACCOUNT_SID` + `TWILIO_FROM` to push route-change alerts to SMS subscribers (max 3/day; web subscribers pull `/api/alerts`).

## Operations (production, `TAREEQ_DEMO=0`)

**Persistence.** `data/tareeq.db` holds reports (purged after 14 days, enforced hourly), trip outcomes and the
state-change event log (kept 14 days, so changes not yet alerted survive a restart). `data/subscriptions.db`
holds alert subscriptions, including each subscriber's daily alert count, so a restart doesn't reset the
3-per-day cap.

**Backing up `TAREEQ_SUBS_KEY`.** Without the key, `subscriptions.db` is unreadable, by design. Keep the key in
a password manager or secret store held by two named people, *separately* from database backups. A
backup of `subscriptions.db` without the key is useless to whoever obtains it, and a key without a backup is harmless.
If the key is lost, delete `subscriptions.db`; subscribers re-subscribe by SMS.

**Rotating `TAREEQ_SUBS_KEY`** (scheduled, or immediately if the key may have leaked):
1. Generate a new secret: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Restart with `TAREEQ_SUBS_KEY="<new>,<old>"`. On startup every row is decrypted with any listed key,
   then re-encrypted and re-keyed (HMAC id) with the newest (MultiFernet). The startup log shows the count.
3. Back up the new key, then restart with `TAREEQ_SUBS_KEY="<new>"` only and destroy the old key.

**Operator console and field reporting** (`backend/ops.py`, `backend/strikes.py`):

| Page / setting | What it does |
|---|---|
| `/admin` | Operator console: approve and expire zones, a queue of contested segments, reporter codes, broadcasts, audit log |
| `/field` | Aid-worker reporting page. Codes are issued in the console; the demo code is `DEMO-0000`. Field reports count as trusted |
| `TAREEQ_OPERATORS="name,name,..."` | Operator names offered in the console's "acting as" picker (no login; default `amal,omar`). At least two are needed for the two-person rule |
| `data/ops.db` | Production only: zones, hashed reporter codes, field notes (purged after 14 days), broadcasts, audit log |
| `data/strikes/events.json` | Cache of non-demo strike hazards, so they survive restarts |
| `FIRMS_MAP_KEY` | Optional NASA FIRMS key. Without it the keyless global 24 h CSVs are used, refreshed every 3 h (`TAREEQ_STRIKES_REFRESH_S`) |
| `TAREEQ_STRIKES_URL` | A partner GeoJSON feed of strike Points |
| `TAREEQ_FIRMS=0` · `TAREEQ_GDELT=0` | Disable that feed |
| `TAREEQ_STRIKES_FETCH=0` | Disable all strike fetching (the tests set this) |

Strike hazards and approved zones become official no-go polygons with their own `buffer_m` and an expiry, so
they override crowd reports like any OCHA/UN zone. `ops.install` wraps `main.reset`, so a demo reset re-applies
approved zones and live strikes.

**Live Twilio test** (needs your account; nothing is sent until these are set):
1. Buy or verify a number in the Twilio console. For WhatsApp, use the sandbox number.
2. `export TWILIO_ACCOUNT_SID=AC… TWILIO_AUTH_TOKEN=… TWILIO_FROM=+1…`, then start the server.
3. Expose it over HTTPS, e.g. `cloudflared tunnel --url http://localhost:8765` or `ngrok http 8765`.
4. In the number's *Messaging → A message comes in* webhook, set `https://<tunnel>/api/twilio` (HTTP POST).
   With `TWILIO_AUTH_TOKEN` set, requests without a valid `X-Twilio-Signature` get 403. The signed URL
   must match exactly, so use the public tunnel URL.
5. From a phone: send `طريق المواصي ناصر` for a reply, then `تنبيه المواصي ناصر`. Then block a segment on that
   route (web map, two reports) and an alert SMS should arrive (3 per day at most). Send `إلغاء` to stop.
6. Note: Arabic SMS is UCS-2, 70 characters per segment, so a 160-character reply costs 3 segments.

## Known simplifications

- One state per segment + a mode passability matrix (instead of fully independent per-mode states).
- Speeds, waits and fares are seeded priors, not learned from confirmed trips yet.
- SMS target is 160 chars per the spec; note Arabic SMS is UCS-2 (70 chars/segment), so replies use 2–3 segments.

Data licences: OSM © contributors (ODbL); UNOSAT road assessment (CC BY-SA).
