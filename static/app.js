"use strict";

// ---- constants and state --------------------------------------------------

const MIN_GAP_MS = 40;        // minimum time between cursor / move updates
const DRAG_THRESHOLD = 5;     // pixels before a press on the list counts as a drag
const LONG_PRESS_MS = 400;    // a finger held still this long on a tile or an element is a long press
const LONG_PRESS_SLOP = 10;   // pixels a finger may wander and still be holding

const $ = (id) => document.getElementById(id);

// This page is /g/<game-id>; everything it talks to lives under that prefix.
const GAME_ID = location.pathname.split("/")[2] || "";
const BASE = `/g/${GAME_ID}`;
const STORE_PREFIX = `alchemy:${GAME_ID}:`;
const workspace = $("workspace");
const cursorLayer = $("cursor-layer");
const sidebar = $("sidebar");
const itemsBox = $("items");
const tooltip = $("tooltip");

const me = { token: null, pid: null, name: null };
const elements = new Map();   // element id -> {id, label, emoji, by}
const itemEls = new Map();    // element id -> sidebar item node
const tiles = new Map();      // tile id -> {data: {id, element, x, y, held_by}, el}
const players = new Map();    // pid -> {pid, name, color}
const cursors = new Map();    // pid -> cursor node
let total = 0;
let source = null;
let rejoining = false;
let gameEnded = false;
let gameCreator = "";  // the username that started this game: only they can save it
let drag = null;              // active drag of a workspace tile
let itemDrag = null;          // active drag from the element list
let cursorShown = false;

// ---- helpers -----------------------------------------------------------------

