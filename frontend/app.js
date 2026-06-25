const COLORS = ["#e6194B", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
  "#469990", "#9A6324", "#800000", "#808000", "#000075"];

const map = L.map("map").setView([35.0116, 135.7681], 12);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "© OpenStreetMap contributors",
}).addTo(map);
const layer = L.layerGroup().addTo(map);
const poolLayer = L.layerGroup().addTo(map);   // known POIs not in the current plan
const draftLayer = L.layerGroup().addTo(map);  // the in-progress "add a POI" pin
const proposalLayer = L.layerGroup().addTo(map); // staged LLM suggestions (not yet in the library)

const $ = (id) => document.getElementById(id);
const val = (id) => $(id).value;
// Escape any user/LLM/Nominatim-supplied string before it goes into innerHTML or a
// popup — names, tags, rationales and geocoder results are untrusted (see models.py).
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const setStatus = (t, warn) => { $("status").textContent = ""; $("status").append(
  warn ? Object.assign(document.createElement("span"), { className: "warn", textContent: t }) : t); };

let locks = {};            // poi_id -> lock object (the user's accumulated decisions)
let lastPlan = null;       // last *solved* plan, so edits can re-render without solving
const touched = new Set(); // poi_ids edited since the last solve (the pending set)
const metaOf = {};         // poi_id -> { name } (to label removed POIs)
let allPois = [];          // the whole POI library (GET /pois)
let draft = null;          // { marker } while the user is adding a POI
let candidates = [];        // staged LLM suggestions awaiting accept/reject
let currentCity = null;    // selected city slug — scopes every POI/plan call
const cityMap = {};        // slug -> { label, base, has_transit, transit_operator }
let currentTrip = null;    // null = unsaved draft; else { id, title, status, start_date, notes }
let routeMode = false;     // HYL-68: per-day start/end anchors instead of one base
let waypoints = [];        // [{name, lat, lon}] ordered; day i = waypoints[i] -> waypoints[i+1]
let poolCities = new Set(); // HYL-79: city slugs whose libraries feed the route candidate pool
const poolCache = {};      // slug -> [pois] cache for non-current pool cities (current city = allPois)
let dayWindows = [];       // HYL-69: [{start,end}] per day, used when "customize each day" is on
let showRoutes = false;    // HYL-70: draw actual road geometry instead of straight connectors
let lastGeometry = null;   // per-day decoded [lat,lon] road paths for lastPlan (null = not fetched)
let geometryPending = false; // guard so overlapping /route-geometry fetches don't pile up

// Every POI/plan endpoint is city-scoped; append the picker's city to the query.
const cityQ = () => (currentCity ? "?city=" + encodeURIComponent(currentCity) : "");

// Disable the action buttons while a solve/save is in flight.
function setBusy(on) {
  ["go", "reopt", "trip-save"].forEach((id) => { const b = $(id); if (b) b.disabled = on; });
}

function numIcon(n, color) {
  return L.divIcon({ className: "", iconSize: [26, 26], iconAnchor: [13, 13],
    html: `<div class="num" style="background:${color}">${n}</div>` });
}
function baseIcon() {
  return L.divIcon({ className: "", iconSize: [30, 30], iconAnchor: [15, 15],
    html: `<div class="base">⌂</div>` });
}
function poolIcon() {
  return L.divIcon({ className: "", iconSize: [16, 16], iconAnchor: [8, 8],
    html: `<div class="pool-dot"></div>` });
}
function draftIcon() {
  return L.divIcon({ className: "", iconSize: [30, 30], iconAnchor: [15, 30],
    html: `<div class="draft">📍</div>` });
}
function proposalIcon(on) {
  return L.divIcon({ className: "", iconSize: [18, 18], iconAnchor: [9, 9],
    html: `<div class="prop-dot ${on ? "on" : ""}"></div>` });
}
function wpIcon(label) {   // a route anchor (overnight / start / end)
  return L.divIcon({ className: "", iconSize: [30, 30], iconAnchor: [15, 15],
    html: `<div class="wp-pin">${label}</div>` });
}

// --- solving (only the initial load, "Plan (fresh)", and "Re-optimize" call this) ---
// Base mode -> POST /replan (one hotel). Route mode (HYL-68) -> POST /plan-route with
// per-day (start,end) anchors from the waypoint chain + this place's POI library as the pool.
function waypointAnchors() {   // the waypoint chain -> per-day (start, end) anchors
  const out = [];
  for (let i = 0; i < waypoints.length - 1; i++) {
    const a = waypoints[i], b = waypoints[i + 1];
    out.push({ start_lat: a.lat, start_lon: a.lon, start_name: a.name,
               end_lat: b.lat, end_lon: b.lon, end_name: b.name });
  }
  return out;
}
// HYL-79: the route candidate pool can span several cities. `poolItems` is the active pool as
// `{p, city}` pairs — in route mode the union of every checked city's library (the current city
// reads live from `allPois`, others from `poolCache`); in base mode just the current city. The
// backend already accepts cross-city `poi_refs` (store.load_pois_by_refs qualifies ids as
// `city:id`), so no backend change is needed.
function poolItems() {
  if (!routeMode) return allPois.map((p) => ({ p, city: currentCity }));
  const out = [];
  poolCities.forEach((slug) => {
    const list = slug === currentCity ? allPois : (poolCache[slug] || []);
    list.forEach((p) => out.push({ p, city: slug }));
  });
  return out;
}
const tripPoiRefs = () => poolItems().map(({ p, city }) => ({ city, id: p.id }));

// Fetch + cache a pool city's library (the current city already lives in `allPois`).
async function loadPoolCity(slug) {
  if (slug === currentCity || poolCache[slug]) return;
  try {
    const res = await fetch("/pois?city=" + encodeURIComponent(slug));
    if (res.ok) poolCache[slug] = (await res.json()).pois || [];
  } catch (e) { /* leave uncached; treated as empty until a retry */ }
}

// The checklist of places that feed the route pool. Rendered only in route mode.
function renderPoolCities() {
  const box = $("pool-cities");
  if (!box) return;
  box.innerHTML = Object.values(cityMap).map((c) =>
    `<label class="pool-city">` +
    `<input type="checkbox" data-slug="${esc(c.slug)}" ${poolCities.has(c.slug) ? "checked" : ""}` +
    `${c.slug === currentCity ? " disabled" : ""}/> ${esc(c.label || c.slug)}` +
    `</label>`).join("");
  box.querySelectorAll("input").forEach((el) =>
    el.addEventListener("change", () => togglePoolCity(el.dataset.slug, el.checked)));
}

