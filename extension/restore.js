// Registered by the service worker (only while the designer has allowed all sites) to run on every page once it has loaded. It is cheap on
// purpose: it asks storage whether this page has saved edits, and only if it does asks the worker to put the picker on the page, which puts
// the edits back. The picker itself stays off until the toolbar button or Alt+Shift+P turns it on.
(() => {
  if (window.__designerHostInstalled) return;   // already there (the picker was turned on, or this page was restored)
  const key = "dz-changes:" + location.origin + location.pathname;
  chrome.storage.local.get(key).then((stored) => {
    if (stored[key]) chrome.runtime.sendMessage({ type: "restore" });
  }).catch(() => { /* the extension was reloaded under this page: nothing to do */ });
})();
