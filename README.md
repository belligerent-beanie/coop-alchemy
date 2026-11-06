# Co-op Alchemy

A co-op, multiplayer element-combination game for a local network. Players share
one workspace: drag elements onto each other to combine them, and every discovery
goes into a book everyone shares. It is written with the Python standard library
only (no packages to install), and the browser side is plain HTML, CSS and
JavaScript.

## Requirements

- Python 3.10 or newer (developed and tested on 3.13).
- A laptop or desktop browser for every player. **Phones are not supported**: they
  are sent to a "please join from a laptop" page and can't use anything else.
- A recipe file (see [Recipes](#recipes)).

## Quick start

```
python3 server.py --recipes recipes.json
```

The server prints two addresses:

```
This machine:  http://localhost:8000
Other players: http://192.168.1.20:8000   (same network)
```

Open the first one yourself and give the second one to the other players (they
must be on the same network). Everyone creates an account, logs in, and then
starts or joins a game.

If other laptops can't reach the server, your computer's firewall is the usual
cause: allow incoming connections for Python on the port you chose.

## How a session works

1. **Account.** On first visit you are sent to the login page. Choose *Create
   account*: a unique username and a password. The username is also your name in
   games, and nobody else can use it (capitals are ignored: `Ann` and `ann` are
   the same name).
2. **Home page.** Shows the games that are running, lets you start a new one, and
   lists your saved games. If you left a game that is still running (within the last
   30 minutes), it appears under **Recently joined** with a *Rejoin* button.
3. **Games.** Each game has its own address, `/g/<game-name>`. Press **🔗 Invite** in a
   game's header to copy that link (using the address other computers can reach, even
   if you opened the game through `localhost`) and send it to your friends.
   At most **3 games** run at once and at most **5 players** are in a game at a
   time. A game closes by itself **10 minutes** after its last player leaves
   (`--idle-minutes` changes this) and is *not* saved when that happens.
4. **Playing.** Drag an element from the list onto the workspace, then drop one
   tile onto another to combine them. Everyone sees the same tiles and each
   other's cursors. Double-click a tile to make a copy of it; the copy appears half a tile right and down, so it overlaps the original by half. Elements are listed alphabetically. Hover an element to see who discovered it. The cat 🐈 clears
   the whole workspace for everyone (after a confirmation); there is no undo, and the other
   players get a message saying who sent the cat. The search box above the element list
   filters it by name.
5. **Hints** (sidebar): something you can make now, a combination to try, or one
   recipe for an element you type in. **📖 Journal** (header) lists every element you have
   discovered. Pick one to see who found it, what it was made from, and what it makes
   when combined with other elements. Only recipes whose ingredients and result have
   all been discovered are listed, so the journal never spoils anything; it updates live while it is open.
6. **The Djinn** 🧞: the Djinn panel is hidden until someone discovers the Djinn element;
   then it slides into the sidebar for everyone and the game gets
   3 wishes plus 1 more for every 50 elements found after that. A wish combines
   one element with everything in the book, and all the results appear in the
   workspace. (Needs an element called "Djinn" in the recipes; without one the
   feature is switched off.)

7. **Time**: when a room has found 100 elements (starters included) the element
   called "Time" is added to everyone's book for free, if nobody has made it yet.
   It shows "Unlocked for free" in its tooltip. Without a Time element in the
   recipes nothing happens.

### Create Recipes and promotions

**Create recipes** on the home page opens a solo playground where every element is
unlocked. Drag two elements together:

- If the pair already has a recipe, you get its result as usual.
- If it has none, a box asks what they make. A new name creates a new element; the name
  of an existing element adds another way to make it. Either way it becomes a **draft**:
  it is saved with your account, shows in *My recipes* and (for new elements) in your
  element list (dashed border), and works only on this page. Real games never see drafts.
- Click an element to drop a tile, double-click a tile to copy it (the copy overlaps the original by half), and drag a tile onto the
  element list to remove it.

In *My recipes*, **Promote** sends your recipe to the admins with your name as credit and an
optional note. If it uses other new elements you made, the recipes that make them
are sent too, each as its own proposal. A draft shows *Waiting for an admin*, then *Approved* or
*Not accepted* with the admin's reply (a rejected one can be promoted again). **Delete**
removes a draft that isn't waiting; a draft other drafts are built on must stay until
those are deleted.

A pair that already has a recipe in the game can't be given a new result: the box refuses it,
and so does the server. Several players may each propose a different result for the same
*new* pair; the review page marks every such proposal ("Bob proposed a different
result for this pair: Breeze") so the admin can approve the best one and reject the rest. Nothing is
rejected automatically, except a proposal whose pair the game has since gained a recipe for: it is
closed as rejected with the reply "The game already has a recipe for this pair."

You can keep 200 drafts and 40 waiting proposals per account (bot accounts, below, have no limit). Once the recipe file gains a
recipe for the same pair, the draft disappears by itself.

**Bots** are accounts marked with `python3 accounts_admin.py make-bot <username>` (`remove-bot`
undoes it; `list` shows them). They are meant for automated proposers such as `python3 -m aigen`: the
200-draft and 40-proposal limits do not apply to them, and nothing else changes (a bot is not an admin).
The account must exist first, so sign up for it on the site, then mark it.

**Admins** are accounts marked with `python3 accounts_admin.py make-admin <username>`
(`remove-admin` undoes it; `list` shows them). Admins see **Review proposals** on the home
page, with the number waiting. The review page works like a swipe deck: one proposal at a
time, shown as **A + B = C** with pictures, the author's credit and note. On a PC press
**→ to accept** or **← to reject** (or use the buttons), and **S** (or **↓**) to skip a proposal and decide later (it goes to the back of the deck and stays waiting), and **Z to undo** your last answer.
The keys are ignored while you type in the optional reply box (shown to the player on their
draft). Under the card, **Depends on C** lists the other elements built on C: for an element
the game already has, everything the game makes with it; and always the elements other
players have proposed on top of it. Tags warn when a proposal needs an element that isn't
proposed yet, or competes with another proposal for the same pair. Any admin can answer; the
first answer wins. **Approved so far** collects accepted proposals the recipe file doesn't
have yet, as a snippet with `"credit"` on every new element and recipe (the recipe loader
ignores that extra key). Accepting does **not** edit `recipes.json`: paste the snippet
in yourself, check it with `validate_recipes.py`, then run `python3 reload.py`.

### AI suggestions (`aigen/`)

An optional tool that proposes new elements and recipes with a local language model
([LM Studio](https://lmstudio.ai)) and sends them to the admin review page like any player's
proposal. Nothing is added to `recipes.json` automatically: an admin still accepts or rejects
every recipe. It needs no packages beyond Python itself.

How it works: the model invents names, and a small scorer trained on your own `recipes.json`
throws out suggestions that don't look like your recipes. Suggestions are credited to the account
`ai-suggester`, with the score in the note.

**One-time setup**

1. In LM Studio, keep a chat model (gpt-oss-20b works) with a **context length of at least 8192**,
   download the embedding model `nomic-embed-text-v1.5`, and start the local server (Developer tab).
2. Sign up for an account called `ai-suggester` on the site, then mark it as a bot so the
   200-draft and 40-proposal limits don't apply to it:
   `python3 accounts_admin.py make-bot ai-suggester`
3. The server must be running this version of the code (a restart or `reload.py` is needed only once, if it
   was started before bots existed). Marking the account takes effect straight away.
4. Check the connection and train the scorer:

```
python3 -m aigen check
python3 -m aigen train
```

**Using it**

```
python3 -m aigen generate --dry-run      # random pairs -> new elements, then recipes built on them
python3 -m aigen target jacket --dry-run # a chain of recipes from existing elements to a noun you choose
python3 -m aigen generate                # without --dry-run: writes the drafts and sends them to the admins
python3 -m aigen target --targets-file wishlist.txt
```

In target mode the tool works backwards from your noun, using existing elements and inventing up to
two helper elements if needed (for example `sewing machine + fabric = clothes`, then
`clothes + armor = jacket`). It also looks for links by appearance, material, use and origin, not just
meaning: a croissant is crescent-shaped, so `moon + bread` is a candidate. Targets that already exist are skipped. `time` counts as available even
though it comes from the 100-element unlock rather than a recipe.

No room for a 20B model on your computer? Add `--groq` to `check`, `generate` and `target` to use Groq's
free API for the naming step instead (set `GROQ_API_KEY` in the same Terminal window first; embeddings and
training stay local).

`aigen/README.md` lists every option, and each run's full log (every suggestion and why it was kept
or dropped) is saved in `aigen/runs/`.

### Daily challenge

Every day has three **target elements**, one per difficulty (🟢 Easy, 🟡 Medium, 🔴 Hard), the same for
everyone. On the home page press **Play** on a tier and you get a private workspace with only the
starting elements. Discover the target while unlocking as **few elements** as you can. Your score is the
number of elements you have discovered when you find it: the target counts, the starting elements do not,
and so do detours and elements a recipe makes as a side effect. Lower is better.

- It is solo: nobody else can join your attempt, and it does not use one of the game slots.
- There are no hints, no Djinn and no free Time element. The 📖 Journal still works.
- You get **3 attempts a day in each tier**. Starting an attempt uses one up, whether or not you finish it,
  and so does **Start over** or **Try again**. **Continue** (picking up an attempt in progress) is free.
  The count is kept in `daily.json`, so restarting the server does not reset it. Each tier has its own
  board of every player's best finished attempt (the earlier finisher wins a tie). An attempt is not
  saved: leave it and it is gone, though **Continue** brings back one in progress (it is closed
  10 minutes after you leave it, like any game).
- Once you find the target the attempt is over, and a message shows your score.
- The **reference** is how many elements a good route unlocks. It comes from a greedy search with some
  randomised passes, so it is a number to beat, not a proven minimum.
- **Reference route:** the steps of that route are shown once you have used all 3 attempts in a tier, or
  when you press **Give up and show the route** (which uses up your remaining attempts for that tier).
  Past days in the history always show theirs.
- **Streak:** the number of days in a row (ending today or yesterday) on which you finished at least one
  tier, and your best ever.
- **History:** your results and each day's winner for the last 30 days, under "History" on the home page.
- **Share:** copies a spoiler-free summary (scores against the reference, per tier, plus your streak).
- **Weekly board:** each finished tier-day earns `round(100 x reference / score)` points (100 for matching
  the reference, more for beating it). The board adds them up per ISO week (Monday to Sunday).
- "Today" is the date on the computer running the server. The three targets are picked from the date
  and stored in `daily.json` with their routes, so they do not change if you edit the recipes later.
  Easy, medium and hard use different depth bands and reference sizes (the constants at the top of
  `daily.py`; with a small recipe file any element can be picked).

### Race mode

On the home page, **Race** lets you create a room with one of two goals, and others join it from the same
list (up to 6 racers, and the host starts it once at least 2 are in):

- **First to discover a target element** (picked at random from the medium difficulty).
- **First to reach 100 elements** (the starting elements count).

Every racer gets their **own private world** with only the starting elements, so nobody can help or hold
anyone back, and "100 elements" means something (in a shared game the book fills up for everyone at once).
There are no hints, no Djinn and no free Time element, and the 📖 Journal still works. After a 3-second
countdown everyone is sent into their world. The bar at the top shows everyone's element count, and the
first to reach the goal wins: the others are stopped and everyone sees the results.

- A racer can leave at any time; if everyone else leaves, the one remaining wins.
- Up to 4 races can be open or running at once. A race does not use one of the game slots.
- A lobby whose host stops checking in closes after 30 seconds; results are kept for 10 minutes and a race
  is closed after an hour.
- Races are **not** carried across a hot reload (`reload.py`): they end with the restart.

### Ending and saving a game

Any player can press **End game**. The creator of the game (the account that
started it) can choose **Save and end**; everyone else can only end it without
saving. Ending closes the game for everybody.

A save keeps the discoveries (and who found each one), the Djinn's wishes used and
the usernames of the players. The workspace tiles are not saved. Saves are kept
for **3 days** and then deleted.

To continue, log in as any account that played in that game and use *Saved games*
on the home page. Resuming works even when 3 games are already running. A resumed
game keeps its original creator, so only they can save it again.

## Command-line options

```
python3 server.py [--host 0.0.0.0] [--port 8000]
                  [--recipes recipes.json]
                  [--saves-dir saves] [--accounts-file accounts.json]
                  [--tls-cert cert.pem --tls-key key.pem]
                  [--idle-minutes 10]
                  [--drafts-file drafts.json] [--daily-file daily.json]
                  [--snapshot-file reload_snapshot.json] [--pid-file alchemy.pid]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--host` | `0.0.0.0` | Address to listen on (`127.0.0.1` for this machine only). |
| `--port` | `8000` | Port to listen on. |
| `--recipes` | `recipes.json` next to `server.py` | The recipe file. |
| `--saves-dir` | `saves` next to `server.py` | Where saved games are kept. |
| `--accounts-file` | `accounts.json` next to `server.py` | Usernames and password hashes. |
| `--require-invite` | off | Signup needs a single-use invite code (`accounts_admin.py invite create`; see DEPLOYMENT.md). |
| `--invites-file` | `invites.json` next to `server.py` | Invite codes, stored as hashes. |
| `--trusted-proxy` | none | A reverse proxy in front of the server (repeatable); its `X-Forwarded-*` headers are believed. See DEPLOYMENT.md. |
| `--turnstile-site-key` | off | Cloudflare Turnstile human check on signup (secret key in the `TURNSTILE_SECRET_KEY` environment variable). |
| `--require-email-verification`, `--public-url` | off | Signup asks for an email and the account works only after its link is opened; links use the public URL. SMTP settings come from `SMTP_*` environment variables. See DEPLOYMENT.md. |
| `--tls-cert`, `--tls-key` | off | Serve over HTTPS (see below). |
| `--idle-minutes` | `10` | How long an empty game stays open. |
| `--drafts-file` | `drafts.json` next to `server.py` | Players' draft recipes and promotion proposals. |
| `--daily-file` | `daily.json` next to `server.py` | The daily challenge's targets, routes, attempts used and scores (the last 400 days). |
| `--snapshot-file` | `reload_snapshot.json` next to `server.py` | Where running games are parked during a reload. |
| `--pid-file` | `alchemy.pid` next to `server.py` | Lets `reload.py` find the running server. |

## Patching without stopping games

Change the code, then run this on the machine running the server:

```
python3 reload.py
```

- **Page files** (`static/`: JavaScript, CSS, HTML, pictures) need no reload at all; the
  server reads them on every request. Players with a game open see a **"A new version
  of the game is ready"** notice and choose when to reload the page.
- **Python files** (and the recipe file) need `reload.py`. It first checks that the new
  code imports and the recipes load (so a typo can't take the server down; if the check
  fails nothing happens and the old code keeps running). Then the server saves every
  running game (workspace tiles, discoveries, players, Djinn) and every login to
  `reload_snapshot.json`, restarts itself in place (same process, same port) and loads
  it again. Players see a pause of a second or two while their browser reconnects;
  nobody has to log in or rejoin, and no tile is lost. A tile someone was holding
  when it happened is let go.
- A player who does not reconnect within 30 seconds loses their seat, as usual.
- If the new copy crashes while starting, fix the problem and start the server again
  within 10 minutes: it picks the snapshot up. An unreadable snapshot is renamed
  `reload_snapshot.json.bad` and the server starts empty.
- Not available on Windows (it uses a Unix signal): stop and start the server there.
- `reload_snapshot.json*` holds login tokens: keep it private like `accounts.json`.
- If a patch changes what a game stores, make the loading code tolerate snapshots
  written by the old version (`GameState.live_restore` skips anything that doesn't fit).

## Accounts and passwords

- Usernames are 3 to 24 characters: letters, digits, spaces and `_ - .`.
  Passwords are 8 to 128 characters.
- Passwords are **never stored**, only a salted PBKDF2-SHA256 hash (600,000
  rounds). This is hashing, not encryption, on purpose: a hash can't be turned
  back into the password, even by someone who has the server's files.
- After 5 wrong passwords in a row, that account is locked for a minute. There is
  no limit per IP address.
- A login lasts 24 hours since you last used it. Sessions are kept in memory, so
  **restarting the server logs everybody out** (and ends the games that were
  running).
- `accounts.json` is created when the first person registers. Keep it private; it
  holds the hashes. At most 500 accounts are allowed.

### Managing accounts

Run these on the machine running the server (the server can stay running):

```
python3 accounts_admin.py list
python3 accounts_admin.py reset <username>     # asks for a new password
python3 accounts_admin.py delete <username>    # also deletes their saved games
```

Resetting a password or deleting an account logs that account out immediately.
Deleting also removes the account's draft recipes. It shows the saved games that will be removed and asks you to type `yes`.
**A saved game that other players also played in is deleted too.** If you use a
different accounts file or saves folder, add `--file path/to/accounts.json` and
`--saves-dir path/to/saves`.

### HTTPS

Without HTTPS, passwords cross the network unencrypted, so people on your network
could read them. For a home or private LAN that may be acceptable (and the login
page tells people not to reuse an important password); for anything else use HTTPS:

```
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem \
        -days 365 -subj "/CN=localhost" -addext "subjectAltName=IP:192.168.1.20"
python3 server.py --recipes recipes.json --tls-cert cert.pem --tls-key key.pem
```

Put the server's own LAN address in `subjectAltName`. A certificate you made
yourself isn't trusted by browsers, so each player sees a warning once and has to
choose to continue. Everything then works normally, now encrypted. Don't commit
`key.pem`.

## Recipes

The game reads one JSON file:

```json
{
  "starters": ["water", "fire"],
  "elements": {
    "water": {"label": "Water", "emoji": "💧"},
    "fire":  {"label": "Fire",  "emoji": "🔥"},
    "steam": {"label": "Steam"}
  },
  "recipes": [
    {"a": "water", "b": "fire", "result": "steam"},
    {"a": "x", "b": "y", "result": ["one", "two"]}
  ]
}
```

- `starters` are the elements everyone starts with.
- Element ids are free text (spaces and capitals are fine) but must be non-empty,
  trimmed, at most 64 characters, without control characters, and no two ids may
  differ only by capitals. `emoji` is optional; an element without one shows its
  first letter.
- A recipe's `result` can be one id or a list (the combination then makes several
  elements). `a + b` and `b + a` are the same recipe. Repeats are merged rather
  than rejected.
- `credit` (optional, on an element): who proposed it. The review page's snippet adds it, and the game marks such elements with a ★ in the list and "Player recipe, proposed by …" in the tooltip, so they stay distinguishable from the original recipes after you paste them in. Recipes may carry a `credit` too; it is kept for your records.
- Pictures: see [Element pictures](#element-pictures). An element without a picture
  shows its emoji (or first letter).
- The Djinn feature looks for an element whose id or label is `Djinn` (capitals
  ignored).

The recipe file is only ever read. These tools check and draw it:

| Tool | What it does |
| --- | --- |
| `python3 validate_recipes.py recipes.json` | Quick pass/fail check: bad ids, unknown names, orphans, unreachable elements. |
| `python3 recipe_report.py recipes.json -o report.md` | Lists each problem with a suggested change. Never edits the file. |
| `python3 recipe_flowchart.py recipes.json --html recipes.html --tiers` | Draws every recipe as a Mermaid flowchart (see below). |

### Element pictures

Put a PNG for each element in `static/elements/`, named after the element: its id (or,
if there is no such file, its label) in lower case, with every run of other characters
changed to `-` and apostrophes dropped. `Alarm Clock` uses `alarm-clock.png`,
`Philosopher's Stone` uses `philosophers-stone.png`. The picture replaces the emoji in
the 60×60 slot in the list and shows small on tiles in the workspace. Pictures are
128×128 PNGs with a transparent background today, and any square size works.

The server lists the folder (`GET /images`), so adding a file only needs a page reload,
not a restart. To see which elements have no picture and which files match nothing:

```
python3 image_check.py recipes.json
```

### Flowchart

`recipe_flowchart.py` writes `recipes.mmd` (Mermaid text) and, with the options
below, more:

- `--html recipes.html` writes a page that draws it in your browser with zoom and
  an SVG download button. It raises Mermaid's size limits, but a very large recipe
  set can still be slow to draw.
- `--svg recipes.svg` writes an SVG with Graphviz instead, with no browser.
  It needs the `dot` program installed (graphviz.org, or `brew install graphviz`).
  `--dot file.dot` also keeps the Graphviz source.
- `--tiers` groups elements by how many steps they take to reach, `--direction TD`
  draws top to bottom, and `--focus <element>` draws only what is needed to make
  that one element (much smaller).

## Where things are stored

| What | Where | Notes |
| --- | --- | --- |
| Accounts | `accounts.json` | Usernames and password hashes; readable only by you on macOS and Linux. |
| Saved games | `saves/<game>.json` | One file each; deleted after 3 days. |
| Draft recipes, promotion proposals | `drafts.json` | Deleted with the account's other data by `accounts_admin.py delete` (proposals stay on record). |
| Daily challenge targets, attempts and scores | `daily.json` | The last 400 days. A damaged file is reported, never overwritten. |
| Running games | memory only | Lost if the server stops; `reload.py` carries them across a restart via `reload_snapshot.json`. |

If you keep the project in git, add `accounts.json`, `saves/`, `*.pem`,
`drafts.json`, `daily.json`, `reload_snapshot.json*`, `alchemy.pid`, `aigen/cache/`, `aigen/model.json` and
`aigen/runs/` to `.gitignore`.

## Tests

```
python3 -m unittest discover
```

The tests use `test_recipes.json` (a small made-up set), so they give the same result whatever
recipes the game uses. The exception is `test_aigen.py`, whose end-to-end tests read (never change)
your real `recipes.json` and talk to a built-in fake LM Studio, so they need no model; they are
skipped if `recipes.json` is missing.

## Project layout

| File | Purpose |
| --- | --- |
| `server.py` | HTTP server, routes, server-sent events, phone block, HTTPS. |
| `state.py` | One game: players, shared workspace, discoveries, hints, Djinn. |
| `games.py` | Running games: the 3-game limit, idle closing, ending, resuming. |
| `saves.py` | Saved games on disk, 3-day expiry. |
| `drafts.py` | Draft recipes and promotion proposals (per account, with admin handling). |
| `hotreload.py`, `reload.py` | Snapshot and restore for patching without stopping games; the command that triggers it. |
| `accounts.py`, `accounts_admin.py` | Accounts, passwords, sessions, and the admin tool (admins and bots). |
| `throttle.py`, `invites.py`, `captcha.py`, `mailer.py` | Rate limits and one-time event tickets, single-use invite codes, the Turnstile check, and verification email (see DEPLOYMENT.md). |
| `bench_cursor.py` | Measures what shared cursors cost the server (`python3 bench_cursor.py`): connections, latency, delivery, CPU. |
| `recipes.py`, `hints.py` | Recipe file loading and checking; hint logic. |
| `race.py` | Race rooms: lobby, countdown, standings and the winner. |
| `daily.py` | The daily challenge: picking the targets, reference routes, score boards, streaks and the weekly board. |
| `validate_recipes.py`, `recipe_report.py`, `recipe_flowchart.py` | Recipe tools. |
| `static/` | The pages: login, home, game, and the "use a laptop" page. |
| `static/create.*`, `static/admin.*` | The Create Recipes page and the admin review page. |
| `static/journal.js` | The in-game journal (elements, what made them, what they make). |
| `static/elements/` | Element pictures, one PNG per element. |
| `aigen/` | The optional AI suggestion tool (see *AI suggestions*); `python3 -m aigen --help`. |
| `image_check.py` | Lists elements without a picture and pictures that match nothing. |
| `test_coop.py`, `test_reload.py`, `test_drafts.py`, `test_daily.py`, `test_race.py`, `test_aigen.py`, `test_security.py`, `test_invites.py`, `test_signup_checks.py`, `test_keepalive.py`, `test_accounts_cache.py`, `test_recipes.json` | Tests and their fixed recipe set. |

## Troubleshooting

- **Other laptops can't connect:** check the firewall, that everyone is on the same
  network, and that you gave them the "Other players" address (not `localhost`).
- **"Please join from a laptop":** the browser looks like a phone. Use a laptop or
  desktop browser.
- **Everyone was logged out:** the server restarted. Log in again; games that were
  running are gone unless they had been saved.
- **"the accounts file cannot be read":** `accounts.json` is damaged. The server
  refuses to overwrite it so no accounts are lost; fix or move the file.
- **Forgot a password:** run `python3 accounts_admin.py reset <username>`.
- **Game says it's full or the name is taken:** a game holds 5 players, and one
  account can only be in a game once. When a player disconnects, their seat and
  name are freed straight away; refreshing the page or reconnecting within about
  30 seconds puts them back as the same player.
