"use strict";
// The icon set overview (/icons/set), for designers and administrators: every element's icon side by side, at the size and on the
// background players see, so an icon that does not match the others stands out. Each picture is measured here in the browser
// (nothing is sent anywhere): how much ink it uses, how much of the canvas it fills, how far its drawing is off-centre, whether it
// touches the edge, and its main colour. An icon far from the rest of the set on one of these is flagged. "Far" is judged with the
// median and the median absolute deviation, so a few odd icons cannot hide each other by moving the average. Weight is compared on
// a log scale (a quarter of the usual ink is as odd as four times it), since icons range widely in how much ink they use.
const $ = (id) => document.getElementById(id);
const SIZE = 128;
const ALPHA = 32;                 // a pixel counts as ink above this opacity
const OFFSET_FLAG = 8;            // pixels off-centre (of 128) before it is flagged: under 2 px on a tile
const SPREAD = 3;                 // flagged beyond this many (scaled) median absolute deviations from the median
const MIN_SPREAD = { weight: 0.25, fill: 0.05 };
const COLOUR_FLAG = 70;           // distance in RGB from the set's usual main colour…
const COLOUR_RARE = 0.05;         // …for a main colour fewer than this share of the icons use

if (!Auth.loggedIn()) Auth.toLogin();

let elements = [];                // from /icons/elements
let previews = {};                // element id -> address of the icon previewed in the game
const measured = new Map();       // address -> measurements (or null if it could not be read)
let stats = null;                 // the set's medians and spreads
let detail = null;                // the element shown in the side panel

const canvas = document.createElement("canvas");
canvas.width = canvas.height = SIZE;
const ctx = canvas.getContext("2d", { willReadFrequently: true });

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("could not load " + src));
    img.src = src;
  });
}

/** Everything the flags are made from, for one picture. */
async function measure(src) {
  const img = await loadImage(src);
  ctx.clearRect(0, 0, SIZE, SIZE);
  ctx.drawImage(img, 0, 0, SIZE, SIZE);
  const d = ctx.getImageData(0, 0, SIZE, SIZE).data;
  let count = 0, minX = SIZE, minY = SIZE, maxX = -1, maxY = -1;
  const buckets = new Map();      // colour, 16 levels per channel -> [count, r, g, b]
  for (let y = 0; y < SIZE; y++) {
    for (let x = 0; x < SIZE; x++) {
      const i = (y * SIZE + x) * 4;
      if (d[i + 3] <= ALPHA) continue;
      count++;
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
      const key = ((d[i] >> 4) << 8) | ((d[i + 1] >> 4) << 4) | (d[i + 2] >> 4);
      const b = buckets.get(key) || [0, 0, 0, 0];
      b[0]++; b[1] += d[i]; b[2] += d[i + 1]; b[3] += d[i + 2];
      buckets.set(key, b);
    }
  }
  if (!count) return { coverage: 0, fill: 0, offset: 0, edge: false, colour: null, colours: 0, empty: true };
  const sorted = [...buckets.values()].sort((a, b) => b[0] - a[0]);
  const main = sorted[0];
  const w = maxX - minX + 1, h = maxY - minY + 1;
  return {
    coverage: count / (SIZE * SIZE),
    weight: Math.log(count / (SIZE * SIZE)),
    fill: Math.max(w, h) / SIZE,
    offset: Math.hypot((minX + maxX) / 2 - (SIZE - 1) / 2, (minY + maxY) / 2 - (SIZE - 1) / 2),
    edge: minX === 0 || minY === 0 || maxX === SIZE - 1 || maxY === SIZE - 1,
    colour: [main[1] / main[0], main[2] / main[0], main[3] / main[0]].map(Math.round),
    colours: sorted.filter((b) => b[0] / count > 0.03).length,
    box: [minX, minY, w, h],
  };
}

const median = (values) => {
  const s = [...values].sort((a, b) => a - b);
  return s.length ? (s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2) : 0;
};
const hex = (c) => "#" + c.map((v) => v.toString(16).padStart(2, "0")).join("");
const colourKey = (c) => c.map((v) => v >> 5).join(",");   // 8 levels per channel: "the same colour" for counting
const distance = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