const clamp01 = (v) => Math.min(1, Math.max(0, v));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function api(path, body = {}) {
  const send = () => fetch(BASE + path, {
    method: "POST",
    headers: Auth.headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({ ...body, player: me.token }),
  });
  let res;
  try {
    res = await send();
  } catch (_) { // the connection dropped, e.g. the server is reloading: try once more
    await sleep(1500);
    res = await send();
  }
  let data = {};
  try {
    data = await res.json();
  } catch (_) { /* non-JSON error page */ }
  if (!res.ok) {
    if (res.status === 401) { // the login session ended (server restarted, or the account was changed)
      Auth.clear();
      Auth.toLogin();
    }
    const err = new Error(data.error || `request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

/** Sends the most recent body only, one request at a time (for cursors and tile moves). */
class LatestSender {
  constructor(path) {
    this.path = path;
    this.busy = false;
    this.pending = null;
    this.waiters = [];
  }

  send(body) {
    this.pending = body;
    if (!this.busy) this.pump();
  }

  async pump() {
    this.busy = true;
    while (this.pending) {
      const body = this.pending;
      this.pending = null;
      try {
        await api(this.path, body);
      } catch (_) { /* a lost position update is harmless */ }
      await sleep(MIN_GAP_MS);
    }
    this.busy = false;
    this.waiters.splice(0).forEach((resolve) => resolve());
  }

  idle() {
    return this.busy ? new Promise((resolve) => this.waiters.push(resolve)) : Promise.resolve();
  }
}


// Sharing my own pointer. The page only records where the pointer is; a timer sends the newest position at most 20
// times a second, skips a tick while the previous send is still on its way, never repeats a position, and sends nothing
// at all while I am alone in the room (daily challenges and races), since there is nobody to show it to.
const CURSOR_TICK_MS = 50;
let wantedCursor = null;   // the position to share next ({x, y}, or {x: null, y: null} to hide); null = nothing new
let lastSentCursor = null;
let cursorBusy = false;

function shareCursor(pos) {
  wantedCursor = pos;
}

async function sendCursorTick() {
  if (cursorBusy || wantedCursor === null || !me.token) return;
  if (players.size <= 1) { // nobody else is here
    wantedCursor = null;
    lastSentCursor = null;
    return;
  }
  const pos = wantedCursor;
  wantedCursor = null;
  if (lastSentCursor && pos.x === lastSentCursor.x && pos.y === lastSentCursor.y) return;
  lastSentCursor = pos;
  cursorBusy = true;
  try {
    await api("/cursor", pos);
  } catch (_) { /* a lost position update is harmless: the next one replaces it */ } finally {
    cursorBusy = false;
  }
}

setInterval(sendCursorTick, CURSOR_TICK_MS);
const moveSender = new LatestSender("/tile/move");

function toast(message, kind = "") {
  const node = document.createElement("div");
  node.className = "toast" + (kind ? " " + kind : "");
  node.textContent = message;
  const box = $("toasts");
  box.appendChild(node);
  while (box.children.length > 4) box.firstElementChild.remove(); // keep a burst of events from covering the screen
  setTimeout(() => node.remove(), 3500);
}

function wsRect() {
  return workspace.getBoundingClientRect();
}

function inside(rect, x, y) {
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}

function animate(node, cls) {
  if (!node) return;
  node.classList.remove(cls);
  void node.offsetWidth; // restart the animation
  node.classList.add(cls);
  node.addEventListener("animationend", () => node.classList.remove(cls), { once: true });
}

// ---- elements (sidebar list) ---------------------------------------------------

function makeFace(el, tag) {
  const node = document.createElement(tag);
  const emoji = document.createElement("span");
  emoji.className = "emoji";
  const label = document.createElement("span");
  label.className = "label";
  node.append(emoji, label);
  setFace(node, el);
  return node;
}

function loadImages() {
  loadImageNames(() => {
    for (const [id, node] of itemEls) applyImage(node, elements.get(id));
    for (const t of tiles.values()) applyImage(t.el, elements.get(t.data.element));
  });
}

loadImages();

function setFace(node, el) {
  applyImage(node, el);
  const glyph = node.querySelector(".emoji");
  glyph.textContent = el ? el.emoji : "";
  // Elements without an emoji show the first letter of their name in the picture slot (see the CSS).
  glyph.dataset.initial = el && el.label ? [...el.label][0].toUpperCase() : "";
  node.querySelector(".label").textContent = el ? el.label : "?";
}

function appendItem(el) {
  const node = makeFace(el, "div");
  node.classList.add("item");
  // The "art" box is the picture slot: it shows the emoji for now, and is the place
  // to put an <img> once elements have images.
  const art = document.createElement("div");
  art.className = "art";
  art.appendChild(node.querySelector(".emoji"));
  node.prepend(art);
  node.querySelector(":scope > img.pic")?.remove();  // setFace ran before the slot existed
  applyImage(node, el);
  node.dataset.element = el.id;
  node.dataset.rarity = el.rarity || "common";
  node.dataset.search = el.label.toLowerCase();
  if (el.credit) node.classList.add("player-made");   // a star in the corner; the tooltip says who proposed it
  node.addEventListener("pointerdown", onItemPointerDown);
  node.addEventListener("pointermove", onItemPointerMove);
  node.addEventListener("pointerup", onItemPointerUp);
  node.addEventListener("pointercancel", onItemPointerUp);
  node.hidden = !matchesFilter(node);
  insertSorted(itemsBox, node);
  itemEls.set(el.id, node);
}

function matchesFilter(node) {
  return node.dataset.search.includes($("filter").value.trim().toLowerCase());
}

function upsertElement(raw) {
  const el = { id: raw.id, label: raw.label, emoji: raw.emoji, by: raw.by ?? null, credit: raw.credit || "",
               rarity: raw.rarity || "common" };
  const had = elements.has(el.id);
  elements.set(el.id, el);
  if (!had) appendItem(el);
  for (const t of tiles.values()) {
    if (t.data.element === el.id) setFace(t.el, el);
  }
  updateCounter();
  return el;
}

let race = null;        // {id, goal, goal_count, target} when this game is a racer's world
let raceTimer = null;
let challenge = null;   // {date, target, reference, starters, done} when this game is a daily challenge attempt

function updateCounter() {
  if (challenge) {
    const unlocked = elements.size - challenge.starters;
    $("count").textContent = `Unlocked ${unlocked}`;
    $("challenge-count").textContent = `${unlocked} unlocked so far`;
    return;
  }
  $("count").textContent = `Found ${elements.size} / ${total}`;
}

function setRace(r) {
  race = r;
  $("race").hidden = !r;
  clearInterval(raceTimer);
  if (!r) return;
  $("race-goal").textContent = r.goal === "target"
    ? `First to discover ${r.target.emoji} ${r.target.label}` : `First to reach ${r.goal_count} elements`;
  $("invite").hidden = true;   // a private world
  $("hints").hidden = true;    // no hints in a race (the server refuses them too)
  $("end").hidden = true;      // leaving goes through "Leave race"
  pollRace();
  raceTimer = setInterval(pollRace, 1500);
}

async function pollRace() {
  if (!race) return;
  try {
    const room = await Auth.fetch(`/races/${race.id}`);
    const me = Auth.user().toLowerCase();
    $("race-standings").textContent = room.state === "countdown" ? "Get ready…"
      : room.racers.map((r) => `${r.name.toLowerCase() === me ? "you" : r.name} ${r.count}`).join(" · ");
    if (room.state === "finished" && !$("race-done").open) {
      clearInterval(raceTimer);
      const won = room.winner && room.winner.toLowerCase() === me;
      $("race-done-title").textContent = won ? "🏆 You won the race!" : `🏁 ${room.winner || "Nobody"} won the race`;
      $("race-done-text").textContent = room.racers.map((r) => `${r.name}: ${r.count}`).join(" · ");
      $("race-done-results").href = `/race/${race.id}`;
      $("race-done").showModal();
    }
  } catch (_) { /* the race ended or the network blipped; the next poll tries again */ }
}

$("race-leave").addEventListener("click", async () => {
  if (!window.confirm("Leave the race? You will be out of it.")) return;
  try { await Auth.fetch(`/races/${race.id}/leave`, {}); } catch (_) { /* already gone */ }
  location.href = "/";
});

function setChallenge(c) {
  challenge = c;
  $("challenge").hidden = !c;
  if (!c) return;
  $("count").title = "Elements you have unlocked, not counting the starting ones";
  const info = TIER_INFO[c.tier] || TIER_INFO.medium;
  $("challenge-tier").textContent = `${info.icon} ${info.label}`;
  $("challenge-target").textContent = `${c.target.emoji} ${c.target.label}`.trim();
  $("challenge-ref").textContent = c.reference ? `(a good route unlocks ${c.reference})` : "";
  $("invite").hidden = true;   // a private attempt: nobody else can join it
  $("hints").hidden = true;    // no hints in the challenge (the server refuses them too)
  $("end").textContent = "Leave";
  $("end-title").textContent = "Leave this attempt?";
  $("end-text").textContent = "Attempts are not saved. You can start a new one from the home page.";
  $("end-save").hidden = true;
  $("end-nosave").textContent = "Leave";
  $("end-cancel").textContent = "Keep trying";
}

/** A solo game: private, saved after every discovery, so there is nothing to invite or to save. */
let solo = false;
function setSolo(on) {
  solo = on;
  if (!on) return;
  $("invite").hidden = true;
  $("end").textContent = "Leave";
  $("end-title").textContent = "Leave solo mode?";
  $("end-text").textContent = "Your progress is saved after every discovery. Solo carries on from here next time.";
  $("end-save").hidden = true;
  $("end-nosave").textContent = "Leave";
  $("end-cancel").textContent = "Stay";
}

let shareStreak = 0;   // the player's streak, for the "Copy result" text

function showChallengeDone(score, reference) {
  const target = challenge.target;
  const beat = reference ? (score < reference ? " That beats the reference route!"
    : score === reference ? " That matches the reference route." : ` The reference route unlocks ${reference}.`) : "";
  $("challenge-done-text").textContent =
    `You found ${`${target.emoji} ${target.label}`.trim()} after unlocking ${score} element${score === 1 ? "" : "s"}.${beat}`;
  challenge.done = score;
  $("challenge-count").textContent = `found it with ${score} unlocked`;
  if (!$("challenge-done").open) $("challenge-done").showModal();
  showAttemptsLeft();
}

/** Shows on the "Try again" button how many attempts are left in this tier today (none: it is disabled). */
async function showAttemptsLeft() {
  try {
    const d = await Auth.fetch("/daily");
    const mine = d.tiers.find((t) => t.tier === challenge.tier);
    const left = d.attempts_max - (mine ? mine.attempts_used : 0);
    shareStreak = d.streak.current;
    $("challenge-again").disabled = left <= 0;
    $("challenge-again").textContent = left > 0 ? `Try again (${left} left)` : "No attempts left in this tier";
  } catch (_) { /* the button still works; the server refuses it if none are left */ }
}

$("challenge-again").addEventListener("click", async () => {
  $("challenge-again").disabled = true;
  try {
    const data = await Auth.fetch("/daily/start", { tier: challenge.tier, fresh: true });
    location.href = data.url;
  } catch (err) {
    $("challenge-done-error").textContent = err.message;
    $("challenge-again").disabled = false;
  }
});

$("challenge-copy").addEventListener("click", async () => {
  const line = resultLine(challenge.tier, challenge.done, challenge.reference);
  const ok = await copyText(shareText(challenge.date, [line], shareStreak));
  $("challenge-done-error").textContent = ok ? "" : "Could not copy. Select the result and copy it by hand.";
  $("challenge-copy").textContent = ok ? "Copied!" : "Copy result";
});

$("filter").addEventListener("input", () => {
  for (const node of itemEls.values()) node.hidden = !matchesFilter(node);
});

// ---- tooltip ------------------------------------------------------------------

function updateTooltip(e) {
  if (e.pointerType === "touch") { // there is no hovering with a finger
    tooltip.hidden = true;
    return;
  }
  const holder = drag || itemDrag ? null : e.target.closest?.("[data-element]");
  const el = holder ? elements.get(holder.dataset.element) : null;
  if (!el) {
    tooltip.hidden = true;
    return;
  }
  tooltip.replaceChildren();
  const title = document.createElement("div");
  title.textContent = `${el.emoji} ${el.label}`.trim();
  const by = document.createElement("div");
  by.className = "by";
  by.textContent = el.by === null ? "Starting element"
    : el.by === "" ? "Unlocked for free" : `Discovered by ${el.by}`;
  tooltip.append(title, by);
  if (el.credit) {
    const credit = document.createElement("div");
    credit.className = "by";
    credit.textContent = `★ Player recipe, proposed by ${el.credit}`;
    tooltip.append(credit);
  }
  tooltip.hidden = false;
  const w = tooltip.offsetWidth;
  const h = tooltip.offsetHeight;
  tooltip.style.left = Math.max(4, Math.min(e.clientX + 14, window.innerWidth - w - 4)) + "px";
  tooltip.style.top = Math.max(4, Math.min(e.clientY + 18, window.innerHeight - h - 4)) + "px";
}

// ---- players and the colour key -----------------------------------------------------

function setPlayers(list) {
  players.clear();
  for (const p of list) players.set(p.pid, p);

  const key = $("key");
  key.replaceChildren();
  const ordered = [...players.values()].sort((a, b) => (a.pid === me.pid ? -1 : b.pid === me.pid ? 1 : 0));
  for (const p of ordered) {
    const li = document.createElement("li");
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.setProperty("--c", p.color);
    const name = document.createElement("span");
    name.textContent = p.name;
    li.dataset.pid = p.pid;
    li.append(dot, name);
    if (p.pid === me.pid) {
      const you = document.createElement("span");
      you.className = "you";
      you.textContent = "(you)";
      li.append(you);
    } else {
      const flag = document.createElement("button");
      flag.type = "button";
      flag.className = "report";
      flag.textContent = "\u2691";
      flag.title = `Report ${p.name}`;
      flag.setAttribute("aria-label", `Report ${p.name}`);
      flag.addEventListener("click", () => openReport(p));
      li.append(flag);
    }
    key.appendChild(li);
    if (shownEmotes.has(p.pid)) paintEmote(p.pid);
  }

  for (const [pid, node] of cursors) {
    const p = players.get(pid);
    if (!p) {
      node.remove();
      cursors.delete(pid);
      cursorMotion.delete(pid);
    } else {
      paintCursor(node, p);
    }
  }
  for (const id of tiles.keys()) styleHeld(id);
}

// ---- remote cursors -------------------------------------------------------------

function paintCursor(node, player) {
  node.style.setProperty("--c", player.color);
  node.querySelector(".cursor-name").textContent = player.name;
}

function makeCursor(pid) {
  const node = document.createElement("div");
  node.className = "cursor";
  node.innerHTML =
    '<svg viewBox="0 0 18 22" aria-hidden="true"><path d="M2 2 L2 17 L6 13 L9.5 20 L12 19 L8.5 12.2 L14 12 Z" ' +
    'fill="currentColor" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/></svg>' +
    '<span class="cursor-name"></span>';
  const player = players.get(pid);
  if (player) paintCursor(node, player);
  cursorLayer.appendChild(node);
  cursors.set(pid, node);
  return node;
}

// A remote cursor glides to each new position instead of jumping, so 20 updates a second look smooth, and it fades out
// when its owner has stopped moving.
const CURSOR_SMOOTH_MS = 60;    // time constant of the glide
const CURSOR_IDLE_MS = 5000;    // a cursor that has not moved for this long fades out
const cursorMotion = new Map(); // pid -> {x, y, tx, ty, seen}: where it is drawn, where it is heading, when last heard of
let cursorFrameId = 0;
let cursorFrameAt = 0;

function moveCursor(pid, x, y) {
  if (pid === me.pid) return;
  let node = cursors.get(pid);
  if (x === null || y === null || x === undefined || y === undefined) {
    if (node) node.hidden = true;
    cursorMotion.delete(pid); // it reappears where it next shows up, rather than gliding in from where it left
    return;
  }
  if (!node) node = makeCursor(pid);
  let m = cursorMotion.get(pid);
  if (!m) {
    m = { x, y, tx: x, ty: y, seen: 0 };
    cursorMotion.set(pid, m);
    placeCursor(node, m);
  }
  m.tx = x;
  m.ty = y;
  m.seen = performance.now();
  node.hidden = false;
  node.classList.remove("idle");
  if (!cursorFrameId) {
    cursorFrameAt = performance.now();
    cursorFrameId = requestAnimationFrame(cursorFrame);
  }
}

function placeCursor(node, m) {
  node.style.left = m.x * 100 + "%";
  node.style.top = m.y * 100 + "%";
}

function cursorFrame(now) {
  const k = 1 - Math.exp(-Math.min(now - cursorFrameAt, 100) / CURSOR_SMOOTH_MS);
  cursorFrameAt = now;
  let moving = false;
  for (const [pid, m] of cursorMotion) {
    const node = cursors.get(pid);
    if (!node) continue;
    if (Math.abs(m.tx - m.x) < 0.0005 && Math.abs(m.ty - m.y) < 0.0005) {
      m.x = m.tx;
      m.y = m.ty;
    } else {
      m.x += (m.tx - m.x) * k;
      m.y += (m.ty - m.y) * k;
      moving = true;
    }
    placeCursor(node, m);
  }
  cursorFrameId = moving ? requestAnimationFrame(cursorFrame) : 0; // no frames at all while everyone is still
}

function fadeIdleCursors() {
  const now = performance.now();
  for (const [pid, m] of cursorMotion) {
    if (now - m.seen > CURSOR_IDLE_MS) cursors.get(pid)?.classList.add("idle");
  }
}

setInterval(fadeIdleCursors, 1000);

// ---- tiles ------------------------------------------------------------------------

function positionTile(id) {
  const t = tiles.get(id);
  t.el.style.left = t.data.x * 100 + "%";
  t.el.style.top = t.data.y * 100 + "%";
}

function styleHeld(id) {
  const t = tiles.get(id);
  if (!t) return;
  const holder = t.data.held_by;
  const other = holder && holder !== me.pid ? players.get(holder) : null;
  t.el.classList.toggle("held-other", Boolean(holder && holder !== me.pid));
  if (other) t.el.style.setProperty("--c", other.color);
}

function refreshHint() {
  workspace.classList.toggle("has-tiles", tiles.size > 0);
}

function addTile(data, pop = false) {
  removeTile(data.id);
  const el = makeFace(elements.get(data.element), "div");
  el.classList.add("tile");
  el.dataset.id = data.id;
  el.dataset.element = data.element;
  el.addEventListener("pointerdown", onTilePointerDown);
  el.addEventListener("pointermove", onTilePointerMove);
  el.addEventListener("pointerup", onTilePointerUp);
  el.addEventListener("pointercancel", onTilePointerUp);
  el.addEventListener("dblclick", () => duplicateTile(data.id));
  if (pop) el.classList.add("pop");
  workspace.appendChild(el);
  tiles.set(data.id, { data: { ...data }, el });
  positionTile(data.id);
  styleHeld(data.id);
  refreshHint();
}

function removeTile(id) {
  const t = tiles.get(id);
  if (!t) return;
  t.el.remove();
  tiles.delete(id);
  if (drag && drag.id === id) drag = null;
  refreshHint();
}

function clearAllTiles() {
  for (const id of [...tiles.keys()]) removeTile(id);
  drag = null;
}

function settle(id, x, y) {
  const t = tiles.get(id);
  if (!t) return;
  t.data.held_by = null;
  t.data.x = x;
  t.data.y = y;
  positionTile(id);
  styleHeld(id);
}

async function addNewTile(elementId, x, y) {
  try {
    addTile(await api("/tile/add", { element: elementId, x, y }), true);
  } catch (err) {
    toast(err.message, "error");
  }
}

function duplicateTile(id) {
  const t = tiles.get(id);
  if (!t || t.data.held_by) return;
  // Half a tile right and down: the copy overlaps the original by 50% along each axis.
  const rect = workspace.getBoundingClientRect();
  const dx = (t.el.offsetWidth / 2) / rect.width, dy = (t.el.offsetHeight / 2) / rect.height;
  addNewTile(t.data.element, clamp01(t.data.x + dx), clamp01(t.data.y + dy));
}

/** The tile (other than `excludeId`) under a screen position, if it is free to combine with. */
function findTarget(clientX, clientY, excludeId) {
  for (const node of document.elementsFromPoint(clientX, clientY)) {
    const id = node.dataset && node.dataset.id;
    if (!id || id === excludeId || !node.classList.contains("tile")) continue;
    const t = tiles.get(id);
    if (t && !t.data.held_by) return id;
  }
  return null;
}

let highlighted = null;

function setTarget(id) {
  if (id === highlighted) return;
  if (highlighted && tiles.has(highlighted)) tiles.get(highlighted).el.classList.remove("target");
  highlighted = id;
  if (id && tiles.has(id)) tiles.get(id).el.classList.add("target");
}

function overSidebar(x, y) {
  return inside(sidebar.getBoundingClientRect(), x, y);
}

function applyCombine(res, ctx) {
  res.results.forEach(upsertElement);
  if (res.results.length === 0) {
    if (ctx.tileId && tiles.has(ctx.tileId)) {
      settle(ctx.tileId, ctx.x, ctx.y);
      animate(tiles.get(ctx.tileId).el, "shake");
    }
    if (tiles.has(ctx.targetId)) animate(tiles.get(ctx.targetId).el, "shake");
    toast("Nothing happens…");
    return;
  }
  res.removed.forEach(removeTile);
  res.tiles.forEach((t) => addTile(t, true));
}

// ---- dragging a tile on the workspace ---------------------------------------------------

function tileSpot(d, clientX, clientY) {
  const r = wsRect();
  return {
    x: clamp01((clientX - d.offX - r.left) / r.width),
    y: clamp01((clientY - d.offY - r.top) / r.height),
  };
}

function onTilePointerDown(e) {
  if (e.button !== 0 || drag || itemDrag) return;
  if (genieArmed) {
    e.preventDefault();
    const id = e.currentTarget.dataset.element;
    setTimeout(() => useGenie(id), 0);
    return;
  }
  const el = e.currentTarget;
  const id = el.dataset.id;
  const t = tiles.get(id);
  if (!t) return;
  if (t.data.held_by && t.data.held_by !== me.pid) {
    toast("Someone else is moving that tile");
    return;
  }
  e.preventDefault();
  const box = el.getBoundingClientRect();
  const d = {
    id,
    pointerId: e.pointerId,
    offX: e.clientX - (box.left + box.width / 2),
    offY: e.clientY - (box.top + box.height / 2),
    origX: t.data.x,
    origY: t.data.y,
    started: false, // becomes true once the pointer has moved far enough to count as a drag
    startX: e.clientX,
    startY: e.clientY,
    ready: false,
    failed: false,
    last: null,
    lastX: e.clientX,
    lastY: e.clientY,
  };
  drag = d;
  el.setPointerCapture(e.pointerId);
}

/** The pointer moved: now it is a drag, so take hold of the tile on the server (a plain click or double-click never does). */
function startTileDrag(d) {
  const t = tiles.get(d.id);
  if (!t) return;
  d.started = true;
  t.el.classList.add("dragging");
  tooltip.hidden = true;
  t.data.held_by = me.pid;
  d.grab = api("/tile/grab", { tile: d.id })
    .then(() => {
      d.ready = true;
      if (d.last) moveSender.send(d.last);
    })
    .catch((err) => {
      d.failed = true;
      cancelDrag(d, err.message);
    });
}

function cancelDrag(d, message) {
  if (drag === d) drag = null;
  const t = tiles.get(d.id);
  if (t) {
    if (t.data.held_by === me.pid) t.data.held_by = null;
    t.data.x = d.origX;
    t.data.y = d.origY;
    t.el.classList.remove("dragging");
    positionTile(d.id);
    styleHeld(d.id);
  }
  setTarget(null);
  sidebar.classList.remove("trash");
  if (message) toast(message, "error");
}

function onTilePointerMove(e) {
  const d = drag;
  if (!d || e.pointerId !== d.pointerId) return;
  const t = tiles.get(d.id);
  if (!t) return;
  if (!d.started) {
    if (Math.hypot(e.clientX - d.startX, e.clientY - d.startY) < DRAG_THRESHOLD) return;
    startTileDrag(d);
  }
  d.lastX = e.clientX;
  d.lastY = e.clientY;
  const { x, y } = tileSpot(d, e.clientX, e.clientY);
  t.data.x = x;
  t.data.y = y;
  positionTile(d.id);
  const trash = overSidebar(e.clientX, e.clientY);
  sidebar.classList.toggle("trash", trash);
  setTarget(trash ? null : findTarget(e.clientX, e.clientY, d.id));
  const r = wsRect();
  d.last = {
    tile: d.id,
    x,
    y,
    cx: clamp01((e.clientX - r.left) / r.width),
    cy: clamp01((e.clientY - r.top) / r.height),
  };
  if (d.ready) moveSender.send(d.last);
}

function onTilePointerUp(e) {
  const d = drag;
  if (!d || e.pointerId !== d.pointerId) return;
  drag = null;
  if (!d.started) return; // just a click: nothing was grabbed, so there is nothing to put down
  finishTileDrag(d, e.type === "pointercancel" ? d.lastX : e.clientX, e.type === "pointercancel" ? d.lastY : e.clientY);
}

async function finishTileDrag(d, clientX, clientY) {
  const t = tiles.get(d.id);
  setTarget(null);
  sidebar.classList.remove("trash");
  if (!t) return;
  t.el.classList.remove("dragging");

  const trash = overSidebar(clientX, clientY);
  const targetId = trash ? null : findTarget(clientX, clientY, d.id);
  const { x, y } = tileSpot(d, clientX, clientY);

  await d.grab;
  if (d.failed) return;
  await moveSender.idle();
  if (!tiles.has(d.id)) return; // someone cleared the workspace meanwhile

  try {
    if (trash) {
      await api("/tile/remove", { tile: d.id });
      removeTile(d.id);
    } else if (targetId) {
      const res = await api("/combine", { a: d.id, b: targetId, x, y });
      applyCombine(res, { tileId: d.id, x, y, targetId });
    } else {
      await api("/tile/drop", { tile: d.id, x, y });
      settle(d.id, x, y);
    }
  } catch (err) {
    toast(err.message, "error");
    if (tiles.has(d.id)) {
      try {
        await api("/tile/drop", { tile: d.id, x, y });
      } catch (_) { /* already released or gone */ }
      settle(d.id, x, y);
    }
  }
}

// ---- dragging an element out of the list ------------------------------------------------------

function onItemPointerDown(e) {
  if (e.button !== 0 || drag || itemDrag) return;
  e.preventDefault();
  const item = e.currentTarget;
  if (genieArmed) {
    const id = item.dataset.element;
    setTimeout(() => useGenie(id), 0);
    return;
  }
  item.setPointerCapture(e.pointerId);
  itemDrag = { id: item.dataset.element, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, ghost: null };
}

function onItemPointerMove(e) {
  const d = itemDrag;
  if (!d || e.pointerId !== d.pointerId) return;
  if (e.pointerType === "touch" && !d.lifted) return; // a finger on the list scrolls it or taps; it drags only once a long press has lifted the element
  if (!d.ghost) {
    if (Math.hypot(e.clientX - d.startX, e.clientY - d.startY) < DRAG_THRESHOLD) return;
    d.ghost = makeFace(elements.get(d.id), "div");
    d.ghost.className = "tile ghost";
    document.body.appendChild(d.ghost);
    tooltip.hidden = true;
  }
  d.ghost.style.left = e.clientX + "px";
  d.ghost.style.top = e.clientY + "px";
  setTarget(inside(wsRect(), e.clientX, e.clientY) ? findTarget(e.clientX, e.clientY, null) : null);
}

function onItemPointerUp(e) {
  const d = itemDrag;
  if (!d || e.pointerId !== d.pointerId) return;
  itemDrag = null;
  setTarget(null);
  if (d.ghost) d.ghost.remove();
  e.currentTarget.classList.remove("lifted");
  if (e.type === "pointercancel") return;
  if (d.lifted && !d.ghost) { // held, then let go without dragging: show this element's page
    peekElement(d.id);
    return;
  }
  if (e.pointerType === "touch" && !d.lifted && Math.hypot(e.clientX - d.startX, e.clientY - d.startY) > 12) return; // a swipe, not a tap

  if (!d.ghost) { // a plain click or tap: drop a new tile somewhere near the middle
    addNewTile(d.id, 0.3 + Math.random() * 0.4, 0.3 + Math.random() * 0.4);
    return;
  }
  const r = wsRect();
  if (!inside(r, e.clientX, e.clientY)) return;
  const x = clamp01((e.clientX - r.left) / r.width);
  const y = clamp01((e.clientY - r.top) / r.height);
  const targetId = findTarget(e.clientX, e.clientY, null);
  if (!targetId) {
    addNewTile(d.id, x, y);
    return;
  }
  api("/combine", { element: d.id, b: targetId, x, y })
    .then((res) => applyCombine(res, { tileId: null, x, y, targetId }))
    .catch((err) => toast(err.message, "error"));
}

// ---- pointer sharing and tooltip (document level) ---------------------------------------------------

document.addEventListener("pointermove", (e) => {
  updateTooltip(e);
  if (drag || !me.token) return; // tile moves already carry the pointer position
  const r = wsRect();
  if (inside(r, e.clientX, e.clientY)) {
    shareCursor({ x: clamp01((e.clientX - r.left) / r.width), y: clamp01((e.clientY - r.top) / r.height) });
    cursorShown = true;
  } else if (cursorShown) {
    shareCursor({ x: null, y: null });
    cursorShown = false;
  }
});

// ---- reporting another player ----------------------------------------------------------------------

let reporting = null; // the player the dialog is about

function openReport(player) {
  reporting = player;
  $("report-who").textContent = player.name;
  $("report-note").value = "";
  $("report-error").textContent = "";
  $("report-dialog").showModal();
}

$("report-cancel").addEventListener("click", () => $("report-dialog").close());

$("report-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!reporting) return;
  $("report-error").textContent = "";
  try {
    await api("/report", { target: reporting.pid, reason: $("report-reason").value, note: $("report-note").value });
    $("report-dialog").close();
    toast("Thanks: the administrators have been told.");
  } catch (err) {
    $("report-error").textContent = err.message;
  }
});

// A finger has no "leaving the page": when it lifts, stop showing a cursor for it to the other players.
for (const type of ["pointerup", "pointercancel"]) {
  document.addEventListener(type, (e) => {
    if (e.pointerType === "touch" && cursorShown) {
      shareCursor({ x: null, y: null });
      cursorShown = false;
    }
  });
}

// ---- an element's page: long press on a phone, right-click on a laptop -----------------------------------
//
// Both open the journal on that element. On a phone, holding a tile for a moment opens it. Holding an element in the list
// "lifts" it: let go and its page opens, or keep going and drag it onto the board (the list scrolls only while you swipe).

let press = null;          // the long press being timed: {timer, id, item, holder, x, y, pointerId, ready}
let lastTouchAt = -Infinity;

function peekElement(id) {
  if (genieArmed || !id || !me.token) return;
  tooltip.hidden = true;
  openJournalFor(id);
}

function cancelPress() {
  if (press && press.timer) clearTimeout(press.timer);
  press?.holder.classList.remove("lifted");
  press = null;
}

function onLongPress() {
  const p = press;
  if (!p) return;
  p.timer = null;
  if (p.item) {
    if (!itemDrag || itemDrag.pointerId !== p.pointerId) { press = null; return; }
    itemDrag.lifted = true;           // from here the finger drags the element instead of scrolling the list
    p.holder.classList.add("lifted");
    navigator.vibrate?.(15);
    return;
  }
  if (drag && drag.pointerId === p.pointerId && !drag.started) drag = null;   // nothing was grabbed: just stop treating it as a click
  else if (drag) { press = null; return; }                                    // a tile is already being dragged
  p.ready = true;                    // the page opens when the finger lifts (opening it under a held finger would click through)
  p.holder.classList.add("lifted");
  navigator.vibrate?.(15);
}

document.addEventListener("pointerdown", (e) => {
  if (e.pointerType !== "touch") return;
  lastTouchAt = performance.now();
  cancelPress();
  const holder = e.target.closest?.(".tile, .item");
  if (!holder || genieArmed) return;
  press = {
    id: holder.dataset.element, item: holder.classList.contains("item"), holder,
    x: e.clientX, y: e.clientY, pointerId: e.pointerId, timer: setTimeout(onLongPress, LONG_PRESS_MS),
  };
});

document.addEventListener("pointermove", (e) => {
  if (!press || e.pointerId !== press.pointerId) return;
  if (itemDrag?.lifted) return;       // already lifted: this movement is the drag
  if (Math.hypot(e.clientX - press.x, e.clientY - press.y) > LONG_PRESS_SLOP) cancelPress();
});

for (const type of ["pointerup", "pointercancel"]) {
  document.addEventListener(type, (e) => {
    if (!press || e.pointerId !== press.pointerId) return;
    const open = press.ready && type === "pointerup" ? press.id : null;
    cancelPress();
    if (open) peekElement(open);
  });
}

// Once an element is lifted the page must not scroll under the finger.
document.addEventListener("touchmove", (e) => {
  if (itemDrag?.lifted && e.cancelable) e.preventDefault();
}, { passive: false });

document.addEventListener("contextmenu", (e) => {
  const holder = e.target.closest?.(".tile, .item");
  if (!holder) return;
  e.preventDefault();   // never the browser's own menu here
  if (performance.now() - lastTouchAt < 1500) return;   // a touch long press is handled above; Android also sends this event
  if (itemDrag || drag) return;
  peekElement(holder.dataset.element);
});

// On a touch screen the board says how to play with fingers.
if (matchMedia("(pointer: coarse)").matches) {
  $("hint").textContent = "Tap an element below to add it, then drag one tile onto another to combine them.";
}

document.documentElement.addEventListener("mouseleave", () => {
  tooltip.hidden = true;
  if (cursorShown) {
    shareCursor({ x: null, y: null });
    cursorShown = false;
  }
});

// ---- server events ------------------------------------------------------------------------------

// ---- hints ---------------------------------------------------------------------------------

const hintResult = $("hint-result");

function chip(el, unknown = false) {
  const node = document.createElement("span");
  node.className = "chip" + (unknown ? " unknown" : "");
  node.textContent = `${el.emoji} ${el.label}`.trim();
  return node;
}

function say(...parts) {
  hintResult.className = "";
  hintResult.replaceChildren(...parts.map((p) => (typeof p === "string" ? document.createTextNode(p) : p)));
}

function note(text) {
  const node = document.createElement("span");
  node.className = "note";
  node.textContent = text;
  return node;
}

function showHint(h) {
  if (h.kind === "can_make") {
    if (!h.result) return say("Nothing new can be made from the elements you have.");
    return say("You can make ", chip(h.result), " with what you have.");
  }
  if (h.kind === "try") {
    if (!h.pair) return say("No combination of your current elements makes anything new.");
    return say("Try ", chip(h.pair[0]), " + ", chip(h.pair[1]));
  }
  if (h.starter) return say(chip(h.target), " is a starting element.");
  if (!h.recipe) return say("No recipe makes ", chip(h.target), ".");
  const [a, b] = h.recipe;
  const parts = [chip(a, !a.known), " + ", chip(b, !b.known), " → ", chip(h.target)];
  const missing = [a, b].filter((e, i, all) => !e.known && all.findIndex((o) => o.id === e.id) === i);
  if (missing.length) {
    parts.push(note(`You haven't discovered ${missing.map((e) => e.label).join(" or ")} yet.`));
  }
  return say(...parts);
}

async function askHint(kind, target) {
  const buttons = $("hints").querySelectorAll("button");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const answer = await api("/hint", { kind, target });
    if (typeof answer.hints_left === "number") hintsLeft = answer.hints_left;
    showHint(answer);
  } catch (err) {
    hintResult.className = "error";
    hintResult.textContent = err.message;
    if (/used today's hint/.test(err.message)) hintsLeft = 0;
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
    paintHintsLeft();
  }
}

document.querySelectorAll("[data-hint]").forEach((button) => {
  button.addEventListener("click", () => askHint(button.dataset.hint));
});

$("hint-find").addEventListener("submit", (e) => {
  e.preventDefault();
  askHint("find", $("hint-target").value);
});

// ---- the genie ------------------------------------------------------------------------------

let genie = { available: false, unlocked: false, left: 0, total: 0 };
let genieArmed = false;

/** Shows the Djinn panel. It stays hidden until the Djinn is unlocked, then slides in (`reveal`) when that happens live. */
function setGenie(status, reveal = false) {
  genie = status;
  const wasHidden = $("genie").hidden;
  $("genie").hidden = !status.available;
  if (!status.available) return;
  if (wasHidden && reveal) {
    const panel = $("genie");
    panel.classList.remove("reveal");
    void panel.offsetWidth; // restart the animation if it ever plays twice
    panel.classList.add("reveal");
  }
  const use = $("genie-use");
  $("genie-status").textContent = `${status.left} wish${status.left === 1 ? "" : "es"} left`;
  $("genie-note").textContent = `Another wish after ${status.next_in} more discoveries.`;
  use.disabled = status.left <= 0;
  if (use.disabled) disarmGenie();
  else if (genieArmed) $("genie-note").textContent = "Click an element in the list, or a tile, to use the wish on it.";
}

function armGenie() {
  genieArmed = true;
  document.body.classList.add("genie-armed");
  $("genie-use").textContent = "Cancel";
  $("genie-note").textContent = "Click an element in the list, or a tile, to use the wish on it.";
}

function disarmGenie() {
  if (!genieArmed && $("genie-use").textContent === "Use a wish") return;
  genieArmed = false;
  document.body.classList.remove("genie-armed");
  $("genie-use").textContent = "Use a wish";
  if (genie.unlocked) $("genie-note").textContent = `Another wish after ${genie.next_in} more discoveries.`;
}

async function useGenie(elementId) {
  disarmGenie();
  const el = elements.get(elementId);
  if (!el) return;
  const message = `Use a Djinn wish on ${`${el.emoji} ${el.label}`.trim()}?\n\n` +
    "It is combined with every element discovered so far, and every result appears on the workspace. " +
    `${genie.left - 1} wish${genie.left - 1 === 1 ? "" : "es"} will be left.`;
  if (!window.confirm(message)) return;
  try {
    const res = await api("/genie", { element: elementId });
    res.results.forEach(upsertElement);
    res.tiles.forEach((t) => addTile(t, true));
    if (!res.results.length) toast("Nothing happens. That wish wasn't used.");
  } catch (err) {
    toast(err.message, "error");
  }
}

$("genie-use").addEventListener("click", () => (genieArmed ? disarmGenie() : armGenie()));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") disarmGenie();
});