async function togglePoolCity(slug, on) {
  if (on) { poolCities.add(slug); await loadPoolCity(slug); }
  else { poolCities.delete(slug); }
  drawPool();   // pool only matters at the next solve; just refresh the map dots now
}

// ===== Per-day hours (HYL-69): one start/end per day, layered over the global default ====
const perDayOn = () => $("perday") && $("perday").checked;
const dayCount = () => routeMode ? Math.max(1, waypoints.length - 1) : Math.max(1, +val("days") || 1);
const HHMM = /^\d{1,2}:\d{2}$/;

// Resize dayWindows to the current day count, seeding any new rows from the global start/end.
function syncDayWindows() {
  const n = dayCount();
  if (dayWindows.length > n) dayWindows.length = n;
  while (dayWindows.length < n) dayWindows.push({ start: val("start"), end: val("end") });
}

function renderDayWindows() {
  const ol = $("day-windows");
  if (!ol) return;
  ol.innerHTML = dayWindows.map((w, i) => `
    <li class="dw">
      <span class="dw-day">Day ${i + 1}</span>
      <input class="dw-start" data-i="${i}" value="${esc(w.start)}" />
      <span class="dw-sep">–</span>
      <input class="dw-end" data-i="${i}" value="${esc(w.end)}" />
    </li>`).join("");
  ol.querySelectorAll(".dw-start").forEach((el) =>
    el.addEventListener("input", () => { dayWindows[+el.dataset.i].start = el.value.trim(); }));
  ol.querySelectorAll(".dw-end").forEach((el) =>
    el.addEventListener("input", () => { dayWindows[+el.dataset.i].end = el.value.trim(); }));
}

// Keep the editor in sync when it's visible and the day count changes.
function refreshDayWindows() {
  if (!perDayOn()) return;
  syncDayWindows();
  renderDayWindows();
}

// The day_windows payload for a solve/save: null when off, false on a bad HH:MM value.
function dayWindowsPayload() {
  if (!perDayOn()) return null;
  syncDayWindows();
  if (dayWindows.some((w) => !HHMM.test(w.start) || !HHMM.test(w.end))) return false;
  return dayWindows.map((w) => ({ start: w.start, end: w.end }));
}

$("perday").addEventListener("change", () => {
  const on = $("perday").checked;
  $("perday-panel").hidden = !on;
  if (on) { syncDayWindows(); renderDayWindows(); }
});
$("days").addEventListener("input", refreshDayWindows);

$("buffers-on").addEventListener("change", () => {
  $("buffers-panel").hidden = !$("buffers-on").checked;
});

// HYL-72 contingency buffers. Always sent (defaulting to 0/no-op) so clearing them on a
// loaded trip persists the clear; the toggle is just UI sugar for revealing the inputs.
function buffersPayload() {
  return {
    travel_buffer_pct: Math.max(0, +val("buf-travel-pct") || 0),
    travel_buffer_min: Math.max(0, +val("buf-travel-min") || 0),
    stop_buffer_min: Math.max(0, +val("buf-stop-min") || 0),
  };
}

function planRequest(useLocks) {
  const dw = dayWindowsPayload();
  const common = {
    start: val("start"), end: val("end"), balance: +val("balance"),
    profile: val("profile"), time_limit: 1, locks: useLocks ? Object.values(locks) : [],
    ...(dw ? { day_windows: dw } : {}),
    ...buffersPayload(),
  };
  if (routeMode) {
    return { url: "/plan-route", body: { ...common, day_anchors: waypointAnchors(), poi_refs: tripPoiRefs() } };
  }
  return { url: "/replan", body: {
    ...common, city: currentCity, days: +val("days"),
    base_lat: +val("blat"), base_lon: +val("blon"),
  } };
}

async function request(useLocks) {
  if (routeMode && waypoints.length < 2) {
    setStatus("Add at least 2 stops for a road trip (start + one more).", true); return;
  }
  if (dayWindowsPayload() === false) {
    setStatus("Each day's hours must be HH:MM (e.g. 09:00).", true); return;
  }
  const { url, body } = planRequest(useLocks);
  setStatus("Planning…");
  setBusy(true);
  try {
    let res;
    try {
      res = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
    } catch (e) {
      setStatus("Network error — is the server running?", true);
      return;
    }
    if (!res.ok) { setStatus(`Error ${res.status}: ${await res.text()}`, true); return; }
    const plan = await res.json();
    if (!plan.feasible) { setStatus("⚠ " + plan.reason, true); return; }  // keep last good view + pending edits
    render(plan);
  } finally {
    setBusy(false);
  }
}

// A fresh plan is a new unsaved draft (it discards the loaded trip + its locks).
const freshPlan = () => { locks = {}; touched.clear(); currentTrip = null; renderTripHeader(); request(false); };  // "Plan (fresh)"
const reoptimize = () => request(true);                                    // "Re-optimize"

// --- edits: stage locally, badge, and mark pending — NO solve ---
function onEdit(id) {
  touched.add(id);
  if (lastPlan) renderTimeline(lastPlan);
  updatePending();
}
window.moveStop = (id, day) => { locks[id] = { poi_id: id, type: "day", day }; onEdit(id); };
window.toggleLock = (id, day) => {
  const l = locks[id];
  if (l && l.type === "day" && l.day === day) delete locks[id];
  else locks[id] = { poi_id: id, type: "day", day };
  onEdit(id);
};
window.excludeStop = (id) => {
  if (locks[id] && locks[id].type === "exclude") delete locks[id];
  else locks[id] = { poi_id: id, type: "exclude" };
  onEdit(id);
};
window.includeStop = (id) => {
  if (locks[id] && locks[id].type === "include") delete locks[id];
  else locks[id] = { poi_id: id, type: "include" };
  onEdit(id);
};
window.restore = (id) => { delete locks[id]; onEdit(id); };
window.pinStop = (id, day) => {
  if (locks[id] && locks[id].type === "pin") { delete locks[id]; onEdit(id); return; }  // toggle off
  const t = (prompt("Pin arrival time (HH:MM) — must fit the stop's hours:", "10:00") || "").trim();
  if (!t) return;
  if (!/^\d{1,2}:\d{2}$/.test(t)) { setStatus("Use HH:MM, e.g. 10:30", true); return; }
  locks[id] = { poi_id: id, type: "pin", day, time: t };
  onEdit(id);
};

