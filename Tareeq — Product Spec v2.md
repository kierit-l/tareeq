# Tareeq (طريق) — Product Spec v2

*3 Oct 2026 · supersedes "Gaza Transit Info — Product Spec" (2 Oct 2026)*

## 1. Where we are

The hackathon build proves the core idea works end to end on real geography. It is a **demo**, not yet a pilot. Every number it shows to a user comes from synthetic data, and the "offline-first" part is only partly built.

### What exists and works

| Area | State | Notes |
| --- | --- | --- |
| Road graph | Built | 7,325 OSM segments (≤250 m) for the Khan Younis–Deir al-Balah–Al-Mawasi corridor; largest connected component only |
| Damage prior | Built | UNOSAT May 2024 damage joined per segment → `degraded` / `foot_only` priors at 0.2 confidence |
| Segment state engine | Built | 6 states, verification (1 trusted or ≥2 independent), exponential decay per state, stale → Unknown, official no-go always wins, burst-outlier filter, daily-salted reporter hashes, 5-min timestamps, 14-day purge |
| Multi-modal router | Built | Layered graph (foot / cart / tuk-tuk / trailer), board only at stands, trailers only on major roads, wheelchair penalty, P50–P85 from Monte Carlo, fare estimate scaled by fuel index |
| SMS channel | Built | Arabic keyword grammar (route / report / alert / help) + Claude fallback for free text; replies ≤160 chars with Arabic digits; Twilio webhook |
| Web map | Built | Leaflet, RTL, states drawn with colour *and* dash pattern, tap-to-report, route card with evidence line, SMS simulator, demo controls (clock, fuel, evacuation toggle) |
| Offline | **Partial** | `sw.js` and a Gaza-only `/tiles` cache proxy exist, but the page never registers the service worker and still loads tiles straight from openstreetmap.org. The "Saved trips" and offline-bar UI are empty shells |

Smoke test (today): boot 1.1 s, route query ~10 ms, sample SMS reply
`المواصي←ناصر الطبى: توكتوك ٤٠–٥٠د ~٩₪ اركب من المواصي · صعب ٥٢٪ · ثقة عالية · قبل ٢١د`.

### Gaps between v1 spec and the build

| v1 promise | Reality | v2 decision |
| --- | --- | --- |
| Passability state **per mode** | One physical state per segment; mode passability comes from a speed table | **Keep the single state.** It's simpler for reporters and works fine. Add one optional attribute, `narrow`, for the trailer case |
| Trip time from **observed** speeds and waits | Hard-coded speed table; illustrative stand waits and fares | Keep as a cold-start prior; replace it with observed data (§4.2) |
| One-tap arrival confirmation, 80% in-range metric | Not built; nothing measures accuracy. The v1 metric was also unreachable: a P50–P85 band holds only ~35% of trips by construction | **P0** *(built)*. Metric redefined as calibration (A3) |
| Routing on the phone, <200 KB deltas | Server-side routing only; `/api/states` is a 180 KB full table every time | Server routing for the pilot; delta sync P1; on-device routing P2 |
| Trusted reporters | `trusted` is a flag the client sets itself, so anyone can claim it | Server-side roster with per-reporter tokens (P0) |
| Alerts on saved routes | Web polling only; Twilio subscribers never get an outbound message | Outbound SMS push (P0 for the pilot) |
| No phone numbers kept | Subscriptions are stored in memory, keyed by the **raw phone number** | Store subscriber numbers encrypted and separate from reports; opt-out keyword (§5) |
| Fuel and fares as live signals | One manual slider | Driver-reported fare and fuel-price keywords (P1) |
| WhatsApp/Telegram bot, USSD | Same Twilio webhook (can serve WhatsApp); no USSD | WhatsApp via the same handler for the pilot; USSD dropped until a telecom partner exists |

### Defects found in review (status as of 3 Oct, later the same day)

