# Putting Co-op Alchemy on a real website

The server was written for a trusted home network. On the open internet, put it behind a
reverse proxy and work through the checklist at the bottom.

## 1. Architecture

```
browser --HTTPS--> Caddy/nginx (TLS, limits, buffering) --HTTP--> server.py on 127.0.0.1:8000
```

- The app listens on loopback only: `--host 127.0.0.1`.
- The proxy terminates TLS, so passwords and session tokens are encrypted, and it absorbs slow or
  oversized requests before they reach Python's `http.server`.
- Tell the app which proxy to believe with `--trusted-proxy 127.0.0.1`. Without it, every visitor
  looks like `127.0.0.1`, so the login and signup rate limits would be shared by everyone and
  the per-address lockout would be useless. With it, the app reads the client address from
  `X-Forwarded-For`, but only when the connection comes from a trusted proxy, and it takes the entry
  your proxy appended, so a client cannot fake its address. It also sends HSTS when the proxy says
  the request was HTTPS (`X-Forwarded-Proto`).

```
python3 server.py --recipes recipes.json --host 127.0.0.1 --port 8000 --trusted-proxy 127.0.0.1
```

### Caddy (simplest: automatic Let's Encrypt certificates)

```
alchemy.example.com {
    request_body {
        max_size 8KB            # the app already refuses bodies over 4 KB
    }
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1       # do not buffer the server-sent event stream
    }
}
```

Caddy forwards `Host`, `X-Forwarded-For` and `X-Forwarded-Proto` by default. The app's cross-site check
compares `Origin` with `Host`, so the proxy must pass the original `Host` through (Caddy and nginx's
`proxy_set_header Host $host` both do).

### nginx (if you prefer it)

```
limit_req_zone $binary_remote_addr zone=auth:10m rate=30r/m;
server {
    listen 443 ssl http2;
    server_name alchemy.example.com;
    # ssl_certificate / ssl_certificate_key from certbot
    client_max_body_size 8k;
    client_header_timeout 15s;
    client_body_timeout 15s;
    location ~ ^/(login|register)$ {
        limit_req zone=auth burst=10 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location ~ /events$ {                  # server-sent events
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

(Use `$remote_addr`, not `$proxy_add_x_forwarded_for`, so a client's own `X-Forwarded-For` is replaced
rather than extended.)

### Connections: keep-alive
The server speaks HTTP/1.1 keep-alive, so a browser reuses one connection for its many small requests (cursor
updates especially). Measured with `bench_cursor.py` (5 players, cursor updates every 40 ms): median latency of an
update fell from 21.5 ms to 1.7 ms, server CPU per update from 2.5 ms to 1.7 ms, and a player's updates no longer
open a connection each. Things to know:
- An idle kept-alive connection is closed after 30 seconds (`Handler.timeout`), and every open connection holds one
  server thread, so capacity is about *open connections*, not requests per second. Count roughly 2 to 4 per
  browser tab (page requests, plus the event stream).
- A request body the server did not read is discarded when it is up to 64 KB; anything larger, chunked, or sent on a
  GET ends the connection instead, because the leftover bytes would otherwise be misread as the next request.
- Behind nginx, add `proxy_http_version 1.1;` and `proxy_set_header Connection "";` to the locations above if you want
  the proxy to keep its own connections to the app open too (Caddy does this by default). The event stream needs no change.

### Shared cursors
Pointer sharing is the busiest traffic in a game, so it is kept cheap (`bench_cursor.py` measures it; 5 players):
- **The page** sends a player's newest pointer position from a 20 Hz timer, skips a tick while the previous send is still on
  its way, never repeats a position, and sends nothing at all while the player is alone in the room (daily challenges
  and races). That is 15% fewer updates and bytes than the old send-wait-pause loop, and about 24 requests a second
  saved per solo player.
- **The server** waits `CURSOR_BATCH_SECONDS` (30 ms, in `server.py`) after a cursor event and sends everything queued for
  that player in one write, keeping only each player's newest cursor position. In an interleaved test (6 rounds of 12 s) it
  cut server CPU from about 22% to about 10% of a core, for 20 to 30 ms of extra cursor delay (median 24 ms, worst 36 ms).
  No event is lost and every player's final position always arrives. Other events are never held back by more than the
  window. Set the constant to 0 to turn batching off.
- **The receiver** glides each remote cursor to its newest position (60 ms time constant, only while something is moving)
  and fades a cursor out after 5 s without movement.
- Cursor updates hold the game lock for about 30 microseconds each (0.3% of the time at 100 updates a second), so
  they are not a contention problem at this room size. Moving to WebSockets only becomes worthwhile for much larger rooms.
- Benchmark numbers on a laptop vary by 20% or more from run to run; compare configurations by alternating them, as
  done here, not by running one after the other.

### Running it as a service (Linux)

```
[Unit]
Description=Co-op Alchemy
After=network.target

