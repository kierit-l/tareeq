// Tareeq operator console (spec 4.6): no-go zones with two-person approval, strike hazards, review queue,
// field-reporter access codes, broadcasts, accuracy metrics and audit log. Needs a connection (never cached).
const LANG = localStorage.getItem("tareeq.lang") === "en" ? "en" : "ar", EN = LANG === "en";
const T = (a, e) => (EN ? e : a);
document.documentElement.lang = LANG;
document.documentElement.dir = EN ? "ltr" : "rtl";
document.querySelectorAll("[data-en]").forEach((el) => { if (EN) el.textContent = el.dataset.en; });
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const when = (t) => t ? new Date(t * 1000).toLocaleString(EN ? "en-GB" : "ar-PS", { timeZone: "Asia/Gaza", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
const agoMin = (m) => m == null ? "—" : m < 60 ? T(`قبل ${m} د`, `${m} min ago`) : T(`قبل ${Math.round(m / 60)} س`, `${Math.round(m / 60)} h ago`);
$("lang").textContent = EN ? "ع" : "EN";
$("lang").onclick = () => { localStorage.setItem("tareeq.lang", EN ? "ar" : "en"); location.reload(); };

let me = localStorage.getItem("tareeq.op.name") || "", demo = false;
async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Operator": me },
                               body: opts.body ? JSON.stringify(opts.body) : undefined, cache: "no-store" });
  const d = await r.json().catch(() => ({}));
  if (r.status === 401 && me) { localStorage.removeItem("tareeq.op.name"); me = ""; }
  if (!r.ok) throw new Error(d.detail || r.statusText);
  return d;
}
const act = async (fn) => { try { await fn(); } catch (e) { alert(e.message); } await render(); };

// ---------------- map ----------------
let map, layers, draft = null, mode = null;
function initMap() {
  map = L.map("map", { maxBounds: [[31.15, 34.1], [31.68, 34.66]] }).setView([31.37, 34.31], 12);
  protomapsL.leafletLayer({ url: "/map/gaza.pmtiles", flavor: "light", lang: LANG, maxDataZoom: 14, className: "basemap",
                            attribution: "© OpenStreetMap contributors · Protomaps" }).addTo(map);
  layers = { zones: L.layerGroup().addTo(map), strikes: L.layerGroup().addTo(map), queue: L.layerGroup().addTo(map), draft: L.layerGroup().addTo(map) };
  map.on("click", (e) => {
    if (mode === "draw") { draft.push([e.latlng.lng, e.latlng.lat]); drawDraft(); }
    else if (mode === "strike") { mode = null; act(() => api("/api/ops/admin/strikes/simulate", { method: "POST", body: { lon: e.latlng.lng, lat: e.latlng.lat } })); }
    else if (mode === "bcarea") { draft.push([e.latlng.lng, e.latlng.lat]); drawDraft(); }
  });
}
function drawDraft() {
  layers.draft.clearLayers();
  if (!draft || !draft.length) return;
  const ll = draft.map(([x, y]) => [y, x]);
  (draft.length > 2 ? L.polygon(ll, { color: "#e8590c", dashArray: "4 4" }) : L.polyline(ll, { color: "#e8590c" })).addTo(layers.draft);
  ll.forEach((p) => L.circleMarker(p, { radius: 4, color: "#e8590c" }).addTo(layers.draft));
  const n = $("draftn"); if (n) n.textContent = draft.length;
}
const ring = () => ({ type: "Polygon", coordinates: [[...draft, draft[0]]] });

// ---------------- tabs ----------------
const TABS = [
  ["overview", T("نظرة عامة", "Overview")], ["zones", T("مناطق محظورة", "No-go zones")], ["strikes", T("ضربات", "Strikes")],
  ["queue", T("للمراجعة", "Review queue")], ["reporters", T("المراسلون", "Field reporters")],
  ["broadcasts", T("بث للمشتركين", "Broadcasts")], ["audit", T("السجل", "Audit log")],
];
let tab = "overview", ov = null;
function nav() {
  const badge = { zones: ov?.pending_zones, broadcasts: ov?.pending_broadcasts };
  $("tabs").innerHTML = TABS.map(([k, label]) => `<button class="${k === tab ? "on" : ""}" data-tab="${k}">${label}${badge[k] ? `<span class="badge">${badge[k]}</span>` : ""}</button>`).join("");
}
$("tabs").onclick = (e) => { const k = e.target.closest("[data-tab]")?.dataset.tab; if (k) { tab = k; mode = null; draft = null; drawDraft(); render(); } };

