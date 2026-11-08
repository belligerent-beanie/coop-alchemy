# Co-op Alchemy

A co-op, multiplayer element-combination game for a local network. Players share
one workspace: drag elements onto each other to combine them, and every discovery
goes into a book everyone shares. It is written with the Python standard library
only (no packages to install), and the browser side is plain HTML, CSS and
JavaScript.

## Requirements

- Python 3.10 or newer (developed and tested on 3.13).
- A current browser for every player, on a laptop, a tablet or a phone. On a touch screen you tap an element to add it
  and drag tiles together to combine them; the layout changes to suit portrait and landscape.
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
2. **Home page.** Three tabs: **Solo** (your private saved game, and Create recipes), **Multiplayer** (running, recent and saved games, and starting one) and **Challenges** (the daily challenge and races). The multiplayer tab shows the games that are running, lets you start a new one, and
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
   other's cursors. Double-click a tile to make a copy of it; the copy appears half a tile right and down, so it overlaps the original by half. Elements are listed alphabetically. Hover an element to see who discovered it. **Right-click** an element or a tile (on a phone: **hold it, then let go**) to open its page in the 📖 Journal. The cat 🐈 clears
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

### Discovery moments, awards and emotes

- **Discovery moments.** Every element has a rarity (common, uncommon, rare, epic) from how far it is from the starting elements
  compared with all the others. Finding one plays a short sound and, from uncommon up, a banner and sparks over the board; other
  players' finds only do this when rare or epic. 🔊 in the header turns the sound off (remembered per browser), and the banner is
  skipped for anyone who has asked their system for reduced motion. Rarity also colours the list, the journal and its Collection.
- **Awards.** When a shared game ends, everyone sees minor awards: *Most elements found* (two or more players), *Longest chain*
  (the element furthest from the starting elements), *Rarest find* (fewest ways to make it), *First off the mark*, and *Never
  gives up* (five or more combinations that did nothing). Solo, daily and race games have none.
- **Emotes.** 😀 in the header of a shared game sends an emote that shows next to your name for everyone, at most one a second. Players
  can only send ids from `emotes.py`, never free text. The glyphs there are placeholders: change them in that one file.

### Companions

With `--bots on`, a person who has been alone in a public game for 20 to 75 seconds is joined by a **companion**: a player with an old-fashioned name
from `cast.py` (Wulfstan, Bwci_Bach, Marigold_Finch...). It stays 6 to 30 minutes, then leaves, and that room gets no other for a good while.
There is one per game at most, and 3 across the server (`--bots-max`).

- **How they play.** A companion uses the same moves a browser does: it puts tiles down, picks one up, drags it onto another, and the cursor
  you see is a model of a human hand (movements timed by Fitts's law, a gentle curve, a bell-shaped speed, the odd overshoot and correction, a
  little tremor, a slow drift when idle). Some only keep a presence; most play like a beginner: they mostly guess pairs, now and then
  try one that works, put old tiles away, and sometimes react with an emote when someone else finds something.
- **They know their place.** A person always gets the seat: a companion never takes the last free one and leaves if the room fills, if it
  empties, or after its stay. They are not counted as people (Mr Alchemy needs two real ones, an empty game still expires), are not saved,
  and are not in hot-reload snapshots. They show up in lists exactly like anyone else; administrators see them on the Operations page, where
  *Add a companion* and *Remove* are the quick way to try one.
- **Hosting.** If a person is looking at the lobby (the home page is fetching the list of games) and there is nothing in it to join, then after 15 to
  60 seconds a companion opens a public room with an ordinary random name and waits in it, only keeping a presence (no discoveries while it is alone),
  so the lobby shows a room with someone in it. If nobody joins within 4 to 10 minutes it leaves and the room closes; if someone does, it starts to play
  and stays a normal visit, and when the people go it goes and the room closes. Only one such room at a time, never the last free game slot, and no
  other for 5 to 15 minutes after one opens. The room has no creator, so whoever is in it may save it. The Operations page can open one on demand.
- Nobody can register under a name in the cast. The terms say some players may be computer-controlled.

### Mr Alchemy

Once per public game, after a random 4 to 15 minutes of two or more people playing together, **Mr Alchemy** drops in: the screen dims, a hat
lands, "You've been struck by Mr Alchemy" flashes up, and 5 to 10 new elements are unlocked for the whole room, one chip at a time, before he
vanishes. Click *Skip* to get the elements at once. For anyone who has asked their system for reduced motion it is a plain card instead.