function updatePending() {
  const n = touched.size;
  const btn = $("reopt");
  if (n > 0) {
    btn.classList.add("pending");
    btn.textContent = `Re-optimize (${n})`;
    setStatus(`${n} pending edit${n > 1 ? "s" : ""} — click Re-optimize`, true);
  } else {
    btn.classList.remove("pending");
    btn.textContent = "Re-optimize";
  }
}

function render(p) {
  if (p !== lastPlan) lastGeometry = null;   // a freshly solved/loaded plan invalidates cached geometry
  lastPlan = p;
  layer.clearLayers();
  const bounds = [];
  const routed = !p.base;   // route mode: per-day start/end anchors, no single base

  if (routed) {
    // Anchor markers from each day's own start/end. A connected waypoint chain (day i's end ==
    // day i+1's start) collapses to one marker per node; but /plan-route + the MCP tools allow
    // DISJOINT legs (a day's start ≠ the prior day's end), so emit a fresh start marker whenever
    // it differs from the running anchor (HYL-79 #2) instead of assuming the chain is connected.
    const same = (a, b) => a && b && Math.abs(a.lat - b.lat) < 1e-7 && Math.abs(a.lon - b.lon) < 1e-7;
    const anchors = [];
    p.days.forEach((d) => {
      if (!d.start || !d.end) return;
      if (!same(anchors[anchors.length - 1], d.start)) anchors.push(d.start);
      anchors.push(d.end);
    });
    anchors.forEach((a, i) => {
      const label = i === 0 ? "A" : (i === anchors.length - 1 ? "Z" : String(i));
      const where = i === 0 ? "start" : (i === anchors.length - 1 ? "end" : "stopover");
      const ll = [a.lat, a.lon];
      L.marker(ll, { icon: wpIcon(label) }).addTo(layer)
        .bindPopup(`<b>${esc(a.name || ("Stop " + (i + 1)))}</b><br>${where}`);
      bounds.push(ll);
    });
  } else {
    const base = [p.base.lat, p.base.lon];
    L.marker(base, { icon: baseIcon() }).addTo(layer).bindPopup("Base (hotel)");
    bounds.push(base);
  }

  p.days.forEach((day, di) => {
    const color = COLORS[di % COLORS.length];
    const start = routed ? [day.start.lat, day.start.lon] : [p.base.lat, p.base.lon];
    const end = routed ? [day.end.lat, day.end.lon] : [p.base.lat, p.base.lon];
    const pts = [start];
    day.stops.forEach((s, si) => {
      metaOf[s.poi_id] = { name: s.name };
      const ll = [s.lat, s.lon];
      L.marker(ll, { icon: numIcon(si + 1, color) }).addTo(layer)
        .bindPopup(`<b>${esc(s.name)}</b><br>Day ${di + 1} · ${s.arrival_hhmm}–${s.departure_hhmm}<br>stay ${s.dwell}m`);
      pts.push(ll);
      bounds.push(ll);
    });
    pts.push(end);
    const geo = showRoutes && lastGeometry && lastGeometry[di];
    if (geo && geo.length)
      L.polyline(geo, { color, weight: 4, opacity: 0.85 }).addTo(layer);                 // actual road route
    else
      L.polyline(pts, { color, weight: 3, opacity: 0.7, dashArray: "6,6" }).addTo(layer); // straight connector
  });

  p.dropped.forEach((d) => {
    metaOf[d.poi_id] = { name: d.name };
    const ll = [d.lat, d.lon];
    L.circleMarker(ll, { radius: 6, color: "#999", fillColor: "#ccc", fillOpacity: 0.85 })
      .addTo(layer).bindPopup(`${esc(d.name)} — dropped (didn't fit)`);
    bounds.push(ll);
  });

  if (bounds.length) map.fitBounds(bounds, { padding: [40, 40] });
  renderTimeline(p);
  const stops = p.days.reduce((a, d) => a + d.stops.length, 0);
  setStatus(`${stops} stops · ${p.total_travel_min}m driving · ${p.dropped.length} dropped`);
  touched.clear();
  updatePending();
  drawPool();
  if (showRoutes && !lastGeometry) ensureGeometry();   // HYL-70: fetch real geometry, then re-render
}

// HYL-70: fetch decoded road geometry for the current plan and re-render with real routes.
// A pure derived view (no solve): runs only when the toggle is on, once per plan, no overlap.
async function ensureGeometry() {
  if (!showRoutes || !lastPlan || lastGeometry || geometryPending) return;
  const plan = lastPlan;
  const profile = plan.profile || val("profile");   // the profile the plan was SOLVED with, not the
                                                     // live dropdown (which may have changed since)
  const days = plan.days.map((day) => {
    const start = plan.base ? [plan.base.lat, plan.base.lon] : [day.start.lat, day.start.lon];
    const end = plan.base ? [plan.base.lat, plan.base.lon] : [day.end.lat, day.end.lon];
    return [start, ...day.stops.map((s) => [s.lat, s.lon]), end];
  });
  geometryPending = true;
  try {
    const res = await fetch("/route-geometry", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        city: currentCity, profile,
        mode: plan.base ? "base" : "route", days,
      }),
    });
    if (res.ok && lastPlan === plan) {           // still the current plan: apply + draw
      lastGeometry = (await res.json()).days || null;
      render(plan);                              // same ref -> no re-invalidation
    }
  } catch (e) {
    /* network error — keep straight lines */
  } finally {
    geometryPending = false;
  }
  // If a re-solve superseded this fetch while it was in flight, that plan's own ensureGeometry()
  // was blocked by the in-flight guard; now that the guard is clear, pick up the newer plan. Gated
  // on the plan having changed so a failure for the *current* plan doesn't spin in a retry loop.
  if (lastPlan !== plan && showRoutes && !lastGeometry) ensureGeometry();
}

function dayOptions(days, cur) {
  let o = "";
  for (let i = 0; i < days; i++) o += `<option value="${i}" ${i === cur ? "selected" : ""}>Day ${i + 1}</option>`;
  return o;
}

