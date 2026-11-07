"use strict";
// The journal: every element the room has discovered, what it was made from, and what it makes.
// It only lists recipes whose ingredients and result have all been discovered (the server decides).
// The Collection tab draws every element of the game: found ones as tiles, the rest as "?" silhouettes. The server sends
// only how many are still to find, never which, so a silhouette gives nothing away.
// Uses `$`, `api`, `elements`, `me` and `toast` from app.js, which is loaded first.

const journalDialog = $("journal-dialog");
const journalList = $("journal-list");
const journalDetail = $("journal-detail");
const journalEntries = new Map();   // element id -> {id, starter, by, made_from, used_in}
let journalSelected = null;
let journalProgress = { found: 0, total: 0, locked: 0 };
let journalTab = "list";

function journalLabel(id) {
  const el = elements.get(id);
  return el ? `${el.emoji} ${el.label}`.trim() : "?";
}

/** A button that jumps to another element in the journal. */
function journalLink(id) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = "journal-chip";
  node.textContent = journalLabel(id);
  node.addEventListener("click", () => selectJournalElement(id));
  return node;
}

function journalSection(title, rows, emptyText) {
  const section = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.appendChild(heading);
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = emptyText;
    section.appendChild(empty);
    return section;
  }
  const list = document.createElement("ul");
  for (const row of rows) {
    const item = document.createElement("li");
    item.append(...row);
    list.appendChild(item);
  }
  section.appendChild(list);
  return section;
}

const textNode = (value) => document.createTextNode(value);

function renderJournalDetail() {
  const entry = journalEntries.get(journalSelected);
  if (!entry) {
    journalDetail.replaceChildren(textNode("Nothing discovered yet."));
    return;
  }
  const title = document.createElement("h2");
  title.textContent = journalLabel(entry.id);
  if (entry.rarity && entry.rarity !== "common") {
    const pill = document.createElement("span");
    pill.className = `rarity-pill ${entry.rarity}`;
    pill.textContent = entry.rarity;
    title.appendChild(pill);
  }
  const origin = document.createElement("p");
  origin.className = "muted";
  origin.textContent = entry.starter ? "A starting element"
    : entry.by === "" ? "Unlocked for free"
    : entry.by ? `Discovered by ${entry.by}` : "";

  const madeFrom = journalSection(
    "Made from",
    entry.made_from.map(([a, b]) => [journalLink(a), textNode(" + "), journalLink(b)]),
    entry.starter ? "It was there from the start." : "No fully discovered recipe yet.",
  );
  const usedIn = journalSection(
    "Used to make",
    entry.used_in.map(([partner, result]) => [textNode("with "), journalLink(partner), textNode(" → "), journalLink(result)]),
    "Nothing discovered yet.",
  );
  journalDetail.replaceChildren(title, origin, madeFrom, usedIn);
}

function renderJournalList() {
  const wanted = $("journal-filter").value.trim().toLowerCase();
  const nodes = [];
  for (const entry of journalEntries.values()) {
    const el = elements.get(entry.id);
    if (!el || (wanted && !el.label.toLowerCase().includes(wanted))) continue;
    const node = document.createElement("button");
    node.type = "button";
    node.className = "journal-entry" + (entry.id === journalSelected ? " selected" : "");
    node.setAttribute("role", "option");
    node.setAttribute("aria-selected", String(entry.id === journalSelected));
    node.textContent = journalLabel(entry.id);
    node.addEventListener("click", () => selectJournalElement(entry.id));
    nodes.push(node);
  }
  if (!nodes.length) {
    const none = document.createElement("p");
    none.className = "muted";
    none.textContent = "No element matches.";
    nodes.push(none);
  }
  journalList.replaceChildren(...nodes);
}

/** The progress bar and count under the title. */
function renderJournalProgress() {
  const { found, total } = journalProgress;
  $("journal-count").textContent = total ? `${found} / ${total} found` : "";
  $("journal-bar-fill").style.width = total ? `${Math.min(100, (found / total) * 100)}%` : "0%";
  const bar = document.querySelector(".journal-bar");
  bar.setAttribute("aria-valuemax", String(total || 1));
  bar.setAttribute("aria-valuenow", String(found));
}

