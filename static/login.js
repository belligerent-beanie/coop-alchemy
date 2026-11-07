"use strict";

const $ = (id) => document.getElementById(id);
let registering = false;
let inviteRequired = false;
let emailRequired = false;
let turnstileKey = null;
let turnstileWidget = null;
let captchaToken = "";

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
  $("terms-hint").hidden = !register;
  $("password2").hidden = !register;
  $("password2-label").hidden = !register;
  $("password2").required = register;
  const showInvite = register && inviteRequired;
  $("invite").hidden = !showInvite;
  $("invite-label").hidden = !showInvite;
  $("invite-hint").hidden = !showInvite;
  $("invite").required = showInvite;
  const showEmail = register && emailRequired;
  $("email").hidden = !showEmail;
  $("email-label").hidden = !showEmail;
  $("email-hint").hidden = !showEmail;
  $("email").required = showEmail;
  $("turnstile-box").hidden = !(register && turnstileKey);
  $("password").autocomplete = register ? "new-password" : "current-password";
  $("auth-error").textContent = "";
  $("auth-info").hidden = true;
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
  if (registering && turnstileKey && !captchaToken) {
    $("auth-error").textContent = "Please complete the human check first.";
    return;
  }
  $("auth-submit").disabled = true;
  try {
    const res = await fetch(registering ? "/register" : "/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: $("username").value, password: $("password").value,
        ...(registering && inviteRequired ? { invite: $("invite").value } : {}),
        ...(registering && emailRequired ? { email: $("email").value } : {}),
        ...(registering && turnstileKey ? { captcha: captchaToken } : {}),
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
    if (data.verify_email) { // the account exists but waits for its emailed link
      $("auth-info").textContent = `Almost done! We sent a confirmation link to ${$("email").value.trim()}. ` +
        "Open it to activate your account, then log in here.";
      $("auth-info").hidden = false;
      $("auth-form").reset();
      setMode(false);
      $("auth-info").hidden = false;
      resetCaptcha();
      $("auth-submit").disabled = false;
      return;
    }
    Auth.set(data.session, data.username);
    location.replace(data.must_change_password ? "/" : nextPage()); // a temporary password is replaced on the home page
  } catch (err) {
    $("auth-error").textContent = err.name === "QuotaExceededError" || err.name === "SecurityError"
      ? "This browser is blocking site storage, so you can't stay logged in."
      : err.message;
    resetCaptcha(); // a Turnstile answer works once, so a failed attempt needs a fresh one
    $("auth-submit").disabled = false;
  }
});

// Already logged in? Skip this page.
if (Auth.token()) {
  fetch("/me", { headers: Auth.headers() }).then((res) => { if (res.ok) location.replace(nextPage()); });
}

// Does this server want an invite code to sign up?
fetch("/auth-config").then((res) => res.json()).then((cfg) => {
  inviteRequired = cfg.invite_required === true;
  emailRequired = cfg.email_required === true;
  turnstileKey = cfg.turnstile_site_key || null;
  $("resend").hidden = !emailRequired;
  if (turnstileKey) loadTurnstile(turnstileKey);
  setMode(registering);
}).catch(() => { /* if a code is needed, the server says so when signup is tried */ });

// ---- Turnstile (the human check) and "send the email again" ----------------------------------------

function resetCaptcha() {
  captchaToken = "";
  if (turnstileWidget !== null && window.turnstile) window.turnstile.reset(turnstileWidget);
}

function loadTurnstile(siteKey) {
  const script = document.createElement("script");
  script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
  script.async = true;
  script.onload = () => {
    turnstileWidget = window.turnstile.render("#turnstile-box", {
      sitekey: siteKey,
      callback: (token) => { captchaToken = token; },
      "expired-callback": () => { captchaToken = ""; },
      "error-callback": () => { captchaToken = ""; },
    });
  };
  script.onerror = () => {
    $("auth-error").textContent = "The human check could not be loaded, so signup is not possible right now.";
  };
  document.head.appendChild(script);
}

$("resend-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("resend-msg").textContent = "";
  try {
    const res = await fetch("/resend-verification", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("resend-email").value }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
    $("resend-msg").textContent = "If that address is waiting for confirmation, a new link is on its way.";
  } catch (err) {
    $("resend-msg").textContent = err.message;
  }
});