// ---- the cat that clears the workspace ---------------------------------------------------

const SMOKE_PEAK_MS = 1000;  // smoke fully covers the workspace: the tiles vanish now
const SMOKE_LIFT_MS = 2000;  // smoke starts to clear
const SMOKE_END_MS = 2900;
let catBusy = false;

function div(className, style = {}) {
  const node = document.createElement("div");
  node.className = className;
  for (const [key, value] of Object.entries(style)) node.style.setProperty(key, value);
  return node;
}

const rand = (min, max) => min + Math.random() * (max - min);

function buildSmoke() {
  const smoke = div("smoke");
  const body = div("smoke-body");
  body.appendChild(div("smoke-fill"));
  for (let row = 0; row < 5; row++) {
    for (let col = 0; col < 6; col++) {
      body.appendChild(div("puff", {
        left: `${(col / 5) * 100 + rand(-6, 6)}%`,
        top: `${(row / 4) * 108 + rand(-5, 5)}%`,
        width: `${rand(30, 42)}%`,
        "animation-delay": `${rand(0, 900).toFixed(0)}ms`,
      }));
    }
  }
  smoke.appendChild(body);

  // Something is scuffling in the smoke: a darker blob with flickering motion lines around it...
  const brawl = div("brawl");
  for (let i = 0; i < 9; i++) {
    brawl.appendChild(div("ray", {
      "--a": `${i * 40 + rand(-12, 12)}deg`,
      "animation-delay": `${rand(0, 160).toFixed(0)}ms`,
    }));
  }
  smoke.appendChild(brawl);

  // ...and a few streaks that dart across the cloud.
  for (let i = 0; i < 12; i++) {
    smoke.appendChild(div("streak", {
      left: `${rand(8, 92)}%`,
      top: `${rand(10, 90)}%`,
      width: `${rand(50, 120)}px`,
      "--a": `${rand(0, 360)}deg`,
      "animation-delay": `${(SMOKE_PEAK_MS - 100 + rand(0, 800)).toFixed(0)}ms`,
      "animation-duration": `${rand(300, 550).toFixed(0)}ms`,
    }));
  }
  return smoke;
}

