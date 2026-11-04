"use strict";

// Small helpers shared by the home page and the game page for the daily challenge.

const TIER_INFO = {
  easy: { icon: "🟢", label: "Easy" },
  medium: { icon: "🟡", label: "Medium" },
  hard: { icon: "🔴", label: "Hard" },
};

/** Copies text; browsers only allow the clipboard API on secure pages, so fall back to the old way. */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) { /* not allowed here: try the fallback */ }
  const box = document.createElement("textarea");
  box.value = text;
  box.setAttribute("readonly", "");
  box.style.cssText = "position:fixed;left:-1000px;top:0";
  document.body.appendChild(box);
  box.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { /* ignore */ }
  box.remove();
  return ok;
}

/** One line for sharing, without naming the target: "🟡 Medium: 7 (reference 8) ⭐". */
function resultLine(tier, score, reference) {
  const info = TIER_INFO[tier];
  if (score === null || score === undefined) return `${info.icon} ${info.label}: not played`;
  const mark = !reference ? "" : score < reference ? " ⭐" : score === reference ? " ✅" : "";
  return `${info.icon} ${info.label}: ${score}${reference ? ` (reference ${reference})` : ""}${mark}`;
}

/** The text to paste to friends. It does not give away any target. */
function shareText(date, lines, streak) {
  const out = [`⚗️ Co-op Alchemy Daily ${date}`, ...lines];
  if (streak >= 2) out.push(`🔥 ${streak}-day streak`);
  return out.join("\n");
}

/** "💧 Water + 🔥 Fire → ♨️ Steam" as an <li>, from a route step ({a, b, makes}). */
function routeItem(step) {
  const name = (el) => `${el.emoji} ${el.label}`.trim();
  const li = document.createElement("li");
  li.textContent = `${name(step.a)} + ${name(step.b)} → ${step.makes.map(name).join(", ")}`;
  return li;
}
