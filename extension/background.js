// The extension's service worker. The toolbar button (or Alt+Shift+P) injects the picker into the current tab and toggles it. With only
// "activeTab", that is all it may touch: the tab the designer just asked about, until they leave it. Screen-size previews open a second
// window and need the optional "all sites" permission to put the picker's edits on it (see options.html).

const FILES = ["host.js", "designer-css.js", "designer.js"];   // host first: it sets window.__designerHost for the picker
const ALL_SITES = { origins: ["<all_urls>"] };

async function inject(tabId) {
  await chrome.scripting.executeScript({ target: { tabId }, files: FILES });
}

async function toggle(tab) {
  if (!tab || tab.id === undefined) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "toggle" });   // already in the page
  } catch (_) {
    try {
      await inject(tab.id);
      await chrome.tabs.sendMessage(tab.id, { type: "toggle" });
    } catch (_error) {   // chrome:// pages, the Web Store, a PDF: the browser does not allow it
      chrome.action.setBadgeText({ tabId: tab.id, text: "×" });
      chrome.action.setTitle({ tabId: tab.id, title: "The design picker cannot run on this page" });
    }
  }
}
chrome.action.onClicked.addListener(toggle);

// ---- screen-size previews: a real window of that size, with the same saved edits on the page
// What is known about each preview tab lives in session storage (a service worker is stopped and restarted whenever it is idle).
const previewTabs = {
  async all() { return (await chrome.storage.session.get("preview-tabs"))["preview-tabs"] || {}; },
  async get(tabId) { return (await this.all())[tabId]; },
  async set(tabId, info) { const all = await this.all(); all[tabId] = info; await chrome.storage.session.set({ "preview-tabs": all }); },
  async remove(tabId) { const all = await this.all(); delete all[tabId]; await chrome.storage.session.set({ "preview-tabs": all }); },
};

async function openPreview({ url, width, height }) {
  if (!(await chrome.permissions.contains(ALL_SITES))) {
    return { error: "Previews need the extension's \"all sites\" permission: right-click its icon, choose Options, and allow it." };
  }
  const win = await chrome.windows.create({ url, type: "popup", width, height });
  const tab = win.tabs && win.tabs[0];
  if (tab) await previewTabs.set(tab.id, { windowId: win.id, width, height, sized: false });
  return { ok: true, windowId: win.id };
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message.type === "who") {
    previewTabs.get(sender.tab ? sender.tab.id : -1).then((info) => respond({ preview: !!info }));
    return true;
  }
  if (message.type === "preview") {
    openPreview(message).then(respond, (error) => respond({ error: String((error && error.message) || error) }));
    return true;
  }
  return false;
});

// A preview window gets the picker (in preview mode: no dock, just the edits) every time its page loads, and is sized once so that
// the inside of the window, not its frame, is the size asked for.
chrome.tabs.onUpdated.addListener(async (tabId, change) => {
  if (change.status !== "complete") return;
  const info = await previewTabs.get(tabId);
  if (!info) return;
  try {
    await inject(tabId);
    if (!info.sized) {
      const [frame] = await chrome.scripting.executeScript({ target: { tabId }, func: () => [outerWidth - innerWidth, outerHeight - innerHeight] });
      const [dx, dy] = (frame && frame.result) || [0, 0];
      await chrome.windows.update(info.windowId, { width: info.width + dx, height: info.height + dy });
      await previewTabs.set(tabId, { ...info, sized: true });
    }
  } catch (_) { /* the page may not allow scripts */ }
});
chrome.tabs.onRemoved.addListener((tabId) => { previewTabs.remove(tabId); });