function renderTimeline(p) {
  const days = p.days.length;
  const routed = !p.base;   // route mode: per-day start/end anchors
  const inDays = new Set(p.days.flatMap((d) => d.stops.map((s) => s.poi_id)));
  let h = "";

  p.days.forEach((day, di) => {
    const color = COLORS[di % COLORS.length];
    const from = routed ? esc(day.start.name || "start") : "base";
    const to = routed ? esc(day.end.name || "end") : "base";
    const head = routed ? `Day ${di + 1} <span class="muted">· ${from} → ${to}</span>` : `Day ${di + 1}`;
    h += `<div class="day"><h3><span class="dot" style="background:${color}"></span>${head}</h3>`;
    if (!day.stops.length) {
      h += routed
        ? `<div class="leg muted">drive ${from} → ${to} — no stops · ${day.travel_min}m</div></div>`
        : `<div class="muted">(free day)</div></div>`;
      return;
    }
    h += `<div class="leg muted">${day.day_start} · leave ${from}</div>`;
    day.stops.forEach((s, si) => {
      const lk = locks[s.poi_id];
      const pinned = lk && lk.type === "pin";
      const lockedHere = lk && lk.type === "day" && lk.day === di;
      const movePending = lk && lk.type === "day" && lk.day !== di;
      const removePending = lk && lk.type === "exclude";
      const sel = lk && (lk.type === "day" || lk.type === "pin") ? lk.day : di;
      let badge = "";
      if (removePending) badge = `<span class="badge remove">will remove</span>`;
      else if (pinned) badge = `<span class="badge lock">📌 ${lk.time}</span>`;
      else if (movePending) badge = `<span class="badge move">→ Day ${lk.day + 1}</span>`;
      else if (lockedHere) badge = `<span class="badge lock">🔒 locked</span>`;
      h += `<div class="stop ${lockedHere || pinned ? "locked" : ""} ${movePending || removePending ? "pending" : ""}">` +
        `<span class="t">${s.arrival_hhmm}</span><b>${si + 1}. ${esc(s.name)}</b>${badge}` +
        `<div class="row">` +
        `<select onchange="moveStop('${s.poi_id}', +this.value)">${dayOptions(days, sel)}</select>` +
        `<button title="lock to this day" onclick="toggleLock('${s.poi_id}', ${di})">${lockedHere ? "🔒" : "🔓"}</button>` +
        `<button title="pin arrival time" onclick="pinStop('${s.poi_id}', ${di})">${pinned ? "📌" : "⏰"}</button>` +
        `<button title="remove from trip" onclick="excludeStop('${s.poi_id}')">${removePending ? "↺ keep" : "✕"}</button>` +
        `<span class="muted">${s.dwell}m · +${s.travel_in}m</span>` +
        `</div></div>`;
    });
    h += `<div class="leg muted">${day.return_hhmm} · ${routed ? "arrive " + to : "back at base"} — ${day.stops.length} stops, ${day.travel_min}m driving</div></div>`;
  });

  if (p.dropped.length) {
    h += `<div class="day"><h3>Dropped (didn't fit)</h3>`;
    [...p.dropped].sort((a, b) => b.importance - a.importance).forEach((d) => {
      const willAdd = locks[d.poi_id] && locks[d.poi_id].type === "include";
      h += `<div class="stop"><span class="muted">✗ ${esc(d.name)}</span>` +
        `${willAdd ? ' <span class="badge add">will add</span>' : ""} ` +
        `<button onclick="includeStop('${d.poi_id}')">${willAdd ? "↺ undo" : "＋ must-visit"}</button></div>`;
    });
    h += `</div>`;
  }

  const removed = Object.values(locks).filter((l) => l.type === "exclude" && !inDays.has(l.poi_id));
  if (removed.length) {
    h += `<div class="day"><h3>Removed</h3>`;
    removed.forEach((l) => {
      const name = (metaOf[l.poi_id] && metaOf[l.poi_id].name) || l.poi_id;
      h += `<div class="stop"><span class="muted">⃠ ${esc(name)}</span> ` +
        `<button onclick="restore('${l.poi_id}')">restore</button></div>`;
    });
    h += `</div>`;
  }

  $("timeline").innerHTML = h;
}

// ===== POI library: load + draw the "known places not in this plan" pool =====
async function loadPois() {
  try {
    const res = await fetch("/pois" + cityQ());
    if (!res.ok) return;
    allPois = (await res.json()).pois || [];
  } catch (e) { return; }
  drawPool();
}

// After a solve every library POI is either a numbered stop or a grey 'dropped'
// dot, so the pool shows exactly the POIs added since — instant feedback that a
// new place landed, pending the next Re-optimize.
function drawPool() {
  poolLayer.clearLayers();
  const inPlan = new Set();
  if (lastPlan) {
    lastPlan.days.forEach((d) => d.stops.forEach((s) => inPlan.add(s.poi_id)));
    lastPlan.dropped.forEach((d) => inPlan.add(d.poi_id));
  }
  poolItems().forEach(({ p, city }) => {
    // Route plans identify pooled POIs by a city-qualified id (store.pool_poi_id); base
    // plans use the bare library id. Match the plan's namespace so "already routed" POIs
    // aren't redrawn as pending. With a multi-city pool (HYL-79) each POI is namespaced by
    // its OWN city, not whichever city happens to be selected.
    const planId = routeMode ? `${city}:${p.id}` : p.id;
    metaOf[planId] = { name: p.name };
    if (inPlan.has(planId)) return;
    const tags = (p.tags || []).join(", ");
    const fromOther = city !== currentCity;
    const where = fromOther ? `in ${esc((cityMap[city] && cityMap[city].label) || city)}'s library`
                            : "in library";
    // The remove button deletes via the selected city's scope, so only offer it for the
    // current city's own POIs (other cities' POIs are managed when that city is selected).
    const rm = fromOther ? ""
      : `<br><button class="linkbtn" onclick="removePoi('${p.id}')">✕ remove from library</button>`;
    L.marker([p.lat, p.lon], { icon: poolIcon() }).addTo(poolLayer).bindPopup(
      `<b>${esc(p.name)}</b>${tags ? `<br><span class="muted">${esc(tags)}</span>` : ""}` +
      `<br><span class="muted">${where} · Re-optimize to route</span>${rm}`
    );
  });
}

window.removePoi = async (id) => {
  if (!confirm("Remove this POI from your library?")) return;
  const res = await fetch(`/pois/${encodeURIComponent(id)}` + cityQ(), { method: "DELETE" });
  if (res.ok) { setStatus("Removed from library."); loadPois(); }
};

// ===== Add a POI — by searching, or by clicking the map (Google-Maps style) ===
let searchTimer = null;
$("q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = val("q").trim();
  if (q.length < 2) { $("results").innerHTML = ""; return; }
  searchTimer = setTimeout(() => runSearch(q), 400);  // debounce — Nominatim is rate-limited
});

