// Tareeq field reports: a Waze-style one-tap reporting page for aid workers and trusted reporters.
// - Sign in with a console-issued access code (kept on this phone only).
// - Report what's on the road at your GPS position or at the map pin; confirm or clear others' reports.
// - Works with a bad connection: reports wait in a local queue and are sent with their real age.
// - The GPS fix is only used to find the road; it is never sent on its own or stored by the server.
const LANG = localStorage.getItem("tareeq.lang") === "en" ? "en" : "ar", EN = LANG === "en";
const T = (a, e) => (EN ? e : a);
document.documentElement.lang = LANG;
document.documentElement.dir = EN ? "ltr" : "rtl";
document.querySelectorAll("[data-en]").forEach((el) => { if (EN) el.textContent = el.dataset.en; });
document.querySelectorAll("[data-en-placeholder]").forEach((el) => { if (EN) el.placeholder = el.dataset.enPlaceholder; });
const ar = (x) => (EN ? String(x) : String(x).replace(/\d/g, (d) => "٠١٢٣٤٥٦٧٨٩"[d]));
const ageTxt = (m) => m == null ? T("لا بلاغات حديثة", "no recent reports")
  : m < 60 ? T(`قبل ${ar(m)} د`, `${m} min ago`) : T(`قبل ${ar(Math.round(m / 60))} س`, `${Math.round(m / 60)} h ago`);

const CODE_KEY = "tareeq.field.code", QUEUE_KEY = "tareeq.field.queue.v1";
const MAP_CACHE = "tareeq-map-v1", BASEMAP_URL = "/map/gaza.pmtiles";

const STATE = {
  open:      { ar: "مفتوح",     en: "Open",      color: "#1a7f37", dash: null },
  degraded:  { ar: "صعب",       en: "Degraded",  color: "#b26b00", dash: "8 5" },
  foot_only: { ar: "مشاة فقط",  en: "Foot only", color: "#6f42c1", dash: "2 5" },
  blocked:   { ar: "مغلق",      en: "Blocked",   color: "#c62828", dash: null },
  unsafe:    { ar: "خطر",       en: "Unsafe",    color: "#6d0f0f", dash: "1 4" },
  unknown:   { ar: "غير معروف", en: "Unknown",   color: "#8a8a8a", dash: null },
};
// what the aid worker taps -> server kind (see backend/ops.py FIELD_KINDS)
const KINDS = [
  { k: "open",       ico: "✅", ar: "سالك",          en: "Clear" },
  { k: "slow",       ico: "🐢", ar: "زحمة / بطيء",   en: "Slow / crowded" },
  { k: "rubble",     ico: "🧱", ar: "ركام",          en: "Rubble" },
  { k: "crater",     ico: "🕳️", ar: "حفرة",          en: "Crater" },
  { k: "flood",      ico: "🌊", ar: "مياه / فيضان",  en: "Flooding" },
  { k: "foot_only",  ico: "🚶", ar: "مشاة فقط",      en: "Foot only" },
  { k: "blocked",    ico: "⛔", ar: "مغلق",          en: "Blocked" },
  { k: "checkpoint", ico: "🛑", ar: "حاجز",          en: "Checkpoint", safety: true },
  { k: "uxo",        ico: "💣", ar: "ذخائر غير منفجرة", en: "Unexploded ordnance", safety: true },
  { k: "strike",     ico: "💥", ar: "قصف / ضربة",    en: "Strike", safety: true },
];
const KIND = Object.fromEntries(KINDS.map((x) => [x.k, x]));
const STATE_TO_KIND = { open: "open", degraded: "slow", foot_only: "foot_only", blocked: "blocked", unsafe: "strike" };

let code = localStorage.getItem(CODE_KEY), me = null, map, segLayer, hazardLayer, repLayer, meMarker, meCircle;
let gps = null, lastAreaCenter = null, areaTimer = null;
const $ = (id) => document.getElementById(id);

