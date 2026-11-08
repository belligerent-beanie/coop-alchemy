"use strict";
// The Euler look's hover effect: what the pointer touches goes black and white and is wrapped in a pulsing dark aura
// that throws off black sparkles. It is only for a mouse (no hover on a touch screen), only in the Euler look, and it keeps
// the grey-out but drops the motion when the browser asks for reduced motion.
(function () {
  if (!matchMedia("(hover: hover) and (pointer: fine)").matches) return;
  const TARGETS = "button, .btn, .home-tab, .game-card, .stat, .item, .collection-tile:not(.locked), .achievement, summary, " +
    ".journal-entry, .journal-chip, .tabs button";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  let current = null;
  let aura = null;

  function clear() {
    if (current) current.classList.remove("blackened");
    if (aura) aura.remove();
    current = aura = null;
  }

  function spark(box, rect, index, count) {
    const star = document.createElement("i");
    star.className = "dark-spark";
    // most sparks start on the edge of the thing, a few inside it
    const edge = Math.random() < 0.75;
    let x = Math.random() * 100, y = Math.random() * 100;
    if (edge) {
      const side = (index % 4);
      if (side === 0) y = -4; else if (side === 1) y = 104; else if (side === 2) x = -3; else x = 103;
    }
    const size = 7 + Math.random() * 8;
    star.style.cssText = `left:${x}%;top:${y}%;width:${size}px;height:${size}px;` +
      `animation-duration:${0.8 + Math.random() * 0.9}s;animation-delay:${(Math.random() * 1.2).toFixed(2)}s;` +
      `--drift:${(-10 - Math.random() * 14).toFixed(0)}px;--turn:${(Math.random() * 180 - 90).toFixed(0)}deg`;
    box.appendChild(star);
  }

  function enter(target) {
    clear();
    current = target;
    target.classList.add("blackened");
    const rect = target.getBoundingClientRect();
    aura = document.createElement("div");
    aura.className = "dark-aura";
    aura.style.cssText = `left:${rect.left}px;top:${rect.top}px;width:${rect.width}px;height:${rect.height}px`;
    if (!reduced.matches) {
      const count = Math.max(6, Math.min(16, Math.round((rect.width + rect.height) / 22)));
      for (let i = 0; i < count; i++) spark(aura, rect, i, count);
    } else {
      aura.classList.add("still");
    }
    // inside an open dialog the page is behind the top layer, so the aura goes in the dialog
    (target.closest("dialog[open]") || document.body).appendChild(aura);
  }

  const active = () => document.documentElement.dataset.look === "euler";

  document.addEventListener("pointerover", (event) => {
    if (event.pointerType !== "mouse" || event.buttons || !active()) return;
    const target = event.target.closest ? event.target.closest(TARGETS) : null;
    if (!target) { if (current && !current.contains(event.target)) clear(); return; }
    if (target !== current) enter(target);
  });
  document.addEventListener("pointerout", (event) => {
    if (current && !(event.relatedTarget && current.contains(event.relatedTarget))) clear();
  });
  for (const type of ["pointerdown", "dragstart", "wheel", "keydown"]) document.addEventListener(type, clear, { passive: true });
  window.addEventListener("scroll", clear, { capture: true, passive: true });
  window.addEventListener("blur", clear);
})();