| # | Defect | Status |
| --- | --- | --- |
| 1 | **Origin/destination swap**: `بدي اروح عالأوروبي من المواصي` was answered as European → Mawasi | **Fixed** (verified): a mid-sentence `من X` is the origin; Levantine contractions are handled |
| 2 | **Trusted spoofing**: `POST /api/report` accepted `trusted: true` from anyone | **Fixed for production**: a client flag only counts when `TAREEQ_DEMO=1` or the reporter is in the `TAREEQ_TRUSTED` allowlist. The full roster (R1) is still to do |
| 3 | **Synthetic data shown as "ثقة عالية" (high confidence)** | **Intentional in the demo** (labelled in the README and footer). Must not reach production: see A6 and P2 |
| 4 | **Report-by-place arbitrary**: first 4 segments touching the node, in dict order | **Mitigated**: deterministic, major roads first. Asking which road (R2) is still to do |
| 5 | **Persistence**: `:memory:` DB, random salt | **Partly fixed**: `TAREEQ_SALT` is honoured. The DB stays in memory for the demo; production needs P1 |
| 6 | **Twilio**: no signature check, no XML escaping | **Fixed**: escaped; HMAC validated when `TWILIO_AUTH_TOKEN` is set |
| 7 | **Unsafe needs two people** | **Fixed**: a single Unsafe report verifies; a newer trusted report can overturn it. S3 still applies to `blocked` |
| 8 | **Offline not wired**: SW not registered; tiles from openstreetmap.org | Open (owned by the frontend session) |
| 9 | **Mobile layout**: three columns, one 1100 px breakpoint | Open (W1) |

### Build progress (3 Oct, evening)

The backend session has built every P0 item in state/router/nlu/sms: S3, S4, A1–A3, A6, C1, C2, C5, R2, P1/P2, plus the `إلغاء`/`خصوصية` intents. I spot-checked `وصلت`, the ask-back reply, the street disambiguation, privacy and `/api/metrics`. Still open:
- **P1:** R3, A4, A5, R4.
- **Frontend:** W1–W3.
- **Ops:** the operator console (§4.6), real OCHA polygons and hosting.

Later the same evening:
- **C3 built** (`alerts.py`): Twilio push, at most 3 per day, removed on `إلغاء`. Tested only with a mocked sender; it needs a live Twilio test before the pilot.
- **Out of coverage:** places outside the corridor get "outside the service area".
- **Street options:** roundabouts list each exit by name and direction.
- **Metrics:** report `within_p85_share` and `within_p50_share`.

**Subscriptions (§5):** built (`subscriptions.py`, only when `TAREEQ_DEMO=0`):
- Stored in a separate `subscriptions.db`.
- Phone number encrypted with Fernet; row id is an HMAC of the number.
- Deleted on `إلغاء`.
- Survives a restart.

Remaining production gaps:
1. The daily alert-cap counter and the state-change event log are in memory. A restart resets the cap and drops changes that weren't alerted yet. Persist both (P0).
2. `TAREEQ_SUBS_KEY` needs a backup and rotation procedure: losing the key makes every subscription unreadable, since it fails closed (P0, ops).
3. The 30-day inactivity purge of subscriber rows from §5 isn't built yet (P1).

**Frontend (checked 3 Oct):**
- Built:
  - service worker registered
  - self-hosted PMTiles basemap (4.4 MB, can be stored offline)
  - saved trips
  - offline/stale banner
  - AR/EN toggle
- W1 partial: the layout stacks to one column below 1100 px, but isn't designed for 360 px.
- Not built: W4 offline report queue, W5 delta sync.

### Build progress (3 Oct, night)

- **Repo and tests (P3/P4):**
  - Git repo at `EmberHack/`.
  - `tareeq/tests` has 63 tests covering state rules, router safety invariants (never enters a buffer), the parser regression set, and the ops/strike flows.
- **Operator console §4.6** (`/admin`):
  - Zones are drawn on the map, need a cited source, and go live only after a second operator approves. Removal needs two operators too.
  - Strike hazards with two-person dismissal.
  - Review queue of provisional and contested segments plus field notes.
  - Field-reporter access codes (issue/revoke), broadcasts with two-person approval, accuracy metrics, audit log.
