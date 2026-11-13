"use strict";
// The icon studio (/icons), for designers and administrators: draw an element's icon on the 128 × 128 canvas the game uses,
// with shape layers (rectangles, ellipses, lines, polygons, stars and freehand curves, each still editable) and pixel layers
// (brush, eraser, fill), in any colours. A cut-out shape cuts a hole in its own layer. Every change is saved in this browser
// (localStorage, one design per element) and can be undone; "Download PNG" gives the 128 × 128 file the game reads,
// named for static/elements/. An element that already has a picture can be edited (as pixels) or traced over.
// Step 2 will save designs on the server and send them to the administrators as proposals.

const $ = (id) => document.getElementById(id);
const SIZE = 128;                 // the canvas every element picture uses
const VIEW = 512;                 // the stage canvas: four screen pixels per icon pixel
const SCALE = VIEW / SIZE;
const GRID = 8;
const HOUSE_NAVY = "#2f3a4a";     // the colour most of today's icons are drawn in
const SUGGESTED = [HOUSE_NAVY, "#ffffff", "#000000", "#d9a400", "#c0392b", "#2e86de", "#27ae60", "#8e44ad", "#f4f1e8"];
const STORE_PREFIX = "alchemy-icon:";
const MAX_HISTORY = 80;

if (!Auth.loggedIn()) Auth.toLogin();

// ---------- state ----------
let elements = [];               // from /icons/elements: {id, label, emoji, file, pic?}
let current = null;              // the element being drawn
let doc = null;                  // {version, layers: [{id, name, kind: "shapes"|"pixels", visible, opacity, shapes?, pixels?}], reference}
let activeLayer = null;          // id
let selected = null;             // {layer, index} of the selected shape
let tool = "rect";
let cutMode = false;
let showGrid = true;
let undoStack = [];
let redoStack = [];
let recent = [];
const pixelCanvases = new Map(); // pixel layer id -> its 128 × 128 canvas (the doc keeps a PNG data URL of it)
let referenceImage = null;       // the picture being traced over (not exported)

const view = $("ic-view");
const vctx = view.getContext("2d");
const out = document.createElement("canvas");   // the finished icon, as exported
out.width = out.height = SIZE;
const octx = out.getContext("2d");
const scratch = document.createElement("canvas");  // one layer at a time, so cut-outs cut only their own layer
scratch.width = scratch.height = SIZE;
const sctx = scratch.getContext("2d");
const hitCtx = document.createElement("canvas").getContext("2d");

const newId = () => Math.random().toString(36).slice(2, 10);
const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
const layerById = (id) => doc.layers.find((l) => l.id === id);

function showError(message) { $("ic-error").textContent = message || ""; }

// ---------- colours ----------
const hex2 = (n) => clamp(Math.round(n), 0, 255).toString(16).padStart(2, "0");
function withAlpha(hex, percent) { return hex + hex2((percent / 100) * 255); }
function splitColour(colour) {   // "#rrggbbaa" -> ["#rrggbb", percent]
  if (!colour) return [null, 100];
  return [colour.slice(0, 7), colour.length === 9 ? Math.round((parseInt(colour.slice(7), 16) / 255) * 100) : 100];
}
const fillColour = () => (fillNone ? null : withAlpha($("ic-fill").value, +$("ic-fill-alpha").value));
const strokeColour = () => withAlpha($("ic-stroke").value, +$("ic-stroke-alpha").value);
let fillNone = false;

function useColour(colour) {
  recent = [colour, ...recent.filter((c) => c !== colour)].slice(0, 8);
  renderSwatches();
}
function renderSwatches() {
  const box = $("ic-swatches");
  box.replaceChildren();
  for (const colour of [...SUGGESTED, ...recent.filter((c) => !SUGGESTED.includes(c.slice(0, 7)))]) {
    const b = document.createElement("button");
    b.type = "button";
    b.title = colour + (colour === HOUSE_NAVY ? " (the colour most icons use)" : "");
    b.style.background = colour;
    b.addEventListener("click", () => {
      const [hex, alpha] = splitColour(colour);
      $("ic-fill").value = hex;
      $("ic-fill-alpha").value = String(alpha);
      fillNone = false;
      syncColourLabels();
      applyToSelected((s) => { s.fill = fillColour(); });
    });
    box.append(b);
  }
}
function syncColourLabels() {
  $("ic-fill-alpha-n").textContent = fillNone ? "none" : $("ic-fill-alpha").value + "%";
  $("ic-stroke-alpha-n").textContent = $("ic-stroke-alpha").value + "%";
  $("ic-stroke-width-n").textContent = $("ic-stroke-width").value + "px";
  $("ic-brush-n").textContent = $("ic-brush").value + "px";
}