function toast(msg, warn = false, ms = 3500) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast" + (warn ? " warn" : ""); t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), ms);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
                               body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(data.detail || r.statusText); e.status = r.status; throw e; }
  return data;
}

// ---------------- sign in ----------------
async function signIn(c) {
  try {
    me = await api(`/api/ops/field/me?code=${encodeURIComponent(c)}`);
  } catch (e) {
    if (e.status === 401) throw e;
    // offline: trust the stored code, reports queue until we can verify
    me = { label: T("مراسل ميداني", "Field reporter"), org: "", offline: true };
  }
  code = c; localStorage.setItem(CODE_KEY, c);
  $("login").hidden = true; $("app").hidden = false;
  $("who").textContent = me.label + (me.org ? " · " + me.org : "");
  startApp();
}

$("loginform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const c = $("code").value.trim().toUpperCase();
  if (!c) return;
  try { await signIn(c); }
  catch (err) { $("loginerr").hidden = false; $("loginerr").textContent = T("رمز غير صحيح أو ملغى.", "Unknown or revoked code."); }
});
$("logout").addEventListener("click", () => {
  if (queue().length && !confirm(T("لديك بلاغات لم تُرسل بعد. الخروج يحذفها. متابعة؟", "You have unsent reports. Signing out deletes them. Continue?"))) return;
  localStorage.removeItem(CODE_KEY); localStorage.removeItem(QUEUE_KEY); location.reload();
});
$("lang").textContent = EN ? "ع" : "EN";
$("lang").addEventListener("click", () => { localStorage.setItem("tareeq.lang", EN ? "ar" : "en"); location.reload(); });

// ---------------- offline queue ----------------
const queue = () => JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]");
const setQueue = (q) => { localStorage.setItem(QUEUE_KEY, JSON.stringify(q)); showQueue(); };
function showQueue() {
  const n = queue().length, el = $("queued");
  el.hidden = !n; el.textContent = T(`⏳ ${ar(n)} بانتظار الإرسال`, `⏳ ${n} waiting`);
}
let flushing = false;
async function flush() {
  if (flushing || !navigator.onLine) return;
  flushing = true;
  try {
    let q = queue();
    while (q.length) {
      const item = q[0];
      const age_s = Math.max(0, (Date.now() - item.made) / 1000);
      try {
        const r = await api("/api/ops/field/report", { method: "POST", body: { ...item.body, code, age_s } });
        q = queue().slice(1); setQueue(q);
        reportResult(r, item.body.kind, age_s > 120);
      } catch (e) {
        if (e.status === 401) { toast(T("رمز الدخول لم يعد صالحاً.", "Your access code is no longer valid."), true, 8000); break; }
        if (e.status >= 400 && e.status < 500) { q = queue().slice(1); setQueue(q); toast(e.message, true); continue; }
        break;    // network/server trouble: keep it and try again later
      }
    }
  } finally { flushing = false; }
  refreshArea(true);
}
window.addEventListener("online", () => { setNet(); flush(); });
window.addEventListener("offline", setNet);
setInterval(flush, 20000);
function setNet() {
  const el = $("net");
  el.className = "net " + (navigator.onLine ? "on" : "off");
  el.textContent = navigator.onLine ? T("متصل", "online") : T("غير متصل", "offline");
}

function reportResult(r, kind, late) {
  const k = KIND[kind];
  if (!r.accepted) return toast(T("البلاغ قديم جداً (أكثر من ٢٤ ساعة) ولم يُحتسب.", "Report too old (over 24 h); not counted."), true);
  if (r.rejected_official && r.rejected_official === r.segments.length)
    return toast(T("هذا الطريق داخل منطقة خطر رسمية؛ لا يمكن تعليمه كآمن.", "This road is inside an official danger zone; it can't be marked safe."), true, 6000);
  const n = r.segments.length;
  toast(`${k.ico} ${T("شكراً — ", "Thanks — ")}${T(k.ar, k.en)} · ${T(`${ar(n)} مقطع`, `${n} road segment${n > 1 ? "s" : ""}`)}${late ? T(" (أُرسل بعد عودة الاتصال)", " (sent after reconnecting)") : ""}`);
}

