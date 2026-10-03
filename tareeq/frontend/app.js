// Tareeq web client: map of segment states, route planner, SMS simulator, demo controls.
// UI language: Arabic by default, English for presenting. Switching reloads; backend data and SMS replies stay Arabic.
const LANG = localStorage.getItem("tareeq.lang") === "en" ? "en" : "ar", EN = LANG === "en";
const T = (a, e) => (EN ? e : a);
document.documentElement.lang = LANG;
document.documentElement.dir = EN ? "ltr" : "rtl";
if (EN) {
  document.title = "Tareeq — Gaza road status & trip times";
  document.querySelectorAll("[data-en]").forEach((el) => (el.innerHTML = el.dataset.en));
  document.querySelectorAll("[data-en-ph]").forEach((el) => (el.placeholder = el.dataset.enPh));
}
document.getElementById("langtoggle").textContent = T("English", "العربية");
document.getElementById("langtoggle").addEventListener("click", () => {
  localStorage.setItem("tareeq.lang", EN ? "ar" : "en");
  location.reload();
});
const ARROW = T("←", "→");
const STATE_INFO = {
  open:      { ar: "مفتوح",     en: "Open",      color: "#1a7f37", dash: null,    w: 4 },
  degraded:  { ar: "صعب",       en: "Degraded",  color: "#b26b00", dash: "8 5",   w: 4 },
  foot_only: { ar: "مشاة فقط",  en: "Foot only", color: "#6f42c1", dash: "2 5",   w: 4 },
  blocked:   { ar: "مغلق",      en: "Blocked",   color: "#c62828", dash: null,    w: 6 },
  unsafe:    { ar: "خطر",       en: "Unsafe",    color: "#6d0f0f", dash: "1 4",   w: 5 },
  unknown:   { ar: "غير معروف", en: "Unknown",   color: "#8a8a8a", dash: null,    w: 1.5 },
};
const MODE_ICON = { foot: "🚶", tuktuk: "🛺", trailer: "🚙", cart: "🫏" };
const MODE_COLOR = { foot: "#222", tuktuk: "#0b5cad", trailer: "#3f2d9c", cart: "#7a4a1d" };
const SOURCE_AR = { v: "بلاغات مؤكدة", p: "صور أقمار صناعية ٢٠٢٤", o: "مصدر رسمي (OCHA/UN)", n: "لا بيانات" };
const SOURCE_EN = { v: "verified reports", p: "2024 satellite imagery", o: "official source (OCHA/UN)", n: "no data" };
const MODE_EN = { foot: "Walk", tuktuk: "Tuk-tuk", trailer: "Trailer", cart: "Donkey cart" };
const stLabel = (s) => STATE_INFO[s][LANG];
// Place and stand names come from the backend in Arabic; English mode shows the OSM English name where one exists.
const PLACE_EN = {}, STAND_PLACE = {};
const pn = (n) => (EN && PLACE_EN[n]) || n;
const standName = (n) => (EN && STAND_PLACE[n] ? `${pn(STAND_PLACE[n])} stand` : n);
const ar = (x) => EN ? String(x) : String(x).replace(/\d/g, (d) => "٠١٢٣٤٥٦٧٨٩"[d]).replace(/\./g, "٫");
const ageAr = (m) => (m == null || m < 0) ? T("لا بلاغات حديثة", "no recent reports")
  : m < 60 ? T(`قبل ${ar(m)} دقيقة`, `${m} min ago`) : T(`قبل ${ar(Math.round(m / 60))} ساعة`, `${Math.round(m / 60)} h ago`);
// api.cachedAt: ms epoch when the last response was fetched if the service worker served a stored copy, else null.
const api = async (path, opts = {}) => {
  let r;
  try {
    r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined });
  } catch { setNet(false); throw new Error("offline"); }
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail;
    if (detail === "offline") setNet(false);   // the service worker had no stored copy
    throw new Error(detail || r.statusText);
  }
  api.cachedAt = r.headers.has("X-Tareeq-Fetched") ? +r.headers.get("X-Tareeq-Fetched") : null;
  if (api.cachedAt === null && !online) setNet(true);
  return r.json();
};