// ---------- shapes: geometry ----------
/** Trace a shape's outline on ctx (which is left transformed: call inside save/restore). Coordinates are icon pixels. */
function tracePath(ctx, s) {
  const cx = s.x + s.w / 2, cy = s.y + s.h / 2;
  ctx.translate(cx, cy);
  if (s.type !== "line") {
    ctx.rotate(((s.rotation || 0) * Math.PI) / 180);
    ctx.scale(s.flipX ? -1 : 1, s.flipY ? -1 : 1);
  }
  const w = s.w, h = s.h, hw = Math.abs(w) / 2, hh = Math.abs(h) / 2;
  ctx.beginPath();
  if (s.type === "rect" || s.type === "rounded") {
    const r = Math.min(s.radius || 0, hw, hh);
    if (ctx.roundRect) ctx.roundRect(-hw, -hh, hw * 2, hh * 2, r); else ctx.rect(-hw, -hh, hw * 2, hh * 2);
  } else if (s.type === "ellipse") {
    ctx.ellipse(0, 0, Math.max(hw, 0.1), Math.max(hh, 0.1), 0, 0, Math.PI * 2);
  } else if (s.type === "line") {
    ctx.moveTo(-w / 2, -h / 2);
    ctx.lineTo(w / 2, h / 2);
  } else if (s.type === "polygon" || s.type === "star") {
    const n = s.sides || 5, star = s.type === "star", count = star ? n * 2 : n;
    for (let i = 0; i < count; i++) {
      const angle = -Math.PI / 2 + (i * Math.PI * 2) / count;
      const k = star && i % 2 ? (s.inner || 45) / 100 : 1;
      const px = Math.cos(angle) * hw * k, py = Math.sin(angle) * hh * k;
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.closePath();
  } else if (s.type === "pen") {
    (s.points || []).forEach(([u, v], i) => {
      const px = -hw + u * hw * 2, py = -hh + v * hh * 2;
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    if (s.closed) ctx.closePath();
  }
}
const filled = (s) => s.type !== "line" && (s.type !== "pen" || s.closed);
const isOpenLine = (s) => s.type === "line" || (s.type === "pen" && !s.closed);   // drawn with its line only, in the fill colour

function drawShape(ctx, s) {
  ctx.save();
  tracePath(ctx, s);
  ctx.globalCompositeOperation = s.cut ? "destination-out" : "source-over";
  if (filled(s) && s.fill) {
    ctx.fillStyle = s.cut ? "#000" : s.fill;
    ctx.fill();
  }
  const lineColour = s.type === "line" || (s.type === "pen" && !s.closed) ? (s.stroke || s.fill || HOUSE_NAVY) : s.stroke;
  const width = s.type === "line" || (s.type === "pen" && !s.closed) ? Math.max(1, s.strokeWidth || 0) : s.strokeWidth || 0;
  if (lineColour && width > 0) {
    ctx.lineWidth = width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = s.cut ? "#000" : lineColour;
    ctx.stroke();
  }
  ctx.restore();
}

function hitShape(s, x, y) {
  hitCtx.save();
  hitCtx.setTransform(1, 0, 0, 1, 0, 0);
  tracePath(hitCtx, s);
  hitCtx.lineWidth = Math.max(6, (s.strokeWidth || 0) + 4);
  const hit = (filled(s) && hitCtx.isPointInPath(x, y)) || hitCtx.isPointInStroke(x, y);
  hitCtx.restore();
  return hit;
}

/** The four corners of a shape's box on the icon, after its turn (for the selection outline and its handles). */
function corners(s) {
  if (s.type === "line") return [[s.x, s.y], [s.x + s.w, s.y + s.h]];
  const cx = s.x + s.w / 2, cy = s.y + s.h / 2, a = ((s.rotation || 0) * Math.PI) / 180, c = Math.cos(a), sn = Math.sin(a);
  return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([dx, dy]) => {
    const lx = (dx * s.w) / 2, ly = (dy * s.h) / 2;
    return [cx + lx * c - ly * sn, cy + lx * sn + ly * c];
  });
}

// ---------- rendering ----------
function renderLayer(ctx, layer) {
  if (layer.kind === "pixels") {
    const canvas = pixelCanvases.get(layer.id);
    if (canvas) ctx.drawImage(canvas, 0, 0);
    return;
  }
  for (const s of layer.shapes) drawShape(ctx, s);
}

/** The finished icon, layer by layer, into `out` (and the extra shape being drawn, if any, into its layer). */
function composite(extra) {
  octx.clearRect(0, 0, SIZE, SIZE);
  for (const layer of doc.layers) {
    if (!layer.visible) continue;
    sctx.clearRect(0, 0, SIZE, SIZE);
    renderLayer(sctx, layer);
    if (extra && extra.layer === layer.id) drawShape(sctx, extra.shape);
    octx.globalAlpha = (layer.opacity ?? 100) / 100;
    octx.drawImage(scratch, 0, 0);
    octx.globalAlpha = 1;
  }
}

function drawChecker() {
  const cell = 16;
  for (let y = 0; y < VIEW; y += cell) for (let x = 0; x < VIEW; x += cell) {
    vctx.fillStyle = ((x + y) / cell) % 2 ? "#e4e4e4" : "#ffffff";
    vctx.fillRect(x, y, cell, cell);
  }
}

function render(extra) {
  if (!doc) return;
  composite(extra);
  vctx.imageSmoothingEnabled = false;
  drawChecker();
  if (referenceImage && doc.reference && doc.reference.visible !== false) {
    vctx.globalAlpha = (doc.reference.opacity ?? 35) / 100;
    vctx.imageSmoothingEnabled = true;
    vctx.drawImage(referenceImage, 0, 0, VIEW, VIEW);
    vctx.imageSmoothingEnabled = false;
    vctx.globalAlpha = 1;
  }
  vctx.drawImage(out, 0, 0, VIEW, VIEW);   // pixelated: these are the pixels the PNG will have
  if (showGrid) {
    vctx.strokeStyle = "rgba(80, 120, 200, 0.25)";
    vctx.lineWidth = 1;
    vctx.beginPath();
    for (let i = GRID; i < SIZE; i += GRID) {
      vctx.moveTo(i * SCALE + 0.5, 0); vctx.lineTo(i * SCALE + 0.5, VIEW);
      vctx.moveTo(0, i * SCALE + 0.5); vctx.lineTo(VIEW, i * SCALE + 0.5);
    }
    vctx.stroke();
    vctx.strokeStyle = "rgba(80, 120, 200, 0.45)";   // the centre lines
    vctx.beginPath();
    vctx.moveTo(VIEW / 2 + 0.5, 0); vctx.lineTo(VIEW / 2 + 0.5, VIEW);
    vctx.moveTo(0, VIEW / 2 + 0.5); vctx.lineTo(VIEW, VIEW / 2 + 0.5);
    vctx.stroke();
  }
  const s = selectedShape();
  if (s) {
    const pts = corners(s).map(([x, y]) => [x * SCALE, y * SCALE]);
    vctx.strokeStyle = "#ff5ca8";
    vctx.lineWidth = 2;
    vctx.setLineDash([6, 4]);
    vctx.beginPath();
    pts.forEach(([x, y], i) => (i ? vctx.lineTo(x, y) : vctx.moveTo(x, y)));
    if (pts.length > 2) vctx.closePath();
    vctx.stroke();
    vctx.setLineDash([]);
    for (const [x, y] of pts) {
      vctx.fillStyle = "#ffffff";
      vctx.fillRect(x - 6, y - 6, 12, 12);
      vctx.strokeRect(x - 6, y - 6, 12, 12);
    }
  }
  renderPreviews();
}

function renderPreviews() {
  for (const [id, size] of [["pv-full", 128], ["pv-list", 60], ["pv-tile-light", 28], ["pv-tile-dark", 28]]) {
    const c = $(id).getContext("2d");
    c.clearRect(0, 0, size, size);
    c.imageSmoothingEnabled = true;
    c.imageSmoothingQuality = "high";
    c.drawImage(out, 0, 0, size, size);   // as the browser shrinks the PNG in the game
  }
}

// ---------- selection ----------
function selectedShape() {
  if (!selected) return null;
  const layer = layerById(selected.layer);
  return layer && layer.kind === "shapes" ? layer.shapes[selected.index] || null : null;
}
function select(layerId, index) {
  selected = index === null ? null : { layer: layerId, index };
  if (selected) activeLayer = layerId;
  showProps();
  renderLayers();
  render();
}
function showProps() {
  const s = selectedShape();
  $("ic-props").hidden = !s;
  if (!s) return;
  for (const row of $("ic-props").querySelectorAll("[data-for]")) row.hidden = !row.dataset.for.split(" ").includes(s.type);
  $("ic-rotation").value = String(Math.round(s.rotation || 0));
  $("ic-radius").value = String(s.radius || 0);
  $("ic-sides").value = String(s.sides || 5);
  $("ic-inner").value = String(s.inner || 45);
  $("ic-closed").checked = !!s.closed;
  $("ic-shape-cut").checked = !!s.cut;
  $("ic-rotation").disabled = s.type === "line";
  syncPropLabels();
  if (s.fill) { const [hex, a] = splitColour(s.fill); $("ic-fill").value = hex; $("ic-fill-alpha").value = String(a); fillNone = false; }
  else if (s.type !== "line") fillNone = true;
  if (s.stroke) { const [hex, a] = splitColour(s.stroke); $("ic-stroke").value = hex; $("ic-stroke-alpha").value = String(a); }
  $("ic-stroke-width").value = String(s.strokeWidth || 0);
  syncColourLabels();
}
function syncPropLabels() {
  $("ic-rotation-n").textContent = $("ic-rotation").value + "°";
  $("ic-radius-n").textContent = $("ic-radius").value;
  $("ic-sides-n").textContent = $("ic-sides").value;
  $("ic-inner-n").textContent = $("ic-inner").value + "%";
}
/** Change the selected shape (a slider being dragged changes it live; the change is committed when it is let go). */
function applyToSelected(change, commitNow = true) {
  const s = selectedShape();
  if (!s) return;
  change(s);
  render();
  if (commitNow) commit();
}

// ---------- layers ----------
function addLayer(kind, above = true) {
  const layer = { id: newId(), name: kind === "pixels" ? "Pixels" : "Shapes", kind, visible: true, opacity: 100 };
  if (kind === "shapes") layer.shapes = [];
  else { layer.pixels = null; pixelCanvases.set(layer.id, blankCanvas()); }
  const at = above ? doc.layers.findIndex((l) => l.id === activeLayer) + 1 : doc.layers.length;
  doc.layers.splice(at <= 0 ? doc.layers.length : at, 0, layer);
  activeLayer = layer.id;
  selected = null;
  return layer;
}
function blankCanvas() {
  const c = document.createElement("canvas");
  c.width = c.height = SIZE;
  return c;
}
/** The layer a tool draws on: the active one if it is the right kind, or a new one of that kind above it. */
function layerFor(kind) {
  const layer = layerById(activeLayer);
  if (layer && layer.kind === kind) return layer;
  return addLayer(kind);
}
function renderLayers() {
  const list = $("ic-layers");
  list.replaceChildren();
  for (const layer of [...doc.layers].reverse()) {   // top layer first, as in drawing programs
    const li = document.createElement("li");
    li.setAttribute("aria-current", String(layer.id === activeLayer));
    const eye = miniButton(layer.visible ? "👁" : "–", layer.visible ? "Hide" : "Show", () => { layer.visible = !layer.visible; commit(); renderLayers(); });
    const name = document.createElement("button");
    name.type = "button";
    name.className = "ic-layer-name";
    name.textContent = layer.name;
    name.title = "Use this layer (double-click to rename)";
    name.addEventListener("click", () => { activeLayer = layer.id; selected = null; showProps(); renderLayers(); render(); });
    name.addEventListener("dblclick", () => {
      const next = prompt("Layer name", layer.name);
      if (next && next.trim()) { layer.name = next.trim().slice(0, 30); commit(); renderLayers(); }
    });
    const kind = document.createElement("span");
    kind.className = "ic-layer-kind";
    kind.textContent = layer.kind === "pixels" ? "pixels" : `${layer.shapes.length} shape${layer.shapes.length === 1 ? "" : "s"}`;
    const opacity = document.createElement("input");
    opacity.type = "range"; opacity.min = "0"; opacity.max = "100"; opacity.value = String(layer.opacity ?? 100);
    opacity.title = "Layer opacity";
    opacity.addEventListener("input", () => { layer.opacity = +opacity.value; render(); });
    opacity.addEventListener("change", commit);
    const i = doc.layers.indexOf(layer);
    li.append(eye, name, kind, opacity,
      miniButton("↑", "Move up", () => moveLayer(i, i + 1), i === doc.layers.length - 1),
      miniButton("↓", "Move down", () => moveLayer(i, i - 1), i === 0),
      miniButton("✕", "Delete layer", () => deleteLayer(layer), doc.layers.length === 1));
    list.append(li);
  }
  $("ic-reference").hidden = !doc.reference;
  if (doc.reference) $("ic-ref-opacity").value = String(doc.reference.opacity ?? 35);
}
function miniButton(text, title, action, disabled = false) {
  const b = document.createElement("button");
  b.type = "button"; b.className = "ic-mini"; b.textContent = text; b.title = title; b.disabled = disabled;
  b.setAttribute("aria-label", title);
  b.addEventListener("click", action);
  return b;
}
function moveLayer(from, to) {
  if (to < 0 || to >= doc.layers.length) return;
  const [layer] = doc.layers.splice(from, 1);
  doc.layers.splice(to, 0, layer);
  commit(); renderLayers();
}
function deleteLayer(layer) {
  if (doc.layers.length === 1) return;
  doc.layers = doc.layers.filter((l) => l !== layer);
  pixelCanvases.delete(layer.id);
  if (activeLayer === layer.id) activeLayer = doc.layers[doc.layers.length - 1].id;
  selected = null;
  showProps(); commit(); renderLayers();
}

// ---------- saving, undo ----------
/** The design as plain data: pixel layers as PNG data URLs. */
function serialize() {
  for (const layer of doc.layers) if (layer.kind === "pixels") layer.pixels = pixelCanvases.get(layer.id).toDataURL("image/png");
  return JSON.stringify(doc);
}
let saveTimer = 0;
function commit() {
  const snapshot = serialize();
  if (undoStack[undoStack.length - 1] !== snapshot) {
    undoStack.push(snapshot);
    if (undoStack.length > MAX_HISTORY) undoStack.shift();
    redoStack = [];
  }
  syncUndoButtons();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => save(snapshot), 300);
  render();
}
function save(snapshot) {
  if (!current) return;
  try {
    localStorage.setItem(STORE_PREFIX + current.id, snapshot);
    $("ic-saved").textContent = "Saved in this browser";
    markDraft(current.id);
  } catch (_) {
    $("ic-saved").textContent = "Could not save (this browser's storage is full): download the PNG to keep it";
  }
}
function syncUndoButtons() {
  $("ic-undo").disabled = undoStack.length <= 1;
  $("ic-redo").disabled = redoStack.length === 0;
}
async function restoreSnapshot(snapshot) {
  await loadDoc(JSON.parse(snapshot));
  save(snapshot);
  syncUndoButtons();
}
async function undo() {
  if (undoStack.length <= 1) return;
  redoStack.push(undoStack.pop());
  await restoreSnapshot(undoStack[undoStack.length - 1]);
}
async function redo() {
  if (!redoStack.length) return;
  const snapshot = redoStack.pop();
  undoStack.push(snapshot);
  await restoreSnapshot(snapshot);
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("could not load a picture"));
    img.src = src;
  });
}
/** Make `data` the design on the stage: its pixel layers are drawn from their data URLs first. */
async function loadDoc(data) {
  pixelCanvases.clear();
  for (const layer of data.layers) {
    if (layer.kind !== "pixels") continue;
    const canvas = blankCanvas();
    if (layer.pixels) {
      try { canvas.getContext("2d").drawImage(await loadImage(layer.pixels), 0, 0, SIZE, SIZE); } catch (_) { /* an empty layer */ }
    }
    pixelCanvases.set(layer.id, canvas);
  }
  doc = data;
  if (!layerById(activeLayer)) activeLayer = doc.layers.length ? doc.layers[doc.layers.length - 1].id : null;
  if (selected && !selectedShape()) selected = null;
  referenceImage = null;
  if (doc.reference) {
    const el = elements.find((e) => e.id === doc.reference.element);   // picture addresses change when the server restarts
    if (el && el.pic) { try { referenceImage = await loadImage(el.pic); } catch (_) { referenceImage = null; } }
  }
  showProps();
  renderLayers();
  render();
}

