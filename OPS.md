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
`data\live.err.log` (server messages), `data\watchdog.log` (restarts), `deploys.log` (what was deployed when), `backups\`.

## What is stored and where
`data\accounts.json` (usernames and password hashes, never passwords), `data\daily.json` (daily challenge scores),
`data\drafts.json` (recipe drafts), `data\invites.json` (hashed invite codes, if used), `data\saves\` (saved games).
Backups hold the same files, so protect `backups\` like `accounts.json`. Login sessions live only in memory.
