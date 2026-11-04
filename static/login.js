"use strict";

const $ = (id) => document.getElementById(id);
let registering = false;

function nextPage() {
  const next = new URLSearchParams(location.search).get("next") || "/";
  return next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/login") ? next : "/";
}

function setMode(register) {
  registering = register;
  $("tab-login").setAttribute("aria-selected", String(!register));
  $("tab-register").setAttribute("aria-selected", String(register));
  $("auth-submit").textContent = register ? "Create account" : "Log in";
  $("username-hint").hidden = !register;
  $("password-hint").hidden = !register;
  $("password2").hidden = !register;
  $("password2-label").hidden = !register;
  $("password2").required = register;
  $("password").autocomplete = register ? "new-password" : "current-password";
  $("auth-error").textContent = "";
}

$("tab-login").addEventListener("click", () => setMode(false));
$("tab-register").addEventListener("click", () => setMode(true));

$("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("auth-error").textContent = "";
  if (registering && $("password").value !== $("password2").value) {
    $("auth-error").textContent = "The two passwords are different.";
    return;
  }
  $("auth-submit").disabled = true;
  try {
    const res = await fetch(registering ? "/register" : "/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("username").value, password: $("password").value }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
    Auth.set(data.session, data.username);
    location.replace(nextPage());
  } catch (err) {
    $("auth-error").textContent = err.name === "QuotaExceededError" || err.name === "SecurityError"
      ? "This browser is blocking site storage, so you can't stay logged in."
      : err.message;
    $("auth-submit").disabled = false;
  }
});

// Already logged in? Skip this page.
if (Auth.token()) {
  fetch("/me", { headers: Auth.headers() }).then((res) => { if (res.ok) location.replace(nextPage()); });
}