/** Smoke descends, `onPeak` clears the tiles while they are hidden, then the smoke lifts. */
function catSweep(onPeak, message = "") {
  if (message) toast(message);
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || catBusy) {
    onPeak();
    if (!catBusy && !message) toast("🐈 The cat tidied up");
    return;
  }
  catBusy = true;
  const smoke = buildSmoke();
  workspace.appendChild(smoke);
  $("cat").classList.add("busy");
  setTimeout(onPeak, SMOKE_PEAK_MS);
  setTimeout(() => smoke.classList.add("lift"), SMOKE_LIFT_MS);
  setTimeout(() => {
    smoke.remove();
    $("cat").classList.remove("busy");
    catBusy = false;
  }, SMOKE_END_MS);
}

// ---- discovery moments: a sound and a flourish that grow with how rare the find is ---------------------------------

let soundOn = true;
try { soundOn = localStorage.getItem("alchemy-sound") !== "off"; } catch (_) { /* private window: sound stays on */ }
let audioContext = null;

/** A few quick notes (frequencies in Hz) from a synthesizer, so there are no sound files to download. */
function playNotes(notes, volume = 0.07) {
  if (!soundOn) return;
  try {
    audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
    if (audioContext.state === "suspended") audioContext.resume();
    const start = audioContext.currentTime + 0.01;
    notes.forEach((hz, i) => {
      const osc = audioContext.createOscillator();
      const amp = audioContext.createGain();
      const at = start + i * 0.085;
      osc.type = "triangle";
      osc.frequency.value = hz;
      amp.gain.setValueAtTime(0.0001, at);
      amp.gain.exponentialRampToValueAtTime(volume, at + 0.02);
      amp.gain.exponentialRampToValueAtTime(0.0001, at + 0.28);
      osc.connect(amp).connect(audioContext.destination);
      osc.start(at);
      osc.stop(at + 0.3);
    });
  } catch (_) { /* no audio on this device: the flourish is silent */ }
}

