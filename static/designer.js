"use strict";
// The designer's UI picker. Accounts marked as designers (accounts_admin.py make-designer) get this on every page (auth.js loads it).
// Alt+Shift+P (or the Design button) turns picker mode on: clicks select an element instead of reaching the page; ↑ selects the
// parent, ↓ a child, Enter opens Fill / Border / Shadow / Text, Esc closes things and clears the selection; Alt+Shift+E copies every change
// as Markdown (the guide, shown the first time and under "Guide", explains all of this). Edits are inline styles on THIS
// browser's copy of the page only: nothing is sent to the server; they are kept in this browser (localStorage, per page) and put back after a reload. "Changes" exports them as
// Markdown (selector, before → after) to hand to a developer or an AI. This is a convenience for designers, not a security
// boundary: anyone can do the same in their browser's developer tools, and the server never trusts any of it.
(function () {
  if (window.__designerLoaded) return;
  window.__designerLoaded = true;

  // ---------- small helpers ----------
  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, "");
      else if (value !== false && value !== null && value !== undefined) node.setAttribute(key, value);
    }
    for (const child of children) if (child) node.append(child);
    return node;
  }
  const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
  const hex2 = (n) => clamp(Math.round(n), 0, 255).toString(16).padStart(2, "0");
  const toHex = (r, g, b) => "#" + hex2(r) + hex2(g) + hex2(b);

  function parseHex(text) {
    const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(text).trim());
    if (!m) return null;
    let v = m[1];
    if (v.length === 3) v = v.split("").map((c) => c + c).join("");
    return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16), parseInt(v.slice(4, 6), 16)];
  }
  function rgbToHsv(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    let hue = 0;
    if (d) {
      if (max === r) hue = ((g - b) / d) % 6;
      else if (max === g) hue = (b - r) / d + 2;
      else hue = (r - g) / d + 4;
      hue *= 60;
      if (hue < 0) hue += 360;
    }
    return [hue, max ? d / max : 0, max];
  }
  function hsvToRgb(hue, s, v) {
    const c = v * s, x = c * (1 - Math.abs(((hue / 60) % 2) - 1)), m = v - c;
    const [r, g, b] = hue < 60 ? [c, x, 0] : hue < 120 ? [x, c, 0] : hue < 180 ? [0, c, x] : hue < 240 ? [0, x, c] : hue < 300 ? [x, 0, c] : [c, 0, x];
    return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
  }
  /** A computed colour ("rgb(1, 2, 3)" or "rgba(…)") as "#rrggbb"; "transparent" when see-through; rgba(…) text when partly. */
  function colourText(computed) {
    const m = /^rgba?\(\s*([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)(?:[ ,/]+([\d.]+%?))?\s*\)$/.exec(String(computed).trim());
    if (!m) return String(computed).trim();
    let alpha = m[4] === undefined ? 1 : m[4].endsWith("%") ? parseFloat(m[4]) / 100 : parseFloat(m[4]);
    if (alpha === 0) return "transparent";
    if (alpha < 1) return `rgba(${Math.round(m[1])}, ${Math.round(m[2])}, ${Math.round(m[3])}, ${alpha})`;
    return toHex(+m[1], +m[2], +m[3]);
  }
  const NO_BORDER = (style, width) => style === "none" || style === "hidden" || parseFloat(width) === 0;
  function borderText(style, width, colour) {
    return NO_BORDER(style, width) ? "none" : `${width} ${style} ${colour}`;
  }
  /** Split a list on the commas that are not inside brackets ("rgba(0, 0, 0, 0.2) 0px 1px, …" has two shadows, not five). */
  function splitTopLevel(text) {
    const parts = [];
    let depth = 0, start = 0;
    for (let i = 0; i < text.length; i++) {
      if (text[i] === "(") depth++;
      else if (text[i] === ")") depth--;
      else if (text[i] === "," && depth === 0) { parts.push(text.slice(start, i).trim()); start = i + 1; }
    }
    parts.push(text.slice(start).trim());
    return parts.filter(Boolean);
  }
  /** One shadow ("rgba(0, 0, 0, 0.2) 0px 4px 12px 0px", the colour first or last, maybe "inset") as
   *  {x, y, blur, spread, rgb: [r, g, b], alpha, inset}; null when it cannot be read. */
  function parseShadow(text) {
    const colour = /rgba?\(([^)]*)\)/.exec(text);
    const numbers = colour ? colour[1].split(/[\s,/]+/).filter(Boolean) : ["0", "0", "0"];
    const lengths = text.replace(/rgba?\([^)]*\)/, "").match(/-?[\d.]+(px)?/g) || [];
    if (lengths.length < 2) return null;
    const [x, y, blur = 0, spread = 0] = lengths.map((n) => Math.round(parseFloat(n)));
    const alphaText = numbers[3];
    const alpha = alphaText === undefined ? 1 : alphaText.endsWith("%") ? parseFloat(alphaText) / 100 : parseFloat(alphaText);
    return { x, y, blur, spread, rgb: numbers.slice(0, 3).map(Number), alpha: Math.round(alpha * 100) / 100, inset: /\binset\b/.test(text) };
  }
  /** A shadow as CSS: offsets, blur, spread, then the colour (#rrggbb when solid, rgba() when see-through), then "inset". */
  function shadowCss(s) {
    const colour = s.alpha >= 1 ? toHex(...s.rgb) : `rgba(${s.rgb.map(Math.round).join(", ")}, ${s.alpha})`;
    return `${s.x}px ${s.y}px ${s.blur}px ${s.spread}px ${colour}${s.inset ? " inset" : ""}`;
  }
  /** A computed box-shadow in the same form as shadowCss, so before and after read alike in the export ("none" for none). */
  function shadowText(computed) {
    const text = String(computed).trim();
    if (!text || text === "none") return "none";
    return splitTopLevel(text).map((part) => { const s = parseShadow(part); return s ? shadowCss(s) : part; }).join(", ");
  }

  // ---------- state ----------
  let on = false;
  let selected = null;
  let hovered = null;
  let climbed = [];            // the elements we came up from, so ↓ goes back the way we came
  let menuOpen = false;
  let tool = null;             // "fill" | "border" | "shadow" | "text" | null
  let showChanges = false;
  let showPatches = false;    // the design patches panel (patches.py): switch them off, to preview, or on
  let tourStep = null;         // the guide page being shown (0-based), or null
  let toastTimer = 0;
  let changesLink = null;      // the "Changes (n)" button in the dock, kept so its count can change without rebuilding the dock
  let exported = true;         // false while there are edits that were not copied or downloaded
  const changes = new Map();   // element -> {selector, tag, snippet, props: Map(prop -> {before, after, original}), text}

  // ---------- page furniture ----------
  const root = h("div", { class: "dz-root", "data-designer": "" });
  const hoverBox = h("div", { class: "dz-box dz-hover", hidden: true });
  const selectBox = h("div", { class: "dz-box dz-picked", hidden: true });
  const dock = h("div", { class: "dz-dock", role: "region", "aria-label": "UI picker", hidden: true });
  const toggle = h("button", { class: "dz-toggle", type: "button", title: "UI picker (Alt+Shift+P); copy your changes with Alt+Shift+E", text: "Design" });
  const toast = h("div", { class: "dz-toast", role: "status", "aria-live": "polite", hidden: true });
  root.append(hoverBox, selectBox, dock, toggle, toast);

  const ours = (node) => node instanceof Node && root.contains(node);

  function place(box, element) {
    if (!element || !document.contains(element)) { box.hidden = true; return; }
    const r = element.getBoundingClientRect();
    box.hidden = false;
    box.style.left = r.left + "px";
    box.style.top = r.top + "px";
    box.style.width = r.width + "px";
    box.style.height = r.height + "px";
  }
  function refreshBoxes() {
    if (selected && !document.contains(selected)) { selected = null; climbed = []; closeTool(); }
    place(hoverBox, on && hovered !== selected ? hovered : null);
    place(selectBox, on ? selected : null);
  }

  // ---------- selectors ----------
  const hashed = (name) => /\d{4,}/.test(name) || name.length > 30 || /^(css|sc|jsx|svelte|_)[-_]?[a-z0-9]{5,}$/i.test(name) || /^[a-z]{1,3}-[A-Za-z0-9]{5,}$/.test(name);
  function cssEscape(text) { return window.CSS && CSS.escape ? CSS.escape(text) : String(text).replace(/[^\w-]/g, "\\$&"); }
  function unique(selector, element) {
    try { const found = document.querySelectorAll(selector); return found.length === 1 && found[0] === element; } catch (_) { return false; }
  }
  /** Our best stable selector: id, then data-* attributes, then stable classes, then a short :nth-child path. */
  function selectorFor(element) {
    const tag = element.tagName.toLowerCase();
    if (element.id && !hashed(element.id)) {
      const byId = "#" + cssEscape(element.id);
      if (unique(byId, element)) return tag + byId;
    }
    for (const attr of element.attributes) {
      if (!attr.name.startsWith("data-") || attr.name === "data-designer" || !attr.value || attr.value.length > 40) continue;
      const byData = `${tag}[${attr.name}="${attr.value.replace(/(["\\])/g, "\\$1")}"]`;
      if (unique(byData, element)) return byData;
    }
    const classes = [...element.classList].filter((c) => !hashed(c));
    const own = tag + classes.map((c) => "." + cssEscape(c)).join("");
    if (classes.length && unique(own, element)) return own;
    const parts = [];
    for (let node = element; node && node.nodeType === 1 && node !== document.documentElement; node = node.parentElement) {
      const t = node.tagName.toLowerCase();
      const cls = [...node.classList].filter((c) => !hashed(c)).slice(0, 2).map((c) => "." + cssEscape(c)).join("");
      const siblings = node.parentElement ? [...node.parentElement.children].filter((s) => s.tagName === node.tagName) : [];
      const nth = siblings.length > 1 ? `:nth-of-type(${siblings.indexOf(node) + 1})` : "";
      if (node.id && !hashed(node.id)) { parts.unshift(t + "#" + cssEscape(node.id)); break; }
      parts.unshift(t + cls + nth);
      if (parts.length >= 2 && unique(parts.join(" > "), element)) break;
      if (node === document.body) break;
    }
    return parts.join(" > ");
  }
  const snippetOf = (element) => element.textContent.replace(/\s+/g, " ").trim().slice(0, 40);
  function label(element) {
    const cls = [...element.classList].filter((c) => !hashed(c)).slice(0, 2).map((c) => "." + c).join("");
    return element.tagName.toLowerCase() + (element.id ? "#" + element.id : "") + cls;
  }

  // ---------- applying and recording edits ----------
  function record(element) {
    let rec = changes.get(element);
    if (!rec) {
      const cs = getComputedStyle(element);
      rec = {
        selector: selectorFor(element), tag: element.tagName.toLowerCase(), snippet: snippetOf(element), props: new Map(),
        text: null,
        before: {
          "background-color": colourText(cs.backgroundColor),
          border: borderText(cs.borderTopStyle, cs.borderTopWidth, colourText(cs.borderTopColor)),
          "box-shadow": shadowText(cs.boxShadow),
        },
      };
      changes.set(element, rec);
    }
    return rec;
  }
  function rememberOriginal(rec, element, prop) {
    if (!rec.props.has(prop)) {
      rec.props.set(prop, { value: element.style.getPropertyValue(prop), priority: element.style.getPropertyPriority(prop) });
    }
  }
  /** Set an inline style; if the site's own CSS still wins, set it again with !important. */
  function setStyle(element, prop, value) {
    const saved = [element.style.getPropertyValue("transition"), element.style.getPropertyPriority("transition")];
    element.style.setProperty("transition", "none");          // read the final value, not a point in an animation
    element.style.setProperty(prop, value);
    const computed = getComputedStyle(element);
    const style = computed.borderTopStyle;
    const skip = prop === "border-width" && NO_BORDER(style, "1px");
    const now = computed.getPropertyValue(prop);
    const same = prop.endsWith("color") ? colourText(now) === value.toLowerCase()
      : prop === "box-shadow" ? shadowText(now) === shadowText(value) : now === value;
    if (!skip && !same) element.style.setProperty(prop, value, "important");
    if (saved[0]) element.style.setProperty("transition", saved[0], saved[1]); else element.style.removeProperty("transition");
  }
  /** An edit was made. The panel is NOT rebuilt (a slider being dragged must stay in the page); only the count and outlines change. */
  function edited() {
    exported = false;
    showCount();
    save();
    refreshBoxes();
  }
  const changeCount = () => changes.size + pending.length;
  function showCount() { if (changesLink) changesLink.textContent = `Changes (${changeCount()})`; }

  // ---------- keeping edits across reloads ----------
  // Every edit is saved in this browser (localStorage, one list per page) and put back when the page loads again, so a
  // reload, a new version of the site or a design patch switched on never costs a designer their work. An edit whose element
  // is not on the page (yet: the game board draws itself later; or any more: the page changed) is kept as it is, listed
  // under Changes and still exported, until it is reset.
  const STORE_KEY = "alchemy-designer-changes:" + location.pathname;
  let pending = [];   // saved edits whose element is not on the page: {selector, snippet, before, after, text}
  const saved = (rec) => ({ selector: rec.selector, snippet: rec.snippet, before: rec.before, after: rec.after || {}, text: rec.text || null });
  function save() {
    try {
      const all = [...[...changes.values()].map(saved), ...pending];
      if (all.length) localStorage.setItem(STORE_KEY, JSON.stringify(all)); else localStorage.removeItem(STORE_KEY);
    } catch (_) { /* storage full or blocked: the edits still work in this tab */ }
  }
  function restore() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORE_KEY) || "[]");
      pending = Array.isArray(raw) ? raw.filter((item) => item && typeof item.selector === "string" && item.before) : [];
    } catch (_) { pending = []; }
    if (!pending.length) return;
    putBack();
    let tries = 0;   // the page may build the element later: look again every second for half a minute
    const timer = setInterval(() => { if (!pending.length || ++tries > 30) clearInterval(timer); else putBack(); }, 1000);
  }
  function putBack() {
    const left = [];
    for (const item of pending) {
      let element = null;
      try { element = document.querySelector(item.selector); } catch (_) { /* a selector the browser rejects */ }
      if (!element || ours(element) || changes.has(element)) { left.push(item); continue; }
      const after = item.after || {};
      if (after["background-color"]) applyFill(element, after["background-color"]);
      if (after.border) {
        const m = /^(\d+)px (\w+) (.+)$/.exec(after.border);
        if (after.border === "none") applyBorder(element, "none", 0, "#000000");
        else if (m) applyBorder(element, m[2], +m[1], m[3]);
      }
      if (after["box-shadow"]) applyShadowValue(element, after["box-shadow"]);
      if (item.text && typeof item.text.after === "string" && !element.children.length) applyText(element, item.text.after);
      const rec = changes.get(element);
      if (rec) {   // "before" is how it was when the designer first changed it, not how it looks after a reload
        rec.before = item.before;
        if (rec.text && item.text) rec.text.before = item.text.before;
      }
    }
    pending = left;
    showCount();
    save();
  }

  function applyFill(element, colour) {
    const rec = record(element);
    rememberOriginal(rec, element, "background-color");
    setStyle(element, "background-color", colour);
    rec.after = Object.assign(rec.after || {}, { "background-color": colour });
    edited();
  }
  function applyBorder(element, style, widthPx, colour) {
    const rec = record(element);
    for (const prop of ["border-style", "border-width", "border-color"]) rememberOriginal(rec, element, prop);
    setStyle(element, "border-style", style);
    setStyle(element, "border-width", widthPx + "px");
    setStyle(element, "border-color", colour);
    rec.after = Object.assign(rec.after || {}, { border: style === "none" ? "none" : `${widthPx}px ${style} ${colour}` });
    edited();
  }
  /** shadow is {x, y, blur, spread, rgb, alpha, inset}, or null for no shadow. */
  function applyShadow(element, shadow) {
    applyShadowValue(element, shadow ? shadowCss(shadow) : "none");
  }
  function applyShadowValue(element, value) {
    const rec = record(element);
    rememberOriginal(rec, element, "box-shadow");
    setStyle(element, "box-shadow", value);
    rec.after = Object.assign(rec.after || {}, { "box-shadow": value });
    edited();
  }
  function applyText(element, text) {
    const rec = record(element);
    if (!rec.text) rec.text = { before: element.textContent, after: element.textContent };
    element.textContent = text;          // text, never markup
    rec.text.after = text;
    edited();
  }
  function resetElement(element) {
    const rec = changes.get(element);
    if (!rec) return;
    for (const [prop, original] of rec.props) {
      if (original.value) element.style.setProperty(prop, original.value, original.priority);
      else element.style.removeProperty(prop);
    }
    if (rec.text) element.textContent = rec.text.before;
    changes.delete(element);
    save();
    renderDock();
    refreshBoxes();
  }
  function forgetPending(item) {
    pending = pending.filter((other) => other !== item);
    save();
    renderDock();
  }
  function resetAll() {
    for (const element of [...changes.keys()]) resetElement(element);
    pending = [];
    save();
    exported = true;
    renderDock();
  }

  // ---------- export ----------
  /** One text block per changed element: its selector (and a snippet of its text to confirm it), then each before → after. */
  function exportBlocks() {
    const blocks = [];
    for (const rec of [...changes.values(), ...pending]) {
      const lines = [];
      for (const prop of ["background-color", "border", "box-shadow"]) {
        const after = rec.after && rec.after[prop];
        if (after !== undefined && after !== rec.before[prop]) lines.push(`  ${prop}: ${rec.before[prop]} → ${after}`);
      }
      if (rec.text && rec.text.after !== rec.text.before) lines.push(`  text: ${JSON.stringify(rec.text.before)} → ${JSON.stringify(rec.text.after)}`);
      if (!lines.length) continue;   // changed and changed back: nothing to hand over
      blocks.push([rec.selector + (rec.snippet ? `  (${JSON.stringify(rec.snippet)})` : ""), ...lines].join("\n"));
    }
    return blocks;
  }
  /** What gets copied or downloaded: a short heading that tells whoever reads it (a developer or Claude) what it is, then the blocks.
   *  It is meant to be pasted into a chat with Claude, who can then make the same changes in the source files. */
  function exportMarkdown() {
    const blocks = exportBlocks();
    if (!blocks.length) return "";
    return [
      "# UI changes from the designer picker",
      `Page: ${location.origin + location.pathname}`,
      `Viewport: ${window.innerWidth}×${window.innerHeight} · ${new Date().toISOString().slice(0, 10)}`,
      "Each block is one element: its selector (with a snippet of its text to confirm it), then before → after. Apply these in the source code.",
      "",
      blocks.join("\n\n"),
      "",
    ].join("\n");
  }
  function showToast(text) {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 3500);
  }
  function fileName() {
    const slug = location.pathname.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase();
    return `ui-changes-${slug || "home"}.md`;
  }
  function downloadExport() {
    const name = fileName();
    const url = URL.createObjectURL(new Blob([exportMarkdown()], { type: "text/markdown" }));
    const link = h("a", { href: url, download: name });
    root.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    exported = true;
    return name;
  }
  /** Copy every change to the clipboard (the Alt+Shift+E shortcut and the Copy button); if the browser refuses, download it instead. */
  async function copyChanges() {
    const count = exportBlocks().length;
    if (!count) { showToast("No changes to export yet."); return false; }
    const text = exportMarkdown();
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch (_) {
      const area = h("textarea", { class: "dz-offscreen", "aria-hidden": "true" });
      area.value = text;
      root.append(area);
      area.select();
      try { ok = document.execCommand("copy"); } catch (_e) { ok = false; }
      area.remove();
    }
    if (ok) {
      exported = true;
      showToast("Copied to Clipboard");
    } else {
      showToast(`The browser blocked copying, so ${downloadExport()} was downloaded instead.`);
    }
    return true;
  }

  // ---------- the colour picker (used by Fill and by Border's colour button) ----------
  function colourPicker(startHex, onChange) {
    let [r, g, b] = parseHex(startHex) || [255, 255, 255];
    let [hue, sat, val] = rgbToHsv(r, g, b);
    const sv = h("div", { class: "dz-sv", "aria-label": "Saturation and brightness" });
    const dot = h("div", { class: "dz-sv-dot" });
    sv.append(dot);
    const hueInput = h("input", { type: "range", min: "0", max: "359", class: "dz-range dz-hue", "aria-label": "Hue" });
    const sliders = {};
    const rows = [];
    for (const [key, name] of [["r", "R"], ["g", "G"], ["b", "B"]]) {
      const input = h("input", { type: "range", min: "0", max: "255", class: `dz-range dz-${key}`, "aria-label": name });
      const number = h("span", { class: "dz-num" });
      sliders[key] = { input, number };
      rows.push(h("label", { class: "dz-row" }, h("span", { text: name }), input, number));
    }
    const hexInput = h("input", { type: "text", class: "dz-hex", maxlength: "7", spellcheck: "false", "aria-label": "Hex colour" });
    const swatch = h("span", { class: "dz-swatch" });

    function show() {
      sv.style.backgroundColor = `hsl(${hue}, 100%, 50%)`;
      dot.style.left = sat * 100 + "%";
      dot.style.top = (1 - val) * 100 + "%";
      hueInput.value = String(Math.round(hue));
      sliders.r.input.value = String(Math.round(r)); sliders.r.number.textContent = String(Math.round(r));
      sliders.g.input.value = String(Math.round(g)); sliders.g.number.textContent = String(Math.round(g));
      sliders.b.input.value = String(Math.round(b)); sliders.b.number.textContent = String(Math.round(b));
      const text = toHex(r, g, b);
      if (document.activeElement !== hexInput) hexInput.value = text;
      swatch.style.backgroundColor = text;
    }
    const fromRgb = () => { [hue, sat, val] = rgbToHsv(r, g, b); if (sat === 0 || val === 0) hue = +hueInput.value; };
    const fromHsv = () => { [r, g, b] = hsvToRgb(hue, sat, val); };
    const emit = () => { show(); onChange(toHex(r, g, b)); };

    function dragSv(event) {
      const box = sv.getBoundingClientRect();
      sat = clamp((event.clientX - box.left) / box.width, 0, 1);
      val = 1 - clamp((event.clientY - box.top) / box.height, 0, 1);
      fromHsv();
      emit();
    }
    sv.addEventListener("pointerdown", (event) => { sv.setPointerCapture(event.pointerId); dragSv(event); });
    sv.addEventListener("pointermove", (event) => { if (sv.hasPointerCapture(event.pointerId)) dragSv(event); });
    hueInput.addEventListener("input", () => { hue = +hueInput.value; fromHsv(); emit(); });
    for (const key of ["r", "g", "b"]) {
      sliders[key].input.addEventListener("input", () => {
        r = +sliders.r.input.value; g = +sliders.g.input.value; b = +sliders.b.input.value;
        fromRgb(); emit();
      });
    }
    hexInput.addEventListener("input", () => {
      const parsed = parseHex(hexInput.value);
      if (!parsed) return;
      [r, g, b] = parsed;
      fromRgb(); show(); onChange(toHex(r, g, b));
    });
    hexInput.addEventListener("blur", show);
    show();
    return h("div", { class: "dz-picker" }, sv, h("label", { class: "dz-row" }, h("span", { text: "Hue" }), hueInput, swatch), ...rows,
             h("label", { class: "dz-row" }, h("span", { text: "Hex" }), hexInput));
  }

  // ---------- the dock ----------
  const NO_TEXT_TAGS = new Set(["IMG", "INPUT", "TEXTAREA", "SELECT", "BR", "HR", "SVG", "CANVAS", "VIDEO", "AUDIO", "IFRAME"]);
  function textBlocked(element) {
    if (element.children.length) return "Elements with children may not have text";
    if (NO_TEXT_TAGS.has(element.tagName.toUpperCase())) return "This element has no text to edit";
    return "";
  }
  function closeTool() { tool = null; }

  // ---------- the guide ----------
  const TOUR_KEY = "alchemy-designer-tour";
  const TOUR = [
    { title: "Select an element", lines: [
      "Alt+Shift+P turns the picker on and off.",
      "While it is on, clicking an element selects it; the click does not reach the page, so buttons and links do nothing."] },
    { title: "Move around", lines: [
      "↑ selects the parent of what you have selected. ↓ goes back down the way you came, or to the first child.",
      "Esc clears the selection."] },
    { title: "Change it", lines: [
      "Enter opens the tools: Fill (colour), Border (style, thickness, colour), Shadow (presets, or offset, blur, spread, opacity, colour) and Text.",
      "Text works only on elements with nothing inside them. The page changes straight away, in this tab only; Done closes the tool."] },
    { title: "Export your changes", lines: [
      "Alt+Shift+E copies every change as Markdown. Paste it to Claude, who can then make the same changes in the code.",
      "\"Changes\" lists them with Copy, Download and Reset. They are kept in this browser, through reloads, until you reset them."] },
  ];
  function endTour() {
    tourStep = null;
    try { localStorage.setItem(TOUR_KEY, "done"); } catch (_) { /* not remembered: it shows again next time */ }
    renderDock();
  }
  function tourPanel() {
    const page = TOUR[tourStep];
    const last = tourStep === TOUR.length - 1;
    return h("div", { class: "dz-tour" },
      h("div", { class: "dz-head" },
        h("strong", { text: `Guide ${tourStep + 1} / ${TOUR.length}` }),
        h("button", { type: "button", class: "dz-link", text: "Skip", onclick: endTour })),
      h("p", { class: "dz-tour-title", text: page.title }),
      ...page.lines.map((line) => h("p", { class: "dz-hint", text: line })),
      h("div", { class: "dz-menu" },
        h("button", { type: "button", class: "dz-menu-button", text: "Back", disabled: tourStep === 0, onclick: () => { tourStep -= 1; renderDock(); } }),
        h("button", { type: "button", class: "dz-menu-button dz-active", text: last ? "Done" : "Next",
                      onclick: () => { if (last) endTour(); else { tourStep += 1; renderDock(); } } })));
  }

  function renderDock() {
    dock.replaceChildren();
    dock.hidden = !on;
    toggle.textContent = on ? "Design: on" : "Design";
    toggle.classList.toggle("dz-on", on);
    if (!on) return;
    if (tourStep !== null) { dock.append(tourPanel()); return; }

    changesLink = h("button", { type: "button", class: "dz-link", text: `Changes (${changeCount()})`, onclick: () => { showChanges = !showChanges; showPatches = false; renderDock(); } });
    const head = h("div", { class: "dz-head" },
      h("strong", { text: selected ? label(selected) : "UI picker" }),
      changesLink,
      h("button", { type: "button", class: "dz-link", text: "Patches", title: "Design patches: switch them off, to preview, or on",
                    onclick: () => { showPatches = !showPatches; showChanges = false; renderDock(); } }),
      h("button", { type: "button", class: "dz-link", text: "Guide", title: "How the picker works", onclick: () => { tourStep = 0; renderDock(); } }),
      h("button", { type: "button", class: "dz-link", text: "Off", title: "Turn picker mode off", onclick: () => setOn(false) }));
    dock.append(head);

    if (showPatches) { dock.append(patchesPanel()); return; }
    if (showChanges) { dock.append(changesPanel()); return; }
    if (!selected) {
      dock.append(h("p", { class: "dz-hint", text: "Click any element to select it. ↑ parent, ↓ child, Enter for the tools, Esc to clear. Alt+Shift+E copies your changes." }));
      return;
    }
    const tools = () => h("div", { class: "dz-menu" },
      menuButton("fill", "Fill"), menuButton("border", "Border"), menuButton("shadow", "Shadow"), menuButton("text", "Text"));
    if (!menuOpen && !tool) {
      dock.append(h("p", { class: "dz-hint", text: "Press Enter, or choose a tool." }), tools());
      return;
    }
    dock.append(tools());
    if (tool === "fill") dock.append(fillPanel());
    else if (tool === "border") dock.append(borderPanel());
    else if (tool === "shadow") dock.append(shadowPanel());
    else if (tool === "text") dock.append(textPanel());
    // Edits apply as they are made, so there is nothing to confirm; Done just closes the tool and lets go of the element
    // (Esc does the same in steps). Without it a phone, which has no Esc key, had no visible way out of a tool.
    dock.append(h("div", { class: "dz-done-row" },
      h("span", { class: "dz-hint", text: "Changes apply as you go." }),
      h("button", { type: "button", class: "dz-menu-button dz-active", text: "Done", title: "Close the tool and deselect (Esc)", onclick: done })));
  }
  function done() {
    closeTool();
    menuOpen = false;
    selected = null;
    climbed = [];
    renderDock();
    refreshBoxes();
  }
  function menuButton(name, text) {
    const blocked = name === "text" && selected ? textBlocked(selected) : "";
    return h("button", {
      type: "button", class: "dz-menu-button" + (tool === name ? " dz-active" : ""), text, disabled: !!blocked,
      title: blocked || undefined, "aria-pressed": tool === name ? "true" : "false",
      onclick: () => { menuOpen = true; tool = tool === name ? null : name; renderDock(); },
    });
  }
  function currentFill(element) {
    const parsed = parseHex(colourText(getComputedStyle(element).backgroundColor));
    return parsed ? toHex(...parsed) : "#ffffff";
  }
  function fillPanel() {
    const element = selected;
    return h("div", { class: "dz-panel" }, colourPicker(currentFill(element), (colour) => applyFill(element, colour)));
  }
  function borderPanel() {
    const element = selected;
    const cs = getComputedStyle(element);
    let style = NO_BORDER(cs.borderTopStyle, cs.borderTopWidth) ? "none" : cs.borderTopStyle;
    let width = Math.round(parseFloat(cs.borderTopWidth)) || 0;
    const colourParsed = parseHex(colourText(cs.borderTopColor));
    let colour = colourParsed ? toHex(...colourParsed) : "#000000";
    const apply = () => applyBorder(element, style, width, colour);

    const styleSelect = h("select", { class: "dz-select", "aria-label": "Border style" },
      ...["none", "solid", "dashed", "dotted", "double"].map((s) => h("option", { value: s, text: s, selected: s === style })));
    const widthInput = h("input", { type: "range", min: "0", max: "20", class: "dz-range", "aria-label": "Border thickness" });
    const widthNumber = h("span", { class: "dz-num" });
    const showWidth = () => { widthInput.value = String(width); widthNumber.textContent = width + "px"; };
    styleSelect.addEventListener("change", () => {
      style = styleSelect.value;
      if (style !== "none" && width === 0) { width = 1; showWidth(); }
      apply();
    });
    widthInput.addEventListener("input", () => {
      width = +widthInput.value;
      if (width > 0 && style === "none") { style = "solid"; styleSelect.value = "solid"; }
      showWidth();
      apply();
    });
    showWidth();
    const holder = h("div", { class: "dz-colour-holder", hidden: true });
    const swatch = h("span", { class: "dz-swatch" });
    swatch.style.backgroundColor = colour;
    const colourButton = h("button", {
      type: "button", class: "dz-menu-button", "aria-expanded": "false",
      onclick: () => {
        holder.hidden = !holder.hidden;
        colourButton.setAttribute("aria-expanded", holder.hidden ? "false" : "true");
        if (!holder.hidden && !holder.firstChild) {
          holder.append(colourPicker(colour, (value) => {
            colour = value; swatch.style.backgroundColor = value;
            if (style === "none") { style = "solid"; styleSelect.value = "solid"; if (width === 0) { width = 1; showWidth(); } }
            apply();
          }));
        }
      },
    }, "Colour ", swatch);
    return h("div", { class: "dz-panel" },
      h("label", { class: "dz-row" }, h("span", { text: "Style" }), styleSelect),
      h("label", { class: "dz-row" }, h("span", { text: "Width" }), widthInput, widthNumber),
      colourButton, holder);
  }
  const SHADOW_PRESETS = [
    ["None", null],
    ["Subtle", { x: 0, y: 1, blur: 3, spread: 0, alpha: 0.15 }],
    ["Soft", { x: 0, y: 4, blur: 12, spread: 0, alpha: 0.15 }],
    ["Medium", { x: 0, y: 8, blur: 24, spread: 0, alpha: 0.2 }],
    ["Strong", { x: 0, y: 16, blur: 40, spread: 0, alpha: 0.3 }],
  ];
  function shadowPanel() {
    const element = selected;
    // Start from the element's own (first) shadow, or from a soft black one that is switched off.
    const first = splitTopLevel(String(getComputedStyle(element).boxShadow)).map(parseShadow).find(Boolean);
    let enabled = !!first;
    const s = first || { x: 0, y: 4, blur: 12, spread: 0, rgb: [0, 0, 0], alpha: 0.15, inset: false };
    const apply = () => applyShadow(element, enabled ? s : null);

    const sliders = {};
    const rows = [];
    for (const [key, name, min, max, unit] of [["x", "X", -40, 40, "px"], ["y", "Y", -40, 40, "px"], ["blur", "Blur", 0, 80, "px"],
                                                ["spread", "Spread", -20, 40, "px"], ["alpha", "Opacity", 0, 100, "%"]]) {
      const input = h("input", { type: "range", min: String(min), max: String(max), class: "dz-range", "aria-label": `Shadow ${name}` });
      const number = h("span", { class: "dz-num" });
      sliders[key] = { input, number, unit };
      input.addEventListener("input", () => {
        s[key] = key === "alpha" ? +input.value / 100 : +input.value;
        enabled = true;
        show();
        apply();
      });
      rows.push(h("label", { class: "dz-row" }, h("span", { text: name }), input, number));
    }
    const inset = h("input", { type: "checkbox", "aria-label": "Inner shadow" });
    inset.addEventListener("change", () => { s.inset = inset.checked; enabled = true; show(); apply(); });
    const swatch = h("span", { class: "dz-swatch" });
    const presetButtons = SHADOW_PRESETS.map(([name, preset]) => h("button", {
      type: "button", class: "dz-menu-button", text: name,
      onclick: () => {
        if (preset) { Object.assign(s, preset, { inset: false }); enabled = true; } else enabled = false;
        show();
        apply();
      },
    }));
    function show() {
      for (const [key, { input, number, unit }] of Object.entries(sliders)) {
        const value = key === "alpha" ? Math.round(s.alpha * 100) : s[key];
        input.value = String(value);
        number.textContent = value + unit;
      }
      inset.checked = s.inset;
      swatch.style.backgroundColor = toHex(...s.rgb);
      presetButtons.forEach((button, i) => {
        const preset = SHADOW_PRESETS[i][1];
        const active = preset ? enabled && !s.inset && Object.entries(preset).every(([k, v]) => s[k] === v) : !enabled;
        button.classList.toggle("dz-active", active);
      });
    }
    const holder = h("div", { class: "dz-colour-holder", hidden: true });
    const colourButton = h("button", {
      type: "button", class: "dz-menu-button", "aria-expanded": "false",
      onclick: () => {
        holder.hidden = !holder.hidden;
        colourButton.setAttribute("aria-expanded", holder.hidden ? "false" : "true");
        if (!holder.hidden && !holder.firstChild) {
          holder.append(colourPicker(toHex(...s.rgb), (value) => { s.rgb = parseHex(value); enabled = true; show(); apply(); }));
        }
      },
    }, "Colour ", swatch);
    show();
    return h("div", { class: "dz-panel dz-shadow" },
      h("div", { class: "dz-menu" }, ...presetButtons),
      ...rows,
      h("label", { class: "dz-row" }, inset, h("span", { text: "Inner shadow (inset)", class: "dz-wide" })),
      colourButton, holder);
  }
  function textPanel() {
    const element = selected;
    const input = h("textarea", { class: "dz-text", rows: "3", "aria-label": "Element text" });
    input.value = element.textContent;
    input.addEventListener("input", () => applyText(element, input.value));
    return h("div", { class: "dz-panel" }, input);
  }
  // ---------- design patches ----------
  const PATCH_STATES = [["off", "Off"], ["preview", "Preview"], ["on", "On"]];
  /** Reload /patches.css in place, so a style patch shows (or goes) without reloading the page. */
  function reloadPatchStyles() {
    const link = document.querySelector('link[rel="stylesheet"][href^="/patches.css"]');
    if (link) link.href = "/patches.css?t=" + Date.now();
  }
  function patchesPanel() {
    const panel = h("div", { class: "dz-panel" }, h("p", { class: "dz-hint", text: "Loading patches…" }));
    Auth.fetch("/patches/list").then(({ patches }) => fillPatches(panel, patches))
      .catch((err) => panel.replaceChildren(h("p", { class: "dz-hint", text: err.message })));
    return panel;
  }
  function fillPatches(panel, patches) {
    panel.replaceChildren(h("p", { class: "dz-hint", text: "Preview: only admins and designers see it. On: everyone." }));
    if (!patches.length) panel.append(h("p", { class: "dz-hint", text: "No patches yet." }));
    for (const patch of patches) {
      const buttons = PATCH_STATES.map(([state, label]) => h("button", {
        type: "button", class: "dz-menu-button" + (patch.state === state ? " dz-active" : ""), text: label,
        "aria-pressed": patch.state === state ? "true" : "false",
        onclick: async () => {
          if (patch.state === state) return;
          try {
            const data = await Auth.fetch("/patches/set", { name: patch.name, state });
            fillPatches(panel, data.patches);
            reloadPatchStyles();
            showToast(patch.text_changes ? `${patch.name}: ${label}. Reload the page for its text changes (your edits are kept).` : `${patch.name}: ${label}`);
          } catch (err) { showToast(err.message); }
        },
      }));
      panel.append(h("div", { class: "dz-patch" },
        h("strong", { text: patch.name }), h("span", { class: "dz-hint", text: patch.title + (patch.problem ? ` (${patch.problem})` : "") }),
        h("div", { class: "dz-menu" }, ...buttons)));
    }
  }

  function changesPanel() {
    const panel = h("div", { class: "dz-panel" });
    if (!changeCount()) { panel.append(h("p", { class: "dz-hint", text: "No changes yet." })); return panel; }
    for (const [element, rec] of changes) {
      panel.append(h("div", { class: "dz-change" },
        h("code", { text: rec.selector }),
        h("button", { type: "button", class: "dz-link", text: "Reset", onclick: () => resetElement(element) })));
    }
    for (const item of pending) {   // saved, but the element is not on the page right now
      panel.append(h("div", { class: "dz-change", title: "Kept and exported, but this element is not on the page right now" },
        h("code", { text: item.selector + " (not on the page)" }),
        h("button", { type: "button", class: "dz-link", text: "Forget", onclick: () => forgetPending(item) })));
    }
    const copy = h("button", { type: "button", class: "dz-menu-button", text: "Copy", title: "Alt+Shift+E", onclick: copyChanges });
    panel.append(h("pre", { class: "dz-preview", text: exportMarkdown() || "(nothing differs from the original)" }),
      h("div", { class: "dz-menu" }, copy,
        h("button", { type: "button", class: "dz-menu-button", text: "Download", onclick: () => showToast(`Downloaded ${downloadExport()}.`) }),
        h("button", { type: "button", class: "dz-menu-button", text: "Reset all", onclick: resetAll })));
    return panel;
  }

  // ---------- picker mode ----------
  function select(element) {
    if (!element || ours(element) || element === document.documentElement) return;
    selected = element;
    closeTool();
    menuOpen = false;
    showChanges = false;
    showPatches = false;
    renderDock();
    refreshBoxes();
  }
  const STOPPED = ["click", "dblclick", "mousedown", "mouseup", "pointerdown", "pointerup", "auxclick", "contextmenu", "submit", "dragstart"];
  function swallow(event) {
    if (ours(event.target)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.type === "click") { climbed = []; select(event.target); }
  }
  function onMove(event) {
    if (ours(event.target)) { hovered = null; } else { hovered = event.target === document.documentElement ? null : event.target; }
    refreshBoxes();
  }
  function onKey(event) {
    if (event.altKey && event.shiftKey && event.code === "KeyP") { event.preventDefault(); setOn(!on); return; }
    if (event.altKey && event.shiftKey && event.code === "KeyE") { event.preventDefault(); copyChanges(); return; }   // works with the picker off too
    if (!on) return;
    if (tourStep !== null) {   // the guide is open: Esc closes it, the arrows turn its pages
      if (event.key === "Escape") { event.preventDefault(); endTour(); return; }
      if (event.key === "ArrowRight" && tourStep < TOUR.length - 1) { event.preventDefault(); tourStep += 1; renderDock(); return; }
      if (event.key === "ArrowLeft" && tourStep > 0) { event.preventDefault(); tourStep -= 1; renderDock(); return; }
    }
    const typing = ours(event.target) && /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
    if (event.key === "Escape") {
      event.preventDefault();
      if (showPatches) showPatches = false;
      else if (showChanges) showChanges = false;
      else if (tool) closeTool();
      else if (menuOpen) menuOpen = false;
      else if (selected) { selected = null; climbed = []; }
      renderDock(); refreshBoxes();
      return;
    }
    if (typing || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "ArrowUp" && selected) {
      event.preventDefault();
      const parent = selected.parentElement;
      if (parent && parent !== document.documentElement && !ours(parent)) { climbed.push(selected); select(parent); }
    } else if (event.key === "ArrowDown" && selected) {
      event.preventDefault();
      let next = climbed.pop();
      if (!next || !selected.contains(next) || next === selected) next = [...selected.children].find((c) => !ours(c)) || null;
      if (next) select(next);
    } else if (event.key === "Enter" && selected && (!ours(event.target) || event.target === toggle)) {   // the Design button keeps focus after a mouse click: Enter must not press it
      event.preventDefault();
      menuOpen = true;
      renderDock();
    }
  }
  function setOn(value) {
    on = value;
    const method = on ? "addEventListener" : "removeEventListener";
    for (const type of STOPPED) window[method](type, swallow, true);
    window[method]("mousemove", onMove, true);
    if (!on) { hovered = null; selected = null; climbed = []; menuOpen = false; tool = null; showChanges = false; tourStep = null; showPatches = false; }
    else {
      let seen = false;
      try { seen = localStorage.getItem(TOUR_KEY) === "done"; } catch (_) { /* unreadable: show the guide */ }
      if (!seen) tourStep = 0;   // the first time on this browser
    }
    renderDock();
    refreshBoxes();
  }

  window.addEventListener("keydown", onKey, true);
  window.addEventListener("scroll", refreshBoxes, { capture: true, passive: true });
  window.addEventListener("resize", refreshBoxes);
  setInterval(() => { if (on && selected) refreshBoxes(); }, 500);   // the page may move things around (the board does)
  toggle.addEventListener("click", () => setOn(!on));
  document.body.append(root);
  restore();
  if (typeof takeGuide === "function" && takeGuide("designer")) {   // asked for on the Guides page
    setOn(true);
    tourStep = 0;
    renderDock();
  }
})();