const map = L.map("map", { preferCanvas: true, zoomControl: true }).setView([31.37, 34.30], 13);
// Basemap: one self-hosted vector file for the whole Strip (see backend/main.py), so it can be stored for offline use.
const BASEMAP_URL = "/map/gaza.pmtiles";
protomapsL.leafletLayer({ url: BASEMAP_URL, flavor: "light", lang: LANG, maxDataZoom: 14, className: "basemap",
  attribution: "© OpenStreetMap contributors · Protomaps" }).addTo(map);
map.createPane("nogo").style.zIndex = 390;
map.createPane("route").style.zIndex = 450;
const routeRenderer = L.canvas({ pane: "route" });

let STATES = [], stateData = {}, segLayers = {}, segProps = {}, routeLayer = L.layerGroup().addTo(map);
let nogoLayer = L.layerGroup().addTo(map), lastChangeTs = 0, lastRoute = null;
const sender = "+970-59-" + Math.floor(100000 + Math.random() * 899999);
document.getElementById("sender").textContent = sender.replace(/\d{4}$/, "xxxx");

function styleFor(id) {
  const d = stateData[id];
  const st = d ? STATES[d[0]] : "unknown";
  const info = STATE_INFO[st];
  const conf = d ? d[1] / 100 : 0;
  if (d && d[4] === "p")  // weak satellite prior (2024): draw faint so fresh evidence stands out
    return { color: info.color, weight: 2, dashArray: info.dash, opacity: 0.35 };
  return { color: info.color, weight: info.w, dashArray: info.dash,
           opacity: st === "unknown" ? 0.5 : 0.5 + 0.5 * Math.max(conf, 0.3) };
}

function popupHtml(id) {
  const p = segProps[id], d = stateData[id] || [5, 0, -1, 0, "n", 0];
  const st = STATES[d[0]], info = STATE_INFO[st];
  const name = p.name || (p.highway === "residential" ? T("شارع فرعي", "Side street") : T("طريق", "Road"));
  const evidence = d[4] === "v"
    ? `${stLabel(st)} · ${ageAr(d[2])} · ${T(`${ar(d[3])} ${d[3] > 2 ? "أشخاص" : "شخص"}`, `${d[3]} ${d[3] === 1 ? "person" : "people"}`)}`
    : `${stLabel(st)} · ${T(SOURCE_AR[d[4]], SOURCE_EN[d[4]])}`;
  const pend = d[5] ? `<div class="note">⏳ ${T(`${ar(d[5])} بلاغ بانتظار التأكيد (يلزم شخصان مستقلان أو مراسل موثوق)`,
    `${d[5]} report(s) awaiting confirmation (needs 2 independent people or a trusted reporter)`)}</div>` : "";
  const dmg = p.damage && !["none", "not_assessed"].includes(p.damage) ? `<div class="note">UNOSAT 2024: ${p.damage}</div>` : "";
  return `<div class="pop"><h4>${name}</h4>
    <div><span class="st" style="color:${info.color}">${stLabel(st)}</span> ${T(`<small>${info.en}</small> · ثقة ${ar(d[1])}٪`, `· ${d[1]}% confidence`)}</div>
    <div class="note">${evidence}</div>${pend}${dmg}
    <div class="rep">
      ${["open", "degraded", "foot_only", "blocked", "unsafe"].map((s) =>
        `<button data-seg="${id}" data-state="${s}">${T("أبلغ", "Report")}: ${stLabel(s)}</button>`).join("")}
    </div>
    <div class="note">${T("كل ضغطة = بلاغ من شخص مختلف (محاكاة). Each click simulates a new independent reporter.", "Each click simulates a new independent reporter.")}</div></div>`;
}

async function loadSegments() {
  const gj = await api("/api/segments");
  const renderer = L.canvas({ tolerance: 6 });
  for (const f of gj.features) {
    const id = f.properties.id;
    segProps[id] = f.properties;
    const ll = f.geometry.coordinates.map(([x, y]) => [y, x]);
    const line = L.polyline(ll, { renderer, ...styleFor(id) });
    line.on("click", (e) => L.popup().setLatLng(e.latlng).setContent(popupHtml(id)).openOn(map));
    line.addTo(map);
    segLayers[id] = line;
  }
}