/** The Collection tab: found elements (filtered by who found them), then one silhouette per element still to find. */
function renderJournalGrid() {
  const filter = $("journal-grid-filter").value;
  const mine = (me.name || "").toLowerCase();
  const tiles = [];
  if (filter !== "locked") {
    for (const entry of journalEntries.values()) {
      const by = (entry.by || "").toLowerCase();
      const byMe = by !== "" && by === mine;
      if (filter === "mine" && !byMe) continue;
      if (filter === "others" && (byMe || entry.starter || entry.by === "")) continue;
      const tile = document.createElement("button");
      tile.type = "button";
      tile.className = "collection-tile";
      tile.dataset.rarity = entry.rarity || "common";
      tile.setAttribute("role", "listitem");
      tile.textContent = journalLabel(entry.id);
      tile.title = entry.starter ? "A starting element"
        : entry.by === "" ? "Unlocked for free" : entry.by ? `Discovered by ${entry.by}` : "";
      tile.addEventListener("click", () => {
        showJournalTab("list");
        selectJournalElement(entry.id);
      });
      tiles.push(tile);
    }
  }
  if (filter === "all" || filter === "locked") {
    for (let i = 0; i < journalProgress.locked; i++) {
      const slot = document.createElement("div");
      slot.className = "collection-tile locked";
      slot.setAttribute("role", "listitem");
      slot.setAttribute("aria-label", "Not discovered yet");
      slot.textContent = "?";
      tiles.push(slot);
    }
  }
  if (!tiles.length) {
    const none = document.createElement("p");
    none.className = "muted";
    none.textContent = filter === "locked" ? "You have found everything. Well done!" : "Nothing here yet.";
    tiles.push(none);
  }
  $("journal-grid").replaceChildren(...tiles);
}

function showJournalTab(tab) {
  journalTab = tab;
  const grid = tab === "grid";
  $("journal-list-view").hidden = grid;
  $("journal-grid-view").hidden = !grid;
  for (const [id, active] of [["journal-tab-list", !grid], ["journal-tab-grid", grid]]) {
    $(id).classList.toggle("selected", active);
    $(id).setAttribute("aria-selected", String(active));
  }
  if (grid) renderJournalGrid();
}

function selectJournalElement(id) {
  if (!journalEntries.has(id)) return;
  journalSelected = id;
  $("journal-filter").value = "";
  renderJournalList();
  renderJournalDetail();
  journalList.querySelector(".selected")?.scrollIntoView({ block: "nearest" });
}

/** Fetches the journal and redraws it (the server lists elements alphabetically). */
async function loadJournal() {
  const data = await api("/journal");
  journalEntries.clear();
  for (const entry of data.elements) journalEntries.set(entry.id, entry);
  journalProgress = data.progress;
  if (!journalEntries.has(journalSelected)) journalSelected = data.elements[0]?.id ?? null;
  renderJournalProgress();
  renderJournalList();
  renderJournalDetail();
  if (journalTab === "grid") renderJournalGrid();
}

/** Called by app.js when something is discovered: keeps an open journal up to date. */
function refreshJournal() {
  if (journalDialog.open) loadJournal().catch(() => { /* the next discovery tries again */ });
}

/** Opens the journal on one element's page (a long press on a phone, a right-click on a laptop; see app.js). */
async function openJournalFor(id) {
  try {
    await loadJournal();
    showJournalTab("list");
    selectJournalElement(id);
    $("journal-detail").scrollTop = 0;
    if (!journalDialog.open) journalDialog.showModal();
  } catch (err) {
    toast(err.message, "error");
  }
}

$("journal-open").addEventListener("click", async () => {
  try {
    await loadJournal();
    journalDialog.showModal();
  } catch (err) {
    toast(err.message, "error");
  }
});
$("journal-close").addEventListener("click", () => journalDialog.close());
$("journal-filter").addEventListener("input", renderJournalList);
$("journal-tab-list").addEventListener("click", () => showJournalTab("list"));
$("journal-tab-grid").addEventListener("click", () => showJournalTab("grid"));
$("journal-grid-filter").addEventListener("change", renderJournalGrid);
journalDialog.addEventListener("click", (e) => {
  if (e.target === journalDialog) journalDialog.close();   // a click on the backdrop
});
