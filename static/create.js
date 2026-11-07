"use strict";

// Create Recipes: a solo sandbox where every element is unlocked. Combining two elements that
// make nothing yet asks for a name and saves a draft recipe on the server (for this account only).
// Tiles live only in this page; the server keeps the drafts.

const $ = (id) => document.getElementById(id);
const DRAG_THRESHOLD = 5;
const MAX_TILES = 150;

if (!Auth.token()) Auth.toLogin();

const elements = new Map();   // id -> {id, label, emoji, draft}
const itemEls = new Map();    // id -> sidebar item
const tiles = new Map();      // tile id -> {id, element, node}
let drafts = [];              // this account's draft recipes, as the server describes them
let tileSeq = 0;
let busy = false;             // a combine is being worked out
let drag = null;              // tile being dragged
let itemDrag = null;          // drag from the element list

const workspace = $("workspace");
const sidebar = $("sidebar");
const itemsBox = $("items");

// ---- helpers ------------------------------------------------------------------------------------

async function call(path, body) {
  return Auth.fetch(path, body);
}

function toast(text, isError = false) {
  const node = document.createElement("div");
  node.className = "toast" + (isError ? " error" : "");
  node.textContent = text;
  $("toasts").appendChild(node);
  setTimeout(() => node.remove(), 3500);
}

function inside(rect, x, y) {
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}

function makeFace(el, tag) {
  const node = document.createElement(tag);
  const emoji = document.createElement("span");
  emoji.className = "emoji";
  emoji.textContent = el.emoji || "";
  emoji.dataset.initial = el.label ? [...el.label][0].toUpperCase() : "";
  const label = document.createElement("span");
  label.className = "label";
  label.textContent = el.label;
  node.append(emoji, label);
  applyImage(node, el);
  return node;
}

function updateCount() {
  $("count").textContent = `${elements.size} elements`;
}

// ---- the element list ---------------------------------------------------------------------------

function addElement(el) {
  if (elements.has(el.id)) return;
  elements.set(el.id, el);
  const node = makeFace(el, "div");
  node.classList.add("item");
  if (el.draft) node.classList.add("is-draft");
  const art = document.createElement("div");
  art.className = "art";
  art.appendChild(node.querySelector(".emoji"));
  node.prepend(art);
  applyImage(node, el);
  node.dataset.element = el.id;
  node.dataset.search = el.label.toLowerCase();
  node.addEventListener("pointerdown", onItemPointerDown);
  node.addEventListener("pointermove", onItemPointerMove);
  node.addEventListener("pointerup", onItemPointerUp);
  node.addEventListener("pointercancel", onItemPointerUp);
  node.hidden = !matches(node);
  insertSorted(itemsBox, node);
  itemEls.set(el.id, node);
  updateCount();
}

function removeElement(id) {
  elements.delete(id);
  itemEls.get(id)?.remove();
  itemEls.delete(id);
  for (const tile of [...tiles.values()]) if (tile.element === id) removeTile(tile);
  updateCount();
}

function matches(node) {
  return node.dataset.search.includes($("filter").value.trim().toLowerCase());
}

$("filter").addEventListener("input", () => {
  for (const node of itemEls.values()) node.hidden = !matches(node);
});

function onItemPointerDown(e) {
  if (e.button !== 0 || drag || itemDrag) return;
  e.preventDefault();
  e.currentTarget.setPointerCapture(e.pointerId);
  itemDrag = { id: e.currentTarget.dataset.element, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, ghost: null };
}

function onItemPointerMove(e) {
  const d = itemDrag;
  if (!d || e.pointerId !== d.pointerId) return;
  if (e.pointerType === "touch") return; // a finger on the list scrolls it or taps; dragging out of the list is for a mouse
  if (!d.ghost) {
    if (Math.hypot(e.clientX - d.startX, e.clientY - d.startY) < DRAG_THRESHOLD) return;
    d.ghost = makeFace(elements.get(d.id), "div");
    d.ghost.className = "tile ghost";
    document.body.appendChild(d.ghost);
  }
  d.ghost.style.left = e.clientX + "px";
  d.ghost.style.top = e.clientY + "px";
  setTarget(inside(workspace.getBoundingClientRect(), e.clientX, e.clientY) ? tileAt(e.clientX, e.clientY, null) : null);
}

