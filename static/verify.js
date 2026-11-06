"use strict";

// The link looks like /verify#<token>. The token is after the #, so it is never sent to the server in the address.
const message = document.getElementById("verify-msg");
const token = location.hash.slice(1);
history.replaceState(null, "", location.pathname); // do not leave the token in the address bar or history

(async () => {
  if (!token) {
    message.textContent = "This page needs the link from your confirmation email.";
    return;
  }
  try {
    const res = await fetch("/verify-email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
    message.textContent = `Thanks, ${data.username}! Your email is confirmed. You can log in now.`;
  } catch (err) {
    message.textContent = err.message;
  }
  document.getElementById("verify-login").hidden = false;
})();