/** Medians and spreads of the set, from every icon measured so far. */
function setStats() {
  const all = [...measured.values()].filter((m) => m && !m.empty);
  const spreadOf = (key) => {
    const values = all.map((m) => m[key]);
    const mid = median(values);
    return { median: mid, spread: Math.max(MIN_SPREAD[key], 1.4826 * median(values.map((v) => Math.abs(v - mid)))) };
  };
  const counts = new Map();
  for (const m of all) counts.set(colourKey(m.colour), (counts.get(colourKey(m.colour)) || 0) + 1);
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
  const usual = top ? all.find((m) => colourKey(m.colour) === top[0]) : null;
  return { weight: spreadOf("weight"), coverage: { median: median(all.map((m) => m.coverage)) }, fill: spreadOf("fill"), colourCounts: counts, usual: usual ? usual.colour : null,
           total: all.length };
}

function flagsOf(m) {
  if (!m || !stats) return [];
  if (m.empty) return ["empty: nothing is drawn"];
  const flags = [];
  const { weight, fill } = stats;
  if (m.weight > weight.median + SPREAD * weight.spread) flags.push("heavier than most (uses a lot more ink)");
  if (m.weight < weight.median - SPREAD * weight.spread) flags.push("lighter than most (uses a lot less ink)");
  if (m.fill > fill.median + SPREAD * fill.spread) flags.push("bigger than most on the canvas");
  if (m.fill < fill.median - SPREAD * fill.spread) flags.push("smaller than most on the canvas");
  if (m.offset > OFFSET_FLAG) flags.push(`off-centre by ${m.offset.toFixed(1)} px`);
  if (m.edge) flags.push("touches the edge of the canvas");
  if (stats.usual && distance(m.colour, stats.usual) > COLOUR_FLAG
      && (stats.colourCounts.get(colourKey(m.colour)) || 0) / stats.total < COLOUR_RARE) {
    flags.push(`main colour ${hex(m.colour)}, which few other icons use`);
  }
  return flags;
}

const srcOf = (e) => ($("is-previews").checked && previews[e.id]) || e.pic || null;

// ---------- the grid ----------
function cell(e) {
  const src = srcOf(e);
  const m = src ? measured.get(src) : undefined;
  const flags = flagsOf(m);
  const size = +$("is-size").value;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "is-cell" + (flags.length ? " flagged" : "") + (src && previews[e.id] && $("is-previews").checked ? " preview" : "")
    + (detail && detail.id === e.id ? " current" : "");
  button.title = e.label + (flags.length ? "\n• " + flags.join("\n• ") : "");
  const bg = document.createElement("span");
  bg.className = `is-cell-bg ${$("is-bg").value}`;
  if (src) {
    const img = document.createElement("img");
    img.src = src; img.alt = ""; img.width = img.height = size; img.loading = "lazy";
    bg.append(img);
  } else {
    const none = document.createElement("span");
    none.className = "is-none";
    none.style.width = none.style.height = size + "px";
    none.textContent = e.emoji || e.label.slice(0, 1);
    bg.append(none);
  }
  const name = document.createElement("span");
  name.className = "is-name";
  name.textContent = e.label;
  button.append(bg, name);
  if (flags.length) {
    const count = document.createElement("span");
    count.className = "is-flag-count";
    count.textContent = String(flags.length);
    count.setAttribute("aria-label", `${flags.length} flag${flags.length === 1 ? "" : "s"}`);
    button.append(count);
  }
  button.addEventListener("click", () => showDetail(e));
  return button;
}

function render() {
  const query = $("is-search").value.trim().toLowerCase();
  const sort = $("is-sort").value;
  const rows = elements
    .filter((e) => $("is-missing").checked || srcOf(e))
    .filter((e) => !query || e.label.toLowerCase().includes(query) || e.id.toLowerCase().includes(query))
    .map((e) => ({ e, m: srcOf(e) ? measured.get(srcOf(e)) : undefined }))
    .map((r) => ({ ...r, flags: flagsOf(r.m) }))
    .filter((r) => !$("is-flagged").checked || r.flags.length);
  const key = {
    name: () => 0,
    weight: (r) => -(r.m?.coverage ?? -1),
    fill: (r) => -(r.m?.fill ?? -1),
    offset: (r) => -(r.m?.offset ?? -1),
    colour: (r) => (r.m?.colour ? hue(r.m.colour) : 999),
    flags: (r) => -r.flags.length,
  }[sort];
  rows.sort((a, b) => key(a) - key(b) || a.e.label.localeCompare(b.e.label));
  const grid = $("is-grid");
  grid.className = `is-grid size-${$("is-size").value}`;
  grid.replaceChildren(...rows.map((r) => cell(r.e)));
  const drawn = elements.filter((e) => e.pic).length;
  const flagged = elements.filter((e) => srcOf(e) && flagsOf(measured.get(srcOf(e))).length).length;
  const pending = elements.filter((e) => srcOf(e) && !measured.has(srcOf(e))).length;
  $("is-summary").textContent = `${drawn} icons · ${elements.length - drawn} without one · ${flagged} flagged`
    + (pending ? ` · measuring, ${pending} to go…` : "") + ` · showing ${rows.length}`;
  if (detail) showDetail(detail, false);
}

