"use strict";

// Admin page: review proposals one at a time, swipe style. Right arrow accepts, left arrow rejects.

const $ = (id) => document.getElementById(id);
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

let deck = [];          // proposals still to review; deck[0] is on screen
const history = [];     // answers this session, newest last, for undo
let busy = false;
let shownId = null;     // proposal currently on screen
let snippetText = "";

if (!Auth.loggedIn()) Auth.toLogin();

function emojiSpan(el) {
  const span = document.createElement("span");
  span.className = "emoji";
  span.textContent = el.emoji || el.label.slice(0, 1).toUpperCase();
  return span;
}

function face(node, el) {
  node.classList.toggle("unreleased", el.draft);
  node.querySelector(".name").textContent = el.label;
  const art = node.querySelector(".art");
  art.replaceChildren(emojiSpan(el));
  applyImage(node, el);
}

function tag(parent, text, cls) {
  const span = document.createElement("span");
  span.className = "tag " + (cls || "");
  span.textContent = text;
  parent.appendChild(span);
}

function warn(text) {
  const div = document.createElement("div");
  div.className = "competing";
  div.textContent = text;
  return div;
}

function renderDependents(p) {
  const d = p.dependents;
  $("dep-title").textContent = d.total
    ? `Depends on ${p.result.label} (${d.total})`
    : `Nothing depends on ${p.result.label} yet`;
  const items = d.elements.map((e) => {
    const li = document.createElement("li");
    li.className = "chip";
    li.title = `${e.via} (${e.status}${e.user ? ", " + e.user : ""})`;
    const art = document.createElement("span");
    art.className = "art";
    art.appendChild(emojiSpan(e));
    li.appendChild(art);
    applyImage(li, e);
    const name = document.createElement("span");
    name.textContent = e.label;
    li.appendChild(name);
    const via = document.createElement("small");
    via.textContent = e.via;
    li.appendChild(via);
    return li;
  });
  if (d.total > d.elements.length) {
    const more = document.createElement("li");
    more.className = "chip more";
    more.textContent = `+${d.total - d.elements.length} more`;
    items.push(more);
  }
  $("dep-list").replaceChildren(...items);
}

function render() {
  const p = deck[0];
  $("swipe").hidden = !p;
  $("empty").hidden = !!p;
  $("undo").disabled = history.length === 0 || busy;
  if (!p) { shownId = null; return; }
  face($("face-a"), p.a);
  face($("face-b"), p.b);
  face($("face-c"), p.result);
  $("left").textContent = `${deck.length} left`;
  const tags = $("tags");
  tags.replaceChildren();
  if (p.new_element) tag(tags, "new element");
  for (const n of p.needs) tag(tags, `needs ${n.label}: ${n.status === "pending" ? "also waiting" : "not proposed"}`, "warn");
  $("credit").textContent = `Proposed by ${p.user}`;
  $("note").textContent = p.note ? `\u201c${p.note}\u201d` : "";
  $("competing").replaceChildren(...p.competing.map((c) => warn(c.same_result
    ? `${c.user} also proposed this result (${c.status})`
    : `${c.user} proposed a different result for this pair: ${c.result_label} (${c.status})`)));
  if (shownId !== p.id) $("reply").value = "";   // keep a half-typed reply across refreshes
  shownId = p.id;
  renderDependents(p);
}

function fly(direction) {
  const card = $("card");
  if (reduceMotion) return Promise.resolve();
  card.classList.add(direction > 0 ? "fly-right" : "fly-left");
  return new Promise((resolve) => setTimeout(() => {
    card.classList.remove("fly-right", "fly-left");
    resolve();
  }, 180));
}

async function answer(action) {
  const p = deck[0];
  if (!p || busy) return;
  busy = true;
  $("admin-msg").textContent = "";
  try {
    await Auth.fetch("/admin/handle", { id: p.id, action, reply: $("reply").value });
    await fly(action === "approve" ? 1 : -1);
    deck.shift();
    history.push({ proposal: p, action });
    loadApproved();
    setTimeout(loadQueue, 0);
  } catch (err) {
    $("admin-msg").textContent = err.message;
    if (err.status === 409) deck.shift();   // someone else answered it
  } finally {
    busy = false;
    render();
  }
}

/** Put this proposal at the back of the deck without answering it; it stays waiting for any admin. */
function skip() {
  if (busy || deck.length === 0) return;
  if (deck.length === 1) {
    $("admin-msg").textContent = "This is the only proposal waiting.";
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
    await Auth.fetch("/admin/reopen", { id: last.proposal.id });
    history.pop();
    deck.unshift(last.proposal);
    loadApproved();
    setTimeout(loadQueue, 0);
  } catch (err) {
    $("admin-msg").textContent = err.message;
  } finally {
    busy = false;
    render();
  }
}

async function loadQueue() {
  try {
    const data = await Auth.fetch("/admin/queue");
    const fresh = new Map(data.proposals.map((p) => [p.id, p]));
    const kept = deck.filter((p) => fresh.has(p.id)).map((p) => fresh.get(p.id));
    const known = new Set(kept.map((p) => p.id));
    deck = kept.concat(data.proposals.filter((p) => !known.has(p.id)));
    if (!busy) render();
  } catch (err) {
    deck = [];
    render();
    $("swipe").hidden = true;
    $("empty").hidden = true;
    $("admin-msg").textContent = err.status === 403 ? "Only admins can see this page." : err.message;
  }
}

async function loadApproved() {
  try {
    const data = await Auth.fetch("/admin/approved");
    $("approved-count").textContent = data.proposals.length;
    $("approved-list").replaceChildren(...data.proposals.map((p) => {
      const li = document.createElement("li");
      li.textContent = `${p.a.label} + ${p.b.label} = ${p.result.label} (${p.user})`;
      return li;
    }));
    snippetText = JSON.stringify(data.snippet, null, 2);
    $("snippet").textContent = snippetText;
    $("copy").disabled = data.proposals.length === 0;
  } catch (_) { /* the queue shows the error */ }
}

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const el = document.activeElement;
  if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) return;
  if (e.key === "ArrowRight") { e.preventDefault(); answer("approve"); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); answer("reject"); }
  else if (e.key === "ArrowDown" || e.key === "s" || e.key === "S") { e.preventDefault(); skip(); }
  else if (e.key === "z" || e.key === "Z") { e.preventDefault(); undo(); }
});
$("accept").addEventListener("click", () => answer("approve"));
$("reject").addEventListener("click", () => answer("reject"));
$("undo").addEventListener("click", undo);
$("skip").addEventListener("click", skip);
$("copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(snippetText); $("copy").textContent = "Copied"; }
  catch (_) { $("snippet").hidden = false; $("copy").textContent = "Select the text below"; }
  setTimeout(() => { $("copy").textContent = "Copy snippet"; }, 1500);
});

loadPictureMap(() => { if (deck.length) render(); });
loadQueue();
loadApproved();
Net.poll(() => { if (!busy) return loadQueue(); }, 15000);
