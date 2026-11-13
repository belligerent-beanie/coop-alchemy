"use strict";

// Login session shared by every page. The server keeps the session in an HttpOnly cookie that scripts cannot read, so the page
// only remembers WHO is logged in (a name, not a secret) to show it and to decide whether to ask for a login. The real check
// is the server's: a 401 clears the name and sends the person to log in. An old tab may still hold the previous version's
// token in localStorage; it is sent once so the server can swap it for the cookie, then deleted (see upgradeOldToken).
const Auth = {
  user() {
    try { return localStorage.getItem("alchemy-user") || ""; } catch (_) { return ""; }
  },
  loggedIn() {
    return this.user() !== "";
  },
  set(user) {
    localStorage.setItem("alchemy-user", user);
  },
  clear() {
    try {
      localStorage.removeItem("alchemy-user");
      localStorage.removeItem("alchemy-session");
    } catch (_) { /* ignore */ }
  },
  oldToken() {
    try { return localStorage.getItem("alchemy-session") || ""; } catch (_) { return ""; }
  },
  headers(extra = {}) {
    const old = this.oldToken();
    return old ? { ...extra, "X-Session": old } : { ...extra };
  },
  /** Go to the login page, which brings the person back here afterwards. */
  toLogin() {
    location.replace("/login?next=" + encodeURIComponent(location.pathname + location.search));
  },
  /** fetch() with the session attached; a 401 sends the person to log in again. */
  async fetch(path, body) {
    const options = { headers: this.headers() };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const res = await fetch(path, options);
    if (res.status === 401) {
      this.clear();
      this.toLogin();
      throw new Error("Please log in.");
    }
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (!res.ok) {
      const err = new Error(data.error || `request failed (${res.status})`);
      err.status = res.status;
      throw err;
    }
    return data;
  },
  async logout() {
    try { await fetch("/logout", { method: "POST", headers: this.headers({ "Content-Type": "application/json" }), body: "{}" }); }  // the reply clears the cookie
    catch (_) { /* the server forgets the session on its own anyway */ }
    this.clear();
    location.href = "/login";
  },
};

/** Move a browser that logged in with the previous version (token in localStorage) over to the cookie, then forget the token. */
async function upgradeOldToken() {
  if (!Auth.oldToken()) return;
  try {
    await fetch("/me", { headers: Auth.headers() });   // the server answers with the cookie
    const res = await fetch("/me");                     // and now it works without the header
    if (res.ok) localStorage.removeItem("alchemy-session");
  } catch (_) { /* try again at the next page load */ }
}
upgradeOldToken();

/** Accounts marked as designers get the on-page UI picker (designer.js). Only an answer from the server ("designer": true on
 *  /me) loads it. It is asked on every page load, and again while the picker is not loaded yet: when the tab comes back into
 *  view and once a minute while it is visible, so an account made a designer gets the picker without reloading. Taking the
 *  role away shows from the next page load. The picker is a convenience, not a security boundary: it only changes this
 *  browser's copy of the page. */
(function loadDesigner() {
  if (!Auth.user()) return;
  const RECHECK_MS = 60000;
  let loaded = false;
  let asking = false;
  async function check() {
    if (loaded || asking || !Auth.user()) return;
    asking = true;
    try {
      const res = await fetch("/me", { headers: Auth.headers() });
      if (!res.ok || (await res.json()).designer !== true) return;
      loaded = true;
      document.head.append(Object.assign(document.createElement("link"), { rel: "stylesheet", href: "/static/designer.css" }));
      document.head.append(Object.assign(document.createElement("script"), { src: "/static/designer.js" }));
    } catch (_) { /* offline: try again at the next check */ } finally { asking = false; }
  }
  check();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) check(); });
  window.addEventListener("focus", check);
  setInterval(() => { if (!document.hidden) check(); }, RECHECK_MS);
})();