// ---------- choosing an element ----------
async function openElement(el) {
  current = el;
  showError("");
  $("ic-current").textContent = `${el.emoji || ""} ${el.label}`.trim();
  $("ic-download").disabled = false;
  $("ic-empty").hidden = true;
  $("ic-editor").hidden = false;
  $("ic-side").hidden = false;
  for (const span of document.querySelectorAll(".pv span")) span.textContent = el.label;
  try { localStorage.setItem(STORE_PREFIX + "last", el.id); } catch (_) { /* not remembered */ }
  renderElementList();
  undoStack = []; redoStack = [];
  let saved = null;
  try { saved = localStorage.getItem(STORE_PREFIX + el.id); } catch (_) { saved = null; }
  if (saved) {
    try { await loadDoc(JSON.parse(saved)); } catch (_) { saved = null; }
  }
  if (!saved) {
    let start = "blank";
    if (el.pic) start = await askHowToStart(el);
    await loadDoc({ version: 1, layers: [], reference: null });
    if (start === "edit") {
      const layer = addLayer("pixels", false);
      layer.name = "Current picture";
      try { pixelCanvases.get(layer.id).getContext("2d").drawImage(await loadImage(el.pic), 0, 0, SIZE, SIZE); } catch (e) { showError(e.message); }
    }
    if (start === "trace") doc.reference = { element: el.id, opacity: 35, visible: true };
    addLayer("shapes", true);
    await loadDoc(JSON.parse(serialize()));   // serialize first: it writes the pixel layers out, so the picture is kept
    $("ic-saved").textContent = "";
  }
  undoStack = [serialize()];
  syncUndoButtons();
  setHint();
}
function askHowToStart(el) {
  return new Promise((resolve) => {
    const dialog = $("ic-start");
    $("ic-start-pic").src = el.pic;
    $("ic-start-title").textContent = `${el.label} already has an icon`;
    dialog.returnValue = "";
    dialog.addEventListener("close", () => resolve(dialog.returnValue || "blank"), { once: true });
    dialog.showModal();
  });
}
function hasDraft(id) {
  try { return localStorage.getItem(STORE_PREFIX + id) !== null; } catch (_) { return false; }
}
function markDraft(id) {
  const button = document.querySelector(`#ic-element-list button[data-id="${CSS.escape(id)}"]`);
  if (button && !button.querySelector(".ic-draft")) button.append(badge("draft", "ic-draft"));
}
function badge(text, extra = "") {
  const b = document.createElement("span");
  b.className = "ic-badge " + extra;
  b.textContent = text;
  return b;
}
function renderElementList() {
  const query = $("ic-search").value.trim().toLowerCase();
  const missingOnly = $("ic-missing-only").checked;
  const list = $("ic-element-list");
  list.replaceChildren();
  const shown = elements
    .filter((e) => (!missingOnly || !e.pic) && (!query || e.label.toLowerCase().includes(query) || e.id.toLowerCase().includes(query)))
    .sort((a, b) => (!!a.pic - !!b.pic) || a.label.localeCompare(b.label));
  for (const e of shown.slice(0, 400)) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.id = e.id;
    b.setAttribute("aria-current", String(current && current.id === e.id));
    const thumb = document.createElement("span");
    thumb.className = "ic-thumb";
    if (e.pic) {
      const img = document.createElement("img");
      img.src = e.pic; img.alt = ""; img.loading = "lazy";
      thumb.append(img);
    } else thumb.textContent = e.emoji || e.label.slice(0, 1);
    const label = document.createElement("span");
    label.className = "ic-label";
    label.textContent = e.label;
    b.append(thumb, label);
    if (!e.pic) b.append(badge("no icon"));
    if (hasDraft(e.id)) b.append(badge("draft", "ic-draft"));
    b.addEventListener("click", () => openElement(e));
    li.append(b);
    list.append(li);
  }
  const missing = elements.filter((e) => !e.pic).length;
  $("ic-counts").textContent = `${missing} of ${elements.length} elements have no icon` + (shown.length > 400 ? " · showing the first 400" : "");
}