// ---------------- reporting ----------------
function submit(kind, extra = {}) {
  const where = document.querySelector("input[name=where]:checked").value;
  let body = { kind, note: $("note").value.trim(), ...extra };
  if (extra.segment === undefined) {
    let ll;
    if (where === "gps" && gps) ll = gps;
    else { const c = map.getCenter(); ll = { lat: c.lat, lng: c.lng }; }
    body.lon = +ll.lng.toFixed(6); body.lat = +ll.lat.toFixed(6);
  }
  setQueue([...queue(), { body, made: Date.now() }]);
  $("note").value = ""; closeSheet();
  if (!navigator.onLine) toast(T("📴 حُفظ البلاغ وسيُرسل عند عودة الاتصال.", "📴 Saved; it will be sent when you're back online."));
  flush();
}

$("kinds").innerHTML = KINDS.map((x) =>
  `<button class="kind${x.safety ? " safety" : ""}" data-kind="${x.k}"><span class="ico">${x.ico}</span>${T(x.ar, x.en)}</button>`).join("");
$("kinds").addEventListener("click", (e) => {
  const b = e.target.closest("[data-kind]");
  if (!b) return;
  const k = KIND[b.dataset.kind];
  if (k.safety && !confirm(T(`تأكيد: ${k.ar}؟ سيُغلق الطريق القريب للجميع.`, `Confirm: ${k.en}? Nearby roads will close for everyone.`))) return;
  submit(b.dataset.kind);
});

function openSheet() {
  const useGps = gps && gps.acc <= 100;
  document.querySelector(`input[name=where][value=${useGps ? "gps" : "pin"}]`).checked = true;
  $("gpsacc").textContent = gps ? T(`(±${ar(Math.round(gps.acc))} م)`, `(±${Math.round(gps.acc)} m)`) : T("(لا يوجد GPS)", "(no GPS)");
  $("crosshair").hidden = useGps;
  $("sheet").hidden = false;
}
function closeSheet() { $("sheet").hidden = true; $("crosshair").hidden = true; }
$("report").addEventListener("click", openSheet);
$("closesheet").addEventListener("click", closeSheet);
document.querySelectorAll("input[name=where]").forEach((r) => r.addEventListener("change", () => {
  $("crosshair").hidden = r.value === "gps" && r.checked;
}));

// ---------------- map ----------------
function startApp() {
  setNet(); showQueue();
  map = L.map("map", { zoomControl: false, maxBounds: [[31.18, 34.15], [31.63, 34.62]], minZoom: 11 })
    .setView([31.37, 34.30], 14);
  protomapsL.leafletLayer({ url: BASEMAP_URL, flavor: "light", lang: LANG, maxDataZoom: 14, className: "basemap",
                            attribution: "© OpenStreetMap contributors · Protomaps" }).addTo(map);
  map.createPane("hazards").style.zIndex = 390;
  segLayer = L.layerGroup().addTo(map);
  hazardLayer = L.layerGroup().addTo(map);
  repLayer = L.layerGroup().addTo(map);
  map.on("moveend", () => { clearTimeout(areaTimer); areaTimer = setTimeout(() => refreshArea(false), 400); });
  $("legend").innerHTML = ["open", "degraded", "foot_only", "blocked", "unsafe"].map((s) =>
    `<div><i style="background:${STATE[s].color}"></i>${T(STATE[s].ar, STATE[s].en)}</div>`).join("") +
    `<div>💥 ${T("ضربة حديثة", "recent strike")}</div>`;
  startGps();
  refreshArea(true);
  flush();
  storeBasemapOnce();
}