function onItemPointerUp(e) {
  const d = itemDrag;
  if (!d || e.pointerId !== d.pointerId) return;
  itemDrag = null;
  setTarget(null);
  if (d.ghost) d.ghost.remove();
  if (e.type === "pointercancel") return;
  if (e.pointerType === "touch" && Math.hypot(e.clientX - d.startX, e.clientY - d.startY) > 12) return; // a swipe, not a tap
  const rect = workspace.getBoundingClientRect();
  if (!d.ghost) { // a plain click or tap: drop a tile near the middle
    addTile(d.id, rect.width * (0.3 + Math.random() * 0.4), rect.height * (0.3 + Math.random() * 0.4));
    return;
  }
  if (!inside(rect, e.clientX, e.clientY)) return;
  const target = tileAt(e.clientX, e.clientY, null);
  const tile = addTile(d.id, e.clientX - rect.left, e.clientY - rect.top);
  if (tile && target) combine(tile, target);
}

// ---- tiles --------------------------------------------------------------------------------------

function addTile(elementId, x, y) {
  if (tiles.size >= MAX_TILES) {
    toast("The workspace is full: clear some tiles.", true);
    return null;
  }
  const el = elements.get(elementId);
  const node = makeFace(el, "div");
  node.className = "tile pop";
  const tile = { id: `t${++tileSeq}`, element: elementId, node };
  place(tile, x, y);
  node.addEventListener("pointerdown", (e) => onTilePointerDown(e, tile));
  node.addEventListener("pointermove", (e) => onTilePointerMove(e, tile));
  node.addEventListener("pointerup", (e) => onTilePointerUp(e, tile));
  node.addEventListener("pointercancel", (e) => onTilePointerUp(e, tile));
  node.addEventListener("dblclick", () => {
    const at = position(tile);
    // Half a tile right and down: the copy overlaps the original by 50% along each axis.
    addTile(tile.element, at.x + node.offsetWidth / 2, at.y + node.offsetHeight / 2);
  });
  node.addEventListener("animationend", () => node.classList.remove("pop"), { once: true });
  workspace.appendChild(node);
  tiles.set(tile.id, tile);
  workspace.classList.add("has-tiles");
  return tile;
}

function place(tile, x, y) {
  const rect = workspace.getBoundingClientRect();
  tile.node.style.left = Math.min(rect.width, Math.max(0, x)) + "px";
  tile.node.style.top = Math.min(rect.height, Math.max(0, y)) + "px";
}

function position(tile) {
  return { x: parseFloat(tile.node.style.left) || 0, y: parseFloat(tile.node.style.top) || 0 };
}

function removeTile(tile) {
  tile.node.remove();
  tiles.delete(tile.id);
  if (!tiles.size) workspace.classList.remove("has-tiles");
}

$("clear").addEventListener("click", () => {
  for (const tile of [...tiles.values()]) removeTile(tile);
});

function tileAt(x, y, except) {
  let best = null;
  for (const tile of tiles.values()) {
    if (tile === except) continue;
    const r = tile.node.getBoundingClientRect();
    if (inside({ left: r.left - 4, right: r.right + 4, top: r.top - 4, bottom: r.bottom + 4 }, x, y)) best = tile;
  }
  return best;
}

let targeted = null;
function setTarget(tile) {
  if (targeted === tile) return;
  targeted?.node.classList.remove("target");
  targeted = tile;
  tile?.node.classList.add("target");
}

function onTilePointerDown(e, tile) {
  if (e.button !== 0 || drag || itemDrag || busy) return;
  e.preventDefault();
  tile.node.setPointerCapture(e.pointerId);
  const at = position(tile);
  drag = { tile, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, baseX: at.x, baseY: at.y, moved: false };
}