async function runSearch(q) {
  let res;
  try { res = await fetch(`/geocode?q=${encodeURIComponent(q)}`); }
  catch (e) { return; }
  if (!res.ok) { $("results").innerHTML = `<div class="result muted">search failed</div>`; return; }
  const hits = (await res.json()).results || [];
  if (!hits.length) { $("results").innerHTML = `<div class="result muted">no matches</div>`; return; }
  $("results").innerHTML = hits.map((h) =>
    `<div class="result"><b>${esc(h.name)}</b><br><span class="muted">${esc(h.display_name)}</span></div>`
  ).join("");
  [...$("results").children].forEach((el, i) => {
    el.onclick = () => {
      $("results").innerHTML = "";
      $("q").value = hits[i].name;
      map.flyTo([hits[i].lat, hits[i].lon], 15);
      beginAdd(hits[i].lat, hits[i].lon, hits[i].name);
    };
  });
}

map.on("click", async (e) => {
  const { lat, lng } = e.latlng;
  if (draft) { draft.marker.setLatLng([lat, lng]); return; }  // already adding → just nudge the pin
  beginAdd(lat, lng, "");
  try {                                                       // best-effort name prefill
    const res = await fetch(`/reverse?lat=${lat}&lon=${lng}`);
    if (res.ok && draft) {
      const r = await res.json();
      if (r.name && !val("f-name")) $("f-name").value = r.name;
    }
  } catch (e) { /* reverse-geocode is optional; the user can just type a name */ }
});

function beginAdd(lat, lon, name) {
  draftLayer.clearLayers();
  const marker = L.marker([lat, lon], { icon: draftIcon(), draggable: true }).addTo(draftLayer);
  draft = { marker };
  $("f-name").value = name || "";
  $("f-dwell").value = 60;
  $("f-open").value = ""; $("f-close").value = "";
  $("f-tags").value = ""; $("f-notes").value = "";
  $("addform").hidden = false;
  $("f-name").focus();
}

function endAdd() {
  draft = null;
  draftLayer.clearLayers();
  $("addform").hidden = true;
  $("results").innerHTML = "";
  $("q").value = "";
}

$("f-cancel").addEventListener("click", endAdd);

$("addform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!draft) return;
  const name = val("f-name").trim();
  if (!name) { $("f-name").focus(); return; }
  const { lat, lng } = draft.marker.getLatLng();
  const body = {
    name, lat, lon: lng,
    importance: +val("f-importance"),
    dwell_min: +val("f-dwell") || 60,
    open: val("f-open").trim(),
    close: val("f-close").trim(),
    tags: val("f-tags").split(",").map((t) => t.trim()).filter(Boolean),
    notes: val("f-notes").trim() || null,
  };
  const res = await fetch("/pois" + cityQ(), {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) { setStatus(`Couldn't add: ${res.status} ${await res.text()}`, true); return; }
  endAdd();
  await loadPois();
  setStatus(`Added “${name}” — Re-optimize to include it`, true);
});