const RARITY_NOTES = {
  common: [660],
  uncommon: [660, 880],
  rare: [523, 659, 784],
  epic: [523, 659, 784, 1047, 1319],
};
const RARITY_NAME = { uncommon: "Uncommon find", rare: "Rare find!", epic: "Epic find!!" };
const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** A banner over the board and a burst of sparks. `mine` is whether this player made the find. */
function flourish(text, sub, rarity) {
  if (reducedMotion()) return;
  const box = document.createElement("div");
  box.className = `moment ${rarity}`;
  const title = document.createElement("div");
  title.className = "moment-title";
  title.textContent = text;
  const small = document.createElement("div");
  small.className = "moment-sub";
  small.textContent = sub;
  box.append(title, small);
  const sparks = rarity === "epic" ? 22 : rarity === "rare" ? 14 : 8;
  for (let i = 0; i < sparks; i++) {
    const spark = document.createElement("span");
    spark.className = "spark";
    spark.style.setProperty("--a", `${(360 / sparks) * i}deg`);
    spark.style.setProperty("--d", `${60 + Math.random() * 70}px`);
    box.appendChild(spark);
  }
  workspace.appendChild(box);
  setTimeout(() => box.remove(), 2600);
}

/** The sound and flourish for a find. You get the full treatment for your own; others' finds only when rare. */
function discoveryMoment(el, rarity, mine) {
  const big = rarity === "rare" || rarity === "epic";
  if (!mine && !big) return;
  playNotes(RARITY_NOTES[rarity] || RARITY_NOTES.common, mine ? 0.07 : 0.03);
  if (rarity !== "common") flourish(`${el.emoji} ${el.label}`.trim(), RARITY_NAME[rarity], rarity);
}

