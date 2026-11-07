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

### Moving to Cloudflare Tunnel and an external host
Step by step with a check after each step: [LAUNCH.md](LAUNCH.md). What follows is the background.

The plan: the game runs in a container on a small rented server that has **no open ports**; a second container, Cloudflare's
tunnel, makes an outbound connection to Cloudflare, and visitors reach the game through your domain on Cloudflare. Your home
address is never involved, Cloudflare answers HTTPS, and there is nothing on the host to port-scan.

**Built and run locally, not yet on the real host.** On 2026-11-07 the image was built with Docker Desktop for Windows and the game
service started from `docker-compose.yml`. Checked: it builds; it runs as the non-root user; the health check reports healthy; accounts
and solo progress are written to the `/data` volume and are still there after a restart; element pictures are served; no port is
published to the host; and a request from another container on the compose network carrying `CF-Connecting-IP` is logged with that
address (so the rate limits see real visitors). `test_deploy_files.py` keeps proving what it can without Docker. **Not yet checked:**
the `cloudflared` tunnel container (it needs a real token), and anything on the rented server. Do those with a throwaway tunnel
before the actual move.

If `docker build` says `docker-credential-desktop` is not found on Windows, the shell was opened before Docker Desktop was installed:
open a new one, or add `C:\Program Files\Docker\Docker\resources\bin` to its PATH.

**What you need to do (these need your accounts):**
1. A domain whose DNS is on Cloudflare (a free account is enough).
2. A small server with Docker. The measurements above say 1 CPU and 1 GB hold about 50 players in rooms; 2 GB is comfortable.
3. In Cloudflare: Zero Trust > Networks > Tunnels > Create a tunnel (type "Cloudflared"). Copy the token. Add a public hostname
   (for example `alchemy.example.com`) whose service is `http://alchemy:8000`.
4. On the server: get the project (`git clone`), `cp .env.example .env`, paste the token after `TUNNEL_TOKEN=`, then
   `docker compose up -d --build`. Open your hostname and `/healthz`.
5. Move the data (stop the Windows server first so nothing changes): on this PC `python ops\backup.py --label migration`, copy that
   backup folder to the server, then there:
   `docker compose cp ./migration/. alchemy:/data`, `docker compose exec -u root alchemy chown -R alchemy /data`,
   `docker compose restart alchemy`. Players log in again afterwards.
6. Then run `powershell -File ops\install_tasks.ps1 -Remove` on this PC if you installed its scheduled tasks.

**What the compose file already does:** passes `--trusted-proxy` for Docker's private address ranges (only the tunnel container can
reach the game, because no port is published) and `--client-ip-header CF-Connecting-IP`, so rate limits and logs see the real visitor,
plus `--max-games 10`. The server learns it is behind HTTPS from `X-Forwarded-Proto`, which Cloudflare sets, and sends HSTS.

**Add a second lock on the admin pages.** In Cloudflare Zero Trust > Access > Applications, add a self-hosted application for your
hostname with the paths `/ops*` and `/admin*` and a policy that allows only your email (one-time PIN). Cloudflare then asks you to
prove who you are before the request reaches the game at all; the game's own administrator check stays as the second lock.
Also useful if your plan allows: a Cloudflare rate-limiting rule on `POST /login` and `POST /register`.

**Turnstile and email.** You will already have the Cloudflare account, so Turnstile (the human check) is a few clicks: create a widget,
add `"--turnstile-site-key", "<key>"` to the `alchemy` command in the compose file, and put the secret key in `.env` as
`TURNSTILE_SECRET_KEY` (and uncomment the `environment` lines). Email confirmation stays off, to keep to the minimal-data rule.

**Day to day on the new host.** Update: `git pull && docker compose up -d --build` (a few seconds of downtime; players log in again).
Roll back: `git checkout <earlier commit> && docker compose up -d --build`. Backups, since `ops/backup.py` is not in the image: from
a host cron job, `docker run --rm -v <project>_alchemy-data:/data -v "$PWD/backups":/backups alpine tar czf /backups/data-$(date +%F).tgz -C /data .`
and delete old ones (`find backups -mtime +14 -delete`). Logs: `docker compose logs alchemy`, and the 14-day activity log is
`/data/live.log` inside the volume. The Windows-only tools in `ops/` (`deploy.py`, `restart-live.ps1`, `install_tasks.ps1`,
`watchdog.py`) are for this PC and are not used there: Docker's `restart: unless-stopped` and the health check replace the watchdog.

### Phones and tablets
Phones play through exactly the same server, pages, API and tools as laptops: one codebase, no separate mobile site.
- **How it plays on a touch screen:** tap an element to add it to the board; drag a tile onto another to combine; swipe the element
  list to scroll it. Hold an element in the list until it lifts (a short buzz on phones that have one), then drag it onto the board or
  straight onto a tile; let go without dragging and its journal page opens. Holding a tile still does the same. The list sits under the board in portrait and moves to the side in
  landscape. Tablets (wider than 700 px) get the desktop layout. The Create Recipes page works the same way.
- **How it was checked:** `touch_check.py` drives a real Chrome as an emulated phone, with real touch input and a touch-screen pointer, and
  checks layout at phone, landscape, tablet and desktop sizes, tapping, swiping, finger-dragging two tiles together, and that every page fits
  the screen. Run it after any change to the pages (`python touch_check.py`; screenshots go to `touch-check-output/`). It is Chromium's engine,
  so it does not cover **iOS Safari**.
