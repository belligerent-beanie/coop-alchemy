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
 *  /me) loads it, and it is asked once per browser tab. The picker is a convenience, not a security boundary: it only changes
 *  this browser's copy of the page. */
(async function loadDesigner() {
  const user = Auth.user();
  if (!user) return;
  let known = null;
  try { known = JSON.parse(sessionStorage.getItem("alchemy-designer") || "null"); } catch (_) { /* unreadable: ask again */ }
  if (!known || known.user !== user) {
    try {
      const res = await fetch("/me", { headers: Auth.headers() });
      if (!res.ok) return;
      known = { user, designer: (await res.json()).designer === true };
      sessionStorage.setItem("alchemy-designer", JSON.stringify(known));
    } catch (_) { return; }   // offline, or storage blocked: no picker this time
  }
  if (!known.designer) return;
  document.head.append(Object.assign(document.createElement("link"), { rel: "stylesheet", href: "/static/designer.css" }));
  document.head.append(Object.assign(document.createElement("script"), { src: "/static/designer.js" }));
})();