// ---------- tools ----------
const SHAPE_TOOLS = ["rect", "rounded", "ellipse", "line", "polygon", "star", "pen"];
const PIXEL_TOOLS = ["brush", "eraser", "bucket"];
const HINTS = {
  select: "Click a shape to select it; drag to move, drag a corner to resize. Arrows nudge (Shift: 8 px).",
  rect: "Drag to draw. Shift keeps it square.", rounded: "Drag to draw. Shift keeps it square; set the corners on the right.",
  ellipse: "Drag to draw. Shift makes a circle.", line: "Drag to draw a line. Shift keeps it straight or at 45°.",
  polygon: "Drag to draw. Set the number of sides on the right.", star: "Drag to draw. Set the points and the inner size on the right.",
  pen: "Drag to draw a freehand curve. Tick \"Closed and filled\" to fill it.",
  brush: "Paint pixels in the fill colour.", eraser: "Erase pixels on this layer.", bucket: "Fill an area of one colour on this layer.",
  picker: "Click the icon to take a colour from it.",
};
function setTool(name) {
  tool = name;
  for (const b of document.querySelectorAll("[data-tool]")) b.setAttribute("aria-pressed", String(b.dataset.tool === name));
  setHint();
}
function setHint() { $("ic-hint").textContent = (cutMode && SHAPE_TOOLS.includes(tool) ? "Cut-out: " : "") + (HINTS[tool] || ""); }

