"use strict";

// The administrator's operations page: status, running games, accounts and invite codes.
// Everything is built with textContent, and risky buttons need a second click within a few seconds.

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 10000;
const CONFIRM_MS = 4000;

if (!Auth.token()) Auth.toLogin();

let accounts = [];

function el(tag, text = "", className = "") {
  const node = document.createElement(tag);
  if (text !== "") node.textContent = text;
  if (className) node.className = className;
  return node;
}

function showError(message) {
  $("ops-error").textContent = message || "";
}

function duration(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

let confirming = 0; // buttons waiting for their second click: the page must not rebuild itself under them

/** A button that asks "Sure?" on the first click and acts on a second click within a few seconds. */
function riskyButton(label, action, className = "") {
  const button = el("button", label, className);
  button.type = "button";
  let armed = null;
  button.addEventListener("click", async () => {
    if (armed === null) {
      button.textContent = "Sure? Click again";
      confirming++;
      armed = setTimeout(() => { button.textContent = label; armed = null; confirming--; }, CONFIRM_MS);
      return;
    }
    clearTimeout(armed);
    armed = null;
    confirming--;
    button.textContent = label;
    await run(action);
  });
  return button;
}

function plainButton(label, action) {
  const button = el("button", label);
  button.type = "button";
  button.addEventListener("click", () => run(action));
  return button;
}

async function run(action) {
  showError("");
  try {
    await action();
  } catch (err) {
    showError(err.message);
  }
  await refresh();
}

// ---- status -------------------------------------------------------------------------------------

function stat(label, value) {
  const box = el("div", "", "stat");
  box.append(el("strong", String(value)), el("span", label, "muted"));
  return box;
}

function renderStatus(s) {
  const since = s.since_start;
  $("stats").replaceChildren(
    stat("running for", duration(s.uptime_seconds)),
    stat("accounts", s.accounts.total),
    stat("waiting for email", s.accounts.pending),
    stat("disabled", s.accounts.disabled),
    stat("logged in now", s.sessions),
    stat(`games (max ${s.games.max})`, s.games.running),
    stat("daily & race worlds", s.games.private),
    stat("players in games", s.games.players),
    stat("logins since start", since.logins || 0),
    stat("failed logins", since.login_failures || 0),
    stat("sign-ups", since.registrations || 0),
    stat("rate-limited", since.throttled || 0),
    stat("server errors", since.server_errors || 0),
  );
  const gates = [];
  if (s.signup.invite_required) gates.push("invite code");
  if (s.signup.email_required) gates.push("confirmed email");
  if (s.signup.human_check) gates.push("human check");
  $("signup-line").textContent = "Sign-up needs: " + (gates.length ? gates.join(", ") : "nothing (open)") +
    (s.signup.behind_proxy ? ". Behind a reverse proxy." : ". Not behind a reverse proxy.") +
    " Counters restart with the server.";
}

// ---- games --------------------------------------------------------------------------------------

function renderGames(data) {
  const body = $("games-table").querySelector("tbody");
  body.replaceChildren();
  for (const game of data.games) {
    const row = el("tr");
    const end = el("td");
    end.append(riskyButton("End game", () => Auth.fetch("/ops/games/end", { id: game.id }), "danger"));
    row.append(el("td", game.name), el("td", game.kind + (game.private ? " (private)" : "")),
      el("td", `${game.players}/${game.max_players}`), el("td", `${game.found}/${game.total}`), end);
    body.append(row);
  }
  $("games-table").hidden = data.games.length === 0;
  $("no-games").hidden = data.games.length !== 0;
}

// ---- accounts -----------------------------------------------------------------------------------

function stateOf(account) {
  const states = [];
  if (account.admin) states.push("admin");
  if (account.bot) states.push("bot");
  if (account.pending) states.push("email not confirmed");
  if (account.disabled) states.push("disabled");
  return states.join(", ") || "ok";
}

function renderAccounts() {
  const wanted = $("account-filter").value.trim().toLowerCase();
  const body = $("accounts-table").querySelector("tbody");
  body.replaceChildren();
  const shown = accounts.filter((a) => a.name.toLowerCase().includes(wanted));
  for (const account of shown) {
    const row = el("tr");
    const actions = el("td", "", "actions");
    if (account.admin) {
      actions.append(el("span", "change with accounts_admin.py", "muted"));
    } else {
      const name = account.name;
      if (account.pending) actions.append(plainButton("Confirm email", () => Auth.fetch("/ops/accounts/verify", { username: name })));
      actions.append(account.disabled
        ? plainButton("Enable", () => Auth.fetch("/ops/accounts/enable", { username: name }))
        : riskyButton("Disable", () => Auth.fetch("/ops/accounts/disable", { username: name })));
      actions.append(riskyButton("New password", async () => {
        const data = await Auth.fetch("/ops/accounts/reset-password", { username: name });
        $("temp-for").textContent = name;
        $("temp-value").textContent = data.temporary_password;
        $("temp-box").hidden = false;
      }));
      actions.append(riskyButton("Delete", () => Auth.fetch("/ops/accounts/delete", { username: account.name }), "danger"));
    }
    row.append(el("td", account.name), el("td", stateOf(account)), el("td", account.created), actions);
    body.append(row);
  }
  $("accounts-table").hidden = shown.length === 0;
  $("no-accounts").hidden = shown.length !== 0;
}

$("account-filter").addEventListener("input", renderAccounts);
$("temp-done").addEventListener("click", () => {
  $("temp-box").hidden = true;
  $("temp-value").textContent = "";
});

// ---- invites ------------------------------------------------------------------------------------

function renderInvites(data) {
  $("invites-off").hidden = data.enabled;
  $("invites-on").hidden = !data.enabled;
  const body = $("invites-table").querySelector("tbody");
  body.replaceChildren();
  for (const code of data.codes) {
    const row = el("tr");
    const revoke = el("td");
    if (!code.used_by) revoke.append(riskyButton("Revoke", () => Auth.fetch("/ops/invites/revoke", { id: code.id })));
    row.append(el("td", code.id), el("td", code.note || ""), el("td", code.used_by ? `used by ${code.used_by}` : "unused"),
      el("td", new Date(code.created * 1000).toLocaleDateString()), revoke);
    body.append(row);
  }
}

$("invite-form").addEventListener("submit", (e) => {
  e.preventDefault();
  run(async () => {
    const data = await Auth.fetch("/ops/invites/create", {
      count: Number($("invite-count").value) || 1, note: $("invite-note").value,
    });
    $("new-codes-list").textContent = data.codes.join("\n");
    $("new-codes").hidden = false;
    $("invite-note").value = "";
  });
});
$("codes-done").addEventListener("click", () => {
  $("new-codes").hidden = true;
  $("new-codes-list").textContent = "";
});

// ---- loading ------------------------------------------------------------------------------------

async function refresh() {
  if (confirming > 0) return; // a confirmation is pending: the next refresh will catch up
  try {
    const [status, games, list, invites] = await Promise.all([
      Auth.fetch("/ops/status"), Auth.fetch("/ops/games"), Auth.fetch("/ops/accounts"), Auth.fetch("/ops/invites"),
    ]);
    renderStatus(status);
    renderGames(games);
    accounts = list.accounts;
    renderAccounts();
    renderInvites(invites);
    $("updated").textContent = "Updated " + new Date().toLocaleTimeString();
  } catch (err) {
    if (err.status === 403) {
      showError("This page is for administrators.");
    } else if (err.message !== "Please log in.") {
      showError(err.message);
    }
  }
}

$("refresh").addEventListener("click", refresh);
refresh();
setInterval(refresh, REFRESH_MS);