async function render() {
  ov = await api("/api/ops/admin/overview");
  nav();
  const el = $("content");
  try { el.innerHTML = await VIEWS[tab](); } catch (e) { el.innerHTML = `<div class="tab err">${esc(e.message)}</div>`; }
  await drawLayers();
}

// ---------------- views ----------------
const VIEWS = {
  async overview() {
    const m = ov.metrics, s = ov.strikes, g = s.gdelt_24h;
    const pct = (x) => x == null ? "—" : Math.round(x * 100) + "%";
    const src = Object.entries(s.sources).map(([k, v]) => `<div>${k}: ${v.ok ? `✅ ${v.n ?? 0}` : `⚠️ ${esc(v.error)}`} <small>${when(v.at)}</small></div>`).join("") || `<small>${T("لم يتم الجلب بعد", "not fetched yet")}</small>`;
    return `<div class="tab">
      <div class="grid2">
        <div class="stat"><small>${T("رحلات ≤ P85 (الهدف ٨٠–٩٠٪)", "Trips ≤ P85 (target 80–90%)")}</small><b>${pct(m.within_p85_share)}</b><small>${m.completed} ${T("مؤكدة", "confirmed")}</small></div>
        <div class="stat"><small>${T("رحلات ≤ P50 (الهدف ٤٠–٦٠٪)", "Trips ≤ P50 (target 40–60%)")}</small><b>${pct(m.within_p50_share)}</b></div>
        <div class="stat"><small>${T("رحلات فاشلة (الهدف <٥٪)", "Failed trips (target <5%)")}</small><b>${pct(m.failed_share)}</b></div>
        <div class="stat"><small>${T("مقاطع خطرة رسمياً", "Officially unsafe segments")}</small><b>${ov.official_unsafe_segments}</b></div>
        <div class="stat"><small>${T("مراسلون نشطون", "Active field reporters")}</small><b>${ov.active_reporters}</b></div>
        <div class="stat"><small>${T("مشتركون بالتنبيهات", "Alert subscribers")}</small><b>${ov.subscribers}</b></div>
      </div>
      <div class="card" style="margin-top:10px"><h4>${T("مصادر الضربات", "Strike feeds")}</h4>${src}
        <div class="row"><small>${T("نشاط إخباري (GDELT، ٢٤ س، للتوعية فقط):", "News activity (GDELT, 24 h, awareness only):")} <b>${g.violent_event_reports}</b> ${T("خبر عنف", "violent-event reports")} · ${g.files} ${T("ملف", "files")}</small></div>
      </div>
      ${ov.pending_zones || ov.pending_broadcasts ? `<div class="hint">⏳ ${T("بانتظار موافقة مشغّل ثانٍ", "Waiting for a second operator")}: ${ov.pending_zones} ${T("منطقة", "zone(s)")}, ${ov.pending_broadcasts} ${T("بث", "broadcast(s)")}</div>` : ""}
    </div>`;
  },

  async zones() {
    const { zones } = await api("/api/ops/admin/zones");
    window._zones = zones;
    const drawing = mode === "draw";
    const form = drawing ? `<div class="card"><h4>${T("منطقة جديدة", "New zone")}</h4>
        <div class="hint">${T("انقر على الخريطة لرسم الحدود", "Click the map to draw the outline")} (<span id="draftn">${draft.length}</span> ${T("نقاط", "points")})</div>
        <input id="zname" placeholder="${T("الاسم (مثلاً: أمر إخلاء — شرق خانيونس)", "Name (e.g. Evacuation order — east Khan Younis)")}">
        <div class="row"><select id="zkind"><option value="evacuation">${T("أمر إخلاء (٣٠٠ م)", "Evacuation order (300 m)")}</option>
          <option value="yellow_line">${T("الخط الأصفر (٥٠٠ م)", "Yellow Line (500 m)")}</option><option value="military">${T("منطقة عسكرية", "Military zone")}</option>
          <option value="uxo">${T("ذخائر غير منفجرة", "Unexploded ordnance")}</option><option value="other">${T("أخرى", "Other")}</option></select>
          <input id="zbuf" type="number" min="0" max="2000" placeholder="${T("هامش م (اختياري)", "buffer m (optional)")}" style="width:140px"></div>
        <input id="zsrc" style="margin-top:6px" placeholder="${T("المصدر (إلزامي): OCHA أمر إخلاء رقم/تاريخ…", "Source (required): OCHA evacuation order no./date…")}">
        <div class="row"><button class="primary" id="zsave">${T("اقترح (يحتاج موافقة ثانية)", "Propose (needs 2nd approval)")}</button><button id="zcancel">${T("إلغاء", "Cancel")}</button></div></div>`
      : `<div class="row" style="margin-bottom:8px"><button class="primary" id="znew">＋ ${T("ارسم منطقة جديدة", "Draw new zone")}</button></div>`;
    const kindAr = { evacuation: T("إخلاء", "evacuation"), yellow_line: T("الخط الأصفر", "Yellow Line"), military: T("عسكرية", "military"), uxo: T("ذخائر", "UXO"), other: T("أخرى", "other") };
    const list = zones.map((z) => {
      const mine = (z.status === "pending" && z.proposed_by === me) || (z.status === "pending_removal" && z.removal_by === me);
      let btns = "";
      if (z.status === "pending" || z.status === "pending_removal")
        btns = mine ? `<small>${T("بانتظار مشغّل آخر", "waiting for another operator")}</small>`
          : `<button class="ok" data-zapprove="${z.id}">${z.status === "pending" ? T("وافق وفعّل", "Approve & activate") : T("وافق على الإزالة", "Approve removal")}</button><button data-zreject="${z.id}">${T("ارفض", "Reject")}</button>`;
      else if (z.status === "active") btns = `<button class="danger" data-zremove="${z.id}">${T("اطلب إزالة", "Request removal")}</button>`;
      return `<div class="card"><h4><a href="#" data-zzoom="${z.id}">${esc(z.name)}</a> <span class="pill ${z.status}">${z.status}</span></h4>
        <small>${kindAr[z.kind] || esc(z.kind)} · ${T("المصدر", "source")}: ${esc(z.source)}${z.buffer_m ? ` · ${z.buffer_m} m` : ""}</small><br>
        <small>${z.baseline ? T("من ملف nogo.geojson", "from nogo.geojson") : `${T("اقترحه", "proposed by")} ${esc(z.proposed_by)} ${when(z.proposed_at)}${z.approved_by ? ` · ${T("وافق", "approved by")} ${esc(z.approved_by)}` : ""}${z.removal_by ? ` · ${T("طلب الإزالة", "removal by")} ${esc(z.removal_by)}` : ""}`}</small>
        <div class="row">${btns}</div></div>`;
    }).join("");
    return `<div class="tab">${form}${list || `<small>${T("لا مناطق", "No zones")}</small>`}</div>`;
  },

  async strikes() {
    const d = await api("/api/ops/strikes");
    window._strikes = d.features;
    const items = d.features.map((f) => {
      const p = f.properties;
      const dis = p.dismiss_requested_by
        ? (p.dismiss_requested_by === me ? `<small>${T("طلبت الإلغاء — بانتظار مشغّل آخر", "You requested dismissal — waiting for another operator")}</small>`
          : `<button class="danger" data-sdismiss="${esc(p.id)}">${T("أكّد الإلغاء", "Confirm dismissal")}</button> <small>${T("طلبه", "requested by")} ${esc(p.dismiss_requested_by)}</small>`)
        : `<button data-sdismiss="${esc(p.id)}">${T("اطلب إلغاء (إنذار خاطئ)", "Request dismissal (false alarm)")}</button>`;
      return `<div class="card"><h4><a href="#" data-szoom="${esc(p.id)}">💥 ${esc(EN ? p.name_en : p.name)}</a> <span class="pill active">${p.source}</span></h4>
        <small>${when(p.time)} · ${T("هامش", "buffer")} ${p.buffer_m} m · ${T("ينتهي", "expires")} ${when(p.expires)}${p.confidence ? ` · ${p.confidence}` : ""}</small>
        <div class="row">${dis}</div></div>`;
    }).join("");
    return `<div class="tab">
      <div class="hint">${T("الضربات تجعل الطرق أقل أماناً فقط، لذلك تُطبّق فوراً وتنتهي تلقائياً. إلغاؤها يحتاج مشغّلَين.", "Strikes only make roads less safe, so they apply at once and expire on their own. Dismissing one needs two operators.")}
        <br><small>${T("لا يوجد مصدر عام دقيق وفوري للضربات في غزة: FIRMS يرصد الحرائق فقط (~٣ س تأخير)، وGDELT للتوعية فقط. أسرع مصدر هو بلاغات المراسلين الميدانيين.", "No public feed gives precise, recent strike locations in Gaza: FIRMS only sees fires (~3 h delay); GDELT is awareness only. Field reporters are the fastest source.")}</small></div>
      <div class="row" style="margin-bottom:8px"><button id="srefresh">${T("حدّث المصادر الآن", "Refresh feeds now")}</button>
        ${demo ? `<button class="danger" id="ssim">${mode === "strike" ? T("انقر على الخريطة…", "Click the map…") : T("محاكاة ضربة (عرض)", "Simulate strike (demo)")}</button>` : ""}</div>
      ${items || `<small>${T("لا ضربات نشطة", "No active strike hazards")}</small>`}</div>`;
  },

  async queue() {
    const q = await api("/api/ops/admin/queue");
    window._queue = q.items;
    const R = { provisional_blocked: T("إغلاق غير مؤكد", "Unconfirmed closure"), contested: T("بلاغات متعارضة", "Conflicting reports"), awaiting_confirmation: T("بانتظار تأكيد", "Awaiting confirmation") };
    const items = q.items.map((x) => `<div class="card"><h4><a href="#" data-qzoom="${x.seg}">${esc(x.name) || T("مقطع", "segment") + " " + x.seg}</a>${x.major ? " ⭐" : ""}</h4>
      <small>${x.reasons.map((r) => `<span class="pill pending">${R[r]}</span>`).join(" ")} · ${T("الحالة", "state")}: ${x.state} (${Math.round(x.conf * 100)}%) · ${agoMin(x.age_min)}
      ${Object.keys(x.pending || {}).length ? ` · ${T("بانتظار", "pending")}: ${esc(JSON.stringify(x.pending))}` : ""}${x.conflict?.length ? ` · ${T("يتعارض مع", "conflicts with")}: ${esc(x.conflict.join(", "))}` : ""}</small></div>`).join("");
    const notes = q.field_notes.map((n) => `<tr><td>${agoMin(n.age_min)}</td><td>${esc(n.kind)}</td><td>${esc(n.note)}</td><td>${n.ll ? `<a href="#" data-llzoom="${n.ll}">📍</a>` : ""}</td></tr>`).join("");
    return `<div class="tab"><div class="hint">${T("اطلب من مراسل ميداني التحقق من هذه المقاطع.", "Ask a field reporter to check these segments.")} ${q.total} ${T("عنصر", "items")}</div>
      ${items || `<small>${T("لا شيء للمراجعة", "Nothing to review")}</small>`}
      <h4>${T("آخر البلاغات الميدانية (٢٤ س)", "Latest field reports (24 h)")}</h4><table>${notes || `<tr><td class="muted">—</td></tr>`}</table></div>`;
  },

  async reporters() {
    const { reporters } = await api("/api/ops/admin/reporters");
    const rows = reporters.map((r) => `<tr><td><b>${esc(r.label)}</b><br><small>${esc(r.org)}</small></td><td>${r.reports} ${T("بلاغ", "reports")}</td>
      <td><small>${esc(r.created_by)} ${when(r.created_at)}</small></td>
      <td>${r.active ? `<button class="danger" data-rrevoke="${r.id}">${T("إلغاء الرمز", "Revoke")}</button>` : `<span class="pill removed">${T("ملغى", "revoked")}</span>`}</td></tr>`).join("");
    return `<div class="tab"><div class="card"><h4>${T("أضف مراسلاً ميدانياً", "Add field reporter")}</h4>
      <div class="row"><input id="rlabel" placeholder="${T("الاسم / الصفة", "Name / role")}" style="flex:1"><input id="rorg" placeholder="${T("المنظمة", "Organisation")}" style="flex:1"></div>
      <div class="row"><button class="primary" id="radd">${T("أصدر رمز دخول", "Issue access code")}</button></div><div id="rcode"></div>
      <small>${T("يفتح المراسل", "The reporter opens")} <b dir="ltr">${location.origin}/field</b> ${T("ويُدخل الرمز. بلاغاته موثوقة فوراً.", "and enters the code. Their reports count as trusted.")}</small></div>
      <table>${rows}</table></div>`;
  },

  async broadcasts() {
    const { broadcasts } = await api("/api/ops/admin/broadcasts");
    const areaMode = mode === "bcarea";
    const items = broadcasts.map((b) => `<div class="card"><div>${esc(b.text)}</div>
      <small><span class="pill ${b.status}">${b.status}</span> ${esc(b.proposed_by)} ${when(b.proposed_at)}${b.has_area ? ` · ${T("منطقة محددة", "area only")}` : ` · ${T("كل المشتركين", "all subscribers")}`}${b.recipients != null ? ` · ${b.recipients} ${T("مستلم", "recipients")}` : ""}</small>
      ${b.status === "pending" ? (b.proposed_by === me ? `<div class="row"><small>${T("بانتظار مشغّل آخر", "waiting for another operator")}</small></div>` : `<div class="row"><button class="ok" data-bapprove="${b.id}">${T("وافق وأرسل", "Approve & send")}</button><button data-breject="${b.id}">${T("ارفض", "Reject")}</button></div>`) : ""}</div>`).join("");
    return `<div class="tab"><div class="card"><h4>${T("رسالة جديدة (≤١٦٠ حرف)", "New message (≤160 chars)")}</h4>
      <textarea id="btext" maxlength="160" rows="3"></textarea><small id="bcount">0/160</small>
      <div class="row"><label style="width:auto"><input type="checkbox" id="barea" ${areaMode ? "checked" : ""} style="width:auto"> ${T("فقط من يمر طريقهم بمنطقة أرسمها", "Only subscribers whose route crosses an area I draw")}</label>
      ${areaMode ? `<small>(<span id="draftn">${draft.length}</span> ${T("نقاط", "points")})</small>` : ""}</div>
      <div class="row"><button class="primary" id="bsend">${T("اقترح (يحتاج موافقة ثانية)", "Propose (needs 2nd approval)")}</button></div></div>${items}</div>`;
  },

  async audit() {
    const { audit } = await api("/api/ops/admin/audit");
    return `<div class="tab"><table>${audit.map((a) => `<tr><td><small>${when(a.ts)}</small></td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td><small>${esc(a.detail)}</small></td></tr>`).join("")}</table></div>`;
  },
};

