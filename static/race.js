"use strict";

// The lobby, countdown and results page of one race. It polls the room once a second; when the race is running
// it sends the racer to their own private world.

const $ = (id) => document.getElementById(id);
const RACE_ID = location.pathname.split("/").pop();

if (!Auth.loggedIn()) Auth.toLogin();

let room = null;
let timer = null;
let skew = 0;  // how long ago (ms) the last status arrived, to count the countdown down smoothly
let receivedAt = 0;

const goalText = (r) => (r.goal === "target"
  ? `First to discover ${r.target.emoji} ${r.target.label}`
  : `First to reach ${r.goal_count} elements`);

function render() {
  if (!room) return;
  $("race-goal").textContent = goalText(room);
  const me = Auth.user().toLowerCase();
  $("race-racers").replaceChildren(...room.racers.map((r) => {
    const item = document.createElement("li");
    if (r.name.toLowerCase() === me) item.className = "me";
    const shown = room.state === "lobby" ? "" : ` — ${r.count} element${r.count === 1 ? "" : "s"}`;
    item.textContent = `${r.winner ? "🏆 " : ""}${r.name}${r.name === room.host ? " (host)" : ""}${shown}`;
    return item;
  }));
  const count = $("race-count");
  count.hidden = true;
  $("race-start").hidden = !(room.state === "lobby" && room.is_host);
  $("race-start").disabled = room.racers.length < room.min_racers;
  $("race-leave").textContent = room.state === "finished" ? "Close" : "Leave";
  $("race-play").hidden = !(room.game && room.state !== "lobby");
  if (room.game) $("race-play").href = room.game;
  if (room.state === "lobby") {
    $("race-status").textContent = room.is_host
      ? `Waiting for racers (${room.racers.length}/${room.max_racers}). You need at least ${room.min_racers}.`
      : `Waiting for ${room.host} to start the race (${room.racers.length}/${room.max_racers} racers).`;
  } else if (room.state === "countdown") {
    const left = Math.max(0, room.starts_in - (Date.now() - receivedAt) / 1000);
    count.hidden = false;
    count.textContent = left > 0 ? String(Math.ceil(left)) : "Go!";
    $("race-status").textContent = "Get ready…";
  } else if (room.state === "running") {
    $("race-status").textContent = "The race is on!";
    if (room.game && !location.search.includes("stay")) location.href = room.game;
  } else {
    $("race-status").textContent = room.winner
      ? (room.winner.toLowerCase() === me ? "You won! 🏆" : `${room.winner} won.`) : "The race is over.";
  }
}

async function poll() {
  try {
    room = await Auth.fetch(`/races/${RACE_ID}`);
    receivedAt = Date.now();
    $("race-error").textContent = "";
    if (!room.you) {
      try { room = await Auth.fetch(`/races/${RACE_ID}/join`, {}); } catch (err) { $("race-error").textContent = err.message; }
    }
    render();
  } catch (err) {
    if (/does not exist/.test(err.message)) {
      if (timer) timer.stop();
      $("race-status").textContent = "That race has ended.";
      $("race-start").hidden = $("race-play").hidden = true;
      $("race-racers").replaceChildren();
    } else {
      $("race-error").textContent = err.message;
    }
  }
}

$("race-start").addEventListener("click", async () => {
  try { room = await Auth.fetch(`/races/${RACE_ID}/start`, {}); receivedAt = Date.now(); render(); }
  catch (err) { $("race-error").textContent = err.message; }
});

$("race-leave").addEventListener("click", async () => {
  try { await Auth.fetch(`/races/${RACE_ID}/leave`, {}); } catch (_) { /* already gone */ }
  location.href = "/";
});

poll();
timer = Net.poll(poll, 1000);
setInterval(render, 200);  // the countdown ticks faster than the polling