async function refreshStates() {
  const s = await api("/api/states");
  statesCachedAt = api.cachedAt;
  updateNetbar();
  STATES = s.states; stateData = s.data;
  for (const id in segLayers) segLayers[id].setStyle(styleFor(id));
  const d = new Date(s.now * 1000);
  document.getElementById("clock").textContent = "🕒 " + d.toLocaleString(T("ar-PS", "en-GB"), { timeZone: "Asia/Gaza", weekday: "short", hour: "2-digit", minute: "2-digit" }) + T(" (محاكاة)", " (simulated)");
  await refreshChanges().catch(() => {});
}

async function refreshChanges() {
  const c = await api(`/api/changes?since=0`);
  const ul = document.getElementById("changes");
  if (!c.events.length) { ul.innerHTML = `<li class="muted">${T("لا تغييرات بعد", "No changes yet")}</li>`; return; }
  ul.innerHTML = c.events.slice(-30).reverse().map((e) => {
    const p = segProps[e.seg] || {};
    const why = e.reason.startsWith("nogo") ? T("أمر إخلاء/منطقة محظورة", "evacuation order / no-go zone") : T("بلاغات مؤكدة", "verified reports");
    return `<li><a href="#" data-zoom="${e.seg}">${p.name || T("مقطع ", "Segment ") + ar(e.seg)}</a>: ${stLabel(e.from)} ${ARROW} <b style="color:${STATE_INFO[e.to].color}">${stLabel(e.to)}</b> <small>${why}</small></li>`;
  }).join("");
}

async function loadPlaces() {
  const p = await api("/api/places");
  for (const x of p.places) if (x.en) for (const n of [x.name, ...(x.aliases || [])]) PLACE_EN[n] ??= x.en;
  for (const s of p.stands) STAND_PLACE[s.name] = s.place;
  document.getElementById("placelist").innerHTML = p.places.map((x) =>
    EN ? `<option value="${x.en || x.name}">${x.en ? x.name : ""}</option>` : `<option value="${x.name}">${x.en || ""}</option>`).join("");
  for (const x of p.places.filter((x) => ["town", "village", "refugee_site", "hospital", "roundabout"].includes(x.kind))) {
    L.marker([x.lat, x.lon], { icon: L.divIcon({ className: "place-label", html: pn(x.name), iconSize: [120, 16], iconAnchor: [60, -4] }), interactive: false }).addTo(map);
  }
  for (const s of p.stands) {
    L.marker([s.lat, s.lon], { icon: L.divIcon({ className: "stand-ico", html: "🚏", iconSize: [24, 24] }) })
      .bindPopup(`<div class="pop"><h4>${standName(s.name)}</h4><div>${s.modes.map((m) => MODE_ICON[m]).join(" ")}</div>
        <div class="note">${T(`انتظار نموذجي ~${ar(s.wait_median_min)} دقيقة · أجرة أساسية ${ar(s.base_fare_ils)}₪ (توضيحي)`,
          `Typical wait ~${s.wait_median_min} min · base fare ${s.base_fare_ils}₪ (illustrative)`)}</div>
        <div class="note">${T("لا نعرض أعداد الناس في المواقف — حمايةً للسلامة.", "We never show how many people are at a stand, for safety.")}</div></div>`).addTo(map);
  }
}

async function loadNogo() {
  const fc = await api("/api/nogo");
  nogoLayer.clearLayers();
  for (const f of fc.features) {
    if (!f.properties.active) continue;
    L.geoJSON(f, { pane: "nogo", style: { color: "#6d0f0f", weight: 2, fillColor: "#c62828", fillOpacity: 0.18, dashArray: "6 4" }, interactive: true })
      .bindTooltip(T(`⛔ ${f.properties.name}<br><small>${f.properties.name_en} — يتم تجنبه مع هامش ٣٠٠م</small>`,
      `⛔ ${f.properties.name_en}<br><small>Avoided with a 300 m buffer</small>`), { sticky: true })
      .addTo(nogoLayer);
  }
}

function drawRoute(r) {
  routeLayer.clearLayers();
  if (!r) return;
  const bounds = [];
  for (const pass of ["casing", "line"]) {
    for (const leg of r.legs) {
      const lls = leg.segments.map((sid) => segLayers[sid].getLatLngs());
      if (pass === "casing") {
        lls.forEach((ll) => bounds.push(...ll));
        L.polyline(lls, { renderer: routeRenderer, color: "#fff", weight: 11, opacity: 0.9, interactive: false }).addTo(routeLayer);
      } else {
        L.polyline(lls, { renderer: routeRenderer, color: MODE_COLOR[leg.mode], weight: 6, interactive: false,
                          dashArray: leg.mode === "foot" ? "4 8" : null }).addTo(routeLayer);
      }
    }
  }
  if (bounds.length) map.fitBounds(L.latLngBounds(bounds).pad(0.2), { paddingTopLeft: [0, 0] });
}