function onTilePointerMove(e, tile) {
  if (!drag || drag.tile !== tile || e.pointerId !== drag.pointerId) return;
  const dx = e.clientX - drag.startX, dy = e.clientY - drag.startY;
  if (!drag.moved) {
    if (Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    drag.moved = true;
    tile.node.classList.add("dragging");
  }
  place(tile, drag.baseX + dx, drag.baseY + dy);
  const over = inside(sidebar.getBoundingClientRect(), e.clientX, e.clientY);
  sidebar.classList.toggle("trash", over);
  setTarget(over ? null : tileAt(e.clientX, e.clientY, tile));
}

function onTilePointerUp(e, tile) {
  if (!drag || drag.tile !== tile || e.pointerId !== drag.pointerId) return;
  const d = drag;
  drag = null;
  tile.node.classList.remove("dragging");
  sidebar.classList.remove("trash");
  const target = targeted;
  setTarget(null);
  if (e.type === "pointercancel" || !d.moved) return;
  if (inside(sidebar.getBoundingClientRect(), e.clientX, e.clientY)) {
    removeTile(tile);
    return;
  }
  if (target && tiles.has(target.id)) combine(tile, target);
}

// ---- combining ----------------------------------------------------------------------------------

/** Replace two tiles by the tiles for `results`, at the middle of where they were. */
function merge(a, b, results) {
  const pa = position(a), pb = position(b);
  removeTile(a);
  removeTile(b);
  results.forEach((el, i) => {
    addElement(el);
    addTile(el.id, (pa.x + pb.x) / 2 + i * 34, (pa.y + pb.y) / 2 + i * 34);
  });
}

async function combine(a, b) {
  if (busy) return;
  busy = true;
  try {
    const { results } = await call("/create/combine", { a: a.element, b: b.element });
    if (results.length) {
      merge(a, b, results);
      return;
    }
    const made = await askName(a, b);
    if (made) {
      merge(a, b, [made.element]);
      toast(`New recipe: ${made.recipe.a_label} + ${made.recipe.b_label} = ${made.recipe.result_label}`);
    } else {
      const at = position(b);  // cancelled: step the dragged tile aside so the two don't overlap
      place(a, at.x + 70, at.y + 10);
      a.node.classList.add("shake");
      a.node.addEventListener("animationend", () => a.node.classList.remove("shake"), { once: true });
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy = false;
  }
}

/** Ask what a + b make. Resolves with the server's reply once the recipe is saved, or null if cancelled. */
function askName(a, b) {
  const dialog = $("name-dialog");
  const input = $("name-input");
  $("name-title").textContent =
    `${elements.get(a.element).label} + ${elements.get(b.element).label} makes…`;
  input.value = "";
  $("name-error").textContent = "";
  return new Promise((resolve) => {
    let finished = false;
    const finish = (value) => {
      if (finished) return;
      finished = true;
      $("name-form").onsubmit = null;
      $("name-cancel").onclick = null;
      dialog.onclose = null;
      if (dialog.open) dialog.close();
      resolve(value);
    };
    $("name-form").onsubmit = async (e) => {
      e.preventDefault();
      $("name-ok").disabled = true;
      try {
        const made = await call("/create/recipe", { a: a.element, b: b.element, name: input.value });
        syncDrafts({ recipes: [...drafts.filter((r) => r.id !== made.recipe.id), made.recipe] });
        if (made.element.draft) addElement(made.element);
        finish(made);
      } catch (err) {
        $("name-error").textContent = err.message;
      } finally {
        $("name-ok").disabled = false;
      }
    };
    $("name-cancel").onclick = () => finish(null);
    dialog.onclose = () => finish(null);  // Escape
    dialog.showModal();
    input.focus();
  });
}

// ---- my recipes ---------------------------------------------------------------------------------

const STATUS_TEXT = {
  draft: "Draft",
  requested: "Waiting for an admin",
  approved: "Approved: it appears in the game once an admin adds it",
  rejected: "Not accepted",
};

/** Show the server's list of drafts; `view.elements`, when given, also adds and removes draft elements. */
function syncDrafts(view) {
  drafts = view.recipes;
  if (view.elements) {
    const wanted = new Set(view.elements.map((e) => e.id));
    for (const el of [...elements.values()]) if (el.draft && !wanted.has(el.id)) removeElement(el.id);
    for (const el of view.elements) addElement(el);
  }
  renderDrafts();
}

function renderDrafts() {
  $("draft-count").textContent = drafts.length ? `(${drafts.length})` : "";
  $("draft-empty").hidden = drafts.length > 0;
  $("draft-list").replaceChildren(...drafts.map((r) => {
    const li = document.createElement("li");
    li.className = "draft status-" + r.status;
    const text = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = `${r.a_label} + ${r.b_label} → ${r.result_label}`;
    const state = document.createElement("span");
    state.className = "meta";
    state.textContent = STATUS_TEXT[r.status] + (r.new_element ? " · new element" : "") + (r.note ? `: ${r.note}` : "");
    text.append(title, state);
    const buttons = document.createElement("div");
    buttons.className = "draft-buttons";
    if (r.status === "draft" || r.status === "rejected") {
      const promote = document.createElement("button");
      promote.type = "button";
      promote.textContent = "Promote";
      promote.addEventListener("click", () => askPromote(r));
      buttons.appendChild(promote);
    }
    if (r.status !== "requested") {
      const del = document.createElement("button");
      del.type = "button";
      del.textContent = "Delete";
      del.addEventListener("click", () => deleteDraft(r));
      buttons.appendChild(del);
    }
    li.append(text, buttons);
    return li;
  }));
}

async function deleteDraft(r) {
  try {
    syncDrafts(await call("/create/delete", { id: r.id }));
  } catch (err) {
    toast(err.message, true);
  }
}

function askPromote(r) {
  const dialog = $("promote-dialog");
  $("promote-title").textContent = `Promote ${r.a_label} + ${r.b_label} → ${r.result_label}?`;
  $("promote-note").value = "";
  $("promote-error").textContent = "";
  $("promote-form").onsubmit = async (e) => {
    e.preventDefault();
    $("promote-ok").disabled = true;
    try {
      const sent = await call("/create/promote", { id: r.id, note: $("promote-note").value });
      syncDrafts({ recipes: sent.recipes });
      dialog.close();
      toast("Sent to the admins. Thank you!");
    } catch (err) {
      $("promote-error").textContent = err.message;
    } finally {
      $("promote-ok").disabled = false;
    }
  };
  $("promote-cancel").onclick = () => dialog.close();
  dialog.showModal();
}

// ---- start --------------------------------------------------------------------------------------

/** Until Solo mode is finished the server sends no elements at all: this page only says how far along the account is. */
function showLocked(solo) {
  const box = document.getElementById("workspace");
  const found = solo && solo.total ? ` You have found ${solo.found} of ${solo.total}.` : "";
  const message = document.createElement("p");
  message.id = "hint";
  message.textContent = `Create Recipes opens once you have found every element in Solo mode.${found}`;
  const link = document.createElement("a");
  link.className = "btn";
  link.href = "/";
  link.textContent = "Back to the home page to play Solo";
  box.replaceChildren(message, link);
  document.getElementById("sidebar").hidden = true;
  document.getElementById("clear").hidden = true;
}

async function start() {
  try {
    const data = await call("/create/data");
    if (data.locked) { showLocked(data.solo); return; }
    for (const el of data.elements) addElement(el);
    syncDrafts(data.drafts);
    loadImageNames(() => {
      for (const [id, node] of itemEls) applyImage(node, elements.get(id));
      for (const tile of tiles.values()) applyImage(tile.node, elements.get(tile.element));
    });
  } catch (err) {
    toast(err.message, true);
  }
}

start();

// On a touch screen the board says how to play with fingers.
if (matchMedia("(pointer: coarse)").matches) {
  const hint = document.getElementById("hint");
  if (hint) hint.textContent = "Everything is unlocked here. Tap an element to add it, then drag one tile onto another: if they don't make anything yet, you can name what they make.";
}