- **Before you tell people phones work, spend ten minutes on a real iPhone and a real Android phone** (the live address on the same Wi-Fi, or
  the real site): log in, join a game, tap three elements, drag one tile onto another (they should combine), swipe the element list (it must
  scroll and not add anything), turn the phone sideways, pull down on the page (it must not refresh), tap the search box (the page must
  not zoom in), and open Account, the Journal and the end-game dialog (they must fit the screen).
- Known limits: the hold gestures (0.4 s) are tested in Chromium only, not iOS Safari; the layout
  is tuned for 360 to 430 px wide phones and has not been looked at on foldables or very small screens.

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

### Strangers' safety (built)
Everything here stores nothing new about the people who use it, and each piece is listed on `/privacy`.
- **Name filter.** Usernames, game names and the names of recipes players create are checked against `blocklist.txt` (a starting list) plus
  your own file (`--blocklist-file`, one term per line). It ignores spacing, capitals, accents, look-alike digits and stretched letters
  (`f.u.c.k`, `sh1t`), blocks some short words only as whole words so `Classic` and `Assassin` pass, and has an allow list for innocent words
  that contain a blocked term (`Scunthorpe`). The error never says which word. It is a speed bump, not a judge: expect to add terms.
  See `namefilter.py` for the rules; existing accounts are not re-checked.
- **One running game per account** (`--max-games-per-account`, default 1; `0` removes the limit) and 10 games an hour per account, so one
  person cannot hold every game slot. Daily challenges and races do not count.
- **Reports.** Each other player in a game has a flag button: the reporter picks name, behaviour or other, and may add up to 140
  characters. **Nothing records who sent it**; the reporter's account is used only in memory to ignore the same person reporting the same
  player twice in a day, and is never written down. Reports about one player for one reason are merged into one entry with a count. They show
  on `/ops` (most reported first), disappear after 14 days, when you dismiss them, or when the reported account is deleted, and are kept
  in `reports.json` (not in backups). From `/ops` you can then disable the account, end its game, or delete it.
- **What this does not do:** no automatic bans, no scanning of anything beyond those names, and no chat (the game has none). Anything
  reaching you through reports is a person's word, so look before you act.

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
- **Activity log.** `--log-file data/live.log` (the live restart script passes it) writes sign-ups, logins, failures, rate limiting,
  password changes, deletions and administrator actions, with client addresses and usernames, to a file that starts afresh each
  midnight and keeps `--log-days` (14) days. That is what `/privacy` promises, so keep the two in step. Tracebacks are logged without
  the client's address. There is no access log (every request) on purpose; a reverse proxy has one if you want it, and you should
  keep it short-lived too and make sure it does not record query strings.
- **Daily summary.** `ops/daily_summary.py` (a scheduled task, see OPS.md) writes yesterday's counts (sign-ups, failed logins, rate
  limiting, administrator actions, server errors, and how many different addresses) to `summaries/<date>.txt`. Counts only, no names or
  addresses, so those files can be kept long after the log is gone. A jump in failed logins or any server error is the thing to look for.
- **Is it up?** `GET /healthz` answers `{"ok": true}`; the watchdog uses it and restarts the server after three misses. Point an
  outside uptime service at it too if you want to be told when the whole PC is off, which the watchdog cannot notice.
- **Live view.** `/ops` shows the same counters since the last restart, plus who is running what right now.

### Capacity (measured)
`bench_load.py` runs rooms of five players (cursors 20 times a second while moving, a tile every 2 seconds) and solo
daily-challenge players against a real server process, on the same PC (so the load generator competes with the server for the
CPU: the numbers are on the pessimistic side). One run on the Windows PC, 12 seconds each:

| Scenario | Players | Server CPU (of one core) | Threads | Memory | Cursor delay p50 / p95 | Tile request p50 / p95 | Errors |
|---|---|---|---|---|---|---|---|
| 1 room | 5 | 14% | 20 | 36 MB | 32 / 35 ms | 1.7 / 3.5 ms | 0 |
| 3 rooms (today's default limit) | 15 | 33% | 50 | 37 MB | 33 / 36 ms | 2.5 / 6 ms | 0 |
| 10 rooms | 50 | 133% | 155 | 43 MB | 38 / 70 ms | 15 / 54 ms | 0 |
| 20 rooms | 100 | 187% | 305 | 52 MB | 98 / 209 ms | 83 / 195 ms | 0 |
| 50 solo daily players | 50 | 5% | 155 | 45 MB | n/a | 2.1 / 3.2 ms | 0 |

What it says:
- The default of 3 games (15 players) is far below the limit. **About 10 rooms (50 players) is where this server is still pleasant**;
  responses slow but nobody would notice. At 20 rooms (100 players) it is saturated, although there the load generator itself
  fell behind 44% of the time, so treat that row as "too many", not as a precise figure.
- Solo players (daily challenges) are cheap: 50 of them cost about as much as one room.
- Memory is not the constraint (about 50 MB at 100 players). The server uses roughly **three threads per connected player**,
  and Python runs one thread's code at a time, so CPU is what runs out first.
- **A sensible launch limit for this PC is `--max-games 10`**, which also bounds the worst case to around 50 players in rooms.
  Raise it only after re-running the load test on the machine you will really use. If you outgrow one process, state is in memory
  and JSON files, so scaling out means a redesign, not a flag.
- Bandwidth: each player receives about 5 KB/s of cursor events while others move, so 50 players is roughly 2 megabits a second
  upstream from your connection; check your upload speed before opening the site wide.

Re-run with `python bench_load.py` (takes a few minutes; `--scenarios 10x5,solo50` for particular ones).

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
- Drop `'unsafe-inline'` from `style-src` once the inline `style=` attributes in the pages and scripts are
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
