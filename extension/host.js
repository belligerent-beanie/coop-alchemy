// The extension's side of the picker's host (see "where it runs" at the top of designer.js). It runs in the page's isolated world
// before designer.js and supplies window.__designerHost: edits kept in chrome.storage (per site and page, out of reach of the page's
// own scripts), the picker's UI in a closed-off Shadow DOM (the page's CSS cannot touch it, and its CSS stays out of the page),
// no design patches (those belong to the game's server), and previews through the background worker.
(() => {
  if (window.__designerHostInstalled) return;
  window.__designerHostInstalled = true;

  const key = "dz-changes:" + location.origin + location.pathname;
  const cache = { edits: null, tour: false };
  let preview = false;

  // designer.js waits for this before it starts, so its reads of saved edits can stay synchronous.
  const ready = Promise.all([
    chrome.storage.local.get([key, "dz-tour"]).then((stored) => { cache.edits = stored[key] ?? null; cache.tour = stored["dz-tour"] === "done"; }),
    chrome.runtime.sendMessage({ type: "who" }).then((answer) => { preview = !!(answer && answer.preview); }).catch(() => { /* no worker: not a preview */ }),
  ]);

  const shadowHost = document.createElement("div");
  shadowHost.id = "design-picker-extension-root";
  shadowHost.style.cssText = "all: initial; position: fixed; top: 0; left: 0; width: 0; height: 0; z-index: 2147483647;";
  const shadow = shadowHost.attachShadow({ mode: "open" });

  window.__designerHost = {
    ready,
    storageKey: () => key,
    readEdits: () => cache.edits,
    writeEdits(_key, value) {
      cache.edits = value;
      if (value === null) chrome.storage.local.remove(key); else chrome.storage.local.set({ [key]: value });
    },
    onEditsChanged(_key, callback) {   // a preview window hears about edits made in the page it mirrors
      chrome.storage.onChanged.addListener((changes, area) => { if (area === "local" && changes[key]) callback(); });
    },
    tourSeen: () => cache.tour,
    markTourSeen() { cache.tour = true; chrome.storage.local.set({ "dz-tour": "done" }); },
    patches: null,
    takeGuide: () => false,
    isPreview: () => preview,
    openPreview: (width, height) => chrome.runtime.sendMessage({ type: "preview", url: location.href, width, height })
      .then((answer) => (answer && answer.error) || true),   // a string is shown to the designer as a message
    mount(root) {
      const sheet = new CSSStyleSheet();   // a constructed sheet: not blocked by the page's content security policy
      sheet.replaceSync(window.__designerCss || "");
      shadow.adoptedStyleSheets = [sheet];
      shadow.append(root);
      document.documentElement.append(shadowHost);
    },
    isOwn: (node) => node === shadowHost,
  };

  chrome.runtime.onMessage.addListener((message) => {
    if (message && message.type === "toggle") ready.then(() => { if (window.__designerApi) window.__designerApi.toggle(); });
  });
})();