// cachedAt: when this answer was fetched, if it is a stored copy (saved trip or offline fallback) rather than live.
function renderResult(data, cachedAt = null) {
  const el = document.getElementById("result");
  el.hidden = false;
  const r = data.route;
  lastRoute = data;
  if (!r) {
    el.innerHTML = staleNote(cachedAt) + `<div class="big" style="color:var(--blocked)">${T("لا يوجد طريق آمن معروف", "No known safe route")}</div>
      <p>${pn(data.from.name)} ${ARROW} ${pn(data.to.name)}</p><div class="warn">${T("لا تخاطر. اتبع أوامر الإخلاء الرسمية.", "Don't take the risk. Follow official evacuation orders.")}</div>`;
    drawRoute(null);
    return;
  }
  const confAr = T({ high: "ثقة عالية", medium: "ثقة متوسطة", low: "ثقة ضعيفة" },
                   { high: "High confidence", medium: "Medium confidence", low: "Low confidence" })[r.confidence_label];
  const share = Object.entries(r.state_share).sort((a, b) => b[1] - a[1]);
  const evid = r.newest_report_min != null
    ? T(`${stLabel(share[0][0])} · آخر بلاغ ${ageAr(r.newest_report_min)} · حتى ${ar(r.max_reporters)} أشخاص لكل مقطع`,
        `Mostly ${stLabel(share[0][0]).toLowerCase()} · last report ${ageAr(r.newest_report_min)} · up to ${r.max_reporters} people per segment`)
    : T("لا بلاغات حديثة على هذا الطريق", "No recent reports on this route");
  const legs = r.legs.map((l) => {
    const head = l.mode === "foot" ? T(`مشي ${ar(l.km)} كم`, `Walk ${l.km} km`) : T(`${l.mode_ar} من ${l.stand}`, `${MODE_EN[l.mode]} from ${standName(l.stand)}`);
    const det = l.mode === "foot" ? T(`~${ar(l.ride_min)} دقيقة`, `~${l.ride_min} min`)
      : T(`انتظار ~${ar(l.wait_min)} د · ركوب ~${ar(l.ride_min)} د · ${ar(l.km)} كم · ~${ar(l.fare_ils)}₪`,
          `wait ~${l.wait_min} min · ride ~${l.ride_min} min · ${l.km} km · ~${l.fare_ils}₪`);
    return `<li><span class="ico">${MODE_ICON[l.mode]}</span><div><b>${head}</b><br><small>${det}</small></div></li>`;
  }).join("");
  const warns = [];
  const pct = (x) => ar(Math.round(x * 100)) + T("٪", "%");
  if (r.unknown_share >= 0.1) warns.push(T(`${pct(r.unknown_share)} من الطريق بلا بيانات حديثة — كن حذراً.`, `${pct(r.unknown_share)} of the route has no recent data — be careful.`));
  if (r.state_share.foot_only) warns.push(T("جزء من الطريق للمشاة فقط.", "Part of the route is passable on foot only."));
  if (r.provisional_share) warns.push(T("بلاغ إغلاق واحد غير مؤكد على هذا الطريق — لا يوجد بديل أفضل حالياً.", "One unconfirmed closure report on this route — no better alternative right now."));
  if (r.weak_evidence_share > 0.2) warns.push(T(`${pct(r.weak_evidence_share)} من الطريق يعتمد على صور أقمار ٢٠٢٤ أو لا بيانات.`, `${pct(r.weak_evidence_share)} of the route relies on 2024 satellite imagery or no data.`));
  if (data.signals.fuel_index > 1.05) warns.push(T(`نقص وقود: الانتظار أطول والأجرة أعلى (×${ar(data.signals.fuel_index.toFixed(1))}).`, `Fuel shortage: longer waits and higher fares (×${data.signals.fuel_index.toFixed(1)}).`));
  el.innerHTML = `
    <div class="big">${ar(r.p50_min)}–${ar(r.p85_min)} <small>${T("دقيقة", "min")}</small></div>
    <div class="pctl">${T(`الوسيط ${ar(r.p50_min)} د · ٨٥٪ من الرحلات خلال ${ar(r.p85_min)} د`, `Median ${r.p50_min} min · 85% of trips within ${r.p85_min} min`)} <small>(P50–P85, door to door)</small></div>
    <div class="badges">
      <span class="badge conf-${r.confidence_label}">${confAr}</span>
      ${r.demo ? `<span class="badge" title="Simulated reports">${T("تجريبي · demo", "Demo data")}</span>` : ""}
      <span class="badge">${ar(r.total_km)} ${T("كم", "km")}</span>
      ${r.fare_ils ? `<span class="badge">~${ar(r.fare_ils)}₪</span>` : ""}
      <span class="badge">${T(`مشي ${ar(r.walk_km)} كم`, `${r.walk_km} km walking`)}</span>
    </div>
    <div class="evidence">${evid}</div>
    <div class="bar">${share.map(([s, v]) => `<span style="width:${v * 100}%;background:${STATE_INFO[s].color}" title="${stLabel(s)}"></span>`).join("")}</div>
    <div class="barkey">${share.map(([s, v]) => `<span>${stLabel(s)} ${pct(v)}</span>`).join("")}</div>
    <ul class="legs">${legs}</ul>
    ${warns.map((w) => `<div class="warn">⚠️ ${w}</div>`).join("")}
    <div class="sms-preview">📱 <b>${T(`رد SMS (${ar(data.sms.length)} حرف):`, `SMS reply (${data.sms.length} chars, sent in Arabic):`)}</b><br>${data.sms}</div>
    <div class="disclaimer">${T("الظروف تتغير بسرعة. أوامر الإخلاء الرسمية لها الأولوية دائماً.", "Conditions change fast. Official evacuation orders always take priority.")}</div>`;
  el.insertAdjacentHTML("afterbegin", staleNote(cachedAt));
  el.insertAdjacentHTML("beforeend", saveControlHtml(data));
  drawRoute(r);
}

