"use strict";
// The designer's UI picker. Accounts marked as designers (accounts_admin.py make-designer) get this on every page (auth.js loads it).
// Alt+Shift+P (or the Design button) turns picker mode on: clicks select an element instead of reaching the page; ↑ selects the
// parent, ↓ a child, Enter opens Fill / Border / Text, Esc closes things and clears the selection. Edits are inline styles on THIS
// browser's copy of the page only: nothing is sent to the server or saved, and a reload wipes them. "Changes" exports them as
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

  // ---------- state ----------
  let on = false;
  let selected = null;
  let hovered = null;
  let climbed = [];            // the elements we came up from, so ↓ goes back the way we came
  let menuOpen = false;
  let tool = null;             // "fill" | "border" | "text" | null
  let showChanges = false;
  let changesLink = null;      // the "Changes (n)" button in the dock, kept so its count can change without rebuilding the dock
  let exported = true;         // false while there are edits that were not copied or downloaded
  const changes = new Map();   // element -> {selector, tag, snippet, props: Map(prop -> {before, after, original}), text}

  // ---------- page furniture ----------
  const root = h("div", { class: "dz-root", "data-designer": "" });
  const hoverBox = h("div", { class: "dz-box dz-hover", hidden: true });
  const selectBox = h("div", { class: "dz-box dz-picked", hidden: true });
  const dock = h("div", { class: "dz-dock", role: "region", "aria-label": "UI picker", hidden: true });
  const toggle = h("button", { class: "dz-toggle", type: "button", title: "UI picker (Alt+Shift+P)", text: "Design" });
  root.append(hoverBox, selectBox, dock, toggle);

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
    const same = prop.endsWith("color") ? colourText(now) === value.toLowerCase() : now === value;
    if (!skip && !same) element.style.setProperty(prop, value, "important");
    if (saved[0]) element.style.setProperty("transition", saved[0], saved[1]); else element.style.removeProperty("transition");
  }
  /** An edit was made. The panel is NOT rebuilt (a slider being dragged must stay in the page); only the count and outlines change. */
  function edited() {
    exported = false;
    if (changesLink) changesLink.textContent = `Changes (${changes.size})`;
    refreshBoxes();
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
    renderDock();
    refreshBoxes();
  }
  function resetAll() {
    for (const element of [...changes.keys()]) resetElement(element);
    exported = true;
    renderDock();
  }

  // ---------- export ----------
  function exportMarkdown() {
    const blocks = [];
    for (const rec of changes.values()) {
      const lines = [];
      for (const prop of ["background-color", "border"]) {
        const after = rec.after && rec.after[prop];
        if (after !== undefined && after !== rec.before[prop]) lines.push(`  ${prop}: ${rec.before[prop]} → ${after}`);
      }
      if (rec.text && rec.text.after !== rec.text.before) lines.push(`  text: ${JSON.stringify(rec.text.before)} → ${JSON.stringify(rec.text.after)}`);
      if (!lines.length) continue;
      blocks.push([rec.selector + (rec.snippet ? `  (${JSON.stringify(rec.snippet)})` : ""), ...lines].join("\n"));
    }
    return blocks.length ? location.origin + location.pathname + "\n" + blocks.join("\n") + "\n" : "";
  }
  async function copyExport(button) {
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
    if (ok) exported = true;
    button.textContent = ok ? "Copied" : "Copy failed: use Download";
    setTimeout(() => { button.textContent = "Copy"; }, 1600);
  }
  function downloadExport() {
    const url = URL.createObjectURL(new Blob([exportMarkdown()], { type: "text/markdown" }));
    const link = h("a", { href: url, download: "ui-changes.md" });
    root.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    exported = true;
  }
  window.addEventListener("beforeunload", (event) => {
    if (changes.size && !exported) { event.preventDefault(); event.returnValue = ""; }
  });

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

  function renderDock() {
    dock.replaceChildren();
    dock.hidden = !on;
    toggle.textContent = on ? "Design: on" : "Design";
    toggle.classList.toggle("dz-on", on);
    if (!on) return;

    changesLink = h("button", { type: "button", class: "dz-link", text: `Changes (${changes.size})`, onclick: () => { showChanges = !showChanges; renderDock(); } });
    const head = h("div", { class: "dz-head" },
      h("strong", { text: selected ? label(selected) : "UI picker" }),
      changesLink,
      h("button", { type: "button", class: "dz-link", text: "Off", title: "Turn picker mode off", onclick: () => setOn(false) }));
    dock.append(head);

    if (showChanges) { dock.append(changesPanel()); return; }
    if (!selected) {
      dock.append(h("p", { class: "dz-hint", text: "Click any element to select it. ↑ parent, ↓ child, Enter for the tools, Esc to clear." }));
      return;
    }
    if (!menuOpen && !tool) {
      dock.append(h("p", { class: "dz-hint", text: "Press Enter, or choose a tool." }),
                  h("div", { class: "dz-menu" }, menuButton("fill", "Fill"), menuButton("border", "Border"), menuButton("text", "Text")));
      return;
    }
    dock.append(h("div", { class: "dz-menu" }, menuButton("fill", "Fill"), menuButton("border", "Border"), menuButton("text", "Text")));
    if (tool === "fill") dock.append(fillPanel());
    else if (tool === "border") dock.append(borderPanel());
    else if (tool === "text") dock.append(textPanel());
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
  function textPanel() {
    const element = selected;
    const input = h("textarea", { class: "dz-text", rows: "3", "aria-label": "Element text" });
    input.value = element.textContent;
    input.addEventListener("input", () => applyText(element, input.value));
    return h("div", { class: "dz-panel" }, input);
  }
  function changesPanel() {
    const panel = h("div", { class: "dz-panel" });
    if (!changes.size) { panel.append(h("p", { class: "dz-hint", text: "No changes yet." })); return panel; }
    for (const [element, rec] of changes) {
      panel.append(h("div", { class: "dz-change" },
        h("code", { text: rec.selector }),
        h("button", { type: "button", class: "dz-link", text: "Reset", onclick: () => resetElement(element) })));
    }
    const copy = h("button", { type: "button", class: "dz-menu-button", text: "Copy", onclick: () => copyExport(copy) });
    panel.append(h("pre", { class: "dz-preview", text: exportMarkdown() || "(nothing differs from the original)" }),
      h("div", { class: "dz-menu" }, copy,
        h("button", { type: "button", class: "dz-menu-button", text: "Download", onclick: downloadExport }),
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
    if (!on) return;
    const typing = ours(event.target) && /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
    if (event.key === "Escape") {
      event.preventDefault();
      if (showChanges) showChanges = false;
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
    } else if (event.key === "Enter" && selected && !ours(event.target)) {
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
    if (!on) { hovered = null; selected = null; climbed = []; menuOpen = false; tool = null; showChanges = false; }
    renderDock();
    refreshBoxes();
  }

  window.addEventListener("keydown", onKey, true);
  window.addEventListener("scroll", refreshBoxes, { capture: true, passive: true });
  window.addEventListener("resize", refreshBoxes);
  setInterval(() => { if (on && selected) refreshBoxes(); }, 500);   // the page may move things around (the board does)
  toggle.addEventListener("click", () => setOn(!on));
  document.body.append(root);
})();