function startGps() {
  if (!("geolocation" in navigator)) return;
  let first = true;
  navigator.geolocation.watchPosition((p) => {
    gps = { lat: p.coords.latitude, lng: p.coords.longitude, acc: p.coords.accuracy };
    const ll = [gps.lat, gps.lng];
    if (!meMarker) {
      meMarker = L.marker(ll, { icon: L.divIcon({ className: "", html: '<div class="me-dot"></div>', iconSize: [16, 16] }), interactive: false }).addTo(map);
      meCircle = L.circle(ll, { radius: gps.acc, color: "#0b5cad", weight: 1, fillOpacity: 0.08, interactive: false }).addTo(map);
    } else { meMarker.setLatLng(ll); meCircle.setLatLng(ll).setRadius(gps.acc); }
    if (first && map.options.maxBounds.contains(ll)) { map.setView(ll, 16); first = false; }
  }, () => {}, { enableHighAccuracy: true, maximumAge: 15000, timeout: 20000 });
}
$("locate").addEventListener("click", () => {
  if (gps) map.setView([gps.lat, gps.lng], 16);
  else toast(T("لا يتوفر موقع GPS بعد. اسحب الخريطة واستخدم العلامة (＋).", "No GPS fix yet. Pan the map and use the pin (＋)."));
});

async function refreshArea(force) {
  if (!map) return;
  const c = map.getCenter();
  if (!force && lastAreaCenter && map.distance(c, lastAreaCenter) < 400) return;
  const r = Math.min(4000, Math.max(800, map.distance(map.getBounds().getNorthWest(), map.getBounds().getSouthEast()) / 2));
  try {
    const d = await api(`/api/ops/field/area?lon=${c.lng.toFixed(5)}&lat=${c.lat.toFixed(5)}&r=${Math.round(r)}`);
    lastAreaCenter = c;
    drawArea(d);
  } catch { /* offline with no cached copy: keep what's drawn */ }
}

function segPopup(s) {
  const st = STATE[s.s];
  const evidence = s.src === "v" ? `${ageTxt(s.age)} · ${T(`${ar(s.n)} بلاغ`, `${s.n} report${s.n > 1 ? "s" : ""}`)}`
    : s.src === "o" ? T("منطقة خطر رسمية / ضربة حديثة", "Official danger zone / recent strike")
    : s.src === "p" ? T("صور أقمار صناعية ٢٠٢٤ فقط", "2024 satellite imagery only") : T("لا بيانات", "No data");
  const prov = s.prov ? `<div>⚠️ ${T("بلاغ إغلاق غير مؤكد", "Unconfirmed closure report")}</div>` : "";
  const canConfirm = s.src !== "o";
  const k = STATE_TO_KIND[s.s];
  return `<div class="pop"><h4>${s.name || T("طريق", "Road")}</h4>
    <div><span class="st" style="color:${st.color}">${T(st.ar, st.en)}</span> · ${evidence}</div>${prov}
    ${canConfirm ? `<div class="still">
      ${k ? `<button data-seg="${s.id}" data-kind="${k}">👍 ${T("ما زال كذلك", "Still there")}</button>` : ""}
      <button data-seg="${s.id}" data-kind="open">✅ ${T("سالك الآن", "Clear now")}</button>
      <button data-seg="${s.id}" data-kind="blocked">⛔ ${T("مغلق", "Blocked")}</button></div>` : ""}</div>`;
}

