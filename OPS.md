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
| Restart the live server by hand | `powershell -File ..\coop-alchemy-live\ops\restart-live.ps1` |
| Check a copy of the code end to end | `python ops\smoke_test.py` |

### What `deploy.py` does
1. Refuses unless the development folder is clean and on `main`, and the live folder can simply move forward.
2. Runs every test file. Any failure: stop, live untouched.
3. Smoke-tests the new code on a scratch copy of the server (sign-up, game, event streams, cursors, tiles). Any failure: stop.
4. Backs up the live data (label `pre-deploy`; the newest 10 are kept).
5. Moves the live folder to `main`, restarts the server, and requires it to answer correctly within 30 seconds.
6. If it does not, it puts the live folder back on the previous commit, restarts, and says so. Data is never rolled back.

Every deploy and rollback is appended to `coop-alchemy-live\deploys.log`. A deploy ends running games and logs everyone out
once (saved games stay). `restart-live.ps1 -Update` also exists but skips all checks: prefer `deploy.py`.

## Keeping it running without you
`ops\install_tasks.ps1` registers three Windows scheduled tasks. It is **not run automatically**; read it, then run it once:

```
powershell -ExecutionPolicy Bypass -File ops\install_tasks.ps1        (to remove: add -Remove)
```

| Task | When | Does |
|---|---|---|
| CoopAlchemy-Start | at logon | starts the server if it is not running |
| CoopAlchemy-Watchdog | every minute | restarts the server after 3 missed health checks in a row; gives up (and logs) after 3 restarts in 30 minutes, since a server that cannot stay up needs a person |
| CoopAlchemy-Backup | daily 04:00 | backs up the data, keeping the newest 14 |

Limits, because the host is a Windows PC:
- The tasks run only while you are logged on to Windows. After a reboot the server returns when you log in; turn on automatic
  sign-in for that account if the PC should recover on its own.
- The PC must not sleep: Settings > System > Power > "When plugged in, put my device to sleep after" > **Never**.
- If the PC is off, the site is off. A power cut or Windows update reboot is the likeliest outage.

Where to look when something seems wrong (all in `coop-alchemy-live`):
`data\live.log` (the activity log, 14 days: sign-ups, logins, failures, admin actions), `data\live.err.log` (anything the server
printed instead, such as a crash on start-up), `data\watchdog.log` (restarts), `deploys.log` (what was deployed when),
`summaries\` (one small counts-only file per day) and `backups\`.

## Day-to-day moderation: the operations page
Log in as an administrator and open `/ops` (the home page shows an "Operations" button). It shows counts since the server started
(accounts, logins, failed logins, sign-ups, rate-limited requests, server errors), the games running now, every account, and invite
codes. Buttons that change things ask for a second click.

| To do this | On `/ops` |
|---|---|
| A player forgot their password | **New password**: give them the temporary one shown once; they must pick their own at next login |
| Lock someone out without deleting anything | **Disable** (and **Enable** to let them back). Their logins stop at once; a game they are in goes on until you **End game** |
| Remove an account | **Delete**: also removes its drafts, daily scores and unshared saved games, and the name from shared ones |
| Stop an abusive or stuck game | **End game** in the games list (it is not saved) |
| Hand out invite codes (needs `--require-invite`) | **Make codes**; copy them from the notice, they are shown only once |

Administrators cannot be disabled, deleted or reset on the page. Make someone an administrator (or remove the role) with
`python accounts_admin.py make-admin <name>` / `remove-admin <name>` on the live data
(`--file ..\coop-alchemy-live\data\accounts.json`). `accounts_admin.py` also has `list`, `verify`, `disable`, `enable`, `delete`
and `reset` for when you are at the machine.

## What is stored and where
`data\accounts.json` (usernames and password hashes, never passwords), `data\daily.json` (daily challenge scores),
`data\drafts.json` (recipe drafts), `data\invites.json` (hashed invite codes, if used), `data\saves\` (saved games).
Backups hold the same files, so protect `backups\` like `accounts.json`. Login sessions live only in memory.