[Service]
User=alchemy
WorkingDirectory=/srv/coop-alchemy
ExecStart=/usr/bin/python3 server.py --host 127.0.0.1 --trusted-proxy 127.0.0.1
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/srv/coop-alchemy
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

On Linux `reload.py` works (it relies on a Unix signal), so you can patch code without ending games.
On Windows a deploy means a restart, which ends every running game.

## 2. What the code now does for you

| Protection | Where |
|---|---|
| Passwords hashed with PBKDF2-SHA256, 600k iterations, per-user salt | `accounts.py` |
| Lockout after 5 wrong passwords, per account **and** client address, so strangers can't lock you out | `accounts.py` |
| 20 logins/min and 5 signups/10 min per client address | `throttle.py`, `server.py` |
| CSP (`script-src 'self'`), `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, HSTS behind HTTPS | `server.py` |
| Cross-site POSTs refused (`Origin` must match `Host`) | `server.py` |
| 30 s read timeout; 4 KB request bodies | `server.py` |
| Event streams use a one-time, 30-second ticket, never the player token in a URL | `throttle.py`, `server.py`, `static/app.js` |
| Single-use, hashed invite codes for signup (`--require-invite`) | `invites.py`, `accounts_admin.py` |
| Cloudflare Turnstile on signup (`--turnstile-site-key`) | `captcha.py` |
| Confirmed email addresses, 24-hour expiry of unconfirmed accounts (`--require-email-verification`) | `accounts.py`, `mailer.py` |
| Auth audit log (logins, failures, throttling, signups, with client address) to stderr | `server.py` |

## 3. The rest, in detail

### Registration: single-use invite codes (built)
Turn it on with `--require-invite`. Signup then needs a code, and the Create account tab shows an "Invite code"
box (the page asks `GET /auth-config` whether one is needed). Login never asks for one.

```
python3 server.py --host 127.0.0.1 --trusted-proxy 127.0.0.1 --require-invite
python3 accounts_admin.py invite create --count 5 --note "beta testers"   # prints the codes once
python3 accounts_admin.py invite list                                     # id, unused / used by whom, note
python3 accounts_admin.py invite revoke <id>                              # delete an unused code
```

- Codes are 12 characters from an alphabet without look-alikes (`7CR6-E76W-PQ4W`, 60 bits), typed with or without
  hyphens and in any case. Guessing is not realistic, and bad guesses count against the per-address signup limit.
- Only SHA-256 hashes are stored in `invites.json` (gitignored, mode 0600), so a leaked file gives away no unused
  codes. That also means a lost code can't be recovered: revoke it and make another.
- A code is spent only when the account is actually created. A taken username or a weak password gives it back.
  Two people racing for one code: exactly one wins (tested).
- You can create or revoke codes while the server runs; it re-reads the file on each signup.
- Up to 1000 codes are kept. Used codes stay in the file as a record of who joined with which invite
  (the `id` shown by `invite list`), so you can trace abuse back to whoever shared a code.
- Not built, if you outgrow invites: email verification or a CAPTCHA (needs a mail service or a Turnstile/hCaptcha key).

### Open signup: Turnstile human check plus email verification (built)
Use these two together if you don't want invite codes. They can also be combined with `--require-invite`.
All secrets come from environment variables, never the command line or git.

```
export TURNSTILE_SECRET_KEY=...          # from Cloudflare (private)
export SMTP_HOST=smtp.example-provider.com SMTP_PORT=587 SMTP_USER=... SMTP_PASSWORD=...
export MAIL_FROM="Co-op Alchemy <noreply@alchemy.example.com>"
python3 server.py --host 127.0.0.1 --trusted-proxy 127.0.0.1 \
    --turnstile-site-key 0x4AAAA... --require-email-verification --public-url https://alchemy.example.com
