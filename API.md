# Co-op Alchemy HTTP API

The server (`server.py`) speaks JSON over HTTP/1.1 for everything except the pages themselves. This file is the reference.
**It is kept in step with the code by `test_api_docs.py`**: that test fails if a route or event exists in the code but not
here, or is listed here but the server does not know it. When you add or change an endpoint, event or server option, update
this file in the same commit (and add a line to the [change log](#change-log)).

## Conventions

- **Format.** Request and response bodies are JSON objects. A POST body is required (send `{}` if there is nothing to say)
  and may be at most 4096 bytes. Positions are fractions (0 to 1) of the workspace size.
- **Errors.** Every error is `{"error": "<message for a person>"}` with an HTTP status. Statuses used: `400` bad or missing
  input, `401` not logged in (or a wrong password), `403` not allowed, `404` not found, `409` conflict (name taken, game full,
  limits), `429` too many attempts (see Limits), `500` the server could not save, `503` the human check is unavailable.
- **Logging in.** `POST /register` or `POST /login` returns a session token. Send it as the header `X-Session: <token>` on every
  request marked *session* below. Sessions end after 24 hours without use, when the account's password changes, or when the
  server restarts (a hot reload keeps them).
- **Game requests.** Joining a game returns a *player token*. Every POST under `/g/<id>` except `/join` carries
  `{"player": "<token>"}` in the body. A player token is not a login: it works only for that game.
- **Same origin.** A browser POST whose `Origin` header does not match the `Host` header is refused with `403`.
- **Connections.** HTTP/1.1 keep-alive; an idle connection is closed after 30 seconds. Responses have `Cache-Control: no-store`
  (JSON) or `no-cache` (files) and carry a Content-Security-Policy, `X-Content-Type-Options`, `X-Frame-Options` and
  `Referrer-Policy` (and `Strict-Transport-Security` over HTTPS).
- **Limits.** Per client address: 20 login attempts a minute, 5 sign-ups per 10 minutes, 10 verification-email requests an
  hour. 5 wrong passwords in a row for one account from one address lock that pair for 60 seconds. At most 5 players per
  game. When the server runs behind a reverse proxy, start it with `--trusted-proxy` (and, for Cloudflare, `--client-ip-header CF-Connecting-IP`) so these limits see real addresses.

## Pages and files (GET, return HTML or files)

| Endpoint | Auth | Description |
|---|---|---|
| `GET /` | none | Home page (alias: `/index.html`): games, daily challenge, races, saved games. |
| `GET /login` | none | Log in / create account page. |
| `GET /verify` | none | Opens the emailed confirmation link (the token is in the URL fragment, never sent to the server). |
| `GET /create` | none | Create Recipes page (its data call needs a session). |
| `GET /admin` | none | Recipe review page (its data calls need an admin session). |
| `GET /ops` | none | The administrator's operations page (its data calls need an admin session). |
| `GET /terms` | none | The terms of use page. The administrator's contact line (`--contact`) is filled in where it says so; `/privacy` has it too. |
| `GET /privacy` | none | What the server stores about players and for how long, in plain language. |
| `GET /race/<id>` | none | A race's lobby and results page. |
| `GET /g/<id>` | none | The game page (a "game isn't running" page if the game has ended). |
| `GET /static/<file>` | none | Scripts, styles and element pictures (`/static/elements/<name>.png`). |

## Service

| Endpoint | Auth | Description |
|---|---|---|
| `GET /healthz` | none | `{"ok": true}` when the server is working. Used by the watchdog and deploy checks. |
| `GET /auth-config` | none | `{"invite_required", "email_required", "turnstile_site_key"}`: what signing up needs (`turnstile_site_key` is null when there is no human check). |
| `GET /version` | none | `{"version"}`: changes whenever a page, script or stylesheet changes (pages offer a reload). |
| `GET /images` | admin | `{"pictures": {element id: address}}`: every element's picture address, for the administrators' pages. `403` for anyone else. |
| `GET /pic/<token>.png` | none | An element picture at its secret address (a keyed hash of the file name, new for each run of the server). Every element's own data carries its address as `"pic"`; the game sends an element only once it is found, so an unfound element's picture cannot be found or guessed. `404` for anything else. |
| `GET /lan` | session | `{"base"}`: the address other players use, for invite links. |

## Accounts

| Endpoint | Auth | Description |
|---|---|---|
| `POST /register` | none | `{"username", "password"}` plus, when the server asks for them (see `/auth-config`), `"invite"` (a single-use invite code), `"captcha"` (the Turnstile answer) and `"email"`. Returns `{"session", "username"}`, or `{"verify_email": true, "username"}` when the account must first confirm its email (no session yet). Errors: `400` invalid username, password or email; `400` also when the username contains a blocked word ("that username is not allowed", which never says which word); `403` invalid invite code; `409` name or email taken; `429`; `503` human check unavailable. |
| `POST /login` | none | `{"username", "password"}` → `{"session", "username"}`, plus `"must_change_password": true` when an administrator gave the account a temporary password (the page then asks for a new one). Errors: `401` wrong username or password; `403` the email is not confirmed yet, or the account was disabled by an administrator (said only once the password is right); `429` too many attempts or locked. |
| `POST /logout` | session | `{}` → `{}`. Ends that session. |
| `GET /me` | session | `{"username", "admin", "must_change_password"}`. |
| `POST /verify-email` | none | `{"token"}` → `{"username"}`: confirms the email address the link was sent to. `400` if the token is wrong, used or older than 24 hours. |
| `POST /resend-verification` | none | `{"email"}` → `{"ok": true}`, always the same answer whether or not such an account exists. `404` ("email verification is not switched on") when the server does not use email verification. |
| `POST /account/change-password` | session | `{"current", "new"}` → `{"session"}`. Checks the current password (`403` if it is wrong, never `401`, which only ever means "no valid login"; wrong ones count toward the lockout and the login limit), then sets the new one and ends every other login of the account; the returned session replaces the caller's. Also clears a temporary-password flag. `400` for a weak new password. |
| `POST /account/delete` | session | `{"password"}` → `{"deleted": {"saves_deleted", "saves_kept_anonymised", "drafts", "daily_entries", "invite_codes", "reports"}}`. Deletes the caller's account and what it left behind: drafts, daily scores, saved games nobody else played, and the name in shared saved games (replaced by "deleted player"). Cannot be undone. `403` if the password is wrong. |

## Games

| Endpoint | Auth | Description |
|---|---|---|
| `GET /games` | session | `{"games": [...], "max_games"}`. A game has `"recent": true` if the caller left it within 30 minutes and it is still running; the caller's own daily attempt appears with `"private": true`. |
| `POST /games` | session | `{"name"?}` → `{"id", "name", "url"}`. An empty or missing name becomes a random 6-character one. `400` if the name contains a blocked word. `409` when the maximum number of games is already running, or when this account already has a game running (`--max-games-per-account`, default 1: "you already have a game running ..."). `429` after 10 games an hour from one account. |
| `POST /games/resume` | session | `{"id"}` → `{"id", "name", "url"}`: brings back a saved game (works even at the maximum, but counts toward the per-account limit). |
| `GET /saved` | session | `{"games": [...]}`: saved games this account played in (kept 3 days). |

## Daily challenge

| Endpoint | Auth | Description |
|---|---|---|
| `GET /solo` | session | `{"found", "total", "complete", "may_create", "achievements": [{"id", "icon", "title", "description", "earned_at"}]}`: the caller's solo-mode progress, whether Create Recipes is open to them (administrators and bot accounts always may), and every achievement (`earned_at` is a unix time, or null). |
| `GET /profile` | session | `{"username", "member_since", "solo": {"found", "total", "complete"}, "stats": {"shared_finds", "shared_games", "epic_finds", "daily_done", "best_streak", "races_done", "race_wins", "struck", "emotes"}, "achievements": {"earned", "total", "recent": [{"id", "icon", "title", "description", "earned_at"}]}}`: the caller's own numbers for the home page (`member_since` is a unix time, or null for an account from before it was recorded; `recent` is the latest three). Nothing new is stored for it. |
| `GET /achievements` | session | `{"solo": [...], "multiplayer": [...], "challenges": [...]}`: every achievement for the caller, each `{"id", "icon", "title", "description", "earned_at"}` (`earned_at` is a unix time, or null). The solo ones are the same as in `GET /solo`; the others are earned in shared games, the daily challenge and races. |
| `GET /emotes` | session | `{"emotes": [{"id", "label", "glyph"}]}`: what players can send with `POST /g/<id>/emote` (the list lives in `emotes.py`). |
| `POST /solo/start` | session | `{}` → `{"id", "url"}`: the caller's private solo game (the one running, or a new one that carries on from what they found; saved after every discovery). |
| `GET /daily` | session | Today's three challenges (easy, medium, hard) with the caller's results, streak and the weekly board (see `daily.py`). |
| `GET /daily/history` | session | `{"days": [...]}`: the latest days with targets, the caller's best and the winner. |
| `POST /daily/start` | session | `{"tier", "fresh"?}` → `{"id", "url"}`: the caller's attempt in that tier. The one in progress is free; a new one uses one of 3 attempts a day (always new with `"fresh": true`); `409` when none are left. |
| `POST /daily/reveal` | session | `{"tier"}` → `{"tier", "route"}`: gives up the rest of today's attempts in that tier and shows the reference route. |

## Races

| Endpoint | Auth | Description |
|---|---|---|
| `GET /races` | session | `{"races": [open lobbies], "mine": id or null, "max_races"}`. |
| `POST /races` | session | `{"goal"}` (`"target"`: first to discover a named element; `"count"`: first to 100 elements) → the new room; the caller is its host. |
| `GET /races/<id>` | session | The room: state (lobby, countdown, running, finished), racers and counts, target, winner, and `"game"` (the caller's own world). |
| `POST /races/<id>/join` | session | `{}` → the room. |
| `POST /races/<id>/leave` | session | `{}` → the room. |
| `POST /races/<id>/start` | session | `{}` → the room. Host only, with at least 2 racers. |

## Inside a game

Every request below carries `{"player": "<token>"}` except `/join` (which needs the login session) and `/events` (which needs a ticket).

| Endpoint | Auth | Description |
|---|---|---|
| `POST /g/<id>/join` | session | `{}` → `{"token", "pid", "name"}` (the name is the username). `409` if 5 players are in or this account is already in. |
| `POST /g/<id>/ticket` | player token | `{}` → `{"ticket"}`: a one-time pass, valid 30 seconds and only for this game, to open the event stream, so the token never appears in a URL. |
| `GET /g/<id>/events` | ticket | Server-sent events (see below). Query: `?ticket=<ticket>`. `403` for a used, old or unknown ticket. |
| `POST /g/<id>/cursor` | player token | `{"x", "y"}` (both null hides the cursor) → `{}`. |
| `POST /g/<id>/tile/add` | player token | `{"element", "x", "y"}` → the tile `{"id", "element", "x", "y", "held_by"}`. |
| `POST /g/<id>/tile/grab` | player token | `{"tile"}` → `{}`. `409` if someone else holds it. |
| `POST /g/<id>/tile/move` | player token | `{"tile", "x", "y", "cx", "cy"}` → `{}` (`cx`, `cy` is the pointer position). |
| `POST /g/<id>/tile/drop` | player token | `{"tile", "x", "y"}` → `{}`. |
| `POST /g/<id>/tile/remove` | player token | `{"tile"}` → `{}`. |
| `POST /g/<id>/tiles/clear` | player token | `{}` → `{"cleared"}`. |
| `POST /g/<id>/combine` | player token | `{"b": target tile, "a": held tile or "element": element id, "x", "y"}` → `{"results", "new", "tiles", "removed"}`. |
| `POST /g/<id>/hint` | player token | `{"kind": "can_make" \| "try" \| "find", "target"}` → a private hint. In a solo game there is one hint a day (UTC): the answer also has `"hints_left"`, and a second hint gets `429`. A hint that found nothing does not use it up. |
| `POST /g/<id>/emote` | player token | `{"emote": id}` → `{}`: shows an emote from `GET /emotes` next to your name for everyone in the game. `400` for an unknown id, `429` faster than one a second, `403` in solo, daily and race games. |
| `POST /g/<id>/journal` | player token | `{}` → `{"elements": [...], "progress": {"found", "total", "locked"}}`: every fully discovered element with the recipes that make it and use it, and how many elements can be found in all. `locked` is only a count (the elements still to find are never named); Time counts although no recipe makes it. |
| `POST /g/<id>/genie` | player token | `{"element"}` → `{"results", "new", "tiles", "genie"}`. |
| `POST /g/<id>/report` | player token | `{"target": pid, "reason": "name", "behaviour" or "other", "note"?}` → `{}`: tells the administrators about another player in the game (`target` is a `pid` from `players`). The note is at most 140 characters. **Nothing records who sent it.** The same reporter reporting the same player again within a day is ignored without an error. `404` if the player is not in the game, `400` for yourself or a bad reason, `429` after 5 reports an hour from one account. |
| `POST /g/<id>/end` | player token | `{"save": true \| false}` → `{"saved"}`: ends the game for everyone. Saving: `403` unless the caller created the game. |

### Events

`GET /g/<id>/events?ticket=...` is a `text/event-stream` (`event: <name>` and `data: <json>` lines). The first event is always
`snapshot`; `: keepalive` comments follow every 3 seconds of silence; after `ended` the server closes the stream. Events caused
by a player are not sent back to that player (except the snapshot). The server waits 30 ms after a `cursor` event and sends
what is queued together, keeping only each player's newest `cursor` event (hiding is a `cursor` event with null `x` and `y`).
A ticket works once, so after a dropped connection a page asks for a fresh ticket and reconnects (it gets a new `snapshot`).

| Event | Payload |
|---|---|
| `snapshot` | `{"challenge", "race", "you": pid, "game": {"id", "name", "creator", "solo", "hints_left"}, "genie", "total", "elements": [...], "players": [...], "tiles": [...]}` (`hints_left` is a number in a solo game and null elsewhere; each element has `"rarity"`). |
| `players` | `{"players": [{"pid", "name", "color"}]}`: who is connected now. |
| `discovered` | `{"id", "label", "emoji", "credit"?, "by", "rarity", "pid", "found", "total"}` (plus `"unlocked"` for Time, with `pid` null). `rarity` is `common`, `uncommon`, `rare` or `epic`: how far the element is from the starting elements compared with all the others. |
| `visitor` | `{"name": "Mr Alchemy", "elements": [element with "by" and "rarity"], "found", "total", "before"}`: Mr Alchemy has visited and the elements are already unlocked for the room. Pages play a cutscene and reveal them one at a time; a player who joins during it simply gets them in the snapshot. Public games only, once per game. |
| `emote` | `{"pid", "name", "emote"}`: someone sent an emote (an `id` from `GET /emotes`); everyone in the game gets it, the sender too. |
| `achievement` | `{"id", "icon", "title", "description"}`: the player earned an achievement. Sent only to that player's own connections, in solo games, shared games, daily challenges and races. |
| `genie` | `{"status", "used_by"}` |
| `cursor` | `{"pid", "x", "y"}` (null `x`, `y`: hidden). |
| `tile_added` | `{"tile": {"id", "element", "x", "y", "held_by"}}` |
| `tile_grabbed` | `{"id", "pid"}` |
| `tile_moved` | `{"id", "x", "y", "pid", "cx", "cy"}` |
| `tile_dropped` | `{"id", "x", "y", "pid"}` |
| `tile_removed` | `{"id"}` |
| `tiles_cleared` | `{"pid", "name"}`: who sent the cat. |
| `race_won` | `{"name"}` |
| `challenge_done` | `{"score", "reference", "target"}` |
| `ended` | `{"by", "saved", "awards": [{"id", "icon", "title", "player", "detail"}]}`: minor awards for a shared game (empty in solo, daily and race games). |

## Creating recipes and reviewing them

| Endpoint | Auth | Description |
|---|---|---|
| `GET /create/data` | session | `{"username", "elements": [...], "drafts": [...]}`: what the Create Recipes page needs. For an account that has not found every element in solo mode it is `{"username", "locked": true, "solo": {"found", "total", "complete"}}` and nothing else: no element, no recipe. All the `/create/...` actions below answer `403` to such an account. |
| `POST /create/combine` | session | `{"a", "b"}` → `{"results": [...]}`: what two elements make, counting this account's drafts. |
| `POST /create/recipe` | session | `{"a", "b", "name"}` → the new draft (`400` if the name contains a blocked word). |
| `POST /create/delete` | session | `{"id"}` → the removed draft. |
| `POST /create/promote` | session | `{"id", "note"?}` → the proposal for an admin to review. |
| `GET /admin/queue` | session + admin | `{"proposals": [...]}` waiting for review. |
| `GET /admin/approved` | session + admin | Accepted proposals, with the snippet to paste into `recipes.json`. |
| `POST /admin/handle` | session + admin | `{"id", "action", "reply"?}`: accept or reject a proposal. |
| `POST /admin/reopen` | session + admin | `{"id"}`: reopen a handled proposal. |

## Operations (administrators only)

The page at `/ops` uses these. Every one needs a session whose account is an administrator (`403` otherwise). Administrators
themselves cannot be disabled, deleted or reset here: remove the role with `accounts_admin.py` first. The responses hold counts
and names, never password hashes, email addresses, login tokens or client addresses.

| Endpoint | Auth | Description |
|---|---|---|
| `GET /ops/status` | session + admin | `{"uptime_seconds", "accounts": {"total", "pending", "disabled", "admins"}, "sessions", "games": {"running", "max", "private", "players"}, "since_start": {"logins", "login_failures", "registrations", "throttled", "accounts_deleted", "server_errors", "reports"}, "load": {"samples", "hours", "now": {"players", "rooms", "cpu"}, "peak": {"players", "rooms", "cpu", "..._at"}, "watch": [{"id", "label", "now", "peak", "limit", "hit"}]}, "reports": {"open"}, "signup": {"invite_required", "email_required", "human_check", "behind_proxy"}}`. `load` is sampled every 30 seconds and covers up to 24 hours (`cpu` is the percent of one core); `since_start` also counts `lobby_full` (a game could not be started because the lobby was full). Counters and samples are in memory and reset when the server restarts. |
| `GET /ops/accounts` | session + admin | `{"accounts": [{"name", "admin", "bot", "pending", "disabled", "created"}]}`, by name. |
| `GET /ops/games` | session + admin | `{"games": [{"id", "name", "players", "max_players", "found", "total", "genie", "kind", "private"}], "max_games"}`; `kind` is `game`, `daily` or `race`. |
| `GET /ops/reports` | session + admin | `{"reports": [{"id", "user", "reason", "note", "game", "count", "first", "at"}]}`, most reported first. Reports are merged per player and reason; there is no reporter in them. They are deleted after 14 days. |
| `POST /ops/reports/dismiss` | session + admin | `{"id"}` → `{"dismissed": true or false}`. |
| `GET /ops/invites` | session + admin | `{"enabled", "codes": [{"id", "note", "created", "used_by", "used_at"}]}`. Codes are stored hashed, so they cannot be shown again. |
| `POST /ops/invites/create` | session + admin | `{"count"?, "note"?}` → `{"codes": [...]}`, shown only now. `409` if invite codes are not switched on (`--require-invite`). |
| `POST /ops/invites/revoke` | session + admin | `{"id"}` → `{"revoked": n}`: deletes an unused code. |
| `POST /ops/bots/add` | session + admin | `{"id", "level"?}` → `{"name"}`: puts a companion (0 keeps a presence, 1 plays like a beginner; random if left out) in a running public game. `409` if no seat can be spared, `404` if the game is not a running public game. |
| `POST /ops/bots/remove` | session + admin | `{"id"?, "name"?}` → `{"removed"}`: takes companions out at once: those of one game, one by name, or all. |
| `POST /ops/games/visit` | session + admin | `{"id"}` → `{"gave": [labels]}`: sends Mr Alchemy to a running public game now, whatever the schedule and the number of players. `404` if it is not running, `409` for a private game or one with nothing left to find. |
| `POST /ops/games/end` | session + admin | `{"id"}` → `{}`: ends a running game for everyone, unsaved. `404` if it is not running. |
| `POST /ops/accounts/verify` | session + admin | `{"username"}` → `{}`: confirms an email address by hand. |
| `POST /ops/accounts/disable` | session + admin | `{"username"}` → `{}`: the account's logins stop at once and it cannot log in; nothing is deleted. A game it is in keeps going until it is ended. |
| `POST /ops/accounts/enable` | session + admin | `{"username"}` → `{}`. |
| `POST /ops/accounts/reset-password` | session + admin | `{"username"}` → `{"temporary_password"}`, shown only now. The old password and sessions stop; at the next login the owner must choose their own. |
| `POST /ops/accounts/delete` | session + admin | `{"username"}` → `{"deleted": {...}}`, the same as `POST /account/delete`. |

## Server options

Command-line options (`python server.py --help`) are described in the README's options table and in `DEPLOYMENT.md`. A test
checks that every option the server accepts appears in the README.

## Change log

Newest first. Add a line whenever an endpoint, event, status code or option is added, changed or removed.

- **2026-11 (profile)**: `GET /profile`.
- **2026-11 (achievements)**: `GET /achievements`; the `achievement` event now also reaches players in shared games, daily challenges and races (to the one who earned it); option `--profile-file`; `profile` in the account-deletion summary.
- **2026-11 (load)**: `load` in the `GET /ops/status` response, and the `lobby_full` counter in `since_start`.
- **2026-11 (companions)**: `POST /ops/bots/add` and `POST /ops/bots/remove`; `companions` and `companions_on` in the `GET /ops/games` response; `POST /register` answers `409` for the cast's names too; options `--bots`, `--bots-max`. Companions appear in `players` and the `snapshot` exactly like people (the same `pid`, `name`, `color`).
- **2026-11 (Mr Alchemy)**: the `visitor` event and `POST /ops/games/visit`; `POST /register` answers `409` "that username is taken" for any spelling of the name Mr Alchemy; options `--mr-alchemy`, `--mr-alchemy-chance`, `--mr-alchemy-min`, `--mr-alchemy-max`.
- **2026-11 (pictures)**: element pictures are no longer served by file name (`/static/elements/...` is `404`) and `GET /images` is no longer public or a list of names: it is `{"pictures": {id: address}}` for administrators only. Pictures are at `GET /pic/<token>.png`; each element's data (`snapshot`, `discovered`, the journal, hints, `GET /create/data`) carries `"pic"` when it has one.
- **2026-11 (moments, awards, emotes, achievements)**: `rarity` on discovered elements and in the `discovered` event; `awards` in the `ended` event; `POST /g/<id>/emote`, `GET /emotes` and the `emote` event; the `achievement` event and `achievements` in `GET /solo`; one hint a day in solo games (`hints_left` in the hint answer and in the snapshot's `game`, `429` when used up).
- **2026-11 (solo mode)**: `GET /solo` and `POST /solo/start`; `GET /create/data` answers `{"locked": true, ...}` and the `/create/...` actions `403` until the account has found every element in solo mode (administrators and bot accounts are exempt); `"solo": true` in the game object of the `snapshot` event; `POST /g/<id>/journal` also returns `progress`; unknown and undiscovered element ids now get the same `400` answer; `solo` in the account-deletion summary; option `--solo-file`.
- **2026-11 (strangers' safety)**: `POST /g/<id>/report`; `GET /ops/reports` and `POST /ops/reports/dismiss`; `reports` in the `/ops/status` response and in the account-deletion summary; blocked-word checks (`400`) on usernames, game names and recipe names; one running game per account and 10 games an hour (`409`, `429`); options `--blocklist-file`, `--reports-file`, `--max-games-per-account`.
- **2026-11 (phones)**: phones are no longer redirected to `/laptop` or refused with `403`; they get the same pages and API as every other browser. `GET /laptop` is gone.
- **2026-11 (options)**: `--client-ip-header`, `--max-games`, `--log-file`, `--log-days` (README options table).
- **2026-11 (accounts and operations)**: `POST /account/change-password` and `POST /account/delete`; `GET /ops` and the `/ops/...`
  endpoints for administrators; `GET /privacy`; `must_change_password` on `GET /me` and `POST /login`; `403` from `/login` for a
  disabled account; the old `accounts_admin.py delete` now keeps shared saved games for the other players (name removed) instead of deleting them.
- **2026-11**: `GET /healthz`; `POST /g/<id>/ticket` and ticket-based `events` (the old `?player=<token>` form is gone);
  `GET /auth-config`, `GET /verify`, `POST /verify-email`, `POST /resend-verification`, optional `invite`, `captcha` and `email`
  fields on `POST /register`; `403` from `/login` for an unconfirmed email; `POST /games` names a game randomly when no name is
  given; cursor events are batched and coalesced; the server speaks HTTP/1.1 keep-alive.
