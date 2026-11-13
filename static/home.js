"use strict";

const $ = (id) => document.getElementById(id);
const POLL_MS = 3000;

if (!Auth.loggedIn()) Auth.toLogin();
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
    paintMultiplayer(data.multiplayer);
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

const openBoards = new Set();   // difficulties whose "Today's best" list is open: kept across refreshes
let dailyData = null;

const nameOf = (el) => `${el.emoji} ${el.label}`.trim();

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function routeList(route, heading) {
  const box = el("div", "route");
  box.append(el("h4", "", heading));
  const list = el("ol");
  list.append(...route.map(routeItem));
  box.append(list);
  return box;
}

function tierBlock(d, t) {
  const info = TIER_INFO[t.tier];
  const left = d.attempts_max - t.attempts_used;
  const box = el("div", "tier");

  const head = el("div", "tier-head");
  head.append(el("strong", "", `${info.icon} ${info.label}`), el("span", "tier-target", nameOf(t.target)));
  const meta = el("p", "muted", [
    `A good route unlocks ${t.reference} elements.`,
    t.best === null ? "Not finished yet." : `Your best: ${t.best}.`,
    left > 0 ? `${left} of ${d.attempts_max} attempts left.` : "No attempts left.",
  ].join(" "));

  const row = el("p", "home-links");
  const play = el("button", "btn", t.running ? "Continue" : left <= 0 ? "No attempts left" : t.best === null ? "Play" : "Play again");
  play.type = "button";
  play.disabled = !t.running && left <= 0;
  play.addEventListener("click", () => startDaily(t.tier, false));
  row.append(play);
  if (t.running) {
    const again = el("button", "btn", "Start over");
    again.type = "button";
    again.disabled = left <= 0;
    again.title = left > 0 ? "Drop this attempt and start a new one (uses an attempt)" : "No attempts left today";
    again.addEventListener("click", () => startDaily(t.tier, true));
    row.append(again);
  }
  if (!t.route) {
    const reveal = el("button", "link", "Give up and show the route");
    reveal.type = "button";
    reveal.addEventListener("click", () => revealRoute(t.tier));
    row.append(reveal);
  }

  const board = el("details", "tier-board");
  board.dataset.tier = t.tier;
  board.open = openBoards.has(t.tier);
  board.addEventListener("toggle", () => (board.open ? openBoards.add(t.tier) : openBoards.delete(t.tier)));
  board.append(el("summary", "", "Today's best"));
  if (t.board.length) {
    const list = el("ol", "daily-board");
    list.append(...t.board.map((r) => {
      const li = el("li", r.name.toLowerCase() === Auth.user().toLowerCase() ? "me" : "", `${r.name}: ${r.score} elements`);
      return li;
    }));
    board.append(list);
  } else {
    board.append(el("p", "muted", "Nobody has finished this one yet."));
  }

  box.append(head, meta, row, board);
  if (t.route) box.append(routeList(t.route, `Reference route (${t.reference} elements)`));
  return box;
}

async function loadDaily() {
  try {
    const d = await Auth.fetch("/daily");
    dailyData = d;
    $("daily").hidden = false;
    $("daily-date").textContent = d.date;
    const streak = $("daily-streak");
    streak.hidden = d.streak.current < 1;
    streak.textContent = `🔥 ${d.streak.current}-day streak` + (d.streak.best > d.streak.current ? ` (best ${d.streak.best})` : "");
    $("daily-tiers").replaceChildren(...d.tiers.map((t) => tierBlock(d, t)));
    $("daily-share").disabled = d.tiers.every((t) => t.best === null);
    $("week-label").textContent = d.week.label;
    $("week-board").replaceChildren(...d.week.board.map((r) => el(
      "li", r.name.toLowerCase() === Auth.user().toLowerCase() ? "me" : "",
      `${r.name}: ${r.points} points (${r.finishes} finished over ${r.days} day${r.days === 1 ? "" : "s"})`)));
    $("week-empty").hidden = d.week.board.length > 0;
    $("daily-error").textContent = "";
  } catch (_) { /* the section stays as it was; the next refresh tries again */ }
}

async function startDaily(tier, fresh) {
  $("daily-error").textContent = "";
  try {
    location.href = (await Auth.fetch("/daily/start", { tier, fresh })).url;
  } catch (err) {
    $("daily-error").textContent = err.message;
    loadDaily();
  }
}

async function revealRoute(tier) {
  const label = TIER_INFO[tier].label.toLowerCase();
  if (!window.confirm(`Show the reference route for today's ${label} challenge?\n\nThis uses up your remaining ${label} attempts for today.`)) return;
  try {
    await Auth.fetch("/daily/reveal", { tier });
  } catch (err) {
    $("daily-error").textContent = err.message;
  }
  loadDaily();
}

