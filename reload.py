"""Patch the running server without dropping games.

Usage:
    python3 reload.py [--pid-file alchemy.pid] [--no-check]

Edit the code, then run this on the machine running the server. It first checks that
the new code imports and that the recipe file loads (so a typo can't take the server
down), then asks the server to restart itself in place. Running games, workspace tiles,
players and logins are kept; browsers reconnect within a second or two. Changes to
static/ files need no reload at all, since they are read on every request (players are
offered a "reload the page" banner).

When the server runs under the supervisor (ops/supervise.py), the reload is a restart by the supervisor: the server saves its
running games as it stops and the new one picks them up, so games are kept the same way, on every system, Windows included.
Otherwise the server is asked to restart itself in place with a Unix signal (not available on Windows).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ops.supervise import pid_alive, request, supervisor_running

BASE_DIR = Path(__file__).resolve().parent
CHECK = ("import server, state, games, accounts, saves, hotreload, hints, recipes;"
         "from recipes import load_recipes; load_recipes(sys.argv[1])")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Reload the Co-op Alchemy server without ending games")
    parser.add_argument("--pid-file", default=str(BASE_DIR / "alchemy.pid"))
    parser.add_argument("--no-check", action="store_true", help="skip the import and recipe check")
    args = parser.parse_args(argv[1:])
    data_dir = Path(args.pid_file).resolve().parent
    supervised = supervisor_running(data_dir)
    if not supervised and not hasattr(signal, "SIGUSR1"):
        print("No supervisor is running for this server, and reloading in place needs a Unix signal: run the server under\n"
              "ops/supervise.py (see ops/install_supervisor.py), or stop and start it.", file=sys.stderr)
        return 1
    try:
        info = json.loads(Path(args.pid_file).read_text(encoding="utf-8"))
        pid, snapshot = int(info["pid"]), Path(info["snapshot"])
    except (OSError, ValueError, KeyError, TypeError):
        print(f"could not read {args.pid_file}: is the server running from this folder?", file=sys.stderr)
        return 1
    if not supervised and not pid_alive(pid):
        print(f"the server (process {pid}) is not running; just start it.", file=sys.stderr)
        return 1
    if not args.no_check:
        check = subprocess.run([info.get("python") or sys.executable, "-c", f"import sys;{CHECK}", str(info["recipes"])],
                               cwd=BASE_DIR, capture_output=True, text=True)
        if check.returncode != 0:
            print("The new code or recipe file has a problem, so the server was NOT reloaded:\n",
                  file=sys.stderr)
            print(check.stderr.strip() or check.stdout.strip(), file=sys.stderr)
            return 1
    done = snapshot.with_name(snapshot.name + ".last")
    before = done.stat().st_mtime_ns if done.exists() else 0
    if supervised:
        request(data_dir, "restart")   # the supervisor stops it politely (games saved) and starts the new code
    else:
        os.kill(pid, signal.SIGUSR1)
    deadline = time.time() + (45 if supervised else 20)
    while time.time() < deadline:
        time.sleep(0.2)
        if done.exists() and done.stat().st_mtime_ns != before and not snapshot.exists():
            print("Reloaded. Players reconnect by themselves.")
            return 0
    print("The server did not confirm the reload in time. Check its console (or data/supervisor.log).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