function iconPoint(event) {
  const r = view.getBoundingClientRect();
  return [((event.clientX - r.left) / r.width) * SIZE, ((event.clientY - r.top) / r.height) * SIZE];
}
const snap = (n) => Math.round(n);

let action = null;   // what the pointer is doing: {kind: "draw"|"move"|"resize"|"paint", …}

function newShape(type, x, y) {
  const s = {
    type, x, y, w: 0, h: 0, rotation: 0, fill: fillColour(), stroke: null,
    strokeWidth: +$("ic-stroke-width").value, cut: cutMode,
  };
  if (s.strokeWidth > 0 || type === "line" || type === "pen") s.stroke = strokeColour();
  if (type === "line" || type === "pen") { s.stroke = fillNone ? strokeColour() : fillColour(); s.strokeWidth = Math.max(2, s.strokeWidth); }
  if (type === "rounded") s.radius = 12;
  if (type === "polygon") s.sides = 6;
  if (type === "star") { s.sides = 5; s.inner = 45; }
  if (type === "pen") { s.points = []; s.closed = false; s.raw = [[x, y]]; }
  return s;
}

/** Turn the pen's screen points into a shape: its box, and each point as a fraction of the box (so resizing scales it). */
function finishPen(s) {
  const raw = s.raw;
  delete s.raw;
  const xs = raw.map((p) => p[0]), ys = raw.map((p) => p[1]);
  const minX = Math.min(...xs), minY = Math.min(...ys), maxX = Math.max(...xs), maxY = Math.max(...ys);
  s.x = minX; s.y = minY; s.w = Math.max(1, maxX - minX); s.h = Math.max(1, maxY - minY);
  s.points = raw.map(([px, py]) => [(px - minX) / s.w, (py - minY) / s.h]);
}

