# Running the live server

Two folders, two jobs:

| | Folder | Branch | Holds |
|---|---|---|---|
| Development | `coop-alchemy` | `main` | the code you change and test; its own scratch data |
| Live | `coop-alchemy-live` (a git worktree) | `live` | what players use on port 8000, with its data in `data\` |

Players never run development code. A change reaches them only through `ops\deploy.py`.

## Everyday commands (run from the development folder)

| To do this | Run |
|---|---|
| Put the latest `main` live, safely | `python ops\deploy.py` |
| Check everything first, change nothing | `python ops\deploy.py --dry-run` |
| Same, skipping the slow AI-tool tests | `python ops\deploy.py --quick` |
| Undo the last deploy | `python ops\deploy.py rollback` (or `rollback <commit>`) |
| Back up the live data now | `python ops\backup.py` |
| See the backups | `python ops\backup.py list` |
| Restore a backup (stop the server first) | `python ops\backup.py restore <name> --yes` |
| Restart the live server by hand (games are kept) | `python ..\coop-alchemy-live\ops\supervise.py restart` |
| Is it running? Restarts, last backup | `python ..\coop-alchemy-live\ops\supervise.py status` |
| Check a copy of the code end to end | `python ops\smoke_test.py` |
| Check phones, tablets and desktop with real touch input (needs Chrome or Edge installed) | `python touch_check.py` |

### What `deploy.py` does
1. Refuses unless the development folder is clean and on `main`, and the live folder can simply move forward.
2. Runs every test file. Any failure: stop, live untouched.
3. Smoke-tests the new code on a scratch copy of the server (sign-up, game, event streams, cursors, tiles). Any failure: stop.
4. Backs up the live data (label `pre-deploy`; the newest 10 are kept).
5. Moves the live folder to `main`, restarts the server, and requires it to answer correctly within 30 seconds.
6. If it does not, it puts the live folder back on the previous commit, restarts, and says so. Data is never rolled back.

Every deploy and rollback is appended to `coop-alchemy-live\deploys.log`. The restart goes through the supervisor (below): the
server saves its running games and logins as it stops and the new code picks them up, so players see a pause of a few seconds.
A deploy needs the supervisor running for the live folder.

This PC is a temporary host: the plan is to move to Cloudflare Tunnel on an external host, which is written up in
`DEPLOYMENT.md` ("Moving to Cloudflare Tunnel and an external host"). Everything on this page then stops applying.

## Keeping it running without you
`ops/supervise.py` keeps the server running, and is the same program on Windows, macOS and in the Docker container. It starts
the server with every data file in `data\`, and then:

| When | It |
|---|---|
| the server crashes | starts it again after 2 s, then 4, 8, … up to a minute if it keeps crashing |
| the server stops answering | restarts it after 3 missed health checks 20 s apart |
| it fails 5 times in 10 minutes | pauses for 10 minutes and says so in its log, since something needs a person |
| another server already answers on the port | waits, and never starts a second one |
| daily at 04:00 / 04:30 | backs up the data (keeping the newest 14) / writes yesterday's summary |
| it is running | keeps the computer from sleeping |

To have it start at every login, run once from the live folder (it reads `--data-dir`, `--port`, and server options after `--`, the
same as `supervise.py run`):

```
python ops\install_supervisor.py install            (Windows: a scheduled task; macOS: a LaunchAgent)
python ops\install_supervisor.py remove             (undo)
python ops\install_supervisor.py show               (print what it would register, change nothing)
```

On Windows this replaces the four scheduled tasks the old `install_tasks.ps1` made (start, watchdog, backup, summary): installing
removes them, since two watchers would fight. `supervise.py stop` stops the server and the supervisor until the next login
(`install_supervisor.py start` starts it again); `supervise.py restart` restarts the server, keeping running games. `reload.py`
asks the supervisor too, so reloading without ending games now works on Windows as well.

Limits:
- It runs only while you are logged on. After a reboot the server returns when you log in; turn on automatic sign-in for that
  account if the computer should recover on its own.
- If the computer is off, the site is off. A power cut or an update reboot is the likeliest outage.

Where to look when something seems wrong (all in `coop-alchemy-live`):
`data\live.log` (the activity log, 14 days: sign-ups, logins, failures, admin actions), `data\live.out.log` (anything the server
printed instead, such as a crash on start-up), `data\supervisor.log` (starts, restarts and why, backups), `deploys.log` (what was
deployed when), `summaries\` (one small counts-only file per day) and `backups\`.

## Day-to-day moderation: the operations page
Log in as an administrator and open `/ops` (the home page shows an "Operations" button). It shows counts since the server started
(accounts, logins, failed logins, sign-ups, rate-limited requests, server errors), the games running now, every account, and invite
codes. Buttons that change things ask for a second click.

| To do this | On `/ops` |
|---|---|
| A player forgot their password | **New password**: give them the temporary one shown once; they must pick their own at next login |
| Lock someone out without deleting anything | **Disable** (and **Enable** to let them back). Their logins stop at once; a game they are in goes on until you **End game** |
| Remove an account | **Delete**: also removes its drafts, daily scores and unshared saved games, and the name from shared ones |
| See what players have reported (no reporter is shown) | **Reports** section: **Dismiss** it, or **Disable account** for the player |
| Stop an abusive or stuck game | **End game** in the games list (it is not saved) |
| Hand out invite codes (needs `--require-invite`) | **Make codes**; copy them from the notice, they are shown only once |

Administrators cannot be disabled, deleted or reset on the page. Make someone an administrator (or remove the role) with
`python accounts_admin.py create-admin` (first time) or `make-admin <name>` / `remove-admin <name>` on the live data
(`--file ..\coop-alchemy-live\data\accounts.json`). `accounts_admin.py` also has `list`, `verify`, `disable`, `enable`, `delete`
and `reset` for when you are at the machine.

## What is stored and where
`data\accounts.json` (usernames and password hashes, never passwords), `data\daily.json` (daily challenge scores),
`data\drafts.json` (recipe drafts), `data\invites.json` (hashed invite codes, if used), `data\saves\` (saved games).
Backups hold the same files, so protect `backups\` like `accounts.json`. Login sessions live only in memory.