- **Field reporting page (new, `/field`):** Waze-style, mobile, AR/EN.
  - One-tap kinds: clear, slow, rubble, crater, flood, foot only, blocked, checkpoint, unexploded ordnance (UXO), strike.
  - "Still there / clear now" confirmations on any road.
  - Offline queue that sends each report with its real age; the Strip basemap is stored up front.
  - GPS is used only to snap the report to a road and is never stored. Reports from code holders count as trusted.
  - Safety kinds close roads within 100–200 m at once.
- **Recent strikes (new, `strikes.py`):** no free public feed gives precise, recent strike locations in Gaza (researched 3 Oct 2026).
  - **NASA FIRMS** satellite fire detections become hazards: 450 m buffer, 48 h expiry, low-confidence points and points outside the Strip dropped. Expect few hits; FIRMS saw nothing in the corridor over the last 7 days.
  - **GDELT** feeds only a Strip-wide activity signal, because its "Gaza (general)" point sits inside the corridor.
  - **Partner GeoJSON feed** supported for NGO or ACLED-partner data.
  - **Fastest real source:** strike reports from field reporters.
  - **Not usable:** ACLED (licence, weekly lag) and ReliefWeb (needs an approved app name).
- **W1 phone layout:** done (single column below 700 px).
- **W4 offline report queue:** done for `/field`.
- **Still open:**
  - real OCHA polygons (operators must enter them by hand; no machine-readable feed found)
  - a live Twilio test
  - hosting
  - W5 delta sync
  - P1 items (R3, R4, A4, A5)

## 2. Product goal (unchanged, sharpened)

Answer **"Can I get from A to B safely today, how long will it take, and where do I catch a ride?"** in Arabic, over SMS first, with an honest range, age and confidence on every answer.

v2 changes the focus from *demonstrating the pipeline* to *earning trust in one corridor*. Nothing ships to real users until the data it shows comes from real people and the safety layer comes from a real source.

## 3. Pilot scope

| In | Out (for now) |
| --- | --- |
| One corridor: Al-Mawasi ↔ Khan Younis ↔ Deir al-Balah (current bbox) | Strip-wide coverage, Gaza City |
| SMS + WhatsApp (one handler) | USSD, Telegram, native app |
| Mobile web/PWA for reporters and power users | Public on-device routing |
| Modes: foot, tuk-tuk, trailer, cart | Buses, private cars, cargo |
| ~30 drivers, ~20 trusted reporters, invited riders | Open public sign-up |
| Operator console for a small remote team | Partner API |

## 4. Requirements

Priority: **P0** = required before the pilot · **P1** = during the pilot · **P2** = after the pilot.

### 4.1 Safety layer

| ID | Requirement | P |
| --- | --- | --- |
| S1 | Ingest official no-go and evacuation polygons (OCHA oPt access maps / evacuation orders) through the operator console. Each polygon carries a source, an effective time and the operator who entered it. Remove all illustrative polygons from production | P0 |
| S2 | Two-person rule: a polygon change goes live only after a second operator approves it. Removing an area needs the same approval | P0 |
| S3 | **Asymmetric safety.** A single `unsafe` report verifies straight away *(built)*; a newer trusted report can overturn it. A single `blocked` report puts the segment into a *provisional warning*: routes avoid it when an alternative exists and the answer shows "بلاغ غير مؤكد بإغلاق" (unconfirmed closure report). It needs normal verification to become confirmed. Reports that make a road look safer always need full verification | P0 |
| S4 | Keep the 300 m buffer and make it configurable per polygon type (Yellow Line 500 m, evacuation order 300 m) | P1 |
| S5 | Every "no safe route" answer tells the person to follow official orders. We never suggest a route that crosses a buffer, even when no other route exists | P0 (built, keep) |

### 4.2 Accuracy loop

