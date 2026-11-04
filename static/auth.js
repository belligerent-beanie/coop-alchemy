"use strict";

// Login session shared by every page. The token is kept in localStorage and sent as X-Session.
const Auth = {
  token() {
    try { return localStorage.getItem("alchemy-session") || ""; } catch (_) { return ""; }
  },
  user() {
    try { return localStorage.getItem("alchemy-user") || ""; } catch (_) { return ""; }
  },
  set(token, user) {
    localStorage.setItem("alchemy-session", token);
    localStorage.setItem("alchemy-user", user);
  },
  clear() {
    try {
      localStorage.removeItem("alchemy-session");
      localStorage.removeItem("alchemy-user");
    } catch (_) { /* ignore */ }
  },
  headers(extra = {}) {
    return { ...extra, "X-Session": this.token() };
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
    try { await fetch("/logout", { method: "POST", headers: this.headers({ "Content-Type": "application/json" }), body: "{}" }); }
    catch (_) { /* the server forgets the session on its own anyway */ }
    this.clear();
    location.href = "/login";
  },
};