// ===== AI suggestions (step 5): brief -> grounded candidates -> accept ========
async function runSuggest() {
  const prompt = val("brief").trim();
  if (!prompt) { $("brief").focus(); return; }
  const body = { prompt, area: val("brief-area").trim() || null, count: +val("brief-count") || 8 };
  setStatus("Asking the AI for ideas…");
  $("suggest").disabled = true;
  let res;
  try {
    res = await fetch("/suggest" + cityQ(), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch (e) { setStatus("Network error — is the server running?", true); $("suggest").disabled = false; return; }
  $("suggest").disabled = false;
  if (!res.ok) { setStatus(`⚠ ${res.status}: ${await res.text()}`, true); return; }  // e.g. 503 = no LLM key
  candidates = (await res.json()).candidates || [];
  candidates.forEach((c) => { c._accept = c.status === "resolved" && !c.duplicate; });
  renderCandidates();
  drawProposals();
  const located = candidates.filter((c) => c.status === "resolved").length;
  setStatus(`${candidates.length} ideas, ${located} located — pick the ones you want, then Re-optimize`, true);
}

function renderCandidates() {
  const box = $("candidates");
  if (!candidates.length) { box.innerHTML = ""; return; }
  let h = `<div class="cand-head"><span>Suggestions</span><button class="linkbtn" id="cand-clear">clear</button></div>`;
  candidates.forEach((c, i) => {
    const bad = c.status !== "resolved";
    h += `<div class="candidate ${c._accept ? "on" : ""} ${bad ? "bad" : ""}">
      <div class="c-top">
        <input type="checkbox" class="c-check" data-i="${i}" ${c._accept ? "checked" : ""} ${bad ? "disabled" : ""}>
        <b>${esc(c.name)}</b>${c.duplicate ? ' <span class="badge remove">in library</span>' : ""}
      </div>
      <div class="muted c-loc">${bad ? "⚠ couldn’t locate — edit the name and re-suggest" : esc(c.display_name)}</div>
      ${c.rationale ? `<div class="c-why">${esc(c.rationale)}</div>` : ""}
      <div class="row">
        <label class="mini">imp <input class="c-imp" data-i="${i}" type="number" step="0.05" min="0" max="1" value="${c.importance}"></label>
        <label class="mini">dwell <input class="c-dwell" data-i="${i}" type="number" min="0" value="${c.dwell_min}"></label>
        ${(c.tags || []).length ? `<span class="muted">${esc(c.tags.join(", "))}</span>` : ""}
      </div>
    </div>`;
  });
  h += `<button id="accept" type="button">Add selected to library</button>`;
  box.innerHTML = h;
  $("cand-clear").onclick = clearCandidates;
  $("accept").onclick = acceptCandidates;
  box.querySelectorAll(".c-check").forEach((el) => el.onchange = () => {
    candidates[+el.dataset.i]._accept = el.checked; renderCandidates(); drawProposals();
  });
  box.querySelectorAll(".c-imp").forEach((el) => el.onchange = () => candidates[+el.dataset.i].importance = +el.value);
  box.querySelectorAll(".c-dwell").forEach((el) => el.onchange = () => candidates[+el.dataset.i].dwell_min = +el.value);
}

function drawProposals() {
  proposalLayer.clearLayers();
  const bounds = [];
  candidates.forEach((c) => {
    if (c.status !== "resolved") return;
    const ll = [c.lat, c.lon];
    L.marker(ll, { icon: proposalIcon(c._accept) }).addTo(proposalLayer)
      .bindPopup(`<b>${esc(c.name)}</b>${c.rationale ? `<br><span class="muted">${esc(c.rationale)}</span>` : ""}`);
    bounds.push(ll);
  });
  if (bounds.length) map.fitBounds(bounds, { padding: [50, 50] });
}

async function acceptCandidates() {
  const picked = candidates.filter((c) => c._accept && c.status === "resolved");
  if (!picked.length) { setStatus("Tick at least one located suggestion first.", true); return; }
  const bodies = picked.map((c) => ({
    name: c.name, lat: c.lat, lon: c.lon,
    importance: +c.importance, dwell_min: +c.dwell_min || 60,
    tags: c.tags || [], notes: c.rationale || null,   // keep the LLM's "why" as the POI note
  }));
  const res = await fetch("/pois/batch" + cityQ(), {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(bodies),
  });
  if (!res.ok) { setStatus(`Couldn't add: ${res.status}`, true); return; }
  clearCandidates();
  await loadPois();
  setStatus(`Added ${bodies.length} place${bodies.length > 1 ? "s" : ""} — Re-optimize to include`, true);
}

function clearCandidates() {
  candidates = [];
  proposalLayer.clearLayers();
  $("candidates").innerHTML = "";
}

$("go").addEventListener("click", freshPlan);
$("reopt").addEventListener("click", reoptimize);
$("suggest").addEventListener("click", runSuggest);

// Point the sidebar + map at a city: its base, map center, title, and whether the
// transit mode is offered (only where the city has a GTFS feed). Does NOT reload.
function applyCity(slug) {
  const c = cityMap[slug];
  if (!c) return;
  currentCity = slug;
  $("city").value = slug;                       // keep the picker in sync on programmatic selects
  if (c.base) {
    $("blat").value = c.base.lat;
    $("blon").value = c.base.lon;
    map.setView([c.base.lat, c.base.lon], 12);
  }
  document.title = `Trip Planner — ${c.label || slug}`;
  const opt = $("profile").querySelector('option[value="transit"]');
  if (opt) {
    opt.disabled = !c.has_transit;
    opt.textContent = c.has_transit ? `transit (${c.transit_operator || "transit"})` : "transit (n/a here)";
    if (!c.has_transit && val("profile") === "transit") $("profile").value = "foot";
  }
  const rm = $("city-remove");
  if (rm) rm.hidden = !c.user_created;          // ✕ only for the user's own places
}

// Switching city is just an API re-scope: reset edits, re-point, reload POIs, replan.
function selectCity(slug) {
  if (!slug || slug === currentCity) return;
  applyCity(slug);
  if (routeMode) { $("routemode").checked = false; setRouteMode(false); }  // anchors are place-specific
  poolCities = new Set();                       // HYL-79: the route pool is place-specific too
  Object.keys(poolCache).forEach((k) => delete poolCache[k]);
  locks = {}; touched.clear(); lastPlan = null;
  currentTrip = null; renderTripHeader();
  layer.clearLayers(); poolLayer.clearLayers(); clearCandidates();
  loadPois();
  loadTrips();
  freshPlan();
}

// (Re)build the place picker from GET /cities — the user's own places grouped above the
// curated catalog. Refreshes cityMap. Returns the slugs in server (label) order.
async function loadCities(selectSlug) {
  const sel = $("city");
  let cities = [];
  try { cities = (await (await fetch("/cities")).json()).cities || []; }
  catch (e) { return []; }
  Object.keys(cityMap).forEach((k) => delete cityMap[k]);
  sel.innerHTML = "";
  const group = (label, list) => {
    if (!list.length) return;
    const g = document.createElement("optgroup"); g.label = label;
    list.forEach((c) => {
      cityMap[c.slug] = c;
      const o = document.createElement("option");
      o.value = c.slug; o.textContent = c.label || c.slug;
      g.appendChild(o);
    });
    sel.appendChild(g);
  };
  group("Your places", cities.filter((c) => c.user_created));
  group("Catalog cities", cities.filter((c) => !c.user_created));
  sel.onchange = () => selectCity(sel.value);
  if (selectSlug && cityMap[selectSlug]) sel.value = selectSlug;
  if (routeMode) renderPoolCities();   // HYL-79: refresh the pool checklist against the new city list
  return cities.map((c) => c.slug);
}

// ===== Destination search: set ANY place as the trip base ====================
// Geocode free text, then POST /cities to create (or reuse) a place — the server resolves
// its US region from the coordinates. The new place is then scoped like any other city.
let destTimer = null;
$("dest").addEventListener("input", () => {
  clearTimeout(destTimer);
  const q = val("dest").trim();
  if (q.length < 2) { $("dest-results").innerHTML = ""; return; }
  destTimer = setTimeout(() => runDestSearch(q), 400);   // debounce — Nominatim is rate-limited
});

async function runDestSearch(q) {
  let res;
  try { res = await fetch(`/geocode?q=${encodeURIComponent(q)}`); }
  catch (e) { return; }
  if (!res.ok) { $("dest-results").innerHTML = `<div class="result muted">search failed</div>`; return; }
  const hits = (await res.json()).results || [];
  if (!hits.length) { $("dest-results").innerHTML = `<div class="result muted">no matches</div>`; return; }
  $("dest-results").innerHTML = hits.map((h) =>
    `<div class="result"><b>${esc(h.name)}</b><br><span class="muted">${esc(h.display_name)}</span></div>`
  ).join("");
  [...$("dest-results").children].forEach((el, i) => { el.onclick = () => createPlace(hits[i]); });
}

async function createPlace(hit) {
  $("dest-results").innerHTML = ""; $("dest").value = "";
  setStatus(`Setting base to “${hit.name}”…`);
  let res;
  try {
    res = await fetch("/cities", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: hit.name, lat: hit.lat, lon: hit.lon }),
    });
  } catch (e) { setStatus("Network error — is the server running?", true); return; }
  if (!res.ok) { setStatus(`⚠ ${await res.text()}`, true); return; }   // e.g. 422 outside coverage
  const city = await res.json();
  const prev = currentCity;
  await loadCities(city.slug);          // refresh the picker + cache, select the new/reused place
  if (city.slug !== prev) { currentCity = null; selectCity(city.slug); }  // re-scope to it
  else { applyCity(city.slug); setStatus(`Already here: ${city.label || city.slug}.`); }
}

async function removePlace() {
  const slug = currentCity, c = cityMap[slug];
  if (!c || !c.user_created) return;
  if (!confirm(`Remove “${c.label || slug}”? Its saved POIs and trips will be deleted.`)) return;
  let res;
  try { res = await fetch(`/cities/${encodeURIComponent(slug)}`, { method: "DELETE" }); }
  catch (e) { setStatus("Network error while removing.", true); return; }
  if (!res.ok) { setStatus(`Couldn't remove: ${await res.text()}`, true); return; }
  const slugs = await loadCities();
  currentCity = null;
  if (slugs[0]) selectCity(slugs[0]);
  setStatus("Place removed.");
}
$("city-remove").addEventListener("click", removePlace);

