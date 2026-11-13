"use strict";

// ---- being polite to the server ------------------------------------------------------------------------------------------
// Two rules every page follows, so a busy server (or a tester with many tabs) cannot turn into a storm of retries:
//  1. Background updates pause while the tab is hidden, and catch up the moment it is shown again (Net.poll).
//  2. When the server says it is busy (503, 429, 502, 504) or cannot be reached, every background update on the page waits:
//     2 s after the first failure, then 4, 8, … up to a minute, each wait randomised (so tabs do not retry in step) and never
//     shorter than the server's Retry-After. The first success resets it. Things the player does (moving a tile, a click)
//     are never held back: only the automatic polling and reconnecting waits.
const Net = {
  failures: 0,
  busyUntil: 0,
  /** How long to wait after the nth failure in a row: doubling from 2 s up to 60 s, then randomised to between half and all of it. */
  backoff(failures) {
    const ceiling = Math.min(60000, 2000 * 2 ** Math.max(0, failures - 1));
    return ceiling / 2 + Math.random() * (ceiling / 2);
  },
  /** Called with every response (and with null when the request never got an answer). */
  note(res) {
    const busy = res === null || [429, 502, 503, 504].includes(res.status);
    if (!busy) { this.failures = 0; this.busyUntil = 0; return; }
    this.failures += 1;
    let wait = this.backoff(this.failures);
    const retryAfter = res && Number(res.headers.get("Retry-After"));
    if (retryAfter > 0) wait = Math.max(wait, retryAfter * 1000 + Math.random() * 1000);
    this.busyUntil = Math.max(this.busyUntil, Date.now() + wait);
  },
  /** Milliseconds until background requests may go again (0: now). */
  wait() {
    return Math.max(0, this.busyUntil - Date.now());
  },
  /** Run fn about every ms (±10%) while the tab is visible, never two at once, waiting out Net.wait(). When a hidden tab is
   *  shown again, fn runs straight away if it is due. Returns {stop()}. fn's own errors are its business: they are swallowed. */
  poll(fn, ms) {
    let timer = 0, due = Date.now() + ms, running = false, stopped = false;
    const schedule = () => {
      clearTimeout(timer);
      timer = 0;
      if (stopped || running || document.hidden) return;
      timer = setTimeout(tick, Math.max(0, due - Date.now(), this.wait()));
    };
    const tick = async () => {
      timer = 0;
      if (stopped || document.hidden) return;
      if (this.wait() > 0) { schedule(); return; }
      running = true;
      try { await fn(); } catch (_) { /* the page shows its own errors */ } finally { running = false; }
      due = Date.now() + ms * (0.9 + Math.random() * 0.2);
      schedule();
    };
    document.addEventListener("visibilitychange", schedule);
    schedule();
    return { stop() { stopped = true; clearTimeout(timer); document.removeEventListener("visibilitychange", schedule); } };
  },
};
// Every fetch on the page reports back to Net, whichever script made it (Auth.fetch, the game's api(), plain fetch calls).
(function watchFetch() {
  const realFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    let res;
    try { res = await realFetch(...args); } catch (err) {
      if (err && err.name !== "AbortError") Net.note(null);   // no answer at all: down, restarting, or the network dropped
      throw err;
    }
    Net.note(res);
    return res;
  };
})();

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
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !Net.wait()) check(); });
  window.addEventListener("focus", () => { if (!Net.wait()) check(); });
  Net.poll(check, RECHECK_MS);   // check() does nothing once the picker is loaded
})();