$("daily-share").addEventListener("click", async () => {
  if (!dailyData) return;
  const lines = dailyData.tiers.map((t) => resultLine(t.tier, t.best, t.reference));
  const ok = await copyText(shareText(dailyData.date, lines, dailyData.streak.current));
  $("daily-share-msg").textContent = ok ? "Copied! Paste it to your friends." : "Could not copy on this page.";
  setTimeout(() => { $("daily-share-msg").textContent = ""; }, 4000);
});

// Past days load when the list is first opened (and each time it is opened again).
function historyDay(day) {
  const box = el("details", "hist-day");
  const marks = Object.entries(day.tiers).map(([tier, t]) => `${TIER_INFO[tier].icon} ${t.best === null ? "–" : t.best}`);
  box.append(el("summary", "", `${day.date} · ${marks.join(" · ")}`));
  for (const [tier, t] of Object.entries(day.tiers)) {
    const info = TIER_INFO[tier];
    const winner = t.winner ? `${t.winner.name} (${t.winner.score})` : "nobody";
    box.append(el("p", "hist-line", `${info.icon} ${info.label}: ${nameOf(t.target)}. Reference ${t.reference}. You: ${t.best === null ? "not finished" : t.best}. Best: ${winner}.`));
    if (t.route.length) {
      const route = el("details", "hist-route");
      route.append(el("summary", "", "Reference route"));
      const list = el("ol");
      list.append(...t.route.map(routeItem));
      route.append(list);
      box.append(route);
    }
  }
  return box;
}

$("daily-history").addEventListener("toggle", async () => {
  if (!$("daily-history").open) return;
  try {
    const data = await Auth.fetch("/daily/history");
    $("history-body").replaceChildren(...(data.days.length ? data.days.map(historyDay) : [el("p", "muted", "No past days yet.")]));
  } catch (err) {
    $("history-body").textContent = err.message;
  }
});

// ---- races ----------------------------------------------------------------------

function raceGoalText(r) {
  return r.goal === "target" ? `reach ${nameOf(r.target)}` : `reach ${r.goal_count} elements`;
}

async function loadRaces() {
  try {
    const d = await Auth.fetch("/races");
    $("race-mine").hidden = !d.mine;
    if (d.mine) $("race-mine-link").href = `/race/${d.mine}`;
    $("race-form").hidden = !!d.mine;
    $("race-empty").hidden = d.races.length > 0;
    $("race-lobbies").replaceChildren(...d.races.map((r) => {
      const item = el("li", "game");
      item.append(el("span", "", `${r.host}'s race: first to ${raceGoalText(r)} (${r.racers}/${r.max_racers})`));
      if (!d.mine && r.racers < r.max_racers) {
        const join = el("button", "btn", "Join");
        join.type = "button";
        join.addEventListener("click", async () => {
          try {
            await Auth.fetch(`/races/${r.id}/join`, {});
            location.href = `/race/${r.id}`;
          } catch (err) {
            $("race-error").textContent = err.message;
            loadRaces();
          }
        });
        item.append(" ", join);
      }
      return item;
    }));
  } catch (_) { /* try again on the next poll */ }
}

$("race-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("race-error").textContent = "";
  try {
    const room = await Auth.fetch("/races", { goal: $("race-goal").value });
    location.href = `/race/${room.id}`;
  } catch (err) {
    $("race-error").textContent = err.message;
  }
});

loadRaces();
Net.poll(loadRaces, POLL_MS);
window.addEventListener("pageshow", loadRaces);

loadDaily();
Net.poll(loadDaily, 15000);

/** The profile card: the player's own numbers, with nothing to compare them against. */
async function loadProfile() {
  try {
    const p = await Auth.fetch("/profile");
    const s = p.stats;
    if ($("profile").hidden) $("profile").open = window.innerWidth >= 700;   // open on a laptop, a one-line summary on a phone
    $("profile").hidden = false;
    $("profile-since").textContent = p.member_since
      ? `· here since ${new Date(p.member_since * 1000).toLocaleDateString(undefined, { month: "long", year: "numeric" })}` : "";
    const tile = (value, label) => {
      const box = el("div", "stat");
      box.append(el("strong", "", String(value)), el("span", "", label));
      return box;
    };
    $("profile-stats").replaceChildren(
      tile(s.shared_finds, "elements found with others"),
      tile(s.shared_games, "games with others"),
      tile(p.solo.total ? `${p.solo.found} / ${p.solo.total}` : "0", "found in Solo"),
      tile(s.daily_done, s.best_streak ? `daily challenges (best run ${s.best_streak})` : "daily challenges"),
      tile(s.races_done, s.race_wins ? `races (${s.race_wins} won)` : "races"),
      tile(`${p.achievements.earned} / ${p.achievements.total}`, "achievements"),
    );
    const more = [];
    if (s.struck) more.push(`Struck by Mr Alchemy ${s.struck} time${s.struck === 1 ? "" : "s"}`);
    if (s.epic_finds) more.push(`${s.epic_finds} epic find${s.epic_finds === 1 ? "" : "s"}`);
    if (s.emotes) more.push(`${s.emotes} emote${s.emotes === 1 ? "" : "s"} sent`);
    $("profile-more").textContent = more.join(" · ");
    $("profile-more").hidden = more.length === 0;
    $("profile-recent").textContent = p.achievements.recent.length
      ? "Latest: " + p.achievements.recent.map((a) => `${a.icon} ${a.title}`).join(" · ") : "No achievements yet: your first is a few moves away.";
  } catch (_) { /* the rest of the page still works */ }
}

