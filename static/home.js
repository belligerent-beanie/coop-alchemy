"use strict";

const $ = (id) => document.getElementById(id);
const POLL_MS = 3000;

if (!Auth.token()) Auth.toLogin();
$("who").textContent = Auth.user();
$("logout").addEventListener("click", () => Auth.logout());

let maxGames = 3;
let running = 0;

function line(game) {
  const parts = [`${game.players}/${game.max_players} players`, `found ${game.found}/${game.total}`];
  const genie = game.genie;
  if (genie && genie.available && genie.unlocked) {
    parts.push(`🧞 ${genie.left} wish${genie.left === 1 ? "" : "es"} left`);
  }
  return parts.join(" · ");
}

function card(game, label = "Join") {
  const li = document.createElement("li");
  li.className = "game-card";
  const info = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = game.name;
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = line(game);
  info.append(name, meta);

  const full = game.players >= game.max_players;
  const action = document.createElement(full ? "span" : "a");
  action.className = "btn" + (full ? " disabled" : "");
  action.textContent = full ? "Full" : label;
  if (!full) action.href = `/g/${encodeURIComponent(game.id)}`;
  li.append(info, action);
  return li;
}

async function refresh() {
  try {
    const data = await Auth.fetch("/games");
    maxGames = data.max_games;
    const games = data.games.filter((g) => !g.private);  // your daily challenge attempt is not a game to join
    running = games.length;
    const recent = games.filter((g) => g.recent);
    $("recent").replaceChildren(...recent.map((g) => card(g, "Rejoin")));
    $("recent-section").hidden = recent.length === 0;
    $("games").replaceChildren(...games.filter((g) => !g.recent).map((g) => card(g)));
    $("no-games").hidden = running > 0;
    $("game-count").textContent = `(${running} of ${maxGames})`;
    const full = running >= maxGames;
    $("create").disabled = full;
    if (full && !$("create-error").textContent) {
      $("create-error").textContent = `${maxGames} games are already running. Join one, or wait for one to end.`;
    } else if (!full && $("create-error").dataset.full) {
      $("create-error").textContent = "";
    }
    $("create-error").dataset.full = full ? "1" : "";
  } catch (_) { /* server unreachable: try again on the next poll */ }
}

$("new-game").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("create-error").textContent = "";
  $("create-error").dataset.full = "";
  try {
    const data = await Auth.fetch("/games", { name: $("new-name").value });
    location.href = data.url;
  } catch (err) {
    $("create-error").textContent = err.message;
    refresh();
  }
});

function savedCard(game) {
  const li = document.createElement("li");
  li.className = "game-card";
  const info = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = game.name;
  const meta = document.createElement("span");
  meta.className = "meta";
  const hours = Math.max(1, Math.round(game.expires_in / 3600));
  const days = hours >= 48 ? `${Math.round(hours / 24)} days` : `${hours} hour${hours === 1 ? "" : "s"}`;
  meta.textContent = `found ${game.found} · players: ${game.players.join(", ") || "none"} · kept for ${days} more`;
  info.append(name, meta);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn";
  button.textContent = "Resume";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const data = await Auth.fetch("/games/resume", { id: game.id });
      location.href = data.url;
    } catch (err) {
      $("saved-msg").textContent = err.message;
      button.disabled = false;
    }
  });
  li.append(info, button);
  return li;
}

async function loadSaved() {
  try {
    const data = await Auth.fetch("/saved");
    $("saved-games").replaceChildren(...data.games.map(savedCard));
    $("no-saved").hidden = data.games.length > 0;
  } catch (_) { /* shown again on the next refresh */ }
}

// ---- the daily challenge ----

async function loadDaily() {
  try {
    const d = await Auth.fetch("/daily");
    $("daily").hidden = false;
    $("daily-date").textContent = d.date;
    $("daily-target").textContent = `${d.target.emoji} ${d.target.label}`.trim();
    const left = d.attempts_max - d.attempts_used;
    const parts = [];
    if (d.reference) parts.push(`A good route unlocks ${d.reference} elements. Can you beat it?`);
    parts.push(d.best === null ? "You haven't finished it today." : `Your best today: ${d.best}.`);
    parts.push(left > 0
      ? `Starting a new attempt uses one of your ${d.attempts_max} a day (${left} left).`
      : `You have used all ${d.attempts_max} of today's attempts.`);
    $("daily-meta").textContent = parts.join(" ");
    const play = $("daily-play");
    play.disabled = !d.running && left <= 0;
    play.textContent = d.running ? "Continue" : left <= 0 ? "No attempts left" : d.best === null ? "Play" : "Play again";
    $("daily-restart").hidden = !d.running;
    $("daily-restart").disabled = left <= 0;
    $("daily-restart").title = left > 0 ? "Drop this attempt and start a new one (uses an attempt)" : "No attempts left today";
    $("daily-board").replaceChildren(...d.board.map((row) => {
      const li = document.createElement("li");
      li.textContent = `${row.name}: ${row.score} elements`;
      if (row.name.toLowerCase() === Auth.user().toLowerCase()) li.className = "me";
      return li;
    }));
    $("daily-empty").hidden = d.board.length > 0;
    $("daily-error").textContent = "";
  } catch (_) { /* the section stays as it was; the next refresh tries again */ }
}

async function startDaily(fresh) {
  $("daily-error").textContent = "";
  try {
    location.href = (await Auth.fetch("/daily/start", { fresh })).url;
  } catch (err) {
    $("daily-error").textContent = err.message;
  }
}
$("daily-play").addEventListener("click", () => startDaily(false));
$("daily-restart").addEventListener("click", () => startDaily(true));
loadDaily();
setInterval(loadDaily, 15000);
window.addEventListener("pageshow", loadDaily);  // coming back with the browser's back button

refresh();
loadSaved();
setInterval(refresh, POLL_MS);
setInterval(loadSaved, 15000); // a game that is resumed or expires drops off the list

// Admins get a link to the promotion requests, with how many are waiting.
async function loadAdmin() {
  try {
    const me = await Auth.fetch("/me");
    if (!me.admin) return;
    $("admin-link").hidden = false;
    const data = await Auth.fetch("/admin/queue");
    $("admin-pending").textContent = data.proposals.length;
    $("admin-pending").hidden = data.proposals.length === 0;
  } catch (_) { /* the link just stays hidden */ }
}
loadAdmin();
setInterval(loadAdmin, 30000);