async function planRoute() {
  const modes = [...document.querySelectorAll(".modes input[value]:checked")].map((x) => x.value).join(",");
  const accessible = document.getElementById("accessible").checked;
  const q = new URLSearchParams({ frm: from.value, to: to.value, modes, accessible });
  try {
    const data = await api(`/api/route?${q}`);
    data.query = { frm: from.value, to: to.value, modes, accessible };
    data.fetchedAt = api.cachedAt ?? Date.now();
    renderResult(data, api.cachedAt);
  } catch (e) {
    const saved = findSaved(from.value, to.value, modes, accessible);
    if (saved) return renderResult(saved.data, saved.savedAt);
    const el = document.getElementById("result"); el.hidden = false;
    el.innerHTML = e.message === "offline"
      ? T(`<div class="warn">📴 لا يوجد اتصال، وهذه الرحلة غير محفوظة على هاتفك. اختر رحلة من «رحلاتي المحفوظة»، أو أرسل SMS عادية: <b>طريق ${from.value} ${to.value}</b> — الرسائل لا تحتاج إنترنت.</div>`,
          `<div class="warn">📴 No connection, and this trip isn't saved on your phone. Pick one from "My saved trips", or send a plain SMS: <b>طريق ${from.value} ${to.value}</b> — SMS needs no internet.</div>`)
      : `<div class="warn">${e.message}</div>`;
  }
}

// ---- SMS simulator ----
const chat = document.getElementById("chat");
function bubble(text, cls, meta) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.textContent = text;
  if (meta) { const m = document.createElement("span"); m.className = "meta"; m.textContent = meta; d.appendChild(m); }
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
}
async function sendSms(text) {
  if (!text.trim()) return;
  bubble(text, "me");
  let r;
  try { r = await api("/api/sms", { method: "POST", body: { text, sender } }); }
  catch { return bubble(T("📴 المحاكي يحتاج اتصالاً بالخادم. (رسائل SMS الحقيقية تعمل بدون إنترنت.)", "📴 The simulator needs the server. (Real SMS works without internet.)"), "alert", "simulator offline"); }
  bubble(r.reply, "bot", `${r.chars} chars · parsed by ${r.parsed.via}`);
  if (r.route) { renderResult({ route: r.route, sms: r.reply, from: { name: r.parsed.from }, to: { name: r.parsed.to }, signals: { fuel_index: +fuel.value } }); }
  if (r.results) await afterChange();
}
document.getElementById("smsform").addEventListener("submit", (e) => { e.preventDefault(); sendSms(smsin.value); smsin.value = ""; });
document.getElementById("chips").addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") sendSms(e.target.textContent); });
bubble(T("مرحباً! أرسل: طريق [من] [إلى]\nمثال: طريق المواصي مستشفى ناصر", "Hi! Send: طريق [from] [to]\n(طريق = \"route\"; replies are in Arabic)\ne.g. طريق المواصي مستشفى ناصر"), "bot", "Tareeq SMS");