function onDown(event) {
  if (!doc || event.button !== 0) return;
  event.preventDefault();
  view.setPointerCapture(event.pointerId);
  const [x, y] = iconPoint(event);
  if (tool === "picker") {
    const [r, g, b, a] = octx.getImageData(clamp(Math.floor(x), 0, SIZE - 1), clamp(Math.floor(y), 0, SIZE - 1), 1, 1).data;
    if (a === 0) return;
    $("ic-fill").value = "#" + hex2(r) + hex2(g) + hex2(b);
    $("ic-fill-alpha").value = String(Math.round((a / 255) * 100));
    fillNone = false;
    syncColourLabels();
    return;
  }
  if (PIXEL_TOOLS.includes(tool)) {
    const layer = layerFor("pixels");
    renderLayers();
    if (tool === "bucket") { floodFill(layer, Math.floor(x), Math.floor(y)); useColour(fillColour() || HOUSE_NAVY); commit(); return; }
    action = { kind: "paint", layer, last: [Math.floor(x), Math.floor(y)] };
    stamp(layer, Math.floor(x), Math.floor(y));
    render();
    return;
  }
  if (tool === "select") {
    const s = selectedShape();
    if (s) {   // a corner handle of the selected shape?
      const handle = corners(s).findIndex(([hx, hy]) => Math.hypot(hx - x, hy - y) * SCALE < 10);
      if (handle >= 0) { action = { kind: "resize", handle, start: JSON.stringify(s) }; return; }
    }
    for (let li = doc.layers.length - 1; li >= 0; li--) {   // the top-most shape under the pointer
      const layer = doc.layers[li];
      if (layer.kind !== "shapes" || !layer.visible) continue;
      for (let i = layer.shapes.length - 1; i >= 0; i--) {
        if (hitShape(layer.shapes[i], x, y)) {
          select(layer.id, i);
          action = { kind: "move", from: [x, y], start: { x: layer.shapes[i].x, y: layer.shapes[i].y } };
          view.classList.add("ic-moving");
          return;
        }
      }
    }
    select(null, null);
    return;
  }
  if (SHAPE_TOOLS.includes(tool)) {
    const layer = layerFor("shapes");
    renderLayers();
    action = { kind: "draw", layer: layer.id, shape: newShape(tool, snap(x), snap(y)), from: [snap(x), snap(y)] };
  }
}

function onMove(event) {
  if (!action) return;
  const [x, y] = iconPoint(event);
  if (action.kind === "paint") {
    const [lx, ly] = action.last, nx = Math.floor(x), ny = Math.floor(y);
    const steps = Math.max(Math.abs(nx - lx), Math.abs(ny - ly), 1);
    for (let i = 1; i <= steps; i++) stamp(action.layer, Math.round(lx + ((nx - lx) * i) / steps), Math.round(ly + ((ny - ly) * i) / steps));
    action.last = [nx, ny];
    render();
    return;
  }
  if (action.kind === "draw") {
    const s = action.shape, [fx, fy] = action.from;
    if (s.type === "pen") { s.raw.push([x, y]); penPreview(s); render({ layer: action.layer, shape: action.preview }); return; }
    let w = snap(x) - fx, h = snap(y) - fy;
    if (event.shiftKey) {
      if (s.type === "line") {   // straight or at 45°
        const angle = Math.round(Math.atan2(h, w) / (Math.PI / 4)) * (Math.PI / 4), len = Math.hypot(w, h);
        w = Math.round(Math.cos(angle) * len); h = Math.round(Math.sin(angle) * len);
      } else { const m = Math.max(Math.abs(w), Math.abs(h)); w = Math.sign(w || 1) * m; h = Math.sign(h || 1) * m; }
    }
    if (s.type === "line") { s.x = fx; s.y = fy; s.w = w; s.h = h; }
    else { s.x = Math.min(fx, fx + w); s.y = Math.min(fy, fy + h); s.w = Math.abs(w); s.h = Math.abs(h); }
    render({ layer: action.layer, shape: s });
    return;
  }
  const s = selectedShape();
  if (!s) return;
  if (action.kind === "move") {
    s.x = action.start.x + snap(x - action.from[0]);
    s.y = action.start.y + snap(y - action.from[1]);
    render();
  } else if (action.kind === "resize") {
    resizeTo(s, JSON.parse(action.start), action.handle, x, y, event.shiftKey);
    render();
  }
}

function penPreview(s) {   // a finished-looking copy of the curve being drawn
  const copy = { ...s, raw: s.raw.slice() };
  finishPen(copy);
  action.preview = copy;
}