function drawArea(d) {
  segLayer.clearLayers(); hazardLayer.clearLayers(); repLayer.clearLayers();
  const renderer = L.canvas({ tolerance: 8 });
  const mid = {};
  for (const s of d.segments) {
    const st = STATE[s.s], ll = s.c.map(([x, y]) => [y, x]);
    mid[s.id] = ll[Math.floor(ll.length / 2)];
    const weak = s.src === "p" || s.s === "unknown";
    L.polyline(ll, { renderer, color: st.color, dashArray: st.dash, weight: weak ? 2 : 5, opacity: weak ? 0.4 : 0.9 })
      .on("click", (e) => L.popup().setLatLng(e.latlng).setContent(segPopup(s)).openOn(map))
      .addTo(segLayer);
  }
  for (const h of d.hazards) {
    const p = h.properties;
    if (p.kind === "strike") {
      const [lon, lat] = h.geometry.coordinates;
      const when = p.time ? new Date(p.time * 1000).toLocaleString(EN ? "en-GB" : "ar-PS", { timeZone: "Asia/Gaza", hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" }) : "";
      L.circle([lat, lon], { pane: "hazards", radius: p.buffer_m || 200, color: "#6d0f0f", weight: 1, fillColor: "#c62828", fillOpacity: 0.15, dashArray: "4 3" }).addTo(hazardLayer);
      L.marker([lat, lon], { icon: L.divIcon({ className: "strike-ico", html: "💥", iconSize: [24, 24] }) })
        .bindPopup(`<div class="pop"><h4>💥 ${T("ضربة حديثة", "Recent strike")}</h4><div>${when}</div>
          <small>${T("المصدر", "Source")}: ${p.source || "—"} · ${T("دقة الموقع", "location precision")} ±${ar(p.precision_m || "?")} ${T("م", "m")}</small>
          <div><small>${T("نتجنب الطرق ضمن", "Routes avoid roads within")} ${ar(p.buffer_m)} ${T("م حتى", "m until")} ${new Date(p.expires * 1000).toLocaleTimeString(EN ? "en-GB" : "ar-PS", { timeZone: "Asia/Gaza", hour: "2-digit", minute: "2-digit" })}</small></div></div>`)
        .addTo(hazardLayer);
    } else {
      L.geoJSON(h, { pane: "hazards", style: { color: "#6d0f0f", weight: 2, fillColor: "#c62828", fillOpacity: 0.15, dashArray: "6 4" } })
        .bindTooltip(`⛔ ${p.name || T("منطقة محظورة", "No-go zone")}`, { sticky: true }).addTo(hazardLayer);
    }
  }
  const seen = new Set();
  for (const r of d.reports) {
    const key = r.seg + r.kind;
    if (seen.has(key) || !mid[r.seg]) continue;
    seen.add(key);
    L.marker(mid[r.seg], { icon: L.divIcon({ className: "rep-ico", html: KIND[r.kind]?.ico || "•", iconSize: [22, 22] }) })
      .bindTooltip(`${T(KIND[r.kind]?.ar, KIND[r.kind]?.en)} · ${ageTxt(r.age)}`).addTo(repLayer);
  }
}

map && map.on("popupopen", () => {});
document.addEventListener("click", (e) => {
  const b = e.target.closest(".pop button[data-seg]");
  if (!b) return;
  submit(b.dataset.kind, { segment: +b.dataset.seg });
  map.closePopup();
});

// keep the whole Strip basemap on the phone for field use (same cache/key the service worker serves from)
async function storeBasemapOnce() {
  if (!("caches" in window) || !navigator.onLine) return;
  const cache = await caches.open(MAP_CACHE);
  if (await cache.match(BASEMAP_URL)) return;
  try {
    const res = await fetch(BASEMAP_URL, { cache: "no-store" });
    if (!res.ok) return;
    const blob = await res.blob();
    await cache.put(BASEMAP_URL, new Response(blob, { headers: { "Content-Type": "application/vnd.pmtiles", "Content-Length": String(blob.size) } }));
    toast(T("✅ خريطة غزة محفوظة للعمل بدون إنترنت.", "✅ Gaza map saved for offline use."));
  } catch { /* try again next visit */ }
}

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
(async function init() {
  if (code) { try { await signIn(code); return; } catch { localStorage.removeItem(CODE_KEY); } }
  $("login").hidden = false;
  try { const r = await fetch("/api/ops/field/me?code=DEMO-0000"); $("demohint").hidden = !r.ok; } catch {}
})();