- The gift is random but sensible: each element can be made from what the room already has plus the gifts before it, so nothing he gives
  has ingredients nobody has found. They show in the journal as "A gift from Mr Alchemy" and win no awards.
- Never in Solo, daily challenges or races, and never to a game with fewer than two people in it. Nobody can register under his name.
- Administrators can send him to a running game from the Operations page (*Send Mr Alchemy*), whatever the schedule says: that is the quick
  way to see the cutscene. `--mr-alchemy off` switches him off; `--mr-alchemy-chance` makes some games miss out.
- His look (a 🎩 glyph) is a placeholder: the cutscene's pieces are in `static/app.js` (`MR_ALCHEMY`, `playVisitor`) and `static/style.css`.

### Your profile

The home page opens with a small card of your own numbers: elements found with others, games with others, how far you are in Solo, daily challenges
finished (and your best run of days), races finished and won, achievements earned and the latest three, and, if they apply, how often Mr Alchemy has
struck, your epic finds and the emotes you sent. It is built from what is already kept (`profiles.py`, `solo.py`) and shows nobody else's numbers or
any ranking: the game is meant to be played together, not against each other.

### Achievements

There are three lists, one on each tab of the home page: **Solo** (12, from a solo game's progress), **Multiplayer** (11) and **Challenges** (8). Earning one
shows a banner and a chime in the game, to that player only. The Multiplayer and Challenges ones come from a few counters kept per account
(`profiles.py`, `achievements.py`): elements you discovered in shared games (and epic ones), shared games played with others, being in a full game,
being there when Mr Alchemy visits, emotes sent, daily challenges finished (hard ones, and runs of days in a row) and races finished and won. Elements
found by companions or Mr Alchemy are not yours, and neither counter nor achievement is awarded in Solo mode (that has its own list).

To add one, put a line in `BADGES` (or `ACHIEVEMENTS` for solo) in `achievements.py` with a test on the counters; an account that already meets it earns
it the next time that counter changes. The counters are written to `profiles.json` every few seconds, not on every discovery.

### Solo mode

**Solo** on the home page starts your own private game: just you, with the Djinn like any game, **one hint a day**, and no invite link. Your
progress is saved after every discovery, so you can leave at any time and the next **Solo** carries on where you stopped. The
📖 Journal's *Collection* tab shows how many elements are left to find. Solo also keeps a list of **achievements** (shown on the Solo tab: elements found, long chains, the Djinn, Time and more; the
list is in `achievements.py`) and gives you **one hint a day**; the day's hint is used up by the first hint that tells you something. Find every element and Solo is complete for good,
which opens Create Recipes (below). Administrators and bot accounts do not need to finish it.

### Create Recipes and promotions

**Create recipes** on the home page opens once you have completed Solo mode; until then it only shows your progress. It is a
solo playground where every element is
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