```

**What each piece does**
- *Turnstile:* the signup form shows Cloudflare's widget. The server checks the answer with Cloudflare **before** anything
  else on signup (so bots can't probe usernames or burn invite codes). If Cloudflare is unreachable, signup is refused
  (fail closed). The CSP gains Cloudflare's origin only while Turnstile is on.
- *Email verification:* signup asks for an address and creates an account that can't log in yet. The emailed link
  (`/verify#token`; the token is after the `#`, so it never appears in server logs or Referer headers) activates it.
  Links last 24 hours, work once, and a new request replaces the old one. Accounts left unconfirmed for 24 hours are
  deleted, which frees the username and address, and a pending signup can't block the real owner of an address.
  Aliases of one mailbox (`+tag`, Gmail dots) count as the same address, so one mailbox can't open many accounts.
  Login says "confirm your email" only after the password is right. "Resend" always gives the same answer, limited to
  10 per address per hour and 3 emails per mailbox per hour.
- `accounts_admin.py verify <user>` confirms an account by hand; `accounts_admin.py list` marks unconfirmed ones.
- Accounts created before you switched this on keep working (they have no address on file).

**What you have to do (in order)**
1. **Get a domain**, then point it at the server and set up the proxy and HTTPS (section 1).
2. **Turnstile:** make a free Cloudflare account, open *Turnstile*, add a widget for your domain, copy the **site key**
   (goes in `--turnstile-site-key`) and **secret key** (goes in `TURNSTILE_SECRET_KEY`). The server needs outbound HTTPS to
   `challenges.cloudflare.com`.
3. **Email:** pick a provider that offers SMTP (Resend, Postmark, Amazon SES, Mailgun, ...), verify your domain with
   them, and add the SPF, DKIM and DMARC DNS records they show you. Without those, confirmation emails land in spam. Put
   the SMTP login in the `SMTP_*` variables and the sender in `MAIL_FROM`.
4. Start the server with the flags above, sign up with a real address of yours, and check the email arrives (and
   not in junk) before you announce the site.

**Trying it before you have a domain**
- Turnstile publishes test keys: site key `1x00000000000000000000AA` with secret `1x0000000000000000000000000000000AA`
  always passes (use `2x00000000000000000000AB` and secret `2x0000000000000000000000000000000AA` to see failures). They only
  work as a test; the widget shows a red "for testing only" banner.
- `SMTP_HOST=console` prints each email, link included, to the server log instead of sending it. **Development only**:
  the log then holds working confirmation links.

**Limits to know about**
- Verification slows down throwaway accounts but doesn't stop a bot with disposable mailboxes. Turnstile is what stops
  scripted signups.
- Signup reveals whether a username or address is already taken ("that email address already has an account"). The
  per-address signup limit (5 per 10 minutes) keeps that slow to harvest, but it is enumeration.
- You now store email addresses: say so in the privacy notice, and plan account deletion (below).
- `MAX_ACCOUNTS` is 500 in `accounts.py`. For open signup, raise it deliberately. Looking up the logged-in account
  no longer re-reads `accounts.json` on every request: it is parsed once and again only when the file changes (about
  50 microseconds per request at any size, down from 1.4 ms at 500 accounts and 15 ms at 5000). Signup, login and
  other changes still rewrite the whole file, which is fine at hundreds to low thousands of accounts.
- Email verification stays available but is off by default: it means storing an email address per player. Recovery works without it (above).

### Account recovery and deletion (built, with no email needed)
The server collects only a username and a password hash, and keeps it that way, so recovery does not use email:
- **Forgot the password:** an administrator opens the operations page (`/ops`), clicks "New password" beside the account, and
  gives the owner the temporary password shown once. The old password and logins stop working at once, and at the next login
  the owner is made to choose their own. (`accounts_admin.py reset` does the same from the command line.) The owner has to
  contact you out of band, so say how on your site.
