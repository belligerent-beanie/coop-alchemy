"use strict";

// Small "?" buttons: hovering, focusing or tapping one shows a short explanation next to it (see .help in style.css).
// Hover and keyboard focus work in plain CSS; this adds tapping for touch screens, one tip open at a time, and Escape or a click
// elsewhere to close.
(function () {
  function closeAll(except) {
    for (const tip of document.querySelectorAll(".help.open")) {
      if (tip === except) continue;
      tip.classList.remove("open");
      tip.querySelector(".help-btn").setAttribute("aria-expanded", "false");
    }
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest(".help-btn");
    if (!button) {
      closeAll(null);
      return;
    }
    event.preventDefault();
    const tip = button.closest(".help");
    closeAll(tip);
    const open = tip.classList.toggle("open");
    button.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeAll(null);
  });
})();
