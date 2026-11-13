"use strict";
// The Guides page (/guides): every tutorial on the site, opened on demand. A guide lives on the page it explains and is opened
// there with ?guide=<name> (see takeGuide in auth.js), so its words are written once, next to what they describe. Guides for
// designers and administrators show only to them. "Seen" comes from the same browser storage each guide uses to open itself only
// the first time; "Show every guide again" clears it.
const $ = (id) => document.getElementById(id);

if (!Auth.loggedIn()) Auth.toLogin();

const GUIDES = [
  {
    name: "hints", title: "💡 Hints", who: "Everyone",
    about: "The three kinds of hint in your solo game, what a hint costs, and how to earn more with daily-challenge streaks.",
    where: "Opens in your solo game.",
    seenKey: () => `alchemy-hint-guide:${Auth.user().toLowerCase()}`, seenValue: "1",
    show: () => true,
    open: async () => {
      const { url } = await Auth.fetch("/solo/start", {});   // your private game: the one running, or carried on from before
      location.href = `${url}?guide=hints`;
    },
  },
  {
    name: "designer", title: "🎨 Designer picker", who: "Designers",
    about: "Select any part of a page, change its fill, border, shadow, text, font, size, spacing or corner radius (with a live contrast check and undo), and copy the changes for a developer. Also how design patches are switched.",
    where: "Opens on the home page, with the picker switched on.",
    seenKey: () => "alchemy-designer-tour", seenValue: "done",
    show: (me) => me.designer,
    open: () => { location.href = "/?guide=designer"; },
  },
  {
    name: "icons", title: "🖌️ Icon studio", who: "Designers and administrators",
    about: "Drawing an element's icon with shapes and pixels, checking it at game size, redrawing old icons, and proposing them.",
    where: "Opens in the Icon studio.",
    seenKey: () => "alchemy-icon-tour", seenValue: "done",
    show: (me) => me.designer || me.admin,
    open: () => { location.href = "/icons?guide=icons"; },
  },
];

function seen(guide) {
  try { return localStorage.getItem(guide.seenKey()) === guide.seenValue; } catch (_) { return false; }
}

function render(me) {
  const list = $("guides-list");
  list.replaceChildren();
  for (const guide of GUIDES.filter((g) => g.show(me))) {
    const card = document.createElement("article");
    card.className = "guide-card";
    card.dataset.guide = guide.name;
    const head = document.createElement("div");
    head.className = "guide-head";
    const title = document.createElement("h2");
    title.textContent = guide.title;
    const state = document.createElement("span");
    state.className = "pill" + (seen(guide) ? " muted" : "");
    state.textContent = seen(guide) ? "seen" : "new";
    head.append(title, state);
    const about = document.createElement("p");
    about.textContent = guide.about;
    const meta = document.createElement("p");
    meta.className = "muted";
    meta.textContent = `For: ${guide.who}. ${guide.where}`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "primary";
    button.textContent = seen(guide) ? "Open again" : "Open";
    button.addEventListener("click", async () => {
      button.disabled = true;
      $("guides-error").textContent = "";
      try { await guide.open(); } catch (err) { $("guides-error").textContent = err.message; button.disabled = false; }
    });
    card.append(head, about, meta, button);
    list.append(card);
  }
}

async function start() {
  let me = { admin: false, designer: false };
  try { me = await Auth.fetch("/me"); } catch (_) { /* the player's guides still show */ }
  render(me);
  $("guides-reset").addEventListener("click", () => {
    for (const guide of GUIDES) {
      try { localStorage.removeItem(guide.seenKey()); } catch (_) { /* nothing to clear */ }
    }
    $("guides-reset-done").hidden = false;
    render(me);
  });
}
start();