| ID | Requirement | P |
| --- | --- | --- |
| A1 | **Arrival confirmation.** After a route reply, the user can text `وصلت` ("I arrived", optionally with minutes) or tap "وصلت" on the web. We record quoted P50/P85, actual minutes and segment IDs (no location trail). Over SMS, prompt once at P85 + 15 min if the user opted in | P0 |
| A2 | **"Got through / didn't"**: `ما قدرت` ("couldn't get through") + place records a failed trip and files a `blocked` report against the leg nearest the place | P0 |
| A3 | Dashboard (`/api/metrics`, *built*): **calibration**, i.e. share of confirmed trips with actual ≤ quoted P85 (target 80–90%; above 90% means the range is padded) and actual ≤ P50 (target 40–60%). Also the share of suggested routes later reported blocked (target <5%), by week and leg type. The reply keeps quoting P50–P85 ("usually X, up to Y"), because the lower bound matters less to riders than the upper | P0 |
| A4 | Replace the speed table with observed per-segment speeds by mode and hour once a segment has ≥5 confirmed traversals. Until then, fall back to the prior and widen the range | P1 |
| A5 | Wait times per stand × hour from driver and rider reports (`انتظار ١٥ [stand]` = "waited 15 at [stand]") | P1 |
| A6 | **Confidence honesty** *(built)*. Route confidence is capped at "medium" while more than 20% of the length rests on the satellite prior or Unknown. Demo output carries `demo: true`; SMS ends "· تجريبي" (demo) and the web shows a badge | P0 |

### 4.3 Reports and reporters

| ID | Requirement | P |
| --- | --- | --- |
| R1 | Trusted-reporter roster managed in the console. Trusted status comes from a server-issued token (web) or an enrolled number hash (SMS), never from the client | P0 |
| R2 | Report-by-place resolves to the *road* at the place: the user picks from up to 3 named roads ("أي شارع؟ ١) صلاح الدين ٢) البحر" = "Which street? 1) Salah al-Din 2) Al-Bahr"), or we use the segment on the user's last quoted route nearest the place. No more "first 4 by dictionary order" | P0 |
| R3 | Reporter weight by track record: agreement with later verified state raises weight, contradiction lowers it. Weight feeds verification (e.g. two low-weight reporters ≠ verified) | P1 |
| R4 | Driver keywords: `أجرة [from] [to] [₪]` (fare), `سولار [₪/L]` (diesel price), `موقف [place] [modes]` (new stand). Fuel price replaces the manual fuel slider as the fuel index | P1 |
| R5 | Operator queue: contested segments (conflicting verified states within the decay window) and provisional warnings, for trusted reporters to check | P1 |

### 4.4 Channels

| ID | Requirement | P |
| --- | --- | --- |
| C1 | **O/D parsing** *(mid-sentence `من X` and Levantine contractions built)*: `من X` anywhere marks the origin; `ل/الى/عال X` marks the destination. Ambiguous → ask back ("من وين لوين؟" = "From where to where?") instead of guessing. Build a regression set of 200+ real phrasings, including Gazan dialect and Arabizi | P0 |
| C2 | Unknown place → reply with the 3 closest gazetteer matches as numbered choices | P0 |
| C3 | Outbound alerts: when a saved route's segment flips state, or a new no-go area cuts it, send one SMS (≤160 chars) with the new route. At most 3 alerts per subscriber per day. `إلغاء` ("cancel") unsubscribes | P0 |
| C4 | WhatsApp through the same handler. Accept a location pin as origin, used once and not stored | P1 |
| C5 | Twilio signature validation, XML escaping, rate limit of 20 messages per sender-hash per hour | P0 |
| C6 | Voice-note intake on WhatsApp (transcribe → same parser) | P2 |

### 4.5 Mobile web / PWA

| ID | Requirement | P |
| --- | --- | --- |
| W1 | Mobile-first single-column layout (360 px target): route form → result card → map behind a toggle. Desktop keeps the three-column demo layout | P0 |
| W2 | Register the service worker; switch the tile layer to `/tiles`; show "آخر تحديث قبل X" (last updated X ago) from `X-Tareeq-Fetched` when offline (netbar) | P0 |
| W3 | Saved trips: save a route with its tiles and last state; works fully offline; refreshes on reconnect | P1 |
| W4 | Report queue: reports made offline are queued in IndexedDB and sent on reconnect with their original (5-min-rounded) time | P1 |
| W5 | Delta sync: `/api/states?since=` returns only changed segments, compact binary or gzip JSON, target <20 KB typical, <200 KB worst case | P1 |
| W6 | On-device routing over a pruned corridor graph | P2 |