/** An achievements list: the earned ones with their icon, the rest locked, and "earned / all" beside the heading. */
function renderAchievements(listId, countId, items) {
  const earned = items.filter((a) => a.earned_at !== null).length;
  $(countId).textContent = `${earned} / ${items.length}`;
  $(listId).replaceChildren(...items.map((a) => {
    const row = el("li", a.earned_at === null ? "achievement locked" : "achievement");
    row.append(el("span", "ach-icon", a.earned_at === null ? "🔒" : a.icon));
    const text = el("span", "ach-text");
    text.append(el("strong", "", a.title), el("span", "muted", ` ${a.description}`));
    row.append(text);
    return row;
  }));
}

/** The achievements of the Multiplayer and Challenges tabs (the Solo tab gets its own with its progress). */
async function loadAchievements() {
  try {
    const all = await Auth.fetch("/achievements");
    renderAchievements("achievements-multiplayer", "achievement-count-multiplayer", all.multiplayer);
    renderAchievements("achievements-challenges", "achievement-count-challenges", all.challenges);
  } catch (_) { /* the rest of the page still works */ }
}

// ---- solo mode ----
async function loadSolo() {
  try {
    const s = await Auth.fetch("/solo");
    $("solo").hidden = false;
    $("solo-count").textContent = s.total ? `${s.found} / ${s.total} found${s.complete ? " ✓" : ""}` : "";
    $("solo-bar-fill").style.width = s.total ? `${Math.min(100, (s.found / s.total) * 100)}%` : "0%";
    const bar = document.querySelector("#solo .journal-bar");
    bar.setAttribute("aria-valuemax", String(s.total || 1));
    bar.setAttribute("aria-valuenow", String(s.found));
    $("solo-start").textContent = s.found > 4 ? "Continue solo" : "Play solo";
    $("solo-hints-count").textContent = String(s.hints);
    $("solo-hints-word").textContent = s.hints === 1 ? "hint" : "hints";
    $("start-here").hidden = s.found > 0;   // a player who has not found anything yet gets the big "Start playing" card
    paintMultiplayer(s.multiplayer);
    renderAchievements("achievements", "achievement-count", s.achievements);
    $("create-link").textContent = s.may_create ? "Create recipes" : "Create recipes 🔒";
    if (!s.may_create) $("create-blurb").textContent = "Finish Solo mode (find every element) to unlock a playground where you can invent new combinations and promote them to the admins.";
  } catch (_) { /* the rest of the page still works */ }
}
async function startSolo(errorBox) {
  errorBox.textContent = "";
  try {
    location.href = (await Auth.fetch("/solo/start", {})).url;
  } catch (err) {
    errorBox.textContent = err.message;
  }
}
$("solo-start").addEventListener("click", () => startSolo($("solo-error")));
$("hero-start").addEventListener("click", () => startSolo($("hero-error")));

/** Shows Multiplayer as locked (with progress) or open; `mp` is {unlocked, earned, needed} from the server. */
function paintMultiplayer(mp) {
  if (!mp) return;
  $("mp-locked").hidden = mp.unlocked;
  $("mp-content").hidden = !mp.unlocked;
  $("home-tab-multiplayer").textContent = mp.unlocked ? "Multiplayer" : "🔒 Multiplayer";
  $("mp-earned").textContent = String(mp.earned);
  $("mp-needed").textContent = String(mp.needed);
  $("mp-bar-fill").style.width = `${Math.min(100, (mp.earned / Math.max(1, mp.needed)) * 100)}%`;
  const bar = document.querySelector("#mp-locked .journal-bar");
  bar.setAttribute("aria-valuemax", String(mp.needed));
  bar.setAttribute("aria-valuenow", String(Math.min(mp.earned, mp.needed)));
}
for (const button of document.querySelectorAll("[data-goto]")) {
  button.addEventListener("click", () => showHomeTab(button.dataset.goto));   // the address stays as it is: a click is not a new page
}
loadSolo();
loadAchievements();
loadProfile();
Net.poll(loadProfile, 30000);
window.addEventListener("pageshow", () => { loadSolo(); loadAchievements(); loadProfile(); });
window.addEventListener("pageshow", loadDaily);  // coming back with the browser's back button