A first administrator can be made with `python3 accounts_admin.py create-admin` (an account called `admin`, with a random temporary password saved to `admin-first-password.txt` beside the accounts file and never printed; it must be changed at first login), and **admins** are accounts marked with `python3 accounts_admin.py make-admin <username>`
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
                  [--drafts-file drafts.json] [--daily-file daily.json] [--solo-file solo.json]
                  [--snapshot-file reload_snapshot.json] [--pid-file alchemy.pid]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--host` | `0.0.0.0` | Address to listen on (`127.0.0.1` for this machine only). |
| `--port` | `8000` | Port to listen on. |
| `--recipes` | `recipes.json` next to `server.py` | The recipe file. |
| `--saves-dir` | `saves` next to `server.py` | Where saved games are kept. |
| `--accounts-file` | `accounts.json` next to `server.py` | Usernames and password hashes. |
| `--client-ip-header` | none | Behind a trusted proxy, take the visitor's address from this header (Cloudflare: `CF-Connecting-IP`); otherwise `X-Forwarded-For` is used. |
| `--contact` | none (or `ALCHEMY_CONTACT`) | An email or web address players can reach you at, shown on the terms of use and privacy pages. |
| `--require-invite` | off | Signup needs a single-use invite code (`accounts_admin.py invite create`; see DEPLOYMENT.md). |
| `--invites-file` | `invites.json` next to `server.py` | Invite codes, stored as hashes. |
| `--trusted-proxy` | none | A reverse proxy in front of the server (repeatable); its `X-Forwarded-*` headers are believed. See DEPLOYMENT.md. |
| `--turnstile-site-key` | off | Cloudflare Turnstile human check on signup (secret key in the `TURNSTILE_SECRET_KEY` environment variable). |
| `--require-email-verification`, `--public-url` | off | Signup asks for an email and the account works only after its link is opened; links use the public URL. SMTP settings come from `SMTP_*` environment variables. See DEPLOYMENT.md. |
| `--tls-cert`, `--tls-key` | off | Serve over HTTPS (see below). |
| `--max-games-per-account` | `1` | How many running games one account may have started at once (`0` for no limit). |
| `--blocklist-file` | none | Extra blocked words for usernames, game names and recipe names, one per line (the built-in list is `blocklist.txt`). |
| `--reports-file` | `reports.json` next to `server.py` | Where player reports wait for the administrators, for 14 days at most. |
| `--bots` | `off` | Companions: computer-controlled players that keep a lone player company in a public game (below). |
| `--bots-host` | `on` | With `--bots on`: a companion opens a room when a person is looking at a lobby with nothing in it to join. |
| `--bots-max` | `3` | The most companions playing at once, across all games. |
| `--mr-alchemy` | `on` | Mr Alchemy, the once-per-game visitor of public games (below). `off` switches him off. |
| `--mr-alchemy-chance` | `1` | The chance (0 to 1) that a game gets his visit at all. |
| `--mr-alchemy-min`, `--mr-alchemy-max` | `4`, `15` | He comes after a random number of minutes in this range, counting only time with two or more players together. |
| `--max-games` | `3` | How many games may run at once (daily challenges and races do not count). |
| `--log-file`, `--log-days` | console, 14 | Write the server's messages (logins, failures, admin actions) to a file that starts afresh each midnight, keeping this many days. |
| `--idle-minutes` | `10` | How long an empty game stays open. |
| `--drafts-file` | `drafts.json` next to `server.py` | Players' draft recipes and promotion proposals. |
| `--daily-file` | `daily.json` next to `server.py` | The daily challenge's targets, routes, attempts used and scores (the last 400 days). |
| `--profile-file` | `profiles.json` next to `server.py` | Each account's achievement counters and the achievements earned in shared games, daily challenges and races. |
| `--solo-file` | `solo.json` next to `server.py` | Each account's solo-mode progress and whether it has found everything (which opens Create Recipes). |
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

The folder is not served by file name: each picture gets a secret address, which the server sends with the element's own
data (so a player only ever learns the pictures of elements they have found). It looks at the folder every few seconds, so adding a file
needs neither a restart nor more than a page reload. The secret changes each time the server starts. To see which elements have no picture and which files match nothing:

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

## Going live

[HUMAN_TODO.md](HUMAN_TODO.md) lists what only a person can do (accounts, decisions, real-phone tests). [LAUNCH.md](LAUNCH.md) is the step-by-step runbook from this PC to a public game behind a Cloudflare Tunnel (server, tunnel, first
administrator, invites, the admin lock, backups and a tested restore). [DEPLOYMENT.md](DEPLOYMENT.md) has the reasons and the security detail.

## Documentation rules

When you add or change an endpoint, a server-sent event, a status code or a command-line option, update `API.md` (the
endpoint tables, the Events table, and its change log) and the options table above in the same commit.
`test_api_docs.py` compares the code with these documents and fails on any difference.

## Looks

The pages wear one of two looks. **Euler** (the default) borrows Project Euler's palette and layout: white pages, brown headings and links, a grey bar with an orange
current tab, square corners, DM Sans (self-hosted in `static/fonts/`, SIL Open Font License), and it follows the browser's light or dark setting. **Classic** is the original
look. Both are in the same stylesheets: `style.css` is the classic look on its own, and `euler.css` overrides it only under `html[data-look="euler"]`.
`look.js` sets that attribute before the page paints, from the choice kept in the browser (`localStorage`, key `look`); a small link at the foot of every page that
scrolls switches it. The game board has no link; it uses whichever look was chosen on the other pages.

## Tests

```
python3 run_tests.py            # everything; the last lines say which tests failed and why
python3 run_tests.py --failed   # only what failed last time
python3 run_tests.py test_bots  # one module, class or test (-x stops at the first failure)
python3 -m unittest discover    # the plain runner
```

`run_tests.py` keeps each test's output and the full tracebacks in `test-output.log`, so the screen only holds the verdict.

The tests use `test_recipes.json` (a small made-up set), so they give the same result whatever
recipes the game uses. The exception is `test_aigen.py`, whose end-to-end tests read (never change)
your real `recipes.json` and talk to a built-in fake LM Studio, so they need no model; they are
skipped if `recipes.json` is missing.

## Project layout

| File | Purpose |
| --- | --- |
| `server.py` | HTTP server, routes, server-sent events, HTTPS. |
| `state.py` | One game: players, shared workspace, discoveries, hints, Djinn. |
| `games.py` | Running games: the 3-game limit, idle closing, ending, resuming. |
| `saves.py` | Saved games on disk, 3-day expiry. |
| `drafts.py` | Draft recipes and promotion proposals (per account, with admin handling). |
| `hotreload.py`, `reload.py` | Snapshot and restore for patching without stopping games; the command that triggers it. |
| `accounts.py`, `accounts_admin.py` | Accounts, passwords, sessions, and the admin tool (admins and bots). |
| `throttle.py`, `invites.py`, `captcha.py`, `mailer.py` | Rate limits and one-time event tickets, single-use invite codes, the Turnstile check, and verification email (see DEPLOYMENT.md). |
| `namefilter.py`, `blocklist.txt`, `reports.py` | Blocked words in names, and player reports for the administrators (no reporter is recorded). |
| `erasure.py` | Deleting an account and everything kept under its name (used by the player, the admin page and the admin tool). |
| `API.md` | The HTTP API: every endpoint, the event stream, errors and limits, plus a change log. `test_api_docs.py` fails if it drifts from the code. |
| `ops/` , `OPS.md` | Running the live server: safe deploys with automatic rollback, backups, a watchdog, a daily summary, scheduled tasks, smoke test. See OPS.md. |
| `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example` | Run the game, and its Cloudflare Tunnel, in containers on any host (not yet built: see DEPLOYMENT.md). |
| `touch_check.py` | Drives a real Chrome as a phone, a tablet and a desktop, with real touch input, and checks layout and gestures (needs Chrome or Edge installed). |
| `bench_load.py` | Load test: rooms of players and solo players against a real server; CPU, memory, threads, delay, errors. |
| `bench_cursor.py` | Measures what shared cursors cost the server (`python3 bench_cursor.py`): connections, latency, delivery, CPU. |
| `recipes.py`, `hints.py` | Recipe file loading and checking; hint logic. |
| `race.py` | Race rooms: lobby, countdown, standings and the winner. |
| `daily.py` | The daily challenge: picking the targets, reference routes, score boards, streaks and the weekly board. |
| `validate_recipes.py`, `recipe_report.py`, `recipe_flowchart.py` | Recipe tools. |
| `static/` | The pages: login, home, game, race and the rest; they adapt to phones, tablets and desktops. |
| `static/create.*`, `static/admin.*` | The Create Recipes page and the admin review page. |
| `static/ops.*`, `static/privacy.html` | The administrator's operations page, and the privacy page. |
| `static/journal.js` | The in-game journal (elements, what made them, what they make). |
| `static/elements/` | Element pictures, one PNG per element. |
| `aigen/` | The optional AI suggestion tool (see *AI suggestions*); `python3 -m aigen --help`. |
| `image_check.py` | Lists elements without a picture and pictures that match nothing. |
| `test_coop.py`, `test_reload.py`, `test_drafts.py`, `test_daily.py`, `test_race.py`, `test_aigen.py`, `test_security.py`, `test_invites.py`, `test_signup_checks.py`, `test_keepalive.py`, `test_accounts_cache.py`, `test_cursors.py`, `test_ops.py`, `test_api_docs.py`, `test_account_tools.py`, `test_logging.py`, `test_deploy_files.py`, `test_safety.py`, `test_recipes.json` | Tests and their fixed recipe set. |

## Troubleshooting

- **Other laptops can't connect:** check the firewall, that everyone is on the same
  network, and that you gave them the "Other players" address (not `localhost`).
- **Everyone was logged out:** the server restarted. Log in again; games that were
  running are gone unless they had been saved.
- **"the accounts file cannot be read":** `accounts.json` is damaged. The server
  refuses to overwrite it so no accounts are lost; fix or move the file.
- **Forgot a password:** run `python3 accounts_admin.py reset <username>`.
- **Game says it's full or the name is taken:** a game holds 5 players, and one
  account can only be in a game once. When a player disconnects, their seat and
  name are freed straight away; refreshing the page or reconnecting within about
  30 seconds puts them back as the same player.
