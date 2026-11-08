"use strict";
// Which look the pages wear: "euler" (the default) or "classic" (the original). It is a per-browser choice kept in localStorage,
// applied before the page paints (this file is loaded in <head>), and switched by a small link at the foot of the pages that scroll.
(function () {
  const KEY = "look";
  let look = "euler";
  try { if (localStorage.getItem(KEY) === "classic") look = "classic"; } catch (e) { /* storage blocked: the default look */ }
  document.documentElement.dataset.look = look;

  document.addEventListener("DOMContentLoaded", () => {
    const body = document.body;
    if (!body.classList.length) return;   // the game board fills the screen; the switch lives on the other pages
    const bar = document.createElement("p");
    bar.className = "look-switch";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "link";
    const show = () => {
      button.textContent = document.documentElement.dataset.look === "classic" ? "Use the new look" : "Use the classic look";
    };
    button.addEventListener("click", () => {
      const next = document.documentElement.dataset.look === "classic" ? "euler" : "classic";
      document.documentElement.dataset.look = next;
      try { localStorage.setItem(KEY, next); } catch (e) { /* not remembered, still applied now */ }
      show();
    });
    show();
    bar.append(button);
    body.append(bar);
  });
})();