$("sound").addEventListener("click", () => {
  soundOn = !soundOn;
  try { localStorage.setItem("alchemy-sound", soundOn ? "on" : "off"); } catch (_) { /* not remembered */ }
  paintSound();
  playNotes([880]);
});
function paintSound() {
  $("sound").textContent = soundOn ? "🔊" : "🔇";
  $("sound").setAttribute("aria-pressed", String(soundOn));
}
paintSound();

// ---- emotes ------------------------------------------------------------------------------------------------------

let emoteList = [];
const shownEmotes = new Map();   // pid -> glyph currently shown next to that player's name

async function loadEmotes() {
  try {
    emoteList = (await Auth.fetch("/emotes")).emotes;
  } catch (_) { emoteList = []; }
  const picker = $("emote-picker");
  picker.replaceChildren(...emoteList.map((e) => {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "menuitem");
    button.title = e.label;
    button.setAttribute("aria-label", e.label);
    button.textContent = e.glyph;
    button.addEventListener("click", () => sendEmote(e.id));
    return button;
  }));
}

function closeEmotePicker() {
  $("emote-picker").hidden = true;
  $("emote-open").setAttribute("aria-expanded", "false");
}

async function sendEmote(id) {
  closeEmotePicker();
  try {
    await api("/emote", { emote: id });
  } catch (err) {
    if (!/a second/.test(err.message)) toast(err.message, "error");   // too fast: just ignore the extra tap
  }
}

