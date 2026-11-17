"use strict";

// Temporary page: a once-over of every recipe in the game, one card at a time (see recipe_review.py).
// Right arrow marks a recipe OK, left arrow flags it. Modelled on admin.js.

const $ = (id) => document.getElementById(id);
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

let deck = [];          // recipes still to review; deck[0] is on screen
const history = [];     // answers this session, newest last, for undo
let busy = false;
let shownId = null;
let counts = null;
let flaggedText = "";

if (!Auth.loggedIn()) Auth.toLogin();

function face(node, el) {
  node.querySelector(".name").textContent = el.label;
  const art = node.querySelector(".art");
  const span = document.createElement("span");
  span.className = "emoji";
  span.textContent = el.emoji || el.label.slice(0, 1).toUpperCase();
  art.replaceChildren(span);
  applyImage(node, el);
}

function render() {
  const card = deck[0];
  $("swipe").hidden = !card;
  $("empty").hidden = !!card || !counts;
  $("undo").disabled = history.length === 0 || busy;
  if (!card) { shownId = null; return; }
  face($("face-a"), card.a);
  face($("face-b"), card.b);
  face($("face-c"), card.result);
  $("left").textContent = counts
    ? `${counts.left} left of ${counts.total} · ${counts.ok} OK · ${counts.flagged} flagged`
    : "";
  $("also").replaceChildren(...(card.also.length ? [Object.assign(document.createElement("div"), {
    className: "competing", textContent: `This pair also makes: ${card.also.join(", ")}`,
  })] : []));
  if (shownId !== card.id) $("note").value = "";   // keep a half-typed note across refreshes
  shownId = card.id;
}

function fly(direction) {
  const node = $("card");
  if (reduceMotion) return Promise.resolve();
  node.classList.add(direction > 0 ? "fly-right" : "fly-left");
  return new Promise((resolve) => setTimeout(() => {
    node.classList.remove("fly-right", "fly-left");
    resolve();
  }, 180));
}

async function answer(verdict) {
  const card = deck[0];
  if (!card || busy) return;
  busy = true;
  $("admin-msg").textContent = "";
  try {
    await Auth.fetch("/admin/recipes/handle", { id: card.id, verdict, note: $("note").value });
    await fly(verdict === "ok" ? 1 : -1);
    deck.shift();
    history.push({ card, verdict });
    setTimeout(refresh, 0);
  } catch (err) {
    $("admin-msg").textContent = err.message;
    if (err.status === 409) deck.shift();   // someone else answered it
  } finally {
    busy = false;
    render();
  }
}

/** Put this recipe at the back of the deck without answering it; it stays waiting. */
function skip() {
  if (busy || deck.length === 0) return;
  if (deck.length === 1) {
    $("admin-msg").textContent = "This is the only recipe waiting.";
    return;
  }
  $("admin-msg").textContent = "";
  deck.push(deck.shift());
  render();
}

async function undo() {
  const last = history[history.length - 1];
  if (!last || busy) return;
  busy = true;
  try {
    await Auth.fetch("/admin/recipes/undo", { id: last.card.id });
    history.pop();
    deck.unshift(last.card);
    setTimeout(refresh, 0);
  } catch (err) {
    $("admin-msg").textContent = err.message;
  } finally {
    busy = false;
    render();
  }
}

/** Reload the next recipes and the counts, keeping the order of what is already on the deck. */
async function refresh() {
  try {
    const data = await Auth.fetch("/admin/recipes/queue");
    const fresh = new Map(data.cards.map((c) => [c.id, c]));
    const kept = deck.filter((c) => fresh.has(c.id));   // drop what someone else has answered meanwhile
    const known = new Set(kept.map((c) => c.id));
    deck = kept.concat(data.cards.filter((c) => !known.has(c.id)));
    counts = data;
    if (!busy) render();
    loadFlagged();
  } catch (err) {
    deck = [];
    render();
    $("swipe").hidden = true;
    $("empty").hidden = true;
    $("admin-msg").textContent = err.status === 403 ? "Only admins can see this page." : err.message;
  }
}

async function loadFlagged() {
  try {
    const data = await Auth.fetch("/admin/recipes/flagged");
    $("flagged-count").textContent = data.flagged.length;
    const lines = data.flagged.map((c) => `${c.a.label} + ${c.b.label} = ${c.result.label}${c.note ? " — " + c.note : ""}`);
    $("flagged-list").replaceChildren(...lines.map((text) => Object.assign(document.createElement("li"), { textContent: text })));
    flaggedText = lines.join("\n");
    $("copy").disabled = lines.length === 0;
  } catch (_) { /* the queue shows the error */ }
}

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const el = document.activeElement;
  if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) return;
  if (e.key === "ArrowRight") { e.preventDefault(); answer("ok"); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); answer("flag"); }
  else if (e.key === "ArrowDown" || e.key === "s" || e.key === "S") { e.preventDefault(); skip(); }
  else if (e.key === "z" || e.key === "Z") { e.preventDefault(); undo(); }
});
$("ok").addEventListener("click", () => answer("ok"));
$("flag").addEventListener("click", () => answer("flag"));
$("undo").addEventListener("click", undo);
$("skip").addEventListener("click", skip);
$("copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(flaggedText); $("copy").textContent = "Copied"; }
  catch (_) { $("copy").textContent = "Copy failed"; }
  setTimeout(() => { $("copy").textContent = "Copy list"; }, 1500);
});

loadPictureMap(() => { if (deck.length) render(); });
refresh();