// ===== Route mode (HYL-68): per-day start/end anchors via a waypoint chain ====
function setRouteMode(on) {
  routeMode = on;
  $("route-panel").hidden = !on;
  document.querySelectorAll(".base-field").forEach((el) => { el.style.display = on ? "none" : ""; });
  if (on && !waypoints.length) {  // seed the first anchor from the current base/place
    const c = cityMap[currentCity];
    const name = (c && c.base && c.base.name) || (c && c.label) || "Start";
    waypoints = [{ name, lat: +val("blat"), lon: +val("blon") }];
  }
  if (on && currentCity) poolCities.add(currentCity);   // HYL-79: the current city always feeds the pool
  renderPoolCities();
  renderWaypoints();
  drawPool();   // reflect the (possibly multi-city) pool on the map right away
}

function renderWaypoints() {
  const ol = $("waypoints");
  if (!ol) return;
  ol.innerHTML = waypoints.map((w, i) => `
    <li class="wp">
      <span class="wp-name">${esc(w.name)}</span>
      <span class="wp-ctl">
        <button title="move up" onclick="wpMove(${i}, -1)" ${i === 0 ? "disabled" : ""}>↑</button>
        <button title="move down" onclick="wpMove(${i}, 1)" ${i === waypoints.length - 1 ? "disabled" : ""}>↓</button>
        <button title="remove" onclick="wpRemove(${i})">✕</button>
      </span>
    </li>`).join("");
  $("days").value = Math.max(1, waypoints.length - 1);   // keep the derived day count in sync
  refreshDayWindows();                                   // HYL-69: re-seed per-day rows to match
}
window.wpMove = (i, d) => {
  const j = i + d;
  if (j < 0 || j >= waypoints.length) return;
  [waypoints[i], waypoints[j]] = [waypoints[j], waypoints[i]];
  renderWaypoints();
};
window.wpRemove = (i) => { waypoints.splice(i, 1); renderWaypoints(); };

let wpTimer = null;
$("wp").addEventListener("input", () => {
  clearTimeout(wpTimer);
  const q = val("wp").trim();
  if (q.length < 2) { $("wp-results").innerHTML = ""; return; }
  wpTimer = setTimeout(() => runWpSearch(q), 400);   // debounce — Nominatim is rate-limited
});
async function runWpSearch(q) {
  let res;
  try { res = await fetch(`/geocode?q=${encodeURIComponent(q)}`); }
  catch (e) { return; }
  if (!res.ok) { $("wp-results").innerHTML = `<div class="result muted">search failed</div>`; return; }
  const hits = (await res.json()).results || [];
  if (!hits.length) { $("wp-results").innerHTML = `<div class="result muted">no matches</div>`; return; }
  $("wp-results").innerHTML = hits.map((h) =>
    `<div class="result"><b>${esc(h.name)}</b><br><span class="muted">${esc(h.display_name)}</span></div>`
  ).join("");
  [...$("wp-results").children].forEach((el, i) => {
    el.onclick = () => {
      waypoints.push({ name: hits[i].name, lat: hits[i].lat, lon: hits[i].lon });
      $("wp").value = ""; $("wp-results").innerHTML = "";
      renderWaypoints();
      map.flyTo([hits[i].lat, hits[i].lon], 11);
    };
  });
}
$("routemode").addEventListener("change", () => setRouteMode($("routemode").checked));

// HYL-70: toggle actual-route geometry. Re-renders the current plan (no solve); render()
// lazily fetches geometry when it's on and not yet cached.
$("show-routes").addEventListener("change", () => {
  showRoutes = $("show-routes").checked;
  if (lastPlan) render(lastPlan);
});

// ===== Trips: save / browse / load / lifecycle =============================
function renderTripHeader() {
  const t = currentTrip;
  $("trip-title").value = t ? t.title : "";
  $("trip-status").value = t ? t.status : "draft";
  $("trip-date").value = (t && t.start_date) ? t.start_date : "";
  $("trip-tag").textContent = t ? `Saved trip · #${t.id}` : "Unsaved draft";
  $("trip-saveas").hidden = !t;
}

async function loadTrips() {
  let trips = [];
  try { trips = (await (await fetch("/trips" + cityQ())).json()).trips || []; }
  catch (e) { return; }
  const box = $("trips");
  if (!trips.length) { box.className = "trips muted"; box.textContent = "No trips yet."; return; }
  box.className = "trips";
  box.innerHTML = trips.map((t) => `
    <div class="trip-row ${currentTrip && currentTrip.id === t.id ? "active" : ""}">
      <div class="trip-row-main"><b>${esc(t.title)}</b><span class="chip ${esc(t.status)}">${esc(t.status)}</span></div>
      <div class="trip-row-sub muted">${t.start_date ? esc(t.start_date) + " · " : ""}${t.mode === "route" ? "🚗 " : ""}${t.num_days}d · ${esc(t.profile)} · ${t.stops} stops</div>
      <div class="row">
        <button onclick="loadTrip(${t.id})">Load</button>
        <button class="danger" onclick="deleteTrip(${t.id})">Delete</button>
      </div>
    </div>`).join("");
}

function tripBody(title) {
  const body = {
    city: currentCity, title,
    status: $("trip-status").value || "draft",
    notes: (currentTrip && currentTrip.notes) || null,   // preserve notes across an in-place Save
    start_date: $("trip-date").value || null,
    start: val("start"), end: val("end"),
    balance: +val("balance"), profile: val("profile"),
    locks: Object.values(locks), result: lastPlan,   // persist exactly what's shown
  };
  if (routeMode) {
    body.mode = "route";
    body.day_anchors = waypointAnchors();
    body.poi_refs = tripPoiRefs();
  } else {
    body.mode = "base";
    body.days = +val("days");
    body.base_lat = +val("blat"); body.base_lon = +val("blon");
  }
  const dw = dayWindowsPayload();
  if (dw) body.day_windows = dw;   // HYL-69: persist per-day hours when customizing
  Object.assign(body, buffersPayload());   // HYL-72: persist contingency buffers
  return body;
}