/** Resize from a corner: the opposite corner stays where it is, measured in the shape's own (turned) frame. */
function resizeTo(s, start, handle, x, y, square) {
  if (s.type === "line") {
    if (handle === 0) { s.x = snap(x); s.y = snap(y); s.w = start.x + start.w - s.x; s.h = start.y + start.h - s.y; }
    else { s.w = snap(x) - start.x; s.h = snap(y) - start.y; }
    return;
  }
  const pts = corners(start), anchor = pts[(handle + 2) % 4];
  const a = ((start.rotation || 0) * Math.PI) / 180, c = Math.cos(a), sn = Math.sin(a);
  const dx = x - anchor[0], dy = y - anchor[1];
  let lw = dx * c + dy * sn, lh = -dx * sn + dy * c;   // the pointer, in the shape's frame, from the fixed corner
  if (square) { const m = Math.max(Math.abs(lw), Math.abs(lh)); lw = Math.sign(lw || 1) * m; lh = Math.sign(lh || 1) * m; }
  const w = Math.max(1, Math.abs(lw)), h = Math.max(1, Math.abs(lh));
  const mx = anchor[0] + (lw / 2) * c - (lh / 2) * sn, my = anchor[1] + (lw / 2) * sn + (lh / 2) * c;   // the new centre
  s.w = Math.round(w); s.h = Math.round(h);
  s.x = Math.round(mx - s.w / 2); s.y = Math.round(my - s.h / 2);
}

function onUp() {
  if (!action) return;
  const a = action;
  action = null;
  view.classList.remove("ic-moving");
  if (a.kind === "draw") {
    const s = a.shape;
    if (s.type === "pen") { if (s.raw.length < 2) return render(); finishPen(s); }
    else if (s.type === "line" ? Math.hypot(s.w, s.h) < 2 : s.w < 2 || s.h < 2) return render();   // a click, not a shape
    const layer = layerById(a.layer);
    layer.shapes.push(s);
    useColour(s.fill || s.stroke || HOUSE_NAVY);
    select(layer.id, layer.shapes.length - 1);
  }
  commit();
}

function stamp(layer, x, y) {
  const ctx = pixelCanvases.get(layer.id).getContext("2d");
  const size = +$("ic-brush").value, half = Math.floor(size / 2);
  if (tool === "eraser") ctx.clearRect(x - half, y - half, size, size);
  else { ctx.fillStyle = fillColour() || HOUSE_NAVY; ctx.fillRect(x - half, y - half, size, size); }
}

function floodFill(layer, x, y) {
  if (x < 0 || y < 0 || x >= SIZE || y >= SIZE) return;
  const ctx = pixelCanvases.get(layer.id).getContext("2d");
  const img = ctx.getImageData(0, 0, SIZE, SIZE), d = img.data;
  const at = (px, py) => (py * SIZE + px) * 4;
  const target = d.slice(at(x, y), at(x, y) + 4);
  const [hex, alpha] = splitColour(fillColour() || HOUSE_NAVY);
  const fill = [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16), Math.round((alpha / 100) * 255)];
  if (fill.every((v, i) => v === target[i])) return;
  const same = (i) => d[i] === target[0] && d[i + 1] === target[1] && d[i + 2] === target[2] && d[i + 3] === target[3];
  const stack = [[x, y]];
  while (stack.length) {
    const [px, py] = stack.pop();
    if (px < 0 || py < 0 || px >= SIZE || py >= SIZE) continue;
    const i = at(px, py);
    if (!same(i)) continue;
    d[i] = fill[0]; d[i + 1] = fill[1]; d[i + 2] = fill[2]; d[i + 3] = fill[3];
    stack.push([px + 1, py], [px - 1, py], [px, py + 1], [px, py - 1]);
  }
  ctx.putImageData(img, 0, 0);
}

// ---------- arranging the selected shape ----------
function moveShape(delta) {
  if (!selected) return;
  const layer = layerById(selected.layer), i = selected.index, j = i + delta;
  if (j < 0 || j >= layer.shapes.length) return;
  [layer.shapes[i], layer.shapes[j]] = [layer.shapes[j], layer.shapes[i]];
  selected.index = j;
  commit(); renderLayers();
}
function duplicateShape() {
  const s = selectedShape();
  if (!s) return;
  const layer = layerById(selected.layer);
  const copy = JSON.parse(JSON.stringify(s));
  copy.x += 4; copy.y += 4;
  layer.shapes.push(copy);
  select(layer.id, layer.shapes.length - 1);
  commit();
}
function deleteShape() {
  if (!selectedShape()) return;
  layerById(selected.layer).shapes.splice(selected.index, 1);
  select(null, null);
  commit(); renderLayers();
}

// ---------- download ----------
function download() {
  if (!current) return;
  render();
  out.toBlob((blob) => {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = current.file;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    $("ic-saved").textContent = `Downloaded ${current.file} (128 × 128): it goes in static/elements/`;
  }, "image/png");
}

// ---------- wiring ----------
view.addEventListener("pointerdown", onDown);
view.addEventListener("pointermove", onMove);
view.addEventListener("pointerup", onUp);
view.addEventListener("pointercancel", onUp);
for (const b of document.querySelectorAll("[data-tool]")) b.addEventListener("click", () => setTool(b.dataset.tool));
$("ic-cut").addEventListener("click", () => {
  cutMode = !cutMode;
  $("ic-cut").setAttribute("aria-pressed", String(cutMode));
  setHint();
});
$("ic-grid").addEventListener("click", () => {
  showGrid = !showGrid;
  $("ic-grid").setAttribute("aria-pressed", String(showGrid));
  render();
});
$("ic-undo").addEventListener("click", undo);
$("ic-redo").addEventListener("click", redo);
$("ic-download").addEventListener("click", download);
$("ic-add-shapes").addEventListener("click", () => { addLayer("shapes"); commit(); renderLayers(); });
$("ic-add-pixels").addEventListener("click", () => { addLayer("pixels"); commit(); renderLayers(); });
$("ic-ref-opacity").addEventListener("input", () => { doc.reference.opacity = +$("ic-ref-opacity").value; render(); });
$("ic-ref-opacity").addEventListener("change", commit);
$("ic-ref-remove").addEventListener("click", () => { doc.reference = null; referenceImage = null; commit(); renderLayers(); });