async function pollAlerts() {
  const a = await api(`/api/alerts?sender=${encodeURIComponent(sender)}`).catch(() => ({ alerts: [] }));
  for (const m of a.alerts) bubble("🔔 " + m, "alert", "alert · saved route");
}

// ---- reporting from the map ----
map.on("popupopen", (e) => {
  e.popup.getElement().querySelectorAll("button[data-seg]").forEach((b) => b.addEventListener("click", async () => {
    const trusted = document.getElementById("trusted").checked;
    const reporter = (trusted ? "trusted-" : "rider-") + Math.random().toString(36).slice(2);
    let r;
    try { r = await api("/api/report", { method: "POST", body: { segment: +b.dataset.seg, state: b.dataset.state, reporter, trusted } }); }
    catch { return e.popup.setContent(popupHtml(+b.dataset.seg) + `<div class="note"><b>${T("📴 لا يوجد اتصال — لم يُرسل البلاغ. حاول لاحقاً أو أرسله SMS.", "📴 No connection — report not sent. Try later or send it by SMS.")}</b></div>`); }
    const res = r.results[0];
    await afterChange();
    const id = +b.dataset.seg;
    e.popup.setContent(popupHtml(id) + `<div class="note"><b>${res.ignored_reason === "contradicts_official_nogo"
      ? T("⛔ رُفض: يتعارض مع منطقة خطر رسمية — البلاغات لا تجعل الطريق أكثر أماناً.", "⛔ Rejected: contradicts an official danger zone — reports can't make a road safer.")
      : res.changed ? T("✅ تم التأكيد وتغيّرت الحالة", "✅ Confirmed — status changed") : T("⏳ سُجل — ننتظر تأكيداً مستقلاً", "⏳ Logged — waiting for independent confirmation")}</b></div>`);
    e.popup.getElement() && map.fire("popupopen", { popup: e.popup });
  }));
});

async function afterChange() {
  await refreshStates();
  if (lastRoute && lastRoute.from && lastRoute.from.node !== undefined) await planRoute();
  else if (document.getElementById("result").hidden === false) await planRoute();
  await pollAlerts();
}

// ---- demo controls ----
document.querySelectorAll("[data-clock]").forEach((b) => b.addEventListener("click", async () => {
  await api("/api/clock", { method: "POST", body: { hours: +b.dataset.clock } });
  await afterChange();
}));
const fuel = document.getElementById("fuel");
fuel.addEventListener("input", () => (fuelv.textContent = ar((+fuel.value).toFixed(1)) + "×"));
fuel.addEventListener("change", async () => { await api("/api/signals", { method: "POST", body: { fuel_index: +fuel.value } }); await afterChange(); });
document.getElementById("evac").addEventListener("change", async (e) => {
  await api("/api/nogo/evac_order_demo", { method: "POST", body: { active: e.target.checked } });
  await loadNogo(); await afterChange();
});
document.getElementById("reset").addEventListener("click", async () => {
  await api("/api/reset", { method: "POST" });
  fuel.value = 1; fuelv.textContent = ar("1.0") + "×"; document.getElementById("evac").checked = false;
  await loadNogo(); await afterChange();
});
document.getElementById("go").addEventListener("click", planRoute);
document.getElementById("changes").addEventListener("click", (e) => {
  const id = e.target.dataset && e.target.dataset.zoom;
  if (id) { e.preventDefault(); map.fitBounds(segLayers[id].getBounds().pad(2)); }
});