// ---------------- map layers ----------------
async function drawLayers() {
  layers.zones.clearLayers(); layers.strikes.clearLayers(); layers.queue.clearLayers();
  const zones = (await api("/api/ops/admin/zones")).zones;
  window._zones = zones;
  for (const z of zones) {
    if (!["active", "pending", "pending_removal"].includes(z.status)) continue;
    const pending = z.status !== "active";
    L.geoJSON(z.geometry, { style: { color: pending ? "#b26b00" : "#6d0f0f", weight: 2, fillColor: pending ? "#ffb84d" : "#c62828", fillOpacity: 0.15, dashArray: pending ? "6 6" : null } })
      .bindTooltip(`${esc(z.name)} · ${z.status}`, { sticky: true }).addTo(layers.zones);
  }
  const strikes = (await api("/api/ops/strikes")).features;
  window._strikes = strikes;
  for (const f of strikes) {
    const [lon, lat] = f.geometry.coordinates;
    L.circle([lat, lon], { radius: f.properties.buffer_m, color: "#6d0f0f", weight: 1, fillColor: "#c62828", fillOpacity: 0.2 }).addTo(layers.strikes);
    L.marker([lat, lon], { icon: L.divIcon({ className: "", html: "💥", iconSize: [20, 20] }) }).bindTooltip(`${f.properties.source} · ${when(f.properties.time)}`).addTo(layers.strikes);
  }
  if (tab === "queue") for (const x of window._queue || []) L.circleMarker([x.ll[1], x.ll[0]], { radius: 7, color: x.reasons.includes("provisional_blocked") ? "#c62828" : "#b26b00", weight: 3 }).addTo(layers.queue);
}

