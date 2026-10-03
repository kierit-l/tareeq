# Gaza Transit Info — Product Spec


## Summary

An Arabic-first, offline-first service that tells Gaza residents which routes are passable today, what transport is running on them, and how long a trip will realistically take, with an honest confidence level on every answer.

Conventional transit apps assume a fixed network, published timetables and live GPS feeds. Gaza has none of these: roads are cratered or blocked by rubble, movement is shaped by military zones and checkpoints, and most trips run on informal shared taxis, tuk-tuks and donkey carts with no schedule. The product therefore treats *road status* and *observed trip times* as its core data, not timetables, and delivers them over the channels people can actually reach (low-end Android, SMS, WhatsApp) under intermittent power and connectivity.

## Context (as of Sept 2026)

Movement in Gaza is limited less by distance than by four things that change week to week: passable roads, the Yellow Line, fuel, and connectivity.

| Factor | Current state | Design implication |
| --- | --- | --- |
| Roads | 68–85% of the road network damaged or destroyed; Khan Younis has lost over 90% of routes ([The Intercept](https://theintercept.com/2026/03/09/israel-gaza-iran-war-transportation/)) | The road graph must carry a per-segment passability state, not assume OSM geometry is drivable |
| Vehicles | Over 80% of vehicles destroyed or damaged ([The National](https://www.thenationalnews.com/news/mena/2026/09/16/gazans-turn-to-taxi-trailers-as-israeli-shortages-narrow-transport-options/)) | Supply of rides is scarce and bursty; show likelihood of finding a ride, not a timetable |
| Modes | Car-towed open trailers (\~20 passengers), tuk-tuks, animal carts, cargo trucks, walking | Model each mode separately: speed, capacity and which surfaces it can cross differ |
| Trip times | Trips that took 5 minutes pre-war now take 30+ minutes; many trips end on foot | Multi-modal routing with a walking leg is the norm, not the edge case |
| Yellow Line | Ceasefire demarcation from Oct 2025; Israeli-controlled share grew from 53% toward 60%+ in 2026, with free-fire zones beyond it ([Wikipedia](<https://en.wikipedia.org/wiki/Yellow_Line_(Gaza)>)) | Hard no-go polygons that update often; routing must never send anyone near them |
| Fuel & parts | Severe shortage of fuel, lubricant oil and spare parts; only Kerem Shalom and Zikim operating ([OCHA, 15 May 2026](https://www.un.org/unispal/document/ocha-humanitarian-situation-report-15-may-2026/)) | Fares and service levels swing with fuel deliveries; track them as signals |
| Connectivity | Repeated phone and internet blackouts; many rely on eSIMs and patchy local networks | Offline-first app, SMS fallback, small payloads |

## Users and core jobs

The primary user is a Gaza resident on a low-end Android phone (or a basic feature phone), with intermittent power and data, reading Arabic.

| Persona | Typical trip | What they need answered |
| --- | --- | --- |
| Displaced parent in a camp (e.g. Al-Mawasi) | To a clinic or aid distribution point 5–10 km away | Is the road open? Where do trailers or tuk-tuks leave from? How long, and how much? |
| Patient or carer | Repeat trips to a functioning hospital | Fastest *safe* route today; how much walking; wheelchair or stretcher feasible? |
| Worker or student | Daily commute between Deir al-Balah and Gaza City | Usual departure points, current fare, typical wait and travel time by time of day |
| Driver (tuk-tuk, trailer, taxi) | Running a corridor all day | Where passengers are waiting; which roads are newly blocked; fuel availability |
| Family relocating | One-off move with belongings | Which modes carry goods; realistic cost; whether the destination area is reachable |

Core jobs, in priority order:

1. **Can I get there safely today?** Route passability and proximity to no-go zones.
2. **How long will it really take?** Door-to-door time, including waiting and walking legs.
3. **Where do I catch a ride, and what will it cost?** Stands, corridors, current fares.
4. **What changed?** Alerts for newly blocked roads, new evacuation orders, fuel shortages on my usual routes.

## What "accurate" means here

Accuracy means an honest estimate with a stated confidence and age, never a precise-looking time the data can't support. Every answer shows a time range, how fresh the evidence is, and how many independent reports back it.

**Segment state.** Each road segment carries a passability state per mode, with a timestamp and confidence:

| State | Meaning | Default routing behaviour |
| --- | --- | --- |
| Open | Recent reports of vehicles passing | Route normally |
| Degraded | Passable slowly (rubble, craters, flooding, crowds) | Route with a speed penalty; cart and foot preferred |
| Foot only | Vehicles can't pass; people on foot can | Vehicle legs end here; add a walking leg |
| Blocked | Impassable to all | Exclude |
| Unsafe | Inside or near a no-go zone, or recent strike reported | Exclude, with a buffer; never overridable by crowd reports |
| Unknown | No evidence in the decay window | Route only if no alternative, flagged clearly |

**Decay.** Confidence decays with time since the last report, faster for states that change quickly (crowding, Unsafe) and slower for physical ones (a collapsed bridge). Stale Open segments drift to Unknown, not to Open.

**Trip time.** Estimated as wait time + in-vehicle time + walking time, each from observed data: per-segment speeds by mode and time of day from completed trips, and wait times by stand and hour. Output is a range (e.g. 35–55 min) with the 50th and 85th percentile, not a single number.

**Measuring ourselves.** Users can confirm actual arrival time in one tap. Track the share of trips that land inside the quoted range (target 80%) and the share of route suggestions later reported as blocked (target under 5%).

## Data sources

Open datasets give the baseline map; fresh, ground-level reports from drivers and riders are what make it accurate day to day.

| Source | Gives us | Freshness | Notes |
| --- | --- | --- | --- |
| [OpenStreetMap](https://www.openstreetmap.org) + HOT tasking | Base road geometry, named places, landmarks | Weeks–months | Pre-war geometry; treat as candidate edges, not passable roads |
| [UNOSAT road damage assessment](https://data.humdata.org/dataset/unosat-gaza-strip-road-network-comprehensive-damage-assessment) (HDX) | Per-segment damage class from satellite imagery | Snapshot (imagery June 2024; later building assessments to Oct 2025) | CC BY-SA; seeds initial Degraded/Blocked priors |
| UNOSAT building damage assessments | Rubble density near roads | Periodic | Proxy for debris spill onto roads |
| OCHA / UNRWA access maps and evacuation orders | No-go areas, Yellow Line, crossings, aid sites | Days | Primary source for Unsafe polygons |
| Logistics Cluster / UNMAS | Convoy-cleared routes, unexploded ordnance hazards | Days | Partner data-sharing agreement needed |
| Driver network (tuk-tuk, trailer, taxi) | Segment states, fares, stands, fuel price | Hours | Highest-value source; incentivise via passenger demand data |
| Rider reports | "Passed / blocked here", wait and arrival times | Hours | One-tap reports in app and via SMS keywords |
| Trusted reporters | Verified checks on contested segments | Hours | Community volunteers with higher weight |

**Verification.** A report changes a segment state only when it meets a threshold: one trusted reporter, or two or more independent reporters within the decay window. Reports that make a road look safer than an Unsafe source says are always discarded. Outliers (e.g. one user reporting many segments far apart in minutes) are down-weighted automatically.

## Product surface

One data layer, three channels, ordered by how many people each reaches when the network is at its worst.

| Channel | Works when | Core features |
| --- | --- | --- |
| SMS / USSD | Only basic GSM signal | Text "طريق \[from\] \[to\]" (route) → reply with mode, time range, status, confidence; report keywords like "مغلق \[place\]" (closed) |
| WhatsApp / Telegram bot | Low, patchy data | Same as SMS plus location pin, voice notes in Arabic, alerts on saved routes |
| Android PWA (offline-first) | Data available occasionally | Offline map tiles and road states synced in small deltas; routing runs on the phone; one-tap reports queued until online |

**UX principles**

- Arabic first, right-to-left, Gaza place names as residents use them (camps, landmarks, "near the X roundabout"), not only official names.
- Show status in words and colour, never colour alone; work on small screens and in bright sun.
- Every answer shows *how old* and *how sure*: "Open · reported 40 min ago by 3 people".
- Under 200 KB per sync and under 160 characters per SMS reply.
- Sharing a route or a report never requires an account.

## Architecture

The heart of the system is a store of road-segment states, fed by verified reports and overridden by safety data, which a routing engine turns into trip-time ranges for each channel.

&#91;embedded content: system architecture · inputs to channels, with feedback loop\]

- **Graph.** OSM roads split into short segments (about 100–300 m), each with a state per mode (foot, cart, tuk-tuk, car/trailer). No-go polygons cut segments out entirely with a safety buffer.
- **Routing.** A multi-modal router (e.g. OpenTripPlanner or Valhalla with custom costing) treats walking legs and "stands" where informal transport gathers as transfer points. Edge cost = observed speed for that mode and hour, plus a penalty for low confidence.
- **Trip-time model.** Starts from simple per-segment medians; moves to a learned model once there are enough confirmed trips.
- **Sync.** The offline app downloads a compressed delta of changed segments (target under 200 KB) and routes on the phone. SMS and bots query the server.
- **Stack.** PostGIS for geometry and state; a small API service; message queue for incoming reports; SMS gateway via a local operator or aggregator. Keep it simple enough for a small team to run remotely.

## Safety, privacy and do-no-harm

A map of where crowds gather and which roads are in use could put people in danger, so the product collects the least data possible and publishes only aggregates.

- **No location trails.** Reports carry a segment ID and a coarse timestamp, never a GPS track or a device ID. Phone numbers used for SMS are hashed and discarded after rate-limiting.
- **No crowd-size or live-gathering data.** Show road state and typical wait, never "120 people at stand X right now".
- **Safety beats speed.** Unsafe polygons from OCHA/UN sources always win; crowd reports can make a route *less* safe, never more. Routes keep a buffer from the Yellow Line and active evacuation-order areas.
- **Say what we don't know.** Unknown is shown as Unknown. Every route carries a reminder that conditions change fast and official evacuation orders take priority.
- **Neutrality.** Follow the humanitarian principles; no political content, no sharing of raw reports with any party to the conflict; publish a data-use policy in Arabic.
- **Misinformation.** Rate-limit reporters, weight by track record, and let trusted reporters overturn false closures quickly.
- **Hosting.** Store data outside the region with a humanitarian or academic partner, encrypted at rest, with short retention (e.g. raw reports deleted after 14 days).

## Partners, MVP and roadmap

Start with one corridor and the SMS/WhatsApp channel, prove the trip-time estimates are within range 80% of the time, then widen.

**Partners to approach:** a local tech or civil-society group in Gaza for reporters and drivers' networks; OCHA oPt and the Logistics Cluster for access and no-go data; HOT (Humanitarian OpenStreetMap Team) for mapping; UNOSAT for updated road damage layers; a telecom or eSIM provider for zero-rated SMS.

| Phase | Scope | Exit criteria |
| --- | --- | --- |
| 0. Discovery (4–6 wks) | Remote interviews with residents and drivers; pick the pilot corridor; data-sharing MoUs | Corridor chosen; 30+ drivers and 20+ trusted reporters signed up |
| 1. MVP (8–10 wks) | One corridor (e.g. Deir al-Balah ↔ Khan Younis ↔ Al-Mawasi); SMS + WhatsApp bot; segment states and trip-time ranges | 80% of trips inside the quoted range; under 5% of suggested routes reported blocked |
| 2. Strip-wide | All populated areas west of the Yellow Line; offline PWA; alerts on saved routes | 10,000 monthly users; median report age under 3 hours on main roads |
| 3. Reconstruction | Formal routes as they return (buses, repaired roads); open API for aid agencies | Partner agencies using the data feed |

**Open questions**

- Which pilot corridor has enough drivers and reporters to keep data fresh?
- Can we get zero-rated SMS from Jawwal or Ooredoo Palestine, or must it be paid?
- Who in Gaza owns the reporter network day to day, and how are they paid safely?
- Which entity hosts the data and signs the data-sharing agreements?

## Sources

- [The Intercept, 9 Mar 2026 — Gaza roads and transit](https://theintercept.com/2026/03/09/israel-gaza-iran-war-transportation/)
- [The National, 16 Sep 2026 — taxi trailers](https://www.thenationalnews.com/news/mena/2026/09/16/gazans-turn-to-taxi-trailers-as-israeli-shortages-narrow-transport-options/)
- [OCHA situation report, 15 May 2026](https://www.un.org/unispal/document/ocha-humanitarian-situation-report-15-may-2026/)
- [Yellow Line (Gaza), Wikipedia](<https://en.wikipedia.org/wiki/Yellow_Line_(Gaza)>)
- [UNOSAT road network damage assessment, HDX](https://data.humdata.org/dataset/unosat-gaza-strip-road-network-comprehensive-damage-assessment)