function hue([r, g, b]) {
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  if (d < 12) return 400 + max / 255;   // greys and near-blacks after the colours, dark to light
  let h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return (h * 60 + 360) % 360;
}

// ---------- the detail panel ----------
function showDetail(e, scroll = true) {
  detail = e;
  const src = srcOf(e);
  const m = src ? measured.get(src) : undefined;
  $("is-detail").hidden = false;
  $("is-d-name").textContent = e.label + (src && previews[e.id] && $("is-previews").checked ? " (in preview)" : "");
  for (const id of ["is-d-128", "is-d-60", "is-d-28l", "is-d-28d"]) {
    $(id).hidden = !src;
    if (src) $(id).src = src;
  }
  const facts = $("is-d-facts");
  facts.replaceChildren();
  const fact = (term, value) => {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = term; dd.textContent = value;
    facts.append(dt, dd);
  };
  if (!src) fact("Icon", "none yet");
  else if (!m) fact("Measuring", "…");
  else if (m.empty) fact("Drawing", "nothing is drawn");
  else {
    const pct = (v) => `${Math.round(v * 100)}%`;
    fact("Weight", `${pct(m.coverage)} of the canvas is ink (most icons: ${pct(stats.coverage.median)})`);
    fact("Size", `fills ${pct(m.fill)} of the canvas (most icons: ${pct(stats.fill.median)})`);
    fact("Off-centre", `${m.offset.toFixed(1)} px`);
    fact("Main colour", `${hex(m.colour)}${stats.usual ? ` (most icons: ${hex(stats.usual)})` : ""}`);
    fact("Colours", m.colours === 1 ? "one" : String(m.colours));
  }
  const flags = flagsOf(m);
  $("is-d-flags").replaceChildren(...flags.map((f) => { const li = document.createElement("li"); li.textContent = f; return li; }));
  $("is-d-flags").hidden = flags.length === 0;
  $("is-d-open").href = `/icons?element=${encodeURIComponent(e.id)}`;
  for (const c of document.querySelectorAll(".is-cell.current")) c.classList.remove("current");
  if (scroll) $("is-detail").scrollIntoView({ block: "nearest" });
}

// ---------- measuring everything, a few at a time ----------
let renderTimer = 0;
function renderSoon() {
  if (!renderTimer) renderTimer = setTimeout(() => { renderTimer = 0; stats = setStats(); render(); }, 250);
}
async function measureAll() {
  const queue = [...new Set(elements.map(srcOf).filter((s) => s && !measured.has(s)))];
  const worker = async () => {
    while (queue.length) {
      const src = queue.shift();
      try { measured.set(src, await measure(src)); } catch (_) { measured.set(src, null); }
      renderSoon();
    }
  };
  await Promise.all(Array.from({ length: 8 }, worker));
  stats = setStats();
  render();
}

async function start() {
  try {
    elements = (await Auth.fetch("/icons/elements")).elements;
  } catch (err) {
    $("is-error").textContent = err.status === 403 ? "The icon set is for designers and administrators." : err.message;
    return;
  }
  try { previews = (await Auth.fetch("/icons/previews")).previews || {}; } catch (_) { previews = {}; }
  elements.sort((a, b) => a.label.localeCompare(b.label));
  render();
  await measureAll();
}

for (const id of ["is-size", "is-bg", "is-sort", "is-flagged", "is-missing"]) $(id).addEventListener("change", render);
$("is-previews").addEventListener("change", () => { render(); measureAll(); });
$("is-search").addEventListener("input", render);
$("is-d-close").addEventListener("click", () => { detail = null; $("is-detail").hidden = true; });
start();