- **Changing a password:** "Account" on the home page (needs the current password; ends the account's other logins).
- **Deleting an account:** "Account" on the home page, or an administrator from `/ops`, or `accounts_admin.py delete`. One routine
  (`erasure.py`) removes the account, recipe drafts, daily scores and attempts, saved games nobody else played, and replaces the
  name in shared saved games with "deleted player" and on the invite code it used. Credit on a recipe already added to
  `recipes.json` is part of the recipe book and is edited by hand if someone asks. Log lines are not edited; they expire.
- **Moderation:** from `/ops`, disable an account (its logins stop, nothing is deleted; reversible), end any running game, or delete an
  account. Administrators can only be changed with `accounts_admin.py`, so a slip on the page cannot lock you out.
- `/privacy` states what is stored and for how long. It is written from what the code does and says the activity log is deleted
  after 14 days: keep that true (see Logging below), and add a way to reach you if you want one on that page.

### Logging, monitoring and alerting
The access log is silenced (`log_message` is a no-op), so only the new auth lines exist. Decide where stderr
goes (journald, a file with rotation) and alert on bursts of `login failed` and `throttled`. Add an uptime check
against `/login`. The proxy's own access log gives you the request side. Make sure it does not log query strings
if you ever re-add tokens to URLs.

### Capacity
Defaults assume a living room: 3 concurrent games, 5 players each, 2000 sessions. Python's `ThreadingHTTPServer`
uses one thread per connection, and each open game page holds a thread for its event stream, plus a thread per
in-flight request. That's fine for dozens of simultaneous players and unproven beyond that. Before launch, run a
load test (for example with `locust` or a small script opening N event streams) and pick limits from the results.
Raise `MAX_GAMES`/`MAX_ACCOUNTS` deliberately rather than by default. If you outgrow one process, the state is in
memory and JSON files, so scaling out means a redesign, not a flag.

### Data, backups and storage
State is in `accounts.json`, `drafts.json`, `daily.json` and `saves/`. Writes are atomic (temp file plus
`os.replace`) but there is no backup. Run a nightly copy off the machine (the files are small) and test a restore.
`accounts.json` holds password hashes and should be mode 0600 and owned by the service user. All of it is
gitignored; keep it that way.

### Content and moderation
Usernames and game names are shown to other players. Rendering is safe (`textContent`, and the CSP blocks
inline script), but nothing stops abusive text. Minimum viable: a way to report, plus admin tooling to rename or
ban an account. Consider a word filter on usernames and game names. Decide whether random room names (now the
default for unnamed games) are enough for invites or whether named public rooms need review.

### Legal and policy
A privacy notice (what you store: username, password hash, game history, IP addresses in logs), terms of use,
and a contact address. If anyone under 13 could play, check COPPA and similar rules. Add a retention rule for logs.

### Remaining hardening
- Drop `'unsafe-inline'` from `style-src` once the inline `style=` attributes and `laptop.html`'s `<style>` are
  moved into `style.css`.
- Session tokens still live in `localStorage`, which any XSS could read. The CSP makes XSS much harder, but
  moving to an `HttpOnly; Secure; SameSite=Strict` cookie plus a CSRF header would remove the exposure. It's a
  larger change touching every page.
- No breached-password check and no absolute session lifetime (sessions slide for 24 hours of use, forever).
- Keep Python patched, and run `pip`-free as now: the stdlib-only design means no dependency to track.
- Run an automated scan (OWASP ZAP) against a staging copy and fix what it finds.
- Keep `aigen/` and `/admin` away from the public. Put `/admin*` behind the proxy's own auth (basic auth or an
  IP allowlist) as a second lock.

## 4. Pre-launch checklist

- [ ] Domain, DNS and TLS certificate (Caddy does this automatically)
- [ ] App on `127.0.0.1` with `--trusted-proxy`; confirm the audit log shows real client addresses
- [ ] Proxy limits: body size, timeouts, rate limit on `/login` and `/register`, no buffering on `/events`
- [ ] Signup gate on: `--require-invite` with a batch of codes, or Turnstile + `--require-email-verification`
- [ ] Recovery and account-deletion policy decided
- [ ] Logs collected and rotated; alert on login-failure bursts; uptime check
- [ ] Backups scheduled and a restore tested
- [ ] Load test run; limits set from it
- [ ] Moderation path and admin lock at the proxy
- [ ] Privacy notice, terms, contact address
- [ ] Staging scan (ZAP), then a manual pass through every page in a real browser