refresh();
loadSaved();
Net.poll(refresh, POLL_MS);
Net.poll(loadSaved, 15000); // a game that is resumed or expires drops off the list

// ---- account: change password, delete account ---------------------------------------------------

let accountForced = false; // an administrator reset the password: the dialog cannot be dismissed until it is changed

function openAccount(forced = false) {
  accountForced = forced;
  const dialog = $("account-dialog");
  $("account-title").textContent = forced ? "Choose a new password" : "Account";
  $("forced-note").hidden = !forced;
  $("delete-box").hidden = forced;
  $("account-close").hidden = forced;
  if (!dialog.open) dialog.showModal();
}

$("account-open").addEventListener("click", () => openAccount(false));
$("account-close").addEventListener("click", () => $("account-dialog").close());
$("account-dialog").addEventListener("cancel", (e) => { if (accountForced) e.preventDefault(); }); // Escape

$("password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("pw-msg").textContent = "";
  if ($("pw-new").value !== $("pw-new2").value) {
    $("pw-msg").textContent = "The two new passwords are different.";
    return;
  }
  try {
    await Auth.fetch("/account/change-password", { current: $("pw-current").value, new: $("pw-new").value });
    // every other login of this account just ended; this one carries on with the new cookie the reply set
    try { localStorage.removeItem("alchemy-session"); } catch (_) { /* an old token would now be refused */ }
    $("password-form").reset();
    accountForced = false;
    $("breach-note").hidden = true;
    $("account-dialog").close();
  } catch (err) {
    $("pw-msg").textContent = err.message;
  }
});

$("delete-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("del-msg").textContent = "";
  try {
    await Auth.fetch("/account/delete", { password: $("del-password").value });
    Auth.clear();
    location.href = "/login";
  } catch (err) {
    $("del-msg").textContent = err.message;
  }
});

function breachAlreadyShown() {
  try {
    if (sessionStorage.getItem("breachShown")) return true;
    sessionStorage.setItem("breachShown", "1");
  } catch (_) { /* private window: it is simply shown at every load */ }
  return false;
}

// Admins get links to the promotion requests (with how many are waiting) and to operations.
async function loadAdmin() {
  try {
    const me = await Auth.fetch("/me");
    if (me.must_change_password && !accountForced) openAccount(true);
    else if (me.password_breached && !breachAlreadyShown()) {   // once per browser tab, not at every page load
      $("breach-note").hidden = false;
      openAccount(false);
    }
    $("icons-link").hidden = !(me.admin || me.designer);   // the icon studio: designers and admins
    if (!me.admin) return;
    $("ops-link").hidden = false;
    $("admin-link").hidden = false;
    const [data, icons] = await Promise.all([Auth.fetch("/admin/queue"), Auth.fetch("/icons/queue").catch(() => ({ waiting: [] }))]);
    const waiting = data.proposals.length + icons.waiting.length;   // recipes and icons
    $("admin-pending").textContent = waiting;
    $("admin-pending").hidden = waiting === 0;
  } catch (_) { /* the link just stays hidden */ }
}
loadAdmin();
Net.poll(loadAdmin, 30000);

// ---- the three ways to play: Solo, Multiplayer, Challenges ----
const HOME_TABS = ["solo", "multiplayer", "challenges"];
function showHomeTab(name, remember = true) {
  if (!HOME_TABS.includes(name)) name = "solo";   // Solo is where a new player should land
  for (const tab of HOME_TABS) {
    $(`panel-${tab}`).hidden = tab !== name;
    const button = $(`home-tab-${tab}`);
    button.classList.toggle("selected", tab === name);
    button.setAttribute("aria-selected", String(tab === name));
  }
  if (remember) {
    try { sessionStorage.setItem("home-tab", name); } catch (_) { /* private window: the tab is just not remembered */ }
  }
}
for (const tab of HOME_TABS) $(`home-tab-${tab}`).addEventListener("click", () => showHomeTab(tab));   // no #tab in the address: a click is not a new page
{
  let wanted = location.hash.slice(1);
  if (!HOME_TABS.includes(wanted)) {
    try { wanted = sessionStorage.getItem("home-tab") || ""; } catch (_) { wanted = ""; }
  }
  showHomeTab(wanted, false);
}
// A link such as /#challenges followed while this page is open switches the tab too.
window.addEventListener("hashchange", () => { if (HOME_TABS.includes(location.hash.slice(1))) showHomeTab(location.hash.slice(1)); });
