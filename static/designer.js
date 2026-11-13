"use strict";
// The designer's UI picker. Accounts marked as designers (accounts_admin.py make-designer) get this on every page (auth.js loads it).
// Alt+Shift+P (or the Design button) turns picker mode on: clicks select an element instead of reaching the page; ↑ selects the
// parent, ↓ a child, Enter opens Fill / Border / Shadow / Text / Font / Size / Spacing / Radius / Layout, Ctrl+Z undoes, Esc closes things and clears the selection; Alt+Shift+E copies every change
// as Markdown (the guide, shown the first time and under "Guide", explains all of this). Edits are inline styles on THIS
// browser's copy of the page only: nothing is sent to the server; they are kept in this browser (localStorage, per page) and put back after a reload. "Changes" exports them as
// Markdown (selector, before → after) to hand to a developer or an AI. This is a convenience for designers, not a security
// boundary: anyone can do the same in their browser's developer tools, and the server never trusts any of it.
// The same file is the Chrome extension's picker (extension/): everything that depends on where it runs is in `host` below.
(function boot() {
  const pre = window.__designerHost;
  if (pre && pre.ready && !pre.loaded) { pre.ready.then(() => { pre.loaded = true; boot(); }); return; }   // the extension loads its saved state first
  if (window.__designerLoaded) return;
  window.__designerLoaded = true;

  // ---------- where it runs ----------
  // Everything that depends on the place the picker runs in goes through `host`. These defaults are the game's own pages (edits in
  // localStorage, patches from the server, a guide on the Guides page). The Chrome extension (extension/) sets window.__designerHost
  // before this file runs to replace any of them: chrome.storage, no patches, its own mount point (a Shadow DOM), and so on.
  const host = Object.assign({
    storageKey: () => "alchemy-designer-changes:" + location.pathname,
    readEdits(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
    writeEdits(key, value) { try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (_) { /* full or blocked: edits still work in this tab */ } },
    onEditsChanged(key, callback) { window.addEventListener("storage", (event) => { if (event.key === key || event.key === null) callback(); }); },
    tourSeen() { try { return localStorage.getItem("alchemy-designer-tour") === "done"; } catch (_) { return false; } },
    markTourSeen() { try { localStorage.setItem("alchemy-designer-tour", "done"); } catch (_) { /* shows again next time */ } },
    patches: typeof Auth !== "undefined" ? {
      list: () => Auth.fetch("/patches/list").then((data) => data.patches),
      set: (name, state) => Auth.fetch("/patches/set", { name, state }).then((data) => data.patches),
      reload() {   // pick up /patches.css again, so a style patch shows (or goes) without reloading the page
        const link = document.querySelector('link[rel="stylesheet"][href^="/patches.css"]');
        if (link) link.href = "/patches.css?t=" + Date.now();
      },
    } : null,
    takeGuide: (name) => typeof takeGuide === "function" && takeGuide(name),
    isPreview: () => window.name === "dz-preview",
    openPreview(width, height) {   // true if a window opened
      const win = window.open(location.href, "dz-preview", `popup,width=${width},height=${height}`);
      if (!win) return false;
      win.focus();
      setTimeout(() => {   // make the inside of the window, not its frame, the size asked for
        try { win.resizeTo(width + (win.outerWidth - win.innerWidth), height + (win.outerHeight - win.innerHeight)); } catch (_) { /* not allowed: it stays as opened */ }
      }, 500);
      return true;
    },
    mount(root) { document.body.append(root); },
    isOwn: () => false,   // a node outside the root that is still the picker's (the extension's shadow host)
  }, pre || {});

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

  // ---------- type and contrast ----------
  const TEXT_ALIGNS = ["left", "center", "right", "justify"];
  const WEIGHTS = [[100, "Thin"], [200, "Extra light"], [300, "Light"], [400, "Regular"], [500, "Medium"], [600, "Semibold"], [700, "Bold"], [800, "Extra bold"], [900, "Black"]];
  /** Line height the way designers say it: a multiple of the font size ("1.5"), or "normal". */
  function lineHeightOf(cs) {
    if (cs.lineHeight === "normal") return "normal";
    const size = parseFloat(cs.fontSize);
    return size ? String(Math.round((parseFloat(cs.lineHeight) / size) * 100) / 100) : cs.lineHeight;
  }
  const alignOf = (cs) => ({ start: "left", end: "right" }[cs.textAlign] || cs.textAlign);

  const colourContext = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  /** Any CSS colour as [r, g, b, alpha 0..1] (the browser does the parsing, so rgb(), hex, hsl() and oklch() all work); null if it cannot. */
  function toRgba(css) {
    if (!colourContext) return null;
    try {
      colourContext.canvas.width = colourContext.canvas.height = 1;
      colourContext.fillStyle = "#000";
      colourContext.fillStyle = css;
      colourContext.fillRect(0, 0, 1, 1);
      const [r, g, b, a] = colourContext.getImageData(0, 0, 1, 1).data;
      return [r, g, b, a / 255];
    } catch (_) { return null; }
  }
  const over = ([r, g, b, a], [br, bg, bb]) => [r * a + br * (1 - a), g * a + bg * (1 - a), b * a + bb * (1 - a)];
  function luminance([r, g, b]) {
    const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  }
  const contrastOf = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
  /** What is behind the element's text: its own background, then its ancestors' until one is solid (white if none is). A picture or
   *  gradient cannot be measured: unknown is true then. */
  function backdrop(element) {
    const layers = [];
    let unknown = false;
    for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
      const cs = getComputedStyle(node);
      if (cs.backgroundImage !== "none") unknown = true;
      const c = toRgba(cs.backgroundColor);
      if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
      if (unknown) break;
    }
    let base = [255, 255, 255];
    for (const layer of layers.reverse()) base = over(layer, base);
    return { rgb: base, unknown };
  }
  /** APCA lightness contrast Lc (the measure proposed for WCAG 3): how readable the text is. Negative means light text on a darker
   *  background. Checked against the published reference values (black on white 106.04, white on black -107.88). */
  function apcaOf(textRgb, backRgb) {
    const y = ([r, g, b]) => 0.2126729 * (r / 255) ** 2.4 + 0.7151522 * (g / 255) ** 2.4 + 0.072175 * (b / 255) ** 2.4;
    const clampBlack = (v) => (v > 0.022 ? v : v + (0.022 - v) ** 1.414);
    const t = clampBlack(y(textRgb)), b = clampBlack(y(backRgb));
    if (Math.abs(b - t) < 0.0005) return 0;
    if (b > t) { const sapc = (b ** 0.56 - t ** 0.57) * 1.14; return sapc < 0.1 ? 0 : (sapc - 0.027) * 100; }
    const sapc = (b ** 0.65 - t ** 0.62) * 1.14;
    return sapc > -0.1 ? 0 : (sapc + 0.027) * 100;
  }
  const apcaUse = (lc) => { const a = Math.abs(lc); return a >= 90 ? "any text" : a >= 75 ? "body text" : a >= 60 ? "large or bold text" : a >= 45 ? "headlines" : a >= 30 ? "spot text only" : "not readable"; };
  /** The element's text colour against what is behind it, with the WCAG 2 thresholds for its size (large text needs less). */
  function contrastInfo(element) {
    const cs = getComputedStyle(element);
    const back = backdrop(element);
    const fore = toRgba(cs.color);
    if (!fore) return { unknown: true };
    const size = parseFloat(cs.fontSize);
    const large = size >= 24 || (parseInt(cs.fontWeight, 10) >= 700 && size >= 18.66);
    const need = large ? 3 : 4.5, needAaa = large ? 4.5 : 7;
    const ratio = contrastOf(over(fore, back.rgb), back.rgb);
    return { unknown: back.unknown, ratio, need, aa: ratio >= need, aaa: ratio >= needAaa, apca: apcaOf(over(fore, back.rgb), back.rgb) };
  }
  const contrastLabel = (info) => (info.unknown ? "unknown" : info.ratio.toFixed(2) + ":1");
  /** A live line under a colour picker: the contrast ratio and whether it passes WCAG AA and AAA. update() after each change. */
  function contrastNode(element) {
    const node = h("p", { class: "dz-contrast" });
    function update() {
      if (!element.textContent.trim()) { node.hidden = true; return; }   // no words in it: nothing to read
      node.hidden = false;
      const info = contrastInfo(element);
      node.className = "dz-contrast " + (info.unknown ? "dz-unknown" : info.aa ? "dz-pass" : "dz-fail");
      node.textContent = info.unknown ? "Contrast: the background is a picture or gradient, so it cannot be measured."
        : `Contrast ${info.ratio.toFixed(2)}:1 · AA ${info.aa ? "✓" : `✗ (needs ${info.need}:1)`} · AAA ${info.aaa ? "✓" : "✗"} · APCA Lc ${Math.round(info.apca)} (${apcaUse(info.apca)})`;
    }
    update();
    return { node, update };
  }

  // ---------- the page's colours ----------
  let paletteCache = null;
  /** The colours this page already uses, so a designer can pick one rather than a near miss: first the site's own colour variables
   *  (--brown and the like, with their names), then the colours most used on the page. [{hex, names, count}] */
  function pagePalette() {
    if (paletteCache && Date.now() - paletteCache.at < 5000) return paletteCache.list;
    const found = new Map();
    const add = (css, name) => {
      const c = toRgba(css);
      if (!c || c[3] < 1) return;
      const hex = toHex(c[0], c[1], c[2]);
      const item = found.get(hex) || { hex, names: [], count: 0 };
      if (name) { if (!item.names.includes(name)) item.names.push(name); } else item.count += 1;
      found.set(hex, item);
    };
    const names = new Set();
    const walk = (rules) => {
      for (const rule of rules) {
        if (rule.style) for (let i = 0; i < rule.style.length; i++) if (rule.style[i].startsWith("--")) names.add(rule.style[i]);
        if (rule.cssRules) walk(rule.cssRules);
      }
    };
    for (const sheet of document.styleSheets) { try { walk(sheet.cssRules); } catch (_) { /* another site's sheet: not readable */ } }
    const rootStyle = getComputedStyle(document.documentElement);
    for (const name of names) {
      const value = rootStyle.getPropertyValue(name).trim();
      if (/^(#|rgb|hsl|oklch|color\()/i.test(value)) add(value, name);
    }
    let seen = 0;
    for (const element of document.body.querySelectorAll("*")) {
      if (++seen > 1500) break;
      if (ours(element)) continue;
      const cs = getComputedStyle(element);
      add(cs.color); add(cs.backgroundColor); add(cs.borderTopColor);
    }
    const list = [...found.values()].sort((a, b) => (b.names.length > 0) - (a.names.length > 0) || b.count - a.count).slice(0, 20);
    paletteCache = { at: Date.now(), list };
    return list;
  }

  // ---------- sizes, spacing and corners ----------
  const SIDES = ["top", "right", "bottom", "left"];
  const CORNERS = ["border-top-left-radius", "border-top-right-radius", "border-bottom-right-radius", "border-bottom-left-radius"];
  /** CSS shorthand for four values (top right bottom left), as short as it can be: "8px", "8px 12px", "8px 12px 4px", or all four. */
  function sidesText(values) {
    const [t, r, b, l] = values;
    if (t === r && r === b && b === l) return t;
    if (t === b && r === l) return `${t} ${r}`;
    if (r === l) return `${t} ${r} ${b}`;
    return `${t} ${r} ${b} ${l}`;
  }
  /** A padding or margin shorthand ("8px 12px") back to four numbers, or null if it is not plain pixels. */
  function parseSides(text) {
    const parts = String(text).trim().split(/\s+/).map((p) => (/^-?\d+(\.\d+)?px$/.test(p) ? parseFloat(p) : NaN));
    if (!parts.length || parts.length > 4 || parts.some(Number.isNaN)) return null;
    const [t, r = t, b = t, l = r] = parts;
    return [t, r, b, l];
  }
  const sideNumbers = (cs, kind) => SIDES.map((side) => Math.round((parseFloat(cs.getPropertyValue(`${kind}-${side}`)) || 0) * 10) / 10);
  const sidesOf = (cs, kind) => sidesText(sideNumbers(cs, kind).map((n) => n + "px"));
  const radiusOf = (cs) => sidesText(CORNERS.map((corner) => cs.getPropertyValue(corner).split(" ")[0]));
  /** The size the designer sees and sets: the element's border box in pixels. */
  function boxSize(element) {
    const r = element.getBoundingClientRect();
    return { width: Math.round(r.width), height: Math.round(r.height) };
  }
  /** The width/height value that makes the border box `target` pixels (the site's boxes are border-box; this covers the ones that are not). */
  function cssSize(element, axis, target) {
    const cs = getComputedStyle(element);
    if (cs.boxSizing === "border-box") return target;
    const extra = axis === "width"
      ? ["paddingLeft", "paddingRight", "borderLeftWidth", "borderRightWidth"] : ["paddingTop", "paddingBottom", "borderTopWidth", "borderBottomWidth"];
    return Math.max(0, target - extra.reduce((sum, key) => sum + (parseFloat(cs[key]) || 0), 0));
  }

  // ---------- state ----------
  let on = false;
  let selected = null;         // the element the tools show the values of: the last one picked
  let selection = [];          // everything selected (Shift-click adds); the tools change all of it
  let hovered = null;
  let climbed = [];            // the elements we came up from, so ↓ goes back the way we came
  let menuOpen = false;
  let tool = null;             // "fill" | "border" | "shadow" | "text" | "font" | "size" | "spacing" | "radius" | "layout" | null
  let showChanges = false;
  let showPreview = false;    // the screen-size preview panel
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

  const ours = (node) => node instanceof Node && (root.contains(node) || host.isOwn(node));
  /** The element that has focus: inside a Shadow DOM document.activeElement is only the shadow host. */
  const activeEl = () => { const top = root.getRootNode(); return top instanceof ShadowRoot ? top.activeElement : document.activeElement; };
  /** What an event really hit: event.target is retargeted to the shadow host for anything inside one. */
  const eventTarget = (event) => (event.composedPath && event.composedPath()[0]) || event.target;

  // Drag handles on the selected element while the Size tool is open: right edge, bottom edge, and the corner (Shift keeps the proportions).
  let sizeSync = null;    // the Size panel's way of updating its fields while a handle is dragged
  let drag = null;        // {dir, x, y, w, h} while a handle is held
  const handles = [["e", "dz-h-e", "Drag to change the width"], ["s", "dz-h-s", "Drag to change the height"],
                   ["se", "dz-h-se", "Drag to change both (Shift keeps the proportions)"]]
    .map(([dir, cls, title]) => h("div", { class: "dz-handle " + cls, title, "data-dir": dir }));
  selectBox.append(...handles);
  for (const handle of handles) {
    handle.addEventListener("pointerdown", (event) => {
      if (!selected || tool !== "size" || event.button !== 0) return;
      event.preventDefault();
      const r = selected.getBoundingClientRect();
      drag = { dir: handle.dataset.dir, x: event.clientX, y: event.clientY, w: r.width, h: r.height };
      lastEdit = { kind: "", elements: [], at: 0 };   // the drag starts a new step ...
      holdStep = true;                                    // ... and stays one however slowly it goes
      handle.setPointerCapture(event.pointerId);
    });
    handle.addEventListener("pointermove", (event) => {
      if (!drag || !selected) return;
      let w = drag.dir.includes("e") ? drag.w + event.clientX - drag.x : null;
      let height = drag.dir.includes("s") ? drag.h + event.clientY - drag.y : null;
      if (drag.dir === "se" && event.shiftKey) {   // keep the shape: follow whichever side was pulled further
        const scale = Math.max(w / drag.w, height / drag.h);
        w = drag.w * scale;
        height = drag.h * scale;
      }
      const sizes = {};
      if (w !== null) sizes.width = Math.max(1, Math.round(w));
      if (height !== null) sizes.height = Math.max(1, Math.round(height));
      applySize(selected, sizes);
      if (sizeSync) sizeSync();
    });
    const release = () => { drag = null; holdStep = false; };
    handle.addEventListener("pointerup", release);
    handle.addEventListener("pointercancel", release);
  }
  function updateHandles() {
    selectBox.classList.toggle("dz-sizing", on && selection.length === 1 && tool === "size" && tourStep === null && !showChanges && !showPatches);
  }


  function place(box, element) {
    if (!element || !document.contains(element)) { box.hidden = true; return; }
    const r = element.getBoundingClientRect();
    box.hidden = false;
    box.style.left = r.left + "px";
    box.style.top = r.top + "px";
    box.style.width = r.width + "px";
    box.style.height = r.height + "px";
  }
  /** Make `list` the selection (the last of it is the one the tools show). Elements that left the page are dropped. */
  function setSelection(list) {
    selection = list.filter((el) => el && document.contains(el));
    selected = selection.length ? selection[selection.length - 1] : null;
  }
  const targets = () => selection.slice();   // what an edit applies to
  const extraBoxes = [];                     // outlines for the selected elements other than the primary one
  function placeSelection() {
    const others = on ? selection.filter((el) => el !== selected) : [];
    while (extraBoxes.length < others.length) { const box = h("div", { class: "dz-box dz-picked dz-more", hidden: true }); root.append(box); extraBoxes.push(box); }
    extraBoxes.forEach((box, i) => place(box, others[i] || null));
  }
  function refreshBoxes() {
    if (selection.some((el) => !document.contains(el))) { setSelection(selection); climbed = []; if (!selected) closeTool(); }
    place(hoverBox, on && hovered !== selected ? hovered : null);
    renderInspect(hovered);
    place(selectBox, on ? selected : null);
    placeSelection();
    updateHandles();
    renderMeasure();
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
          "border-radius": radiusOf(cs),
          padding: sidesOf(cs, "padding"),
          margin: sidesOf(cs, "margin"),
          width: boxSize(element).width + "px",
          height: boxSize(element).height + "px",
          color: colourText(cs.color),
          "font-size": cs.fontSize,
          "font-weight": cs.fontWeight,
          "line-height": lineHeightOf(cs),
          "letter-spacing": cs.letterSpacing === "normal" ? "normal" : cs.letterSpacing,
          "text-align": alignOf(cs),
          contrast: contrastLabel(contrastInfo(element)),
          display: cs.display,
          "flex-direction": cs.flexDirection,
          "flex-wrap": cs.flexWrap,
          "justify-content": cs.justifyContent,
          "align-items": cs.alignItems,
          "justify-items": cs.justifyItems,
          gap: cs.rowGap === cs.columnGap ? cs.rowGap : `${cs.rowGap} ${cs.columnGap}`,
          opacity: cs.opacity,
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
      : prop === "box-shadow" ? shadowText(now) === shadowText(value)
      : prop === "line-height" ? Math.abs(parseFloat(now) - parseFloat(value) * parseFloat(computed.fontSize)) < 0.5 : now === value;
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
  /** Only an end state that differs from the start is a change: set and set back (or switched and switched back) is not. */
  const hasChange = (item) => Object.entries(item.after || {}).some(([prop, value]) => value !== (item.before || {})[prop])
    || !!(item.text && item.text.after !== item.text.before);
  const changeCount = () => [...changes.values(), ...pending].filter(hasChange).length;
  function showCount() { if (changesLink) changesLink.textContent = `Changes (${changeCount()})`; }

  // ---------- undo and redo ----------
  // A step is a snapshot, taken just before an edit, of the properties the picker manages on the element (never the whole style
  // attribute: the game board moves its tiles with inline styles, and undoing must not touch those), its record, and its text.
  // Edits of one kind to one element close together in time (a slider being dragged, digits being typed) are one step.
  const MANAGED = ["background-color", "color", "font-size", "font-weight", "line-height", "letter-spacing", "text-align",
    "border-style", "border-width", "border-color", "border-radius", "box-shadow", "width", "height",
    "display", "flex-direction", "flex-wrap", "justify-content", "align-items", "justify-items", "gap", "opacity",
    ...["padding", "margin"].flatMap((kind) => ["top", "right", "bottom", "left"].map((side) => `${kind}-${side}`))];
  const MERGE_MS = 1200;
  const MAX_STEPS = 100;
  const undoStack = [];
  const redoStack = [];
  let restoring = false;       // edits being put back after a reload are not new history
  let bulk = 0;                // above 0 while one step covers several edits (Reset all, a paste, an edit of several elements)
  let holdStep = false;        // a handle is being dragged: the whole drag is one step
  let lastEdit = { kind: "", elements: [], at: 0 };
  let undoLink = null;
  let redoLink = null;

  function cloneRec(rec) {
    if (!rec) return null;
    return { ...rec, props: new Map([...rec.props].map(([k, v]) => [k, { ...v }])), text: rec.text ? { ...rec.text } : null,
             before: { ...rec.before }, after: rec.after ? { ...rec.after } : undefined };
  }
  function snapshotOf(element) {
    const style = {};
    for (const prop of MANAGED) style[prop] = [element.style.getPropertyValue(prop), element.style.getPropertyPriority(prop)];
    return { element, style, text: element.children.length ? null : element.textContent, rec: cloneRec(changes.get(element)) };
  }
  function putSnapshotBack(snap) {
    const element = snap.element;
    if (!document.contains(element)) return;
    for (const [prop, [value, priority]] of Object.entries(snap.style)) {
      if (value) element.style.setProperty(prop, value, priority); else element.style.removeProperty(prop);
    }
    if (snap.text !== null && !element.children.length && element.textContent !== snap.text) element.textContent = snap.text;
    if (snap.rec) changes.set(element, snap.rec); else changes.delete(element);
  }
  function showHistory() {
    if (undoLink) undoLink.disabled = !undoStack.length;
    if (redoLink) redoLink.disabled = !redoStack.length;
  }
  function pushStep(snaps, keepPending) {
    undoStack.push({ snaps, pending: keepPending ? [...pending] : null });
    if (undoStack.length > MAX_STEPS) undoStack.shift();
    redoStack.length = 0;
    showHistory();
  }
  /** Call just before an edit to `element`; `kind` names what is being edited ("fill", "size", …) so unlike edits are separate steps. */
  function checkpoint(kind, target) {
    const elements = Array.isArray(target) ? target : [target];
    if (comparing) setCompare(false);   // an edit while the original is showing: bring the edits back first
    if (restoring || bulk) return;
    const now = Date.now();
    const sameElements = lastEdit.elements.length === elements.length && elements.every((el, i) => el === lastEdit.elements[i]);
    const same = sameElements && lastEdit.kind === kind && (holdStep || now - lastEdit.at < MERGE_MS);
    lastEdit = { kind, elements, at: now };
    if (!same) pushStep(elements.map(snapshotOf), false);
  }
  /** Walk one step back (from = undoStack, to = redoStack) or forward (the other way round). */
  function moveStep(from, to) {
    const entry = from.pop();
    if (!entry) return;
    to.push({ snaps: entry.snaps.map((snap) => snapshotOf(snap.element)), pending: entry.pending ? [...pending] : null });
    entry.snaps.forEach(putSnapshotBack);
    if (entry.pending) pending = entry.pending;
    lastEdit = { kind: "", elements: [], at: 0 };
    exported = false;
    showCount();
    save();
    renderDock();
    refreshBoxes();
  }
  function undo() { moveStep(undoStack, redoStack); }
  function redo() { moveStep(redoStack, undoStack); }

  // ---------- keeping edits across reloads ----------
  // Every edit is saved in this browser (localStorage, one list per page) and put back when the page loads again, so a
  // reload, a new version of the site or a design patch switched on never costs a designer their work. An edit whose element
  // is not on the page (yet: the game board draws itself later; or any more: the page changed) is kept as it is, listed
  // under Changes and still exported, until it is reset.
  const STORE_KEY = host.storageKey();
  let pending = [];   // saved edits whose element is not on the page: {selector, snippet, before, after, text}
  const saved = (rec) => ({ selector: rec.selector, snippet: rec.snippet, before: rec.before, after: rec.after || {}, text: rec.text || null });
  /** An entry that changes nothing (everything put back as it was) is not worth keeping, counting or exporting. */
  function save() {
    if (PREVIEW) return;
    try {
      const all = [...[...changes.values()].map(saved), ...pending].filter(hasChange);
      host.writeEdits(STORE_KEY, all.length ? JSON.stringify(all) : null);
    } catch (_) { /* the edits still work in this tab */ }
  }
  function restore() {
    try {
      const raw = JSON.parse(host.readEdits(STORE_KEY) || "[]");
      pending = Array.isArray(raw) ? raw.filter((item) => item && typeof item.selector === "string" && item.before && hasChange(item)) : [];
    } catch (_) { pending = []; }
    if (!pending.length) return;
    putBack();
    let tries = 0;   // the page may build the element later: look again every second for half a minute
    const timer = setInterval(() => { if (!pending.length || ++tries > 30) clearInterval(timer); else putBack(); }, 1000);
  }
  function putBack() {
    restoring = true;
    try { putBackEdits(); } finally { restoring = false; }
  }
  function putBackEdits() {
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
      if (/^(\d+(\.\d+)?(px|%)( |$)){1,4}$/.test(after["border-radius"] || "")) applyRadius(element, after["border-radius"]);
      for (const kind of ["padding", "margin"]) {
        const sides = after[kind] ? parseSides(after[kind]) : null;
        if (sides) applySpacing(element, kind, sides);
      }
      const sizes = {};
      for (const axis of ["width", "height"]) {
        const m = /^(\d+(?:\.\d+)?)px$/.exec(after[axis] || "");
        if (m) sizes[axis] = +m[1];
      }
      if (Object.keys(sizes).length) applySize(element, sizes);
      const type = {};
      if (/^#[0-9a-f]{6}$/i.test(after.color || "")) type.color = after.color;
      if (/^\d+(\.\d+)?px$/.test(after["font-size"] || "")) type["font-size"] = after["font-size"];
      if (/^[1-9]00$/.test(after["font-weight"] || "")) type["font-weight"] = after["font-weight"];
      if (/^\d+(\.\d+)?$/.test(after["line-height"] || "")) type["line-height"] = after["line-height"];
      if (/^-?\d+(\.\d+)?px$/.test(after["letter-spacing"] || "")) type["letter-spacing"] = after["letter-spacing"];
      if (TEXT_ALIGNS.includes(after["text-align"])) type["text-align"] = after["text-align"];
      if (Object.keys(type).length) applyType(element, type);
      const layout = {};
      for (const prop of Object.keys(LAYOUT_OPTIONS)) if (/^[a-z]+(-[a-z]+)*$/.test(after[prop] || "")) layout[prop] = after[prop];
      if (/^\d+(\.\d+)?px$/.test(after.gap || "")) layout.gap = after.gap;
      if (/^(0|1|0?\.\d+)$/.test(after.opacity || "")) layout.opacity = after.opacity;
      if (Object.keys(layout).length) applyLayout(element, layout);
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

  function applyFillOne(element, colour) {
    checkpoint("fill", element);
    const rec = record(element);
    rememberOriginal(rec, element, "background-color");
    setStyle(element, "background-color", colour);
    rec.after = Object.assign(rec.after || {}, { "background-color": colour });
    edited();
  }
  function applyBorderOne(element, style, widthPx, colour) {
    checkpoint("border", element);
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
  function applyShadowValueOne(element, value) {
    checkpoint("shadow", element);
    const rec = record(element);
    rememberOriginal(rec, element, "box-shadow");
    setStyle(element, "box-shadow", value);
    rec.after = Object.assign(rec.after || {}, { "box-shadow": value });
    edited();
  }
  /** padding or margin: four sides in pixels (top, right, bottom, left). */
  function applySpacingOne(element, kind, sides) {
    checkpoint("spacing:" + kind, element);
    const rec = record(element);
    SIDES.forEach((side, i) => {
      rememberOriginal(rec, element, `${kind}-${side}`);
      setStyle(element, `${kind}-${side}`, sides[i] + "px");
    });
    rec.after = Object.assign(rec.after || {}, { [kind]: sidesText(sides.map((n) => n + "px")) });
    edited();
  }
  /** Every corner the same: "12px", or "9999px" for a pill. */
  function applyRadiusOne(element, value) {
    checkpoint("radius", element);
    const rec = record(element);
    rememberOriginal(rec, element, "border-radius");
    setStyle(element, "border-radius", value);
    rec.after = Object.assign(rec.after || {}, { "border-radius": value });
    edited();
  }
  /** sizes is {width?, height?} in pixels (the border box). */
  function applySizeOne(element, sizes) {
    checkpoint("size", element);
    const rec = record(element);
    for (const axis of ["width", "height"]) {
      if (sizes[axis] === undefined) continue;
      rememberOriginal(rec, element, axis);
      setStyle(element, axis, cssSize(element, axis, sizes[axis]) + "px");
      rec.after = Object.assign(rec.after || {}, { [axis]: sizes[axis] + "px" });
    }
    edited();
  }
  /** Back to the size the page gives it: put the original inline width and height back. */
  function clearSizeOne(element) {
    const rec = changes.get(element);
    if (!rec) return;
    checkpoint("size-clear", element);
    for (const axis of ["width", "height"]) {
      const original = rec.props.get(axis);
      if (!original) continue;
      if (original.value) element.style.setProperty(axis, original.value, original.priority); else element.style.removeProperty(axis);
      rec.props.delete(axis);
      if (rec.after) delete rec.after[axis];
    }
    if (!rec.props.size && !rec.text) changes.delete(element);   // nothing left to hand over: no longer a change
    edited();
  }
  function applyText(element, text) {
    checkpoint("text", element);
    const rec = record(element);
    if (!rec.text) rec.text = { before: element.textContent, after: element.textContent };
    element.textContent = text;          // text, never markup
    rec.text.after = text;
    edited();
  }
  function resetElement(element) {
    const rec = changes.get(element);
    if (!rec) return;
    if (!bulk) pushStep([snapshotOf(element)], false);
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
    pushStep([], true);
    pending = pending.filter((other) => other !== item);
    save();
    renderDock();
  }
  function resetAll() {
    if (changes.size || pending.length) pushStep([...changes.keys()].map(snapshotOf), true);
    bulk++;
    try { for (const element of [...changes.keys()]) resetElement(element); } finally { bulk--; }
    pending = [];
    save();
    exported = true;
    renderDock();
  }

  // ---------- export ----------
  /** One text block per changed element: its selector (and a snippet of its text to confirm it), then each before → after. */
  function exportBlocks() {
    const blocks = [];
    for (const [element, rec] of [...changes.entries(), ...pending.map((item) => [null, item])]) {
      const lines = [];
      for (const prop of ["background-color", "color", "font-size", "font-weight", "line-height", "letter-spacing", "text-align", "border", "border-radius", "box-shadow", "padding", "margin", "width", "height", "display", "flex-direction", "flex-wrap", "justify-content", "align-items", "justify-items", "gap", "opacity"]) {
        const after = rec.after && rec.after[prop];
        if (after !== undefined && after !== rec.before[prop]) lines.push(`  ${prop}: ${rec.before[prop]} → ${after}`);
      }
      if (element && document.contains(element) && rec.before.contrast && rec.after && (rec.after["background-color"] !== undefined || rec.after.color !== undefined)) {
        const info = contrastInfo(element);
        if (contrastLabel(info) !== rec.before.contrast) lines.push(`  contrast: ${rec.before.contrast} → ${contrastLabel(info)}${info.unknown ? "" : ` (WCAG AA ${info.aa ? "pass" : "fail"}, APCA Lc ${Math.round(info.apca)})`}`);
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
    const sized = blocks.some((block) => /^  (width|height):/m.test(block));
    return [
      "# UI changes from the designer picker",
      `Page: ${location.origin + location.pathname}`,
      `Viewport: ${window.innerWidth}×${window.innerHeight} · ${new Date().toISOString().slice(0, 10)}`,
      "Each block is one element: its selector (with a snippet of its text to confirm it), then before → after. Apply these in the source code.",
      ...(sized ? ["Width and height are fixed pixel sizes set in this window: when applying them keep the layout working on smaller screens (min-width, percentages or clamp())."] : []),
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
      if (activeEl() !== hexInput) hexInput.value = text;
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
    const colours = pagePalette();
    let paletteBox = null;
    if (colours.length) {
      paletteBox = h("div", { class: "dz-palette", role: "group", "aria-label": "Colours on this page" }, h("span", { class: "dz-hint", text: "On this page" }));
      for (const item of colours) {
        const chip = h("button", { type: "button", class: "dz-chip", title: item.names.length ? `${item.hex} · ${item.names.join(", ")}` : item.hex,
          "aria-label": item.names.length ? `${item.hex}, ${item.names.join(", ")}` : item.hex,
          onclick: () => { [r, g, b] = parseHex(item.hex); fromRgb(); emit(); } });
        chip.style.backgroundColor = item.hex;
        paletteBox.append(chip);
      }
    }
    return h("div", { class: "dz-picker" }, sv, h("label", { class: "dz-row" }, h("span", { text: "Hue" }), hueInput, swatch), ...rows,
             h("label", { class: "dz-row" }, h("span", { text: "Hex" }), hexInput), paletteBox);
  }

  // ---------- compare with the original ----------
  // Hold the Compare link (or the \ key) to see the page without your edits; let go and they are back. Nothing is recorded or lost.
  let comparing = null;   // while held: what was swapped out, per element, to put back
  const compareBadge = h("div", { class: "dz-badge", hidden: true, text: "Original: let go to see your changes" });
  root.append(compareBadge);
  function setCompare(want) {
    if (want === !!comparing) return;
    if (want) {
      comparing = [];
      for (const [element, rec] of changes) {
        if (!document.contains(element)) continue;
        const stash = [];
        for (const [prop, original] of rec.props) {
          stash.push([prop, element.style.getPropertyValue(prop), element.style.getPropertyPriority(prop)]);
          if (original.value) element.style.setProperty(prop, original.value, original.priority); else element.style.removeProperty(prop);
        }
        const text = rec.text && !element.children.length ? element.textContent : null;
        if (text !== null) element.textContent = rec.text.before;
        comparing.push({ element, stash, text });
      }
    } else {
      for (const { element, stash, text } of comparing) {
        for (const [prop, value, priority] of stash) {
          if (value) element.style.setProperty(prop, value, priority); else element.style.removeProperty(prop);
        }
        if (text !== null && !element.children.length) element.textContent = text;
      }
      comparing = null;
    }
    compareBadge.hidden = !comparing;
    refreshBoxes();
  }
  const isEditingField = (t) => t instanceof HTMLElement && (t.isContentEditable || t.tagName === "TEXTAREA" || (t.tagName === "INPUT" && !/^(range|checkbox|radio|button)$/.test(t.type)));
  window.addEventListener("keyup", (event) => {
    if (event.key === "\\") setCompare(false);
    if (event.key.toLowerCase() === "m" && measuring) { measuring = false; renderMeasure(); }
  }, true);
  window.addEventListener("blur", () => { setCompare(false); if (measuring) { measuring = false; renderMeasure(); } });

  // ---------- preview at other screen sizes ----------
  // Opens this page in a real browser window of that size (so its media queries really run). That window, named dz-preview,
  // shows no picker: it puts the same saved edits on the page, and reloads when they change here (the storage event).
  const PREVIEW = host.isPreview();
  const PREVIEW_SIZES = [["Phone", 390, 844], ["Tablet", 820, 1180], ["Laptop", 1280, 800]];
  function openPreview(width, height) {
    // true or false straight away (the game's pages), or a promise of true or a message to show (the extension)
    Promise.resolve(host.openPreview(width, height)).then((result) => {
      if (result === false) showToast("The browser blocked the window: allow pop-ups for this site, then try again.");
      else if (typeof result === "string") showToast(result);
    });
  }
  function previewPanel() {
    return h("div", { class: "dz-panel" },
      h("p", { class: "dz-hint", text: "Opens this page in a window of that size, with your edits on it. It updates when you change something here. Fixed sizes you set show how they hold up." }),
      h("div", { class: "dz-menu" }, ...PREVIEW_SIZES.map(([name, width, height]) =>
        h("button", { type: "button", class: "dz-menu-button", title: `${width} × ${height}`, text: `${name} ${width}`, onclick: () => openPreview(width, height) }))));
  }
  function startPreviewMode() {
    const badge = h("div", { class: "dz-badge dz-size-badge" });
    const show = () => { badge.textContent = `Preview ${window.innerWidth} × ${window.innerHeight}`; };
    show();
    window.addEventListener("resize", show);
    host.mount(h("div", { class: "dz-root", "data-designer": "" }, badge));
    restore();
    let timer = 0;
    host.onEditsChanged(STORE_KEY, () => {
      clearTimeout(timer);
      timer = setTimeout(() => location.reload(), 300);
    });
  }

  // ---------- the Layout tool ----------
  const LAYOUT_OPTIONS = {
    display: ["block", "flex", "grid", "inline-block", "inline-flex"],
    "flex-direction": ["row", "row-reverse", "column", "column-reverse"],
    "flex-wrap": ["nowrap", "wrap", "wrap-reverse"],
    "justify-content": ["flex-start", "center", "flex-end", "space-between", "space-around", "space-evenly"],
    "align-items": ["stretch", "flex-start", "center", "flex-end", "baseline"],
    "justify-items": ["stretch", "start", "center", "end"],
  };
  const LAYOUT_LABELS = {
    display: "Display", "flex-direction": "Direction", "flex-wrap": "Wrap", "justify-content": "Justify", "align-items": "Align", "justify-items": "Items",
  };
  const isFlex = (display) => display === "flex" || display === "inline-flex";
  const isGrid = (display) => display === "grid" || display === "inline-grid";
  /** values: CSS property -> value (display, flex-direction, flex-wrap, justify-content, align-items, justify-items, gap "12px", opacity "0.8"). */
  function applyLayoutOne(element, values) {
    checkpoint("layout:" + Object.keys(values).join(), element);
    const rec = record(element);
    for (const [prop, value] of Object.entries(values)) {
      rememberOriginal(rec, element, prop);
      setStyle(element, prop, value);
      rec.after = Object.assign(rec.after || {}, { [prop]: value });
    }
    edited();
  }
  function layoutPanel() {
    const element = selected;
    const cs = getComputedStyle(element);
    const display = cs.display;
    const set = (values, rebuild) => { applyLayout(targets(), values); if (rebuild) renderDock(); };

    const choice = (prop, rebuild) => {
      const current = cs.getPropertyValue(prop);
      const options = LAYOUT_OPTIONS[prop].includes(current) ? LAYOUT_OPTIONS[prop] : [current, ...LAYOUT_OPTIONS[prop]];
      const select = h("select", { class: "dz-select", "aria-label": LAYOUT_LABELS[prop] },
        ...options.map((value) => h("option", { value, text: value, selected: value === current })));
      select.addEventListener("change", () => set({ [prop]: select.value }, rebuild));
      return h("label", { class: "dz-row" }, h("span", { text: LAYOUT_LABELS[prop] }), select);
    };
    const rows = [choice("display", true)];
    const flex = isFlex(display), grid = isGrid(display);
    if (flex) rows.push(choice("flex-direction"), choice("flex-wrap"), choice("justify-content"), choice("align-items"));
    if (grid) rows.push(choice("justify-items"), choice("align-items"));
    if (flex || grid) {
      let gap = Math.round(parseFloat(cs.rowGap) || 0);
      const range = h("input", { type: "range", min: "0", max: "64", class: "dz-range", "aria-label": "Gap between children" });
      const number = h("span", { class: "dz-num" });
      const show = () => { range.value = String(gap); number.textContent = gap + "px"; };
      range.addEventListener("input", () => { gap = +range.value; show(); set({ gap: gap + "px" }); });
      show();
      rows.push(h("label", { class: "dz-row" }, h("span", { text: "Gap" }), range, number));
    }
    let opacity = Math.round((parseFloat(cs.opacity) || 0) * 100);
    const opacityRange = h("input", { type: "range", min: "0", max: "100", class: "dz-range", "aria-label": "Opacity" });
    const opacityNumber = h("span", { class: "dz-num" });
    const showOpacity = () => { opacityRange.value = String(opacity); opacityNumber.textContent = opacity + "%"; };
    opacityRange.addEventListener("input", () => { opacity = +opacityRange.value; showOpacity(); set({ opacity: String(opacity / 100) }); });
    showOpacity();
    rows.push(h("label", { class: "dz-row" }, h("span", { text: "Opacity" }), opacityRange, opacityNumber));
    const hint = flex || grid ? null
      : h("p", { class: "dz-hint dz-note", text: "This is not a flex or grid container. Set Display to flex or grid to line up and space its children." });
    return h("div", { class: "dz-panel dz-layout" }, ...rows, hint);
  }

  // ---------- copy and paste a look ----------
  let copiedLook = null;
  function lookOf(element) {
    const cs = getComputedStyle(element);
    const border = toRgba(cs.borderTopColor) || [0, 0, 0, 1];
    return {
      from: label(element),
      fill: colourText(cs.backgroundColor), color: colourText(cs.color),
      "font-size": cs.fontSize, "font-weight": cs.fontWeight, lineHeight: lineHeightOf(cs),
      letterSpacing: cs.letterSpacing === "normal" ? "0px" : cs.letterSpacing, "text-align": alignOf(cs),
      border: { style: NO_BORDER(cs.borderTopStyle, cs.borderTopWidth) ? "none" : cs.borderTopStyle, width: Math.round(parseFloat(cs.borderTopWidth)) || 0, colour: toHex(border[0], border[1], border[2]) },
      radius: radiusOf(cs), shadow: shadowText(cs.boxShadow), padding: sideNumbers(cs, "padding"),
    };
  }
  function copyLook(element) {
    copiedLook = lookOf(element);
    showToast(`Copied the look of ${copiedLook.from}. Select another element and paste it (Alt+Shift+V).`);
    renderDock();
  }
  /** Fill, text colour and type, border, corners, shadow and padding: not size, margin or layout, which belong to where it sits. */
  function pasteLook(target) {
    const elements = Array.isArray(target) ? target : [target];
    if (!copiedLook) { showToast("Copy an element's look first (Alt+Shift+C)."); return; }
    if (!elements.length) return;
    const look = copiedLook;
    checkpoint("paste:" + Date.now(), elements);   // the whole paste is one step to undo
    bulk++;
    try {
      applyFill(elements, look.fill);
      const type = { color: look.color, "font-size": look["font-size"], "font-weight": look["font-weight"], "letter-spacing": look.letterSpacing, "text-align": look["text-align"] };
      if (look.lineHeight !== "normal") type["line-height"] = look.lineHeight;
      applyType(elements, type);
      applyBorder(elements, look.border.style, look.border.width, look.border.colour);
      applyRadius(elements, look.radius);
      applyShadowValue(elements, look.shadow);
      applySpacing(elements, "padding", look.padding);
    } finally { bulk--; }
    showToast(`Pasted the look of ${look.from} onto ${elements.length > 1 ? elements.length + " elements" : label(elements[0])}.`);
    renderDock();
  }

  // ---------- measuring ----------
  // Hold M with an element selected and move over another: the distances between them, and the selected element's size.
  const measureLayer = h("div", { class: "dz-measure" });
  root.append(measureLayer);
  let measuring = false;
  function renderMeasure() {
    measureLayer.replaceChildren();
    if (!measuring || !on || !selected || !document.contains(selected)) return;
    const a = selected.getBoundingClientRect();
    const mark = (x1, y1, x2, y2, text) => {
      const line = h("div", { class: "dz-m-line" });
      line.style.left = Math.min(x1, x2) + "px"; line.style.top = Math.min(y1, y2) + "px";
      line.style.width = Math.max(1, Math.abs(x2 - x1)) + "px"; line.style.height = Math.max(1, Math.abs(y2 - y1)) + "px";
      measureLayer.append(line);
      tag(text, (x1 + x2) / 2, (y1 + y2) / 2);
    };
    const tag = (text, x, y) => {
      const chip = h("div", { class: "dz-m-label", text });
      chip.style.left = x + "px"; chip.style.top = y + "px";
      measureLayer.append(chip);
    };
    tag(`${Math.round(a.width)} × ${Math.round(a.height)}`, a.left + a.width / 2, a.bottom + 14);
    const other = hovered && hovered !== selected && document.contains(hovered) && !ours(hovered) ? hovered : null;
    if (!other) return;
    const b = other.getBoundingClientRect();
    const px = (n) => String(Math.round(n * 10) / 10).replace(/\.0$/, "");
    const inside = (outer, inner) => outer.left <= inner.left && outer.right >= inner.right && outer.top <= inner.top && outer.bottom >= inner.bottom;
    const [outer, inner] = inside(b, a) ? [b, a] : inside(a, b) ? [a, b] : [null, null];
    if (outer) {   // one is inside the other: the distance to each edge
      const mx = (inner.left + inner.right) / 2, my = (inner.top + inner.bottom) / 2;
      if (inner.left - outer.left >= 0.5) mark(outer.left, my, inner.left, my, px(inner.left - outer.left));
      if (outer.right - inner.right >= 0.5) mark(inner.right, my, outer.right, my, px(outer.right - inner.right));
      if (inner.top - outer.top >= 0.5) mark(mx, outer.top, mx, inner.top, px(inner.top - outer.top));
      if (outer.bottom - inner.bottom >= 0.5) mark(mx, inner.bottom, mx, outer.bottom, px(outer.bottom - inner.bottom));
      return;
    }
    const ox = [Math.max(a.left, b.left), Math.min(a.right, b.right)], oy = [Math.max(a.top, b.top), Math.min(a.bottom, b.bottom)];
    const midY = oy[0] < oy[1] ? (oy[0] + oy[1]) / 2 : (a.top + a.bottom) / 2;
    const midX = ox[0] < ox[1] ? (ox[0] + ox[1]) / 2 : (a.left + a.right) / 2;
    if (a.right <= b.left) mark(a.right, midY, b.left, midY, px(b.left - a.right));
    else if (b.right <= a.left) mark(b.right, midY, a.left, midY, px(a.left - b.right));
    if (a.bottom <= b.top) mark(midX, a.bottom, midX, b.top, px(b.top - a.bottom));
    else if (b.bottom <= a.top) mark(midX, b.bottom, midX, a.top, px(a.top - b.bottom));
  }

  /** An edit works on one element or on a list (the selection): the same change to each, one step to undo. */
  function forAll(kindOf, apply) {
    return function (target, ...args) {
      const elements = Array.isArray(target) ? target : [target];
      if (!elements.length) return;
      checkpoint(kindOf(...args), elements);
      bulk++;
      try { for (const element of elements) apply(element, ...args); } finally { bulk--; }
    };
  }
  const applyFill = forAll(() => "fill", applyFillOne);
  const applyBorder = forAll(() => "border", applyBorderOne);
  const applyShadowValue = forAll(() => "shadow", applyShadowValueOne);
  const applySpacing = forAll((kind) => "spacing:" + kind, applySpacingOne);
  const applyRadius = forAll(() => "radius", applyRadiusOne);
  const applySize = forAll(() => "size", applySizeOne);
  const clearSize = forAll(() => "size-clear", clearSizeOne);
  const applyType = forAll((values) => "font:" + Object.keys(values).join(), applyTypeOne);
  const applyLayout = forAll((values) => "layout:" + Object.keys(values).join(), applyLayoutOne);

  // ---------- the hover inspector ----------
  // Press I (or use Inspect in the dock) and hover anything: what it is, its size, type, colours, contrast (WCAG and APCA) and what
  // assistive technology would make of it. An idea from VisBug (Apache-2.0, github.com/GoogleChromeLabs/ProjectVisBug), written afresh here.
  let inspecting = false;
  const inspectTip = h("div", { class: "dz-tip", hidden: true });
  root.append(inspectTip);
  /** What a screen reader would call the element: aria-label, then a label it points at, then alt, title or its own text. */
  function accessibleName(element) {
    const byId = (ids) => ids.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean).map((el) => el.textContent.trim()).join(" ");
    const name = element.getAttribute("aria-label") || (element.getAttribute("aria-labelledby") && byId(element.getAttribute("aria-labelledby")))
      || (element.labels && element.labels[0] && element.labels[0].textContent.trim()) || element.getAttribute("alt")
      || element.getAttribute("title") || (element.children.length < 4 ? element.textContent.trim() : "");
    return name.replace(/\s+/g, " ").slice(0, 60);
  }
  const IMPLICIT_ROLES = { a: "link", button: "button", img: "img", input: "textbox", select: "combobox", textarea: "textbox", nav: "navigation", main: "main",
    header: "banner", footer: "contentinfo", ul: "list", ol: "list", li: "listitem", table: "table", h1: "heading", h2: "heading", h3: "heading", h4: "heading", h5: "heading", h6: "heading" };
  function renderInspect(element) {
    inspectTip.replaceChildren();
    if (!inspecting || !on || !element || ours(element) || element === document.documentElement || !document.contains(element)) { inspectTip.hidden = true; return; }
    const cs = getComputedStyle(element);
    const box = boxSize(element);
    const info = element.textContent.trim() ? contrastInfo(element) : null;
    const tag = element.tagName.toLowerCase();
    const role = element.getAttribute("role") || IMPLICIT_ROLES[tag] || "";
    const name = accessibleName(element);
    const focusable = element.tabIndex >= 0 || element.matches("a[href], button, input, select, textarea, [tabindex]");
    const lines = [
      [label(element), "dz-tip-title"],
      [`${box.width} × ${box.height}px`, ""],
      [`${cs.fontFamily.split(",")[0].replace(/"/g, "")} ${cs.fontSize} / ${lineHeightOf(cs)}, weight ${cs.fontWeight}`, ""],
    ];
    if (info) {
      const back = backdrop(element);
      lines.push([`text ${colourText(cs.color)} on ${back.unknown ? "an image or gradient" : toHex(...back.rgb.map(Math.round))}`, ""]);
      lines.push([info.unknown ? "contrast: cannot be measured here" : `${info.ratio.toFixed(2)}:1 · WCAG AA ${info.aa ? "✓" : "✗"} · AAA ${info.aaa ? "✓" : "✗"} · APCA Lc ${Math.round(info.apca)}`, info.unknown ? "" : info.aa ? "dz-pass" : "dz-fail"]);
    }
    lines.push([`padding ${sidesOf(cs, "padding")} · margin ${sidesOf(cs, "margin")}`, ""]);
    lines.push([`${role ? "role " + role + " · " : ""}${name ? "name “" + name + "”" : "no accessible name"}${focusable ? " · focusable" : ""}`,
      !name && (role === "button" || role === "link" || role === "img" || tag === "input") ? "dz-fail" : ""]);
    if (tag === "img" && !element.hasAttribute("alt")) lines.push(["missing alt text", "dz-fail"]);
    for (const [text, cls] of lines) inspectTip.append(h("div", { class: cls, text }));
    inspectTip.hidden = false;
    const r = element.getBoundingClientRect();
    const tip = inspectTip.getBoundingClientRect();
    inspectTip.style.left = Math.max(8, Math.min(window.innerWidth - tip.width - 8, r.left)) + "px";
    inspectTip.style.top = (r.bottom + tip.height + 12 < window.innerHeight ? r.bottom + 8 : Math.max(8, r.top - tip.height - 8)) + "px";
  }
  function setInspect(value) {
    inspecting = value;
    renderInspect(hovered);
    if (inspectLink) inspectLink.classList.toggle("dz-on-link", inspecting);
  }
  let inspectLink = null;

  // ---------- the dock ----------
  const NO_TEXT_TAGS = new Set(["IMG", "INPUT", "TEXTAREA", "SELECT", "BR", "HR", "SVG", "CANVAS", "VIDEO", "AUDIO", "IFRAME"]);
  function textBlocked(element) {
    if (element.children.length) return "Elements with children may not have text";
    if (NO_TEXT_TAGS.has(element.tagName.toUpperCase())) return "This element has no text to edit";
    return "";
  }
  function closeTool() { tool = null; }

  // ---------- the guide ----------
  const TOUR = [
    { title: "Select an element", lines: [
      "Alt+Shift+P turns the picker on and off.",
      "While it is on, clicking an element selects it; the click does not reach the page, so buttons and links do nothing."] },
    { title: "Move around", lines: [
      "↑ selects the parent of what you have selected. ↓ goes back down the way you came, or to the first child.",
      "Shift-click adds elements to the selection (or takes them out): the tools then change all of them at once. Esc clears the selection."] },
    { title: "Change it", lines: [
      "Enter opens the tools: Fill (colour), Border (style, thickness, colour), Shadow (presets, or offset, blur, spread, opacity, colour), Text (the words), Font (text colour, size, weight, line height, letter spacing, alignment), Size (width and height, or drag the pink handles), Spacing (padding and margin), Radius (rounded corners) and Layout (display, flex and grid alignment and gap, opacity). Alt+Shift+C copies an element's look and Alt+Shift+V pastes it onto another; hold M to measure distances, and press I to inspect what is under the pointer (size, type, colours, WCAG and APCA contrast, accessible name). Fill and Font show the contrast against what is behind the text, and every colour picker lists the colours already on the page. Hold Compare (or the \\ key) to see the original, and Preview opens the page at phone, tablet and laptop sizes.",
      "Text works only on elements with nothing inside them. The page changes straight away, in this tab only; Done closes the tool. Ctrl+Z undoes a step and Ctrl+Shift+Z (or Ctrl+Y) redoes it."] },
    { title: "Export your changes", lines: [
      "Alt+Shift+E copies every change as Markdown. Paste it to Claude, who can then make the same changes in the code.",
      "\"Changes\" lists them with Copy, Download and Reset. They are kept in this browser, through reloads, until you reset them."] },
  ];
  function endTour() {
    tourStep = null;
    host.markTourSeen();
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
    sizeSync = null;   // the Size panel, if it is open, sets this again when it is built
    dock.hidden = !on;
    toggle.textContent = on ? "Design: on" : "Design";
    toggle.classList.toggle("dz-on", on);
    if (!on) return;
    updateHandles();
    if (tourStep !== null) { dock.append(tourPanel()); return; }

    changesLink = h("button", { type: "button", class: "dz-link", text: `Changes (${changeCount()})`, onclick: () => { showChanges = !showChanges; showPatches = false; showPreview = false; renderDock(); } });
    undoLink = h("button", { type: "button", class: "dz-link", text: "Undo", title: "Undo (Ctrl+Z)", onclick: undo });
    redoLink = h("button", { type: "button", class: "dz-link", text: "Redo", title: "Redo (Ctrl+Shift+Z or Ctrl+Y)", onclick: redo });
    showHistory();
    inspectLink = h("button", { type: "button", class: "dz-link" + (inspecting ? " dz-on-link" : ""), text: "Inspect", title: "Hover anything to see its size, type, colours, contrast and accessible name (I)", "aria-pressed": String(inspecting),
      onclick: () => { setInspect(!inspecting); inspectLink.setAttribute("aria-pressed", String(inspecting)); } });
    const compareLink = h("button", { type: "button", class: "dz-link", text: "Compare", title: "Hold to see the original page (or hold the \\ key)" });
    compareLink.addEventListener("pointerdown", (event) => { event.preventDefault(); setCompare(true); });
    for (const type of ["pointerup", "pointerleave", "pointercancel"]) compareLink.addEventListener(type, () => setCompare(false));
    const previewLink = h("button", { type: "button", class: "dz-link", text: "Preview", title: "See this page at phone, tablet and laptop sizes",
      onclick: () => { showPreview = !showPreview; showChanges = false; showPatches = false; renderDock(); } });
    const head = h("div", { class: "dz-head" },
      h("strong", { text: selection.length > 1 ? `${selection.length} elements` : selected ? label(selected) : "UI picker" }),
      undoLink, redoLink, inspectLink, compareLink, previewLink,
      changesLink,
      host.patches && h("button", { type: "button", class: "dz-link", text: "Patches", title: "Design patches: switch them off, to preview, or on",
                    onclick: () => { showPatches = !showPatches; showChanges = false; showPreview = false; renderDock(); } }),
      h("button", { type: "button", class: "dz-link", text: "Guide", title: "How the picker works", onclick: () => { tourStep = 0; renderDock(); } }),
      h("button", { type: "button", class: "dz-link", text: "Off", title: "Turn picker mode off", onclick: () => setOn(false) }));
    dock.append(head);

    if (showPatches) { dock.append(patchesPanel()); return; }
    if (showPreview) { dock.append(previewPanel()); return; }
    if (showChanges) { dock.append(changesPanel()); return; }
    if (!selected) {
      dock.append(h("p", { class: "dz-hint", text: "Click any element to select it; Shift-click adds more. ↑ parent, ↓ child, Enter for the tools, Esc to clear. Hold M to measure, I to inspect. Alt+Shift+E copies your changes." }));
      return;
    }
    const lookRow = () => h("div", { class: "dz-menu dz-look" },
      h("button", { type: "button", class: "dz-menu-button", text: "Copy look", title: "Copy this element's fill, type, border, corners, shadow and padding (Alt+Shift+C)", onclick: () => copyLook(selected) }),
      h("button", { type: "button", class: "dz-menu-button", text: "Paste look", disabled: !copiedLook, title: copiedLook ? `Paste the look of ${copiedLook.from} (Alt+Shift+V)` : "Copy an element's look first",
                    onclick: () => pasteLook(targets()) }));
    const tools = () => h("div", { class: "dz-tools" }, h("div", { class: "dz-menu" },
      menuButton("fill", "Fill"), menuButton("border", "Border"), menuButton("shadow", "Shadow"), menuButton("text", "Text"), menuButton("font", "Font"),
      menuButton("size", "Size"), menuButton("spacing", "Spacing"), menuButton("radius", "Radius"), menuButton("layout", "Layout")), lookRow());
    if (!menuOpen && !tool) {
      dock.append(h("p", { class: "dz-hint", text: "Press Enter, or choose a tool." }), tools());
      return;
    }
    dock.append(tools());
    if (tool === "fill") dock.append(fillPanel());
    else if (tool === "border") dock.append(borderPanel());
    else if (tool === "shadow") dock.append(shadowPanel());
    else if (tool === "text") dock.append(textPanel());
    else if (tool === "font") dock.append(fontPanel());
    else if (tool === "size") dock.append(sizePanel());
    else if (tool === "spacing") dock.append(spacingPanel());
    else if (tool === "radius") dock.append(radiusPanel());
    else if (tool === "layout") dock.append(layoutPanel());
    // Edits apply as they are made, so there is nothing to confirm; Done just closes the tool and lets go of the element
    // (Esc does the same in steps). Without it a phone, which has no Esc key, had no visible way out of a tool.
    dock.append(h("div", { class: "dz-done-row" },
      h("span", { class: "dz-hint", text: "Changes apply as you go." }),
      h("button", { type: "button", class: "dz-menu-button dz-active", text: "Done", title: "Close the tool and deselect (Esc)", onclick: done })));
  }
  function done() {
    closeTool();
    menuOpen = false;
    setSelection([]);
    climbed = [];
    renderDock();
    refreshBoxes();
  }
  function menuButton(name, text) {
    const blocked = name === "text" && selected ? (selection.length > 1 ? "Text edits one element at a time" : textBlocked(selected)) : "";
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
    const contrast = contrastNode(element);
    return h("div", { class: "dz-panel" }, colourPicker(currentFill(element), (colour) => { applyFill(targets(), colour); contrast.update(); }), contrast.node);
  }
  function borderPanel() {
    const element = selected;
    const cs = getComputedStyle(element);
    let style = NO_BORDER(cs.borderTopStyle, cs.borderTopWidth) ? "none" : cs.borderTopStyle;
    let width = Math.round(parseFloat(cs.borderTopWidth)) || 0;
    const colourParsed = parseHex(colourText(cs.borderTopColor));
    let colour = colourParsed ? toHex(...colourParsed) : "#000000";
    const apply = () => applyBorder(targets(), style, width, colour);

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
    const apply = () => applyShadow(targets(), enabled ? s : null);

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
  // ---------- the Font tool ----------
  /** values: CSS property -> value ("color" "#336699", "font-size" "18px", "font-weight" "600", "line-height" "1.5", "letter-spacing" "1px", "text-align" "center"). */
  function applyTypeOne(element, values) {
    checkpoint("font:" + Object.keys(values).join(), element);
    const rec = record(element);
    for (const [prop, value] of Object.entries(values)) {
      rememberOriginal(rec, element, prop);
      setStyle(element, prop, value);
      rec.after = Object.assign(rec.after || {}, { [prop]: value });
    }
    edited();
  }
  function fontPanel() {
    const element = selected;
    const cs = getComputedStyle(element);
    const rgba = toRgba(cs.color) || [0, 0, 0, 1];
    const lineNow = lineHeightOf(cs);
    const state = {
      colour: toHex(rgba[0], rgba[1], rgba[2]),
      size: Math.round(parseFloat(cs.fontSize)) || 16,
      weight: parseInt(cs.fontWeight, 10) || 400,
      line: lineNow === "normal" ? 1.2 : parseFloat(lineNow),
      spacing: cs.letterSpacing === "normal" ? 0 : Math.round(parseFloat(cs.letterSpacing) * 2) / 2,
      align: alignOf(cs),
    };
    const contrast = contrastNode(element);
    const set = (values) => { applyType(targets(), values); contrast.update(); };

    const swatch = h("span", { class: "dz-swatch" });
    swatch.style.backgroundColor = state.colour;
    const holder = h("div", { class: "dz-colour-holder", hidden: true });
    const colourButton = h("button", { type: "button", class: "dz-menu-button", "aria-expanded": "false",
      onclick: () => {
        holder.hidden = !holder.hidden;
        colourButton.setAttribute("aria-expanded", holder.hidden ? "false" : "true");
        if (!holder.hidden && !holder.firstChild) {
          holder.append(colourPicker(state.colour, (value) => { state.colour = value; swatch.style.backgroundColor = value; set({ color: value }); }));
        }
      } }, "Text colour ", swatch);

    const sizeRange = h("input", { type: "range", min: "8", max: "72", class: "dz-range", "aria-label": "Font size slider" });
    const sizeField = numberField("Font size in pixels", 6, 200, (value) => { state.size = value; showSize(); set({ "font-size": value + "px" }); });
    const showSize = () => { sizeRange.value = String(Math.min(72, Math.max(8, state.size))); if (activeEl() !== sizeField) sizeField.value = String(state.size); };
    sizeRange.addEventListener("input", () => { state.size = +sizeRange.value; showSize(); set({ "font-size": state.size + "px" }); });

    const known = WEIGHTS.some(([value]) => value === state.weight);
    const weightSelect = h("select", { class: "dz-select", "aria-label": "Font weight" },
      ...(known ? [] : [h("option", { value: String(state.weight), text: String(state.weight), selected: true })]),
      ...WEIGHTS.map(([value, name]) => h("option", { value: String(value), text: `${value} ${name}`, selected: value === state.weight })));
    weightSelect.addEventListener("change", () => { state.weight = +weightSelect.value; set({ "font-weight": String(state.weight) }); });

    const lineRange = h("input", { type: "range", min: "0.8", max: "2.4", step: "0.05", class: "dz-range", "aria-label": "Line height" });
    const lineNumber = h("span", { class: "dz-num" });
    const showLine = () => { lineRange.value = String(state.line); lineNumber.textContent = String(Math.round(state.line * 100) / 100); };
    lineRange.addEventListener("input", () => { state.line = +lineRange.value; showLine(); set({ "line-height": String(state.line) }); });

    const spaceRange = h("input", { type: "range", min: "-2", max: "10", step: "0.5", class: "dz-range", "aria-label": "Letter spacing" });
    const spaceNumber = h("span", { class: "dz-num" });
    const showSpace = () => { spaceRange.value = String(state.spacing); spaceNumber.textContent = state.spacing + "px"; };
    spaceRange.addEventListener("input", () => { state.spacing = +spaceRange.value; showSpace(); set({ "letter-spacing": state.spacing + "px" }); });

    const alignButtons = TEXT_ALIGNS.map((align) => h("button", { type: "button", class: "dz-menu-button", text: align[0].toUpperCase() + align.slice(1),
      "aria-pressed": "false", onclick: () => { state.align = align; showAlign(); set({ "text-align": align }); } }));
    const showAlign = () => alignButtons.forEach((button, i) => {
      const on = TEXT_ALIGNS[i] === state.align;
      button.classList.toggle("dz-active", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });
    showSize(); showLine(); showSpace(); showAlign();
    return h("div", { class: "dz-panel dz-font" },
      colourButton, holder, contrast.node,
      h("label", { class: "dz-row" }, h("span", { text: "Size" }), sizeRange, sizeField, h("span", { class: "dz-unit", text: "px" })),
      h("label", { class: "dz-row" }, h("span", { text: "Weight" }), weightSelect),
      h("label", { class: "dz-row" }, h("span", { text: "Line" }), lineRange, lineNumber),
      h("label", { class: "dz-row" }, h("span", { text: "Letters" }), spaceRange, spaceNumber),
      h("div", { class: "dz-menu" }, ...alignButtons));
  }

  // ---------- size, spacing and radius tools ----------
  const SIZE_HELP = "Type a size, or drag the pink handles on the page (Shift keeps the proportions).";
  /** A pixel field: a number box that Shift+↑/↓ moves by 10, beside its slider. */
  function numberField(label, min, max, onChange) {
    const field = h("input", { type: "number", class: "dz-field", min: String(min), max: String(max), step: "1", "aria-label": label });
    field.addEventListener("input", () => { const n = field.valueAsNumber; if (Number.isFinite(n)) onChange(Math.max(min, Math.min(max, n))); });
    field.addEventListener("keydown", (event) => {
      if (!event.shiftKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
      event.preventDefault();
      const n = (Number.isFinite(field.valueAsNumber) ? field.valueAsNumber : 0) + (event.key === "ArrowUp" ? 10 : -10);
      field.value = String(Math.max(min, Math.min(max, n)));
      field.dispatchEvent(new Event("input"));
    });
    return field;
  }
  function sizePanel() {
    const element = selected;
    const inline = getComputedStyle(element).display === "inline";
    const note = h("p", { class: "dz-hint dz-note" });
    const controls = {};
    const askFor = {};   // the size last asked for, to tell the designer when the page's own rules keep the element from it
    function sync() {
      const now = boxSize(element);
      for (const axis of ["width", "height"]) {
        const { range, field } = controls[axis];
        range.max = String(Math.max(600, now[axis] * 2));
        range.value = String(now[axis]);
        if (activeEl() !== field) field.value = String(now[axis]);
      }
      const stuck = ["width", "height"].filter((axis) => askFor[axis] !== undefined && Math.abs(now[axis] - askFor[axis]) > 1);
      note.textContent = inline ? "This is inline text: it flows with its line and cannot be sized. Select its parent, or a box around it."
        : stuck.length ? `The page's own rules keep the ${stuck.join(" and ")} at ${stuck.map((a) => now[a] + "px").join(" × ")} (a min or max size, or its container).`
        : SIZE_HELP;
    }
    function set(axis, value) {
      askFor[axis] = value;
      applySize(targets(), { [axis]: value });
      sync();
    }
    const rows = ["width", "height"].map((axis) => {
      const range = h("input", { type: "range", min: "1", max: "600", class: "dz-range", "aria-label": `${axis === "width" ? "Width" : "Height"} slider`, disabled: inline });
      const field = numberField(axis === "width" ? "Width in pixels" : "Height in pixels", 1, 4000, (value) => set(axis, value));
      field.disabled = inline;
      range.addEventListener("input", () => set(axis, +range.value));
      controls[axis] = { range, field };
      return h("label", { class: "dz-row dz-sizing-row" }, h("span", { text: axis === "width" ? "Width" : "Height" }), range, field, h("span", { class: "dz-unit", text: "px" }));
    });
    const auto = h("button", { type: "button", class: "dz-menu-button", text: "Back to auto", disabled: inline,
      title: "Remove the width and height you set, so the page sizes it again",
      onclick: () => { askFor.width = askFor.height = undefined; clearSize(targets()); sync(); } });
    sync();
    sizeSync = sync;
    return h("div", { class: "dz-panel" }, ...rows, note, h("div", { class: "dz-menu" }, auto));
  }
  /** One group (padding or margin): a slider for all four sides, or one each when "same on all sides" is off. */
  function spacingGroup(element, kind, title, min, max) {
    const sides = sideNumbers(getComputedStyle(element), kind);
    const same = h("input", { type: "checkbox", "aria-label": `${title}: same on all sides` });
    same.checked = sides.every((n) => n === sides[0]);
    const body = h("div", { class: "dz-panel" });
    const apply = () => applySpacing(targets(), kind, sides);
    function build() {
      body.replaceChildren();
      const rows = same.checked ? [["All", null]] : SIDES.map((side, i) => [side[0].toUpperCase() + side.slice(1), i]);
      for (const [name, index] of rows) {
        const range = h("input", { type: "range", min: String(min), max: String(max), class: "dz-range", "aria-label": `${title} ${name.toLowerCase()}` });
        const number = h("span", { class: "dz-num" });
        const show = () => { const v = index === null ? sides[0] : sides[index]; range.value = String(v); number.textContent = v + "px"; };
        range.addEventListener("input", () => {
          const v = +range.value;
          if (index === null) sides.fill(v); else sides[index] = v;
          show();
          apply();
        });
        show();
        body.append(h("label", { class: "dz-row" }, h("span", { text: name }), range, number));
      }
    }
    same.addEventListener("change", () => { if (same.checked) sides.fill(sides[0]); build(); });
    build();
    return h("div", { class: "dz-spacing" },
      h("label", { class: "dz-row dz-group-head" }, h("strong", { text: title }), h("span", { class: "dz-wide", text: "same on all sides" }), same), body);
  }
  function spacingPanel() {
    const element = selected;
    return h("div", { class: "dz-panel dz-spacing-panel" }, spacingGroup(element, "padding", "Padding", 0, 80), spacingGroup(element, "margin", "Margin", -40, 80));
  }
  const RADIUS_PRESETS = [["Square", "0px"], ["Soft", "6px"], ["Round", "16px"], ["Pill", "9999px"]];
  function radiusPanel() {
    const element = selected;
    const cs = getComputedStyle(element);
    const first = cs.getPropertyValue(CORNERS[0]).split(" ")[0];
    const box = boxSize(element);
    let value = first.endsWith("%") ? Math.round((parseFloat(first) / 100) * Math.min(box.width, box.height)) : Math.round(parseFloat(first) || 0);
    const range = h("input", { type: "range", min: "0", max: "64", class: "dz-range", "aria-label": "Corner radius" });
    const number = h("span", { class: "dz-num" });
    const buttons = RADIUS_PRESETS.map(([name, css]) => h("button", { type: "button", class: "dz-menu-button", text: name,
      onclick: () => { value = parseFloat(css); show(); applyRadius(targets(), css); } }));
    function show() {
      range.value = String(Math.min(64, value));
      number.textContent = value >= 999 ? "Pill" : value + "px";
      buttons.forEach((button, i) => button.classList.toggle("dz-active", parseFloat(RADIUS_PRESETS[i][1]) === value));
    }
    range.addEventListener("input", () => { value = +range.value; show(); applyRadius(targets(), value + "px"); });
    show();
    return h("div", { class: "dz-panel" }, h("div", { class: "dz-menu" }, ...buttons), h("label", { class: "dz-row" }, h("span", { text: "Radius" }), range, number));
  }
  // ---------- design patches ----------
  const PATCH_STATES = [["off", "Off"], ["preview", "Preview"], ["on", "On"]];
  function patchesPanel() {
    const panel = h("div", { class: "dz-panel" }, h("p", { class: "dz-hint", text: "Loading patches…" }));
    host.patches.list().then((patches) => fillPatches(panel, patches))
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
            fillPatches(panel, await host.patches.set(patch.name, state));
            host.patches.reload();
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
      if (!hasChange(rec)) continue;
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
  function select(element, add) {
    if (!element || ours(element) || element === document.documentElement) return;
    if (add && selection.includes(element)) setSelection(selection.filter((el) => el !== element));
    else setSelection(add ? [...selection, element] : [element]);
    if (!add) { closeTool(); menuOpen = false; }   // adding more keeps the tool that is open
    showChanges = false;
    showPatches = false;
    showPreview = false;
    renderDock();
    refreshBoxes();
  }
  const STOPPED = ["click", "dblclick", "mousedown", "mouseup", "pointerdown", "pointerup", "auxclick", "contextmenu", "submit", "dragstart"];
  function swallow(event) {
    if (ours(eventTarget(event))) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.type === "click") { climbed = []; select(eventTarget(event), event.shiftKey); }
  }
  function onMove(event) {
    if (ours(eventTarget(event))) { hovered = null; } else { hovered = eventTarget(event) === document.documentElement ? null : eventTarget(event); }
    refreshBoxes();
  }
  function onKey(event) {
    if (event.altKey && event.shiftKey && event.code === "KeyP") { event.preventDefault(); setOn(!on); return; }
    if (event.altKey && event.shiftKey && event.code === "KeyE") { event.preventDefault(); copyChanges(); return; }   // works with the picker off too
    if (!on) return;
    if (event.altKey && event.shiftKey && (event.code === "KeyC" || event.code === "KeyV") && selected) {
      event.preventDefault();
      if (event.code === "KeyC") copyLook(selected); else pasteLook(targets());
      return;
    }
    if (tourStep !== null) {   // the guide is open: Esc closes it, the arrows turn its pages
      if (event.key === "Escape") { event.preventDefault(); endTour(); return; }
      if (event.key === "ArrowRight" && tourStep < TOUR.length - 1) { event.preventDefault(); tourStep += 1; renderDock(); return; }
      if (event.key === "ArrowLeft" && tourStep > 0) { event.preventDefault(); tourStep -= 1; renderDock(); return; }
    }
    if ((event.ctrlKey || event.metaKey) && !event.altKey && /^[zy]$/i.test(event.key)) {
      const editing = isEditingField(eventTarget(event));
      const forward = event.key.toLowerCase() === "y" || event.shiftKey;
      if (!editing && (forward ? redoStack.length : undoStack.length)) { event.preventDefault(); if (forward) redo(); else undo(); }
      return;
    }
    if (event.key.toLowerCase() === "i" && !event.ctrlKey && !event.metaKey && !event.altKey && !isEditingField(eventTarget(event))) {   // toggle the inspector
      event.preventDefault();
      if (!event.repeat) { setInspect(!inspecting); renderDock(); }
      return;
    }
    if (event.key.toLowerCase() === "m" && !event.ctrlKey && !event.metaKey && !event.altKey && selected && !isEditingField(eventTarget(event))) {   // hold to measure
      event.preventDefault();
      if (!event.repeat) { measuring = true; renderMeasure(); }
      return;
    }
    if (event.key === "\\" && !event.ctrlKey && !event.metaKey && !event.altKey && !isEditingField(eventTarget(event))) {   // hold to compare
      event.preventDefault();
      if (!event.repeat) setCompare(true);
      return;
    }
    const typing = ours(eventTarget(event)) && /^(INPUT|TEXTAREA|SELECT)$/.test(eventTarget(event).tagName);
    if (event.key === "Escape") {
      event.preventDefault();
      if (showPreview) showPreview = false;
      else if (showPatches) showPatches = false;
      else if (showChanges) showChanges = false;
      else if (tool) closeTool();
      else if (menuOpen) menuOpen = false;
      else if (selected) { setSelection([]); climbed = []; }
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
    } else if (event.key === "Enter" && selected && (!ours(eventTarget(event)) || eventTarget(event) === toggle)) {   // the Design button keeps focus after a mouse click: Enter must not press it
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
    if (!on) { inspecting = false; renderInspect(null); hovered = null; setSelection([]); climbed = []; menuOpen = false; tool = null; showChanges = false; tourStep = null; showPatches = false; }
    else {
      if (!host.tourSeen()) tourStep = 0;   // the first time on this browser
    }
    renderDock();
    refreshBoxes();
  }

  window.__designerApi = { toggle: () => setOn(!on), isOn: () => on };   // the extension's toolbar button
  if (PREVIEW) { startPreviewMode(); return; }   // the screen-size preview window
  window.addEventListener("keydown", onKey, true);
  window.addEventListener("scroll", refreshBoxes, { capture: true, passive: true });
  window.addEventListener("resize", refreshBoxes);
  setInterval(() => { if (on && selected) refreshBoxes(); }, 500);   // the page may move things around (the board does)
  toggle.addEventListener("click", () => setOn(!on));
  host.mount(root);
  restore();
  if (host.takeGuide("designer")) {   // asked for on the Guides page
    setOn(true);
    tourStep = 0;
    renderDock();
  }
})();