// legend
document.getElementById("legend").innerHTML = Object.entries(STATE_INFO).map(([k, v]) =>
  `<div><svg width="34" height="8"><line x1="0" y1="4" x2="34" y2="4" stroke="${v.color}" stroke-width="${Math.min(v.w, 5)}" ${v.dash ? `stroke-dasharray="${v.dash}"` : ""}/></svg>${T(`${v.ar} <small>${v.en}</small>`, v.en)}</div>`).join("") +
  `<div><svg width="34" height="10"><rect width="34" height="10" fill="#c6282830" stroke="#6d0f0f" stroke-dasharray="4 3"/></svg>${T("منطقة محظورة <small>No-go (+300 m)</small>", "No-go zone <small>(+300 m buffer)</small>")}</div>`;

// ---- offline: saved trips and the stored basemap ----
// No live location by design: a saved trip is the route drawn on a stored map, followed by place names and stands.
const SAVED_KEY = "tareeq.saved.v1", MAX_SAVED = 10, MAP_CACHE = "tareeq-map-v1";
let statesCachedAt = null, online = navigator.onLine;
const loadSaved = () => { try { return JSON.parse(localStorage.getItem(SAVED_KEY)) || []; } catch { return []; } };
const storeSaved = (list) => localStorage.setItem(SAVED_KEY, JSON.stringify(list.slice(0, MAX_SAVED)));
const tripKey = (d) => JSON.stringify([d.query.frm, d.query.to, d.query.modes, d.query.accessible]);
// Typed text depends on the UI language, so also accept the matched place's Arabic and English names.
const findSaved = (frm, to, modes, accessible) => loadSaved().find(({ data: d }) =>
  d.query.modes === modes && d.query.accessible === accessible &&
  [d.query.frm, d.from.name, pn(d.from.name)].includes(frm) && [d.query.to, d.to.name, pn(d.to.name)].includes(to));
const sinceAr = (ts) => { const m = Math.round((Date.now() - ts) / 60000); return m < 1 ? T("الآن", "just now") : ageAr(m); };

function setNet(isOnline) { online = isOnline; updateNetbar(); }
function updateNetbar() {
  const bar = document.getElementById("netbar");
  const age = statesCachedAt ? T(` — حالة الطرق محفوظة ${sinceAr(statesCachedAt)}`, ` — road status saved ${sinceAr(statesCachedAt)}`) : "";
  bar.hidden = online && !statesCachedAt;
  bar.className = "netbar" + (online ? " weak" : " off");
  bar.innerHTML = online
    ? T(`📶 اتصال ضعيف${age}. <small>Weak connection: showing stored data</small>`, `📶 Weak connection${age}. Showing stored data.`)
    : T(`📴 لا يوجد اتصال${age}. الرحلات المحفوظة تعمل. <small>Offline</small>`, `📴 Offline${age}. Saved trips still work.`);
}
window.addEventListener("online", () => { setNet(true); refreshStates().catch(() => {}); });
window.addEventListener("offline", () => setNet(false));

function staleNote(ts) {
  return ts ? `<div class="warn">${T(`📴 نسخة محفوظة ${sinceAr(ts)} — حالة الطرق ربما تغيّرت منذ ذلك. اسأل السائقين وتحقق عند توفر اتصال.`,
    `📴 Saved copy from ${sinceAr(ts)} — roads may have changed since. Ask drivers and re-check when you're online.`)}</div>` : "";
}

const basemapStored = async () => "caches" in window && !!(await (await caches.open(MAP_CACHE)).match(BASEMAP_URL));

// The basemap is one file for the whole Strip: stored once, it serves every saved trip.
async function storeBasemap(status) {
  const res = await fetch(BASEMAP_URL, { cache: "no-store" });
  if (!res.ok) throw new Error(res.statusText);
  const reader = res.body.getReader(), parts = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parts.push(value); got += value.length;
    status.textContent = T(`⬇️ خريطة غزة: ${ar((got / 1048576).toFixed(1))} ميغابايت`, `⬇️ Gaza map: ${(got / 1048576).toFixed(1)} MB`);
  }
  const blob = new Blob(parts);
  await (await caches.open(MAP_CACHE)).put(BASEMAP_URL, new Response(blob, {
    headers: { "Content-Type": "application/vnd.pmtiles", "Content-Length": String(blob.size) } }));
}