### 4.6 Operator console (new)

A password-protected admin page for the remote team. It has polygon entry and approval (S1–S2), the trusted roster (R1), the contested-segment queue (R5), the accuracy dashboard (A3), a stand editor, and broadcasts to all subscribers in an area, which need two-person approval.

### 4.7 Platform

| ID | Requirement | P |
| --- | --- | --- |
| P1 | Persistent store: PostGIS (or SQLite on disk for the pilot) for reports, subscriptions and audit log. Salt from a secret manager so hashes survive restarts. Purge job runs daily | P0 |
| P2 | `DEMO_MODE` flag: seed data, clock jump, fuel slider and the illustrative no-go areas exist only in demo mode. Production refuses to start with the seed on | P0 |
| P3 | Tests: state engine rules (verification, decay, official override, asymmetric safety), parser regression set, router invariants (never enters an unsafe buffer) | P0 |
| P4 | `requirements.txt`, README, git repo, a reproducible `ingest.py` run with pinned data snapshots | P0 |
| P5 | Hosting outside the region with a humanitarian or academic partner; encryption at rest; access logs | P0 |

## 5. Privacy and do-no-harm (v1 rules stand, plus)

- Reports keep a segment ID, rounded time and daily reporter hash. No GPS, no device ID. *(built)*
- **Subscriber numbers** are the one exception, because outbound SMS needs them. Store them encrypted, in a separate table from reports, with no link to report hashes. Delete them on `إلغاء` or after 30 days without activity.
- Arrival confirmations store quoted vs actual minutes and segment IDs, never origin or destination coordinates beyond the gazetteer place.
- We never show crowd counts or live gathering at stands. *(built: stand popups say so)*
- We publish the data-use policy in Arabic and reply with a short version to `خصوصية` ("privacy").

## 6. Success metrics (pilot, 8 weeks)

| Metric | Target |
| --- | --- |
| Confirmed trips with actual ≤ quoted P85 | 80–90% |
| Confirmed trips with actual ≤ quoted P50 | 40–60% |
| Suggested routes later reported blocked within 2 h | < 5% |
| Suggested routes crossing an official buffer | 0 |
| Median age of newest report on main corridor segments | < 3 h |
| Weekly active SMS users | 1,000 |
| Arrival-confirmation rate | ≥ 15% of route replies |
| Parser accuracy on regression set | ≥ 95% intent, ≥ 90% correct O/D |

## 7. Milestones

| Phase | Weeks | Exit criteria |
| --- | --- | --- |
| **Hardening** | 1–3 | All P0 defects fixed (O/D, trusted spoofing, persistence, Twilio, demo flag); tests green; mobile layout; SW wired |
| **Ground truth** | 3–5 | Arrival confirmation, asymmetric safety, operator console with real OCHA polygons and two-person approval; 20 trusted reporters enrolled |
| **Closed pilot** | 5–13 | Invited drivers and riders on one corridor; outbound alerts live; weekly accuracy review; observed speeds replacing priors where data allows |
| **Decision** | 13 | Go / no-go to widen, based on §6 metrics |

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| Too few reports, so the map decays to Unknown | Driver incentive: aggregate demand data back to drivers ("طلب مرتفع على [corridor]" = "high demand on [corridor]"), with no counts; trusted reporters on fixed beats |
| Coordinated false reports | Reporter weighting, burst filter, asymmetric safety, operator queue |
| Official data lag (evacuation orders faster than OCHA maps) | Trusted-reporter `unsafe` + S3 provisional warnings; operator quick-entry with two-person approval |
| Product used for targeting | Aggregates only, no trails, short retention, hosting outside the region, no raw-report sharing |
| SMS cost | Seek zero-rating; cap outbound alerts; WhatsApp where data exists |

## 9. Open questions

1. Which partner hosts the data and holds the Twilio or local SMS gateway account?
2. Can OCHA provide machine-readable evacuation-order polygons, or do operators digitise them by hand?
3. Who recruits and pays trusted reporters and drivers safely, and how?
4. Should a single trusted `open` report be allowed to clear a provisional `unsafe` warning, or only an operator?