// Colours change the selected shape too.
for (const [input, change] of [
  ["ic-fill", (s) => { fillNone = false; s.fill = fillColour(); if (isOpenLine(s)) s.stroke = s.fill; }],
  ["ic-fill-alpha", (s) => { fillNone = false; s.fill = fillColour(); if (isOpenLine(s)) s.stroke = s.fill; }],
  ["ic-stroke", (s) => { s.stroke = strokeColour(); if (!s.strokeWidth && s.type !== "line") s.strokeWidth = 2; }],
  ["ic-stroke-alpha", (s) => { s.stroke = strokeColour(); }],
  ["ic-stroke-width", (s) => { s.strokeWidth = +$("ic-stroke-width").value; if (!s.stroke) s.stroke = strokeColour(); }],
]) {
  $(input).addEventListener("input", () => {
    if (input.startsWith("ic-fill")) fillNone = false;
    syncColourLabels();
    applyToSelected(change, false);
  });
  $(input).addEventListener("change", () => { if (selectedShape()) commit(); });
}
$("ic-no-fill").addEventListener("click", () => { fillNone = true; syncColourLabels(); applyToSelected((s) => { s.fill = null; }); });
$("ic-brush").addEventListener("input", syncColourLabels);
for (const [input, change] of [
  ["ic-rotation", (s) => { s.rotation = +$("ic-rotation").value; }],
  ["ic-radius", (s) => { s.radius = +$("ic-radius").value; }],
  ["ic-sides", (s) => { s.sides = +$("ic-sides").value; }],
  ["ic-inner", (s) => { s.inner = +$("ic-inner").value; }],
]) {
  $(input).addEventListener("input", () => { syncPropLabels(); applyToSelected(change, false); });
  $(input).addEventListener("change", () => { if (selectedShape()) commit(); });
}
$("ic-closed").addEventListener("change", () => applyToSelected((s) => { s.closed = $("ic-closed").checked; if (s.closed && !s.fill) s.fill = fillColour() || withAlpha(HOUSE_NAVY, 100); }));
$("ic-shape-cut").addEventListener("change", () => applyToSelected((s) => { s.cut = $("ic-shape-cut").checked; }));
$("ic-front").addEventListener("click", () => moveShape(1));
$("ic-back").addEventListener("click", () => moveShape(-1));
$("ic-dup").addEventListener("click", duplicateShape);
$("ic-delete").addEventListener("click", deleteShape);
$("ic-flip-h").addEventListener("click", () => applyToSelected((s) => {
  if (s.type === "line") { s.x += s.w; s.w = -s.w; } else s.flipX = !s.flipX;
}));
$("ic-flip-v").addEventListener("click", () => applyToSelected((s) => {
  if (s.type === "line") { s.y += s.h; s.h = -s.h; } else s.flipY = !s.flipY;
}));
$("ic-center-h").addEventListener("click", () => applyToSelected((s) => { s.x = Math.round((SIZE - s.w) / 2); }));
$("ic-center-v").addEventListener("click", () => applyToSelected((s) => { s.y = Math.round((SIZE - s.h) / 2); }));
$("ic-search").addEventListener("input", renderElementList);
$("ic-missing-only").addEventListener("change", renderElementList);

const KEYS = { v: "select", r: "rect", u: "rounded", o: "ellipse", l: "line", y: "polygon", s: "star", p: "pen", b: "brush", e: "eraser", f: "bucket", i: "picker" };
document.addEventListener("keydown", (event) => {
  if (!doc || $("ic-start").open) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName) && event.target.type !== "range" && event.target.type !== "checkbox") return;
  const mod = event.metaKey || event.ctrlKey;
  const key = event.key.toLowerCase();
  if (mod && key === "z") { event.preventDefault(); if (event.shiftKey) redo(); else undo(); return; }
  if (mod && key === "y") { event.preventDefault(); redo(); return; }
  if (mod && key === "d") { event.preventDefault(); duplicateShape(); return; }
  if (mod || event.altKey) return;
  if ((key === "backspace" || key === "delete") && selectedShape()) { event.preventDefault(); deleteShape(); return; }
  if (key === "escape") { select(null, null); return; }
  if (key === "]") { moveShape(1); return; }
  if (key === "[") { moveShape(-1); return; }
  if (key === "x") { $("ic-cut").click(); return; }
  if (key === "#") { $("ic-grid").click(); return; }
  if (event.key.startsWith("Arrow") && selectedShape()) {
    event.preventDefault();
    const step = event.shiftKey ? GRID : 1;
    const dx = event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0;
    const dy = event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0;
    applyToSelected((s) => { s.x += dx; s.y += dy; });
    return;
  }
  if (KEYS[key] && !event.shiftKey) setTool(KEYS[key]);
});

async function start() {
  try {
    elements = (await Auth.fetch("/icons/elements")).elements;
  } catch (err) {
    showError(err.status === 403 ? "The icon studio is for designers and administrators." : err.message);
    return;
  }
  renderSwatches();
  syncColourLabels();
  setTool("rect");
  renderElementList();
  let last = null;
  try { last = localStorage.getItem(STORE_PREFIX + "last"); } catch (_) { last = null; }
  const el = last && elements.find((e) => e.id === last);
  if (el) openElement(el);
}
start();
