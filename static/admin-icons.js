"use strict";
// The review page's icon section (admin.html): icons designers proposed from the Icon studio, each shown next to the picture it
// would replace at the sizes players see. Approve puts it in the game at once; the latest approval of an element can be undone.
// Wrapped in a function so its names cannot clash with admin.js, which shares the page.
(function () {
  const byId = (id) => document.getElementById(id);
  let labels = new Map();   // element id -> label (from /icons/elements)
  let busy = false;

  function node(tag, text = "", className = "") {
    const n = document.createElement(tag);
    if (text) n.textContent = text;
    if (className) n.className = className;
    return n;
  }
  function img(src, className) {
    const i = document.createElement("img");
    i.src = src; i.alt = ""; if (className) i.className = className;
    return i;
  }
  /** One icon at the sizes players see it: full size, the element list's slot, and a tile in the light and the dark theme. */
  function sizes(src, title) {
    const side = node("div", "", "ir-side");
    side.append(node("h3", title));
    const row = node("div", "", "ir-sizes");
    if (!src) {
      row.append(node("div", "No picture yet", "ir-none"));
    } else {
      const light = node("span", "", "ir-tile light"), dark = node("span", "", "ir-tile dark");
      light.append(img(src)); dark.append(img(src));
      row.append(img(src, "ir-full"), img(src, "ir-list60"), light, dark);
    }
    side.append(row);
    return side;
  }
  const fileUrl = (name) => `/icons/file/${encodeURIComponent(name)}.png`;
  const label = (id) => labels.get(id) || id;
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "");

  async function act(path, body) {
    if (busy) return;
    busy = true;
    byId("ir-msg").textContent = "";
    try { await Auth.fetch(path, body); } catch (err) { byId("ir-msg").textContent = err.message; }
    busy = false;
    await load();
  }

  function render(data) {
    const list = byId("ir-list");
    list.replaceChildren();
    byId("ir-empty").hidden = data.waiting.length > 0;
    byId("ir-count").hidden = data.waiting.length === 0;
    byId("ir-count").textContent = String(data.waiting.length);
    for (const p of data.waiting) {
      const card = node("article", "", "ir-card");
      const head = node("div", "", "ir-head");
      head.append(node("strong", label(p.element)), node("span", `by ${p.author} · ${when(p.created)}`, "who"));
      const compare = node("div", "", "ir-compare");
      compare.append(sizes(fileUrl(p.png), "Proposed"), sizes(p.current, p.current ? "In the game now" : "In the game now"));
      card.append(head, compare);
      if (p.note) card.append(node("p", p.note, "note"));
      const actions = node("div", "", "ir-actions");
      const reply = document.createElement("input");
      reply.type = "text"; reply.maxLength = 300; reply.placeholder = "Optional reply to the designer";
      const approve = node("button", "Approve: put it in the game", "primary");
      const reject = node("button", "Not this one", "danger");
      approve.type = reject.type = "button";
      approve.addEventListener("click", () => act("/icons/handle", { id: p.id, action: "approve", reply: reply.value }));
      reject.addEventListener("click", () => act("/icons/handle", { id: p.id, action: "reject", reply: reply.value }));
      actions.append(reply, approve, reject);
      const preview = node("label", "", "ir-preview");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = Boolean(p.preview);
      box.addEventListener("change", () => act("/icons/preview", { id: p.id, on: box.checked }));
      preview.append(box, " Preview in the game (administrators and designers see it in place of the current picture)");
      card.append(actions, preview);
      list.append(card);
    }
    const recent = byId("ir-recent");
    recent.replaceChildren();
    for (const p of data.recent) {
      const li = node("li");
      const words = { approved: "approved", rejected: "not accepted", undone: "approved, then undone" }[p.status] || p.status;
      li.append(img(fileUrl(p.png)), node("strong", label(p.element)),
        node("span", `by ${p.author} · ${words}${p.handled_by ? ` by ${p.handled_by}` : ""} · ${when(p.handled_at)}`, "who"));
      if (p.can_undo) {
        const undo = node("button", p.replaced ? "Undo: put the old picture back" : "Undo: remove it again");
        undo.type = "button";
        undo.addEventListener("click", () => act("/icons/undo", { id: p.id }));
        li.append(undo);
      }
      recent.append(li);
    }
  }

  async function load() {
    try {
      render(await Auth.fetch("/icons/queue"));
    } catch (err) {
      if (err.status !== 403) byId("ir-msg").textContent = err.message;
      else byId("icon-review").hidden = true;
    }
  }

  (async () => {
    try { labels = new Map((await Auth.fetch("/icons/elements")).elements.map((e) => [e.id, e.label])); } catch (_) { /* ids then */ }
    await load();
    Net.poll(() => { if (!busy) return load(); }, 15000);
  })();
})();