$("emote-open").addEventListener("click", (e) => {
  e.stopPropagation();
  const picker = $("emote-picker");
  picker.hidden = !picker.hidden;
  $("emote-open").setAttribute("aria-expanded", String(!picker.hidden));
});
document.addEventListener("click", (e) => {
  if (!$("emote-picker").hidden && !e.target.closest("#emote-picker")) closeEmotePicker();
});

function paintEmote(pid) {
  const row = $("key").querySelector(`li[data-pid="${CSS.escape(pid)}"]`);
  if (!row) return;
  row.querySelector(".emote")?.remove();
  const glyph = shownEmotes.get(pid);
  if (!glyph) return;
  const bubble = document.createElement("span");
  bubble.className = "emote";
  bubble.textContent = glyph;
  row.appendChild(bubble);
}

// ---- the solo hint of the day --------------------------------------------------------------------------------------

let hintsLeft = null;   // null: unlimited hints (a shared game); a number in a solo game

function paintHintsLeft() {
  const note = $("hints-left");
  note.hidden = hintsLeft === null;
  if (hintsLeft !== null) {
    note.textContent = hintsLeft > 0 ? "You have 1 hint today." : "You have used today's hint. A new one comes tomorrow.";
  }
  $("hints").querySelectorAll("button").forEach((b) => { b.disabled = hintsLeft === 0; });
}

const handlers = {
  ended({ by, saved, awards }) {
    gameEnded = true;
    if (source) source.close();
    forget();
    $("ended-title").textContent = "This game has ended";
    $("ended-text").textContent = `${by} ended the game. ` + (saved
      ? "It was saved: you can bring it back from the home page for 3 days."
      : "It was not saved.");
    $("end-dialog").open && $("end-dialog").close();
    const list = $("awards-list");
    list.replaceChildren(...(awards || []).map((a) => {
      const row = document.createElement("li");
      const head = document.createElement("strong");
      head.textContent = `${a.icon} ${a.title}: ${a.player}`;
      const detail = document.createElement("span");
      detail.className = "muted";
      detail.textContent = ` ${a.detail}`;
      row.append(head, detail);
      return row;
    }));
    $("awards").hidden = !(awards && awards.length);
    $("ended").hidden = false;
  },

  emote(d) {
    shownEmotes.set(d.pid, (emoteList.find((e) => e.id === d.emote) || {}).glyph || "");
    paintEmote(d.pid);
    setTimeout(() => {
      shownEmotes.delete(d.pid);
      paintEmote(d.pid);
    }, 4000);
  },

  achievement(d) {
    toast(`🏆 Achievement unlocked: ${d.icon} ${d.title}`);
    playNotes([523, 659, 784, 1047], 0.08);
    flourish(`${d.icon} ${d.title}`, "Achievement unlocked", "epic");
  },
  snapshot(s) {
    me.pid = s.you;
    gameCreator = s.game.creator || "";
    total = s.total;
    $("game-name").textContent = s.game.name;
    document.title = `${s.game.name} · Co-op Alchemy`;
    setGenie(s.genie);
    setChallenge(s.challenge);
    setRace(s.race);
    setSolo(s.game.solo === true);
    hintsLeft = typeof s.game.hints_left === "number" ? s.game.hints_left : null;
    paintHintsLeft();
    $("emote-open").hidden = !!(s.challenge || s.race || s.game.solo);   // emotes are for shared games
    if (!$("emote-open").hidden && !emoteList.length) loadEmotes();
    clearAllTiles();
    elements.clear();
    itemEls.clear();
    itemsBox.replaceChildren();
    s.elements.forEach(upsertElement);
    for (const node of cursors.values()) node.remove();
    cursors.clear();
    cursorMotion.clear();
    setPlayers(s.players);
    s.tiles.forEach((t) => addTile(t));
    updateCounter();
    if (challenge && challenge.done !== null) showChallengeDone(challenge.done, challenge.reference);
    if (typeof refreshJournal === "function") refreshJournal();
  },

  players(s) {
    setPlayers(s.players);
  },

  discovered(d) {
    const el = upsertElement(d);
    if (d.unlocked) { // given to the whole room for reaching a milestone, not made by anyone
      toast(`🔓 ${d.unlocked} elements found: ${el.emoji} ${el.label} is unlocked!`.replace("  ", " "));
      const item = itemEls.get(el.id);
      if (item) {
        item.classList.add("new");
        setTimeout(() => item.classList.remove("new"), 3000);
      }
      if (typeof refreshJournal === "function") refreshJournal();
      return;
    }
    const who = d.pid === me.pid ? "You" : players.get(d.pid)?.name ?? el.by ?? "Someone";
    toast(`${who} discovered ${el.emoji} ${el.label}!`.replace("  ", " "));
    discoveryMoment(el, el.rarity, d.pid === me.pid);
    const item = itemEls.get(el.id);
    if (item) {
      item.classList.add("new");
      setTimeout(() => item.classList.remove("new"), 3000);
      if (d.pid === me.pid) item.scrollIntoView({ block: "nearest" });
    }
    if (total && elements.size >= total) toast("🎉 Every element has been discovered!");
    if (typeof refreshJournal === "function") refreshJournal();
  },

  cursor(d) {
    moveCursor(d.pid, d.x, d.y);
  },

  tile_added(d) {
    addTile(d.tile, true);
  },

  tile_grabbed(d) {
    const t = tiles.get(d.id);
    if (!t) return;
    t.data.held_by = d.pid;
    styleHeld(d.id);
  },

  tile_moved(d) {
    const t = tiles.get(d.id);
    if (!t) return;
    t.data.x = d.x;
    t.data.y = d.y;
    t.data.held_by = d.pid;
    positionTile(d.id);
    styleHeld(d.id);
    if (d.cx !== null && d.cx !== undefined) moveCursor(d.pid, d.cx, d.cy);
  },

  tile_dropped(d) {
    settle(d.id, d.x, d.y);
  },

  tile_removed(d) {
    removeTile(d.id);
  },

  genie(d) {
    const before = genie;
    setGenie(d.status, true);
    if (d.used_by) {
      const el = d.used_by.element;
      const who = d.used_by.name === me.name ? "You" : d.used_by.name;
      toast(`🧞 ${who} used a wish on ${el.emoji} ${el.label}`.replace("  ", " "));
    }
    if (d.status.unlocked && !before.unlocked) {
      toast(`🧞 The Djinn is unlocked: ${d.status.left} wishes!`);
    } else if (d.status.total > before.total) {
      toast("🧞 The Djinn earned another wish");
    }
  },

  race_won() {
    pollRace();
  },

  challenge_done(d) {
    if (challenge) showChallengeDone(d.score, d.reference);
  },

  tiles_cleared(d) {
    const who = d.name || players.get(d.pid)?.name || "Someone";
    catSweep(clearAllTiles, `🐈 ${who} sent the cat to clear the workspace`);
  },
};

