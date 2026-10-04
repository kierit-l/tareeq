// Tareeq service worker: keeps the app, the last-known road data and the basemap usable with no connection.
//   app shell  -> network first (short timeout), cached copy when offline
//   /api GETs  -> network first (short timeout), cached copy stamped with when it was fetched
//   basemap    -> the map library reads /map/gaza.pmtiles with Range requests; once the whole file is stored
//                 (the app does that when a trip is saved) every range is answered from the stored copy
const SHELL = "tareeq-shell-v2", DATA = "tareeq-data-v1", MAP = "tareeq-map-v1";
const BASEMAP_URL = "/map/gaza.pmtiles";
const SHELL_URLS = ["/", "/static/app.js", "/static/style.css", "/static/vendor/leaflet/leaflet.js",
  "/static/vendor/leaflet/leaflet.css", "/static/vendor/leaflet/images/marker-icon.png",
  "/static/vendor/leaflet/images/marker-shadow.png", "/static/vendor/protomaps-leaflet/protomaps-leaflet.js"];
const NO_CACHE_API = ["/api/alerts", "/api/metrics"];   // live-only: a stale copy would look current
const NO_CACHE_API_PREFIXES = ["/api/ops/admin"];       // approval queues: stale state is dangerous

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  const keep = [SHELL, DATA, MAP];
  e.waitUntil(caches.keys()
    .then((ks) => Promise.all(ks.filter((k) => !keep.includes(k)).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname === BASEMAP_URL) return e.respondWith(basemap(req));
  if (url.pathname.startsWith("/api/")) {
    if (NO_CACHE_API.includes(url.pathname) || NO_CACHE_API_PREFIXES.some((p) => url.pathname.startsWith(p))) return;
    return e.respondWith(networkFirst(req, DATA, 5000, true));
  }
  // Pages are cached as they are visited (not precached: one missing file in SHELL_URLS would fail the whole install).
  if (["/", "/field"].includes(url.pathname) || url.pathname.startsWith("/static/"))
    return e.respondWith(networkFirst(req, SHELL, 3000, false));
});

// Serve basemap byte ranges from the stored full file; without it, pass through to the network untouched.
let basemapBuf = null;   // held in memory: the map reads many small ranges per screen
async function basemap(req) {
  if (!basemapBuf) {
    const full = await (await caches.open(MAP)).match(BASEMAP_URL);
    if (!full) return fetch(req).catch(() => new Response("", { status: 504, statusText: "offline" }));
    basemapBuf = await full.arrayBuffer();
  }
  const buf = basemapBuf;
  const m = /bytes=(\d+)-(\d*)/.exec(req.headers.get("range") || "");
  if (!m) return new Response(buf, { headers: { "Content-Type": "application/vnd.pmtiles" } });
  const start = +m[1], end = m[2] ? Math.min(+m[2], buf.byteLength - 1) : buf.byteLength - 1;
  return new Response(buf.slice(start, end + 1), { status: 206, headers: {
    "Content-Type": "application/vnd.pmtiles", "Content-Range": `bytes ${start}-${end}/${buf.byteLength}`,
    "Content-Length": String(end - start + 1) } });
}

// Race the network against a timeout: on a patchy link a stale answer now beats a fresh one never.
// The network request keeps going after the timeout so the cache still gets refreshed.
async function networkFirst(req, name, timeoutMs, stamp) {
  const cache = await caches.open(name);
  // cache: "no-cache" revalidates with the server (a cheap 304 when unchanged) so the browser's HTTP cache
  // can't hand back a stale app.js/style.css while we are online.
  const network = fetch(req, { cache: "no-cache" }).then(async (res) => {
    if (res.ok) await cache.put(req, stamp ? await stamped(res.clone()) : res.clone());
    return res;
  });
  const cached = await cache.match(req);
  if (!cached) return network.catch(() => offlineResponse(req));
  const timeout = new Promise((resolve) => setTimeout(() => resolve(cached), timeoutMs));
  return Promise.race([network.catch(() => cached), timeout]);
}

// Cached API copies carry X-Tareeq-Fetched (ms epoch) so the page can say how old the data it shows is.
async function stamped(res) {
  const headers = new Headers(res.headers);
  headers.set("X-Tareeq-Fetched", String(Date.now()));
  return new Response(await res.blob(), { status: res.status, statusText: res.statusText, headers });
}

function offlineResponse(req) {
  if (new URL(req.url).pathname.startsWith("/api/"))
    return new Response(JSON.stringify({ detail: "offline" }), { status: 503, headers: { "Content-Type": "application/json" } });
  return new Response("offline", { status: 503 });
}
