"""Try this copy of the game in a browser with throwaway data: your real accounts, saves and scores are never touched.

    python ops/preview.py [--port 8765] [--unlock N]

Starts the server on http://localhost:PORT with every data file in a temporary folder (deleted when you stop it with Ctrl+C), so
the first account you sign up is a brand-new player. `--unlock 0` opens Multiplayer to everyone; the default is the real rule.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench_cursor import server_command, wait_up  # noqa: E402  (shared helper for launching a scratch server)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--unlock", type=int, default=10, help="achievements needed for Multiplayer (default 10; 0 opens it)")
    args = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix="alchemy-preview-"))
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
