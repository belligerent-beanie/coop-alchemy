"""Try this copy of the game in a browser with throwaway data: your real accounts, saves and scores are never touched.

    python ops/preview.py [--port 8765] [--unlock N] [--streak NAME=DAYS]

Starts the server on http://localhost:PORT with every data file in a temporary folder (deleted when you stop it with Ctrl+C), so
the first account you sign up is a brand-new player. `--unlock 0` opens Multiplayer to everyone; the default is the real rule.
`--streak NAME=3` makes the account you will sign up as NAME look as if it had finished a daily challenge on each of the 3 days
before today, so finishing one today gives a streak of 4 and earns the hint (without it, a new player's streak is 1: no hint).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench_cursor import server_command, wait_up  # noqa: E402  (shared helper for launching a scratch server)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--unlock", type=int, default=10, help="achievements needed for Multiplayer (default 10; 0 opens it)")
    parser.add_argument("--streak", metavar="NAME=DAYS", help="pretend NAME finished a daily challenge on each of the DAYS days before today")
    args = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix="alchemy-preview-"))
    if args.streak:
        name, _, days = args.streak.partition("=")
        if not name or not days.isdigit():
            parser.error("--streak wants NAME=DAYS, for example --streak tester=3")
        past = {(date.today() - timedelta(days=n)).isoformat():
                {"tiers": {"easy": {"scores": {" ".join(name.split()).casefold(): {"name": name, "score": 5, "at": 0}}}}}
                for n in range(1, int(days) + 1)}
        (folder / "daily.json").write_text(json.dumps({"version": 2, "days": past}), encoding="utf-8")
    command = server_command(ROOT, args.port, folder, None)
    command[command.index("--multiplayer-unlock") + 1] = str(args.unlock)
    server = subprocess.Popen(command, cwd=str(ROOT))
    try:
        wait_up(args.port)
        print(f"\nPreview running: http://localhost:{args.port}   (scratch data in {folder}; Ctrl+C to stop and delete it)\n", flush=True)
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(folder, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