function saveControlHtml(data) {
  if (!data.query || !("serviceWorker" in navigator)) return "";
  const saved = loadSaved().some((t) => t.key === tripKey(data));
  basemapStored().then((has) => {
    const st = document.getElementById("savestatus");
    if (st && !saved) st.textContent = has ? T("الخريطة محفوظة مسبقاً · الطريق أقل من ١٠ كيلوبايت", "Map already stored · this route is under 10 KB")
                                         : T("أول مرة: خريطة غزة كاملة ~٤٫٤ ميغابايت، مرة واحدة لكل الرحلات", "First time: full Gaza map ~4.4 MB, once for all trips");
  });
  return `<div class="savebox"><button id="savetrip">${saved ? T("🔄 حدّث النسخة المحفوظة", "🔄 Update saved copy") : T("📥 احفظ للاستخدام بدون إنترنت", "📥 Save for offline use")}</button>
    <small id="savestatus">${saved ? T("✅ محفوظة على هاتفك", "✅ Saved on your phone") : ""}</small></div>`;
}

async function saveTrip() {
  const data = lastRoute, btn = document.getElementById("savetrip"), status = document.getElementById("savestatus");
  if (!data?.route || !data.query) return;
  btn.disabled = true;
  await navigator.serviceWorker.ready;
  if (!navigator.serviceWorker.controller) { status.textContent = T("أعد تحميل الصفحة مرة واحدة ثم احفظ.", "Reload the page once, then save."); btn.disabled = false; return; }
  navigator.storage?.persist?.();   // ask the browser not to evict the stored map under storage pressure
  try {
    // The route is drawn on our road network, so that must be stored too (once).
    for (const u of ["/api/segments", "/api/places", "/api/nogo", "/api/states"])
      if (!(await caches.match(u))) { status.textContent = T("⬇️ شبكة الطرق…", "⬇️ Road network…"); await fetch(u); }
    if (!(await basemapStored())) await storeBasemap(status);
  } catch {
    btn.disabled = false;
    status.textContent = T("⚠️ انقطع الاتصال أثناء الحفظ — أعد المحاولة", "⚠️ Connection lost while saving — try again");
    return;
  }
  const key = tripKey(data);
  storeSaved([{ key, savedAt: data.fetchedAt ?? Date.now(), data }, ...loadSaved().filter((t) => t.key !== key)]);
  renderSaved();
  btn.disabled = false;
  btn.textContent = T("🔄 حدّث النسخة المحفوظة", "🔄 Update saved copy");
  status.textContent = T("✅ محفوظة — تعمل بدون إنترنت", "✅ Saved — works offline");
}

function renderSaved() {
  const list = loadSaved();
  document.getElementById("saved").hidden = !list.length;
  document.getElementById("savedlist").innerHTML = list.map((t, i) => `<li>
    <button class="link" data-open="${i}">${pn(t.data.from.name)} ${ARROW} ${pn(t.data.to.name)}</button>
    <small>${sinceAr(t.savedAt)}</small>
    <button class="del" data-del="${i}" title="${T("حذف · Delete", "Delete")}">✕</button></li>`).join("");
}

// Opening a saved trip re-plans it: live if the network answers, otherwise planRoute falls back to the stored copy.
function openSaved(t) {
  const q = t.data.query;
  from.value = q.frm; to.value = q.to;
  const modes = q.modes.split(",");
  document.querySelectorAll(".modes input[value]").forEach((x) => (x.checked = modes.includes(x.value)));
  document.getElementById("accessible").checked = q.accessible;
  return planRoute();
}

document.getElementById("result").addEventListener("click", (e) => { if (e.target.id === "savetrip") saveTrip(); });
document.getElementById("savedlist").addEventListener("click", (e) => {
  const list = loadSaved(), d = e.target.dataset;
  if (d.open) openSaved(list[+d.open]);
  if (d.del) { list.splice(+d.del, 1); storeSaved(list); renderSaved(); }
});

(async function init() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  renderSaved();
  updateNetbar();
  await loadSegments();
  await Promise.all([loadPlaces(), loadNogo()]);
  if (EN) { renderSaved(); from.value = pn(from.value); to.value = pn(to.value); }
  await refreshStates();
  const saved = loadSaved();
  await (!online && saved.length ? openSaved(saved[0]) : planRoute());
  setInterval(pollAlerts, 5000);
})();