// ---------------- actions ----------------
$("content").addEventListener("click", (e) => {
  const t = e.target.closest("button, a");
  if (!t) return;
  const d = t.dataset;
  const zoomGeo = (g) => map.fitBounds(L.geoJSON(g).getBounds().pad(0.3));
  if (t.tagName === "A") e.preventDefault();
  if (t.id === "znew") { mode = "draw"; draft = []; render(); }
  else if (t.id === "zcancel") { mode = null; draft = null; drawDraft(); render(); }
  else if (t.id === "zsave") {
    if (draft.length < 3) return alert(T("ارسم ٣ نقاط على الأقل", "Draw at least 3 points"));
    const buf = $("zbuf").value;
    act(async () => {
      const r = await api("/api/ops/admin/zones", { method: "POST", body: { name: $("zname").value, kind: $("zkind").value, source: $("zsrc").value,
        geometry: ring(), buffer_m: buf ? +buf : null } });
      alert(T(`اقتُرحت. ستؤثر على ${r.segments_affected} مقطع بعد موافقة مشغّل ثانٍ.`, `Proposed. Will affect ${r.segments_affected} road segments once a second operator approves.`));
      mode = null; draft = null; drawDraft();
    });
  }
  else if (d.zapprove) act(() => api(`/api/ops/admin/zones/${d.zapprove}/approve`, { method: "POST" }));
  else if (d.zreject) act(() => api(`/api/ops/admin/zones/${d.zreject}/reject`, { method: "POST" }));
  else if (d.zremove) { if (confirm(T("إزالة منطقة خطر تجعل الطرق أقل أماناً. متأكد؟", "Removing a danger zone makes routes less safe. Sure?"))) act(() => api(`/api/ops/admin/zones/${d.zremove}/remove`, { method: "POST" })); }
  else if (d.zzoom) zoomGeo(window._zones.find((z) => z.id === d.zzoom).geometry);
  else if (t.id === "srefresh") act(() => api("/api/ops/admin/strikes/refresh", { method: "POST" }));
  else if (t.id === "ssim") { mode = "strike"; render(); }
  else if (d.sdismiss) act(() => api(`/api/ops/admin/strikes/${encodeURIComponent(d.sdismiss)}/dismiss`, { method: "POST" }));
  else if (d.szoom) { const f = window._strikes.find((x) => x.properties.id === d.szoom); map.setView([f.geometry.coordinates[1], f.geometry.coordinates[0]], 16); }
  else if (d.qzoom) { const x = window._queue.find((q) => q.seg === +d.qzoom); map.setView([x.ll[1], x.ll[0]], 17); }
  else if (d.llzoom) { const [lon, lat] = d.llzoom.split(",").map(Number); map.setView([lat, lon], 17); }
  else if (t.id === "radd") act(async () => {
    const r = await api("/api/ops/admin/reporters", { method: "POST", body: { label: $("rlabel").value, org: $("rorg").value } });
    setTimeout(() => { $("rcode").innerHTML = `<div class="codebox">${esc(r.code)}</div><small>${T("يظهر مرة واحدة فقط. شاركه بشكل خاص.", "Shown once only. Share it privately.")}</small>`; }, 50);
  });
  else if (d.rrevoke) { if (confirm(T("إلغاء الرمز؟", "Revoke this code?"))) act(() => api(`/api/ops/admin/reporters/${d.rrevoke}/revoke`, { method: "POST" })); }
  else if (t.id === "bsend") act(async () => {
    const area = $("barea").checked;
    if (area && draft.length < 3) throw new Error(T("ارسم المنطقة على الخريطة (٣ نقاط على الأقل)", "Draw the area on the map (at least 3 points)"));
    await api("/api/ops/admin/broadcasts", { method: "POST", body: { text: $("btext").value, geometry: area ? ring() : null } });
    mode = null; draft = null; drawDraft();
  });
  else if (d.bapprove) { if (confirm(T("إرسال الرسالة للمشتركين الآن؟", "Send to subscribers now?"))) act(() => api(`/api/ops/admin/broadcasts/${d.bapprove}/approve`, { method: "POST" })); }
  else if (d.breject) act(() => api(`/api/ops/admin/broadcasts/${d.breject}/reject`, { method: "POST" }));
});
$("content").addEventListener("change", (e) => {
  if (e.target.id === "barea") { mode = e.target.checked ? "bcarea" : null; draft = e.target.checked ? [] : null; drawDraft(); render(); }
});
$("content").addEventListener("input", (e) => { if (e.target.id === "btext") $("bcount").textContent = `${e.target.value.length}/160`; });

// ---------------- start ----------------
// No login: pick who you are acting as. The two-person rule still needs a different name to approve.
$("whoami").onchange = (e) => { me = e.target.value; localStorage.setItem("tareeq.op.name", me); render(); };
(async function start() {
  $("app").hidden = false;
  initMap();
  ov = await api("/api/ops/admin/overview").catch(() => api("/api/ops/admin/overview"));
  me = ov.me;
  $("whoami").innerHTML = ov.operators.map((n) => `<option${n === me ? " selected" : ""}>${esc(n)}</option>`).join("");
  demo = ov.demo; $("demoflag").hidden = !demo;
  await render();
  setInterval(() => { if (!mode) render().catch(() => {}); }, 30000);
})();
