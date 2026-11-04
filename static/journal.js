"use strict";
// The journal: every element the room has discovered, what it was made from, and what it makes.
// It only lists recipes whose ingredients and result have all been discovered (the server decides).
// Uses `$`, `api`, `elements` and `toast` from app.js, which is loaded first.

const journalDialog = $("journal-dialog");
const journalList = $("journal-list");
const journalDetail = $("journal-detail");
const journalEntries = new Map();   // element id -> {id, starter, by, made_from, used_in}
let journalSelected = null;

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
  if (!journalEntries.has(journalSelected)) journalSelected = data.elements[0]?.id ?? null;
  renderJournalList();
  renderJournalDetail();
}

/** Called by app.js when something is discovered: keeps an open journal up to date. */
function refreshJournal() {
  if (journalDialog.open) loadJournal().catch(() => { /* the next discovery tries again */ });
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
journalDialog.addEventListener("click", (e) => {
  if (e.target === journalDialog) journalDialog.close();   // a click on the backdrop
});