let reconnectTimer = null;

/** Opens the event stream with a one-time ticket, so the player token never appears in a URL. */
async function connect() {
  clearTimeout(reconnectTimer);
  if (source) source.close();
  let ticket;
  try {
    ticket = (await api("/ticket")).ticket;
  } catch (err) {
    if (gameEnded) return;
    if (err.status === 403 || err.status === 404) rejoin(); // the server no longer knows us (e.g. it restarted)
    else reconnectTimer = setTimeout(connect, 3000);       // the server is unreachable: keep trying
    return;
  }
  source = new EventSource(`${BASE}/events?ticket=${encodeURIComponent(ticket)}`);
  for (const [name, handler] of Object.entries(handlers)) {
    source.addEventListener(name, (e) => handler(JSON.parse(e.data)));
  }
  source.onopen = () => $("status").classList.add("on");
  source.onerror = () => {
    $("status").classList.remove("on");
    // A ticket works once, so the browser's own retry cannot reuse it: close and ask for a fresh one.
    source.close();
    if (!gameEnded) reconnectTimer = setTimeout(connect, 1500);
  };
}

// ---- joining ---------------------------------------------------------------------------------------

function remember() {
  try {
    sessionStorage.setItem(STORE_PREFIX + "token", me.token);
    sessionStorage.setItem(STORE_PREFIX + "name", me.name);
  } catch (_) { /* storage may be unavailable */ }
}

function forget() {
  try {
    sessionStorage.removeItem(STORE_PREFIX + "token");
  } catch (_) { /* ignore */ }
}

async function loadGameInfo() {
  try {
    const data = await Auth.fetch("/games");
    const game = data.games.find((g) => g.id === GAME_ID);
    if (game) {
      $("join-game").textContent = `${game.name}: ${game.players}/${game.max_players} players in`;
      document.title = `${game.name} · Co-op Alchemy`;
    } else {
      $("join-game").textContent = "This game isn't running.";
      $("join-form").querySelector("button[type=submit]").disabled = true;
    }
  } catch (_) { /* the form still works without this */ }
}

async function join() {
  const r = await api("/join"); // the server uses the logged-in username as the player's name
  me.token = r.token;
  me.pid = r.pid;
  me.name = r.name;
  remember();
}

function showApp() {
  $("join").hidden = true;
  $("app").hidden = false; // visible before connecting, so the layout has a real size
}

function showJoin(message = "") {
  if (source) source.close();
  me.token = null;
  cursorShown = false;
  wantedCursor = null;
  lastSentCursor = null;
  $("app").hidden = true;
  $("join").hidden = false;
  $("join-error").textContent = message;
}

async function rejoin() {
  if (rejoining) return;
  rejoining = true;
  try {
    await join();
    connect();
    toast("Reconnected");
  } catch (err) {
    forget();
    showJoin(`Lost connection to the server and could not rejoin (${err.message}).`);
  } finally {
    rejoining = false;
  }
}

$("join-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("join-error").textContent = "";
  try {
    await join();
    showApp();
    connect();
  } catch (err) {
    $("join-error").textContent = err.message;
  }
});

$("invite").addEventListener("click", async () => {
  let base = location.origin;
  if (["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)) {
    // "localhost" only works on this computer, so use the address other computers can reach.
    try { base = (await Auth.fetch("/lan")).base || base; } catch (_) { /* keep what we have */ }
  }
  const link = `${base}/g/${encodeURIComponent(GAME_ID)}`;
  if (await copyText(link)) toast("Invite link copied: " + link);
  else toast("Send this link to your friends: " + link);
});

async function endGame(save) {
  $("end-dialog").close();
  try {
    await api("/end", { save });
    // The "ended" event shows the same message (and the awards) to everyone, including us; this is only a fallback for it
    // not arriving.
    setTimeout(() => { if (!gameEnded) handlers.ended({ by: me.name, saved: save }); }, 1200);
  } catch (err) {
    toast(err.message, "error");
  }
}

$("end").addEventListener("click", () => {
  const maySave = !gameCreator || gameCreator.toLowerCase() === me.name.toLowerCase();
  $("end-save").hidden = solo || !maySave;
  $("end-note").textContent = solo ? "" : maySave ? "" : `Only ${gameCreator}, who started this game, can save it.`;
  $("end-dialog").showModal();
});
$("end-save").addEventListener("click", () => endGame(true));
$("end-nosave").addEventListener("click", () => endGame(false));
$("end-cancel").addEventListener("click", () => $("end-dialog").close());

$("cat").addEventListener("click", () => {
  if (catBusy) return;
  if (tiles.size && !window.confirm("Let the cat clear the shared workspace for everyone?")) return;
  catSweep(async () => {
    try {
      if (tiles.size) await api("/tiles/clear"); // the other players' cats sweep when they hear about it
      clearAllTiles();
    } catch (err) {
      toast(err.message, "error");
    }
  });
});

(function start() {
  if (!Auth.token()) {
    Auth.toLogin();
    return;
  }
  $("name").textContent = Auth.user();
  $("logout").addEventListener("click", () => Auth.logout());
  let token = null;
  let name = null;
  try {
    token = sessionStorage.getItem(STORE_PREFIX + "token");
    name = sessionStorage.getItem(STORE_PREFIX + "name");
  } catch (_) { /* ignore */ }
  const loaded = loadGameInfo();
  if (token && name === Auth.user()) { // page refresh: keep the same identity if the server still knows us
    me.token = token;
    me.name = name;
    showApp();
    connect();
  } else if (GAME_ID.startsWith("race-")) {
    // A racer's world is theirs alone and the race is already on: no "Join" step.
    loaded.then(() => $("join-form").requestSubmit());
  }
})();

// ---- new-version notice ------------------------------------------------------------------------
// The server's page files can change while a game is running; offer a reload instead of forcing one.

let pageVersion = null;

async function checkVersion() {
  try {
    const res = await fetch("/version", { cache: "no-store" });
    const { version } = await res.json();
    if (pageVersion === null) pageVersion = version;
    else if (version !== pageVersion) $("update").hidden = false;
  } catch (_) { /* offline or reloading: try again later */ }
}

$("update-reload").addEventListener("click", () => location.reload());
$("update-later").addEventListener("click", () => { $("update").hidden = true; pageVersion = null; checkVersion(); });
checkVersion();
setInterval(checkVersion, 20000);
