"use strict";
// Which look the pages wear: "euler" (the default) or "classic" (the original). It is a per-browser choice kept in localStorage,
// applied before the page paints (this file is loaded in <head>), and switched by a small link at the foot of the pages that scroll.
(function () {
  const KEY = "look";
  let look = "euler";
  try { if (localStorage.getItem(KEY) === "classic") look = "classic"; } catch (e) { /* storage blocked: the default look */ }
  document.documentElement.dataset.look = look;

  // Which page this is, so a design patch (patches.py) can aim at one page: [data-page="home"] .card { … }
  const first = location.pathname.split("/")[1] || "";
  const page = first === "" ? "home" : first === "g" ? "game" : first;
  document.documentElement.dataset.page = page;

  // Design patches that change text: the styles come in /patches.css (linked in <head>); the words are put in here.
  // Only elements with no children are changed (as the designer picker's Text tool does), and only with text, never markup.
  document.addEventListener("DOMContentLoaded", () => {
    fetch("/patches/text").then((res) => (res.ok ? res.json() : { changes: [] })).then(({ changes }) => {
      for (const change of changes || []) {
        if (change.page !== "*" && change.page !== page) continue;
        let nodes = [];
        try { nodes = document.querySelectorAll(change.selector); } catch (e) { continue; }   // a selector the browser rejects
        for (const node of nodes) if (!node.children.length) node.textContent = change.text;
      }
    }).catch(() => { /* no text patches this time */ });
  });

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