async function saveCurrent(forceNew) {
  if (!lastPlan) { setStatus("Plan something first, then Save.", true); return; }
  if (dayWindowsPayload() === false) { setStatus("Each day's hours must be HH:MM.", true); return; }
  const title = $("trip-title").value.trim() || "Untitled trip";
  const id = (!forceNew && currentTrip) ? currentTrip.id : null;
  setBusy(true); setStatus(id ? "Saving…" : "Saving trip…");
  try {
    const res = await fetch(id ? `/trips/${id}` : "/trips", {
      method: id ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(tripBody(title)),
    });
    if (!res.ok) { setStatus(`Save failed: ${res.status} ${await res.text()}`, true); return; }
    const t = await res.json();
    currentTrip = { id: t.id, title: t.title, status: t.status, start_date: t.start_date, notes: t.notes };
    renderTripHeader();
    await loadTrips();
    setStatus(`Saved “${t.title}”.`);
  } catch (e) { setStatus("Network error while saving.", true); }
  finally { setBusy(false); }
}

window.loadTrip = async (id) => {
  setBusy(true); setStatus("Loading trip…");
  let t;
  try {
    const res = await fetch(`/trips/${id}`);
    if (!res.ok) { setStatus(`Load failed: ${res.status}`, true); setBusy(false); return; }
    t = await res.json();
  } catch (e) { setStatus("Network error while loading.", true); setBusy(false); return; }
  setBusy(false);
  const wins = (t.days || []).map((d) => ({ start: d.day_start, end: d.day_end }));
  const w0 = wins[0] || { start: "09:00", end: "19:00" };
  $("start").value = w0.start; $("end").value = w0.end;
  $("profile").value = t.profile; $("balance").value = t.balance;
  if (t.mode === "route") {     // rebuild the route UI + waypoint chain from the saved anchors
    routeMode = true; $("routemode").checked = true; $("route-panel").hidden = false;
    document.querySelectorAll(".base-field").forEach((el) => { el.style.display = "none"; });
    waypoints = [];
    if (t.days.length && t.days[0].start) {
      waypoints.push({ name: t.days[0].start.name, lat: t.days[0].start.lat, lon: t.days[0].start.lon });
      t.days.forEach((d) => { if (d.end) waypoints.push({ name: d.end.name, lat: d.end.lat, lon: d.end.lon }); });
    }
    // HYL-79: restore the multi-city pool. A route trip's stops/dropped carry city-qualified
    // ids (`city:id`), so their distinct prefixes are exactly the cities that fed the pool.
    poolCities = new Set([currentCity]);
    const cityOf = (pid) =>
      (typeof pid === "string" && pid.includes(":")) ? pid.slice(0, pid.indexOf(":")) : null;
    t.days.forEach((d) => d.stops.forEach((s) => { const c = cityOf(s.poi_id); if (c) poolCities.add(c); }));
    (t.dropped || []).forEach((dp) => { const c = cityOf(dp.poi_id); if (c) poolCities.add(c); });
    await Promise.all([...poolCities].map(loadPoolCity));
    renderPoolCities();
    renderWaypoints();
  } else {
    routeMode = false; $("routemode").checked = false; $("route-panel").hidden = true;
    document.querySelectorAll(".base-field").forEach((el) => { el.style.display = ""; });
    poolCities = new Set();   // HYL-79: base trips have no cross-city pool
    $("days").value = t.num_days;
    $("blat").value = t.base.lat; $("blon").value = t.base.lon;
  }
  // HYL-69: restore per-day hours; reveal the editor only when days actually differ.
  dayWindows = wins.map((w) => ({ start: w.start, end: w.end }));
  const customized = wins.length > 0 && !wins.every((w) => w.start === w0.start && w.end === w0.end);
  $("perday").checked = customized;
  $("perday-panel").hidden = !customized;
  if (customized) renderDayWindows();
  // HYL-72: restore contingency buffers; reveal the editor only when any are set.
  $("buf-travel-pct").value = t.travel_buffer_pct || 0;
  $("buf-travel-min").value = t.travel_buffer_min || 0;
  $("buf-stop-min").value = t.stop_buffer_min || 0;
  const hasBuffers = (t.travel_buffer_pct || t.travel_buffer_min || t.stop_buffer_min) > 0;
  $("buffers-on").checked = hasBuffers;
  $("buffers-panel").hidden = !hasBuffers;
  locks = {}; (t.locks || []).forEach((l) => { locks[l.poi_id] = l; });
  touched.clear();
  currentTrip = { id: t.id, title: t.title, status: t.status, start_date: t.start_date, notes: t.notes };
  renderTripHeader();
  render(t);            // the GET /trips/{id} payload matches render()'s plan shape
  loadTrips();          // refresh the active highlight
};

window.deleteTrip = async (id) => {
  if (!confirm("Delete this saved trip?")) return;
  const res = await fetch(`/trips/${id}`, { method: "DELETE" });
  if (res.ok) {
    if (currentTrip && currentTrip.id === id) { currentTrip = null; renderTripHeader(); }
    loadTrips(); setStatus("Trip deleted.");
  }
};

// PATCH a metadata field on the loaded saved trip (status / start_date / title).
// For an unsaved draft the header inputs just seed the next Save.
async function patchTrip(fields) {
  if (!currentTrip) return;
  try {
    const res = await fetch(`/trips/${currentTrip.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fields),
    });
    if (!res.ok) return;
    const t = await res.json();
    currentTrip = { id: t.id, title: t.title, status: t.status, start_date: t.start_date, notes: t.notes };
    loadTrips();
  } catch (e) { /* non-fatal */ }
}

$("trip-save").addEventListener("click", () => saveCurrent(false));
$("trip-saveas").addEventListener("click", () => saveCurrent(true));
$("trip-new").addEventListener("click", () => freshPlan());     // fresh plan == new draft
$("trip-status").addEventListener("change", () => patchTrip({ status: $("trip-status").value }));
$("trip-date").addEventListener("change", () => patchTrip({ start_date: $("trip-date").value || null }));
$("trip-title").addEventListener("change", () => { if (currentTrip) patchTrip({ title: $("trip-title").value.trim() || currentTrip.title }); });

// Bootstrap: load the places into the picker, default to the served city, THEN plan.
async function init() {
  let defaultCity = null;
  try { defaultCity = (await (await fetch("/config")).json()).default_city || null; } catch (e) {}
  const slugs = await loadCities();
  const start = (defaultCity && cityMap[defaultCity]) ? defaultCity : slugs[0];
  if (start) applyCity(start);
  else currentCity = defaultCity;        // no /cities → single-city fallback
  renderTripHeader();
  loadPois();
  loadTrips();
  freshPlan();
}
init();
