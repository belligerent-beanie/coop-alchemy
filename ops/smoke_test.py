"""A quick end-to-end check that a copy of the code really works: used before every deploy.

    python ops/smoke_test.py [--server-dir DIR]

Starts the server from `server_dir` on a free port with a throwaway data folder (your real data is never touched), then
does what players do: loads the pages, signs up two accounts, starts a game, joins, opens event streams, moves a cursor,
adds a tile, ends the game, and checks each result. Prints the problems found; the exit status is 0 only if there are none.
"""
from __future__ import annotations

import argparse
import http.client
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench_cursor import free_port, server_command, wait_up  # noqa: E402  (shared helpers for launching a scratch server)


class Stream:
    """An event stream read on a background thread, so the checks can wait for a particular event with a timeout."""

    def __init__(self, port: int, slug: str, ticket: str) -> None:
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.conn.request("GET", f"/g/{slug}/events?ticket={ticket}")
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        name = ""
        try:
            for raw in self.resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: ") and name:
                    self.events.put((name, json.loads(line[6:])))
        except (OSError, http.client.HTTPException, ValueError):
            pass

    def wait_for(self, wanted: str, seconds: float = 5.0) -> Any | None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                name, data = self.events.get(timeout=max(0.05, end - time.monotonic()))
            except queue.Empty:
                break
            if name == wanted:
                return data
        return None

    def close(self) -> None:
        try:
            self.conn.close()
        except OSError:
            pass


def run(server_dir: Path = ROOT) -> list[str]:
    """Run the checks against the code in `server_dir`; returns a list of problems (empty if all is well)."""
    problems: list[str] = []

    def check(ok: bool, what: str) -> bool:
        if not ok:
            problems.append(what)
        return ok

    tmp = tempfile.TemporaryDirectory()
    port = free_port()
    server = subprocess.Popen(server_command(server_dir, port, Path(tmp.name), None), cwd=str(server_dir),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    streams: list[Stream] = []
    try:
        wait_up(port)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)

        def call(method: str, path: str, body: Any = None, session: str = "") -> tuple[int, Any, http.client.HTTPResponse]:
            headers = {"Content-Type": "application/json"}
            if session:
                headers["X-Session"] = session
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                data = json.loads(raw) if raw[:1] in (b"{", b"[") else raw
            except ValueError:
                data = raw
            return resp.status, data, resp

        status, _, resp = call("GET", "/login")
        check(status == 200, f"/login answered {status}")
        check(resp.version == 11, "the server does not speak HTTP/1.1")
        check(bool(resp.getheader("Content-Security-Policy")), "responses carry no Content-Security-Policy header")
        status, data, _ = call("GET", "/healthz")
        check(status == 200 and data == {"ok": True}, f"/healthz answered {status} {data!r}")
        status, data, _ = call("GET", "/auth-config")
        check(status == 200 and isinstance(data, dict) and "invite_required" in data, "/auth-config is wrong")
        status, body, _ = call("GET", "/")
        check(status == 200 and b"Co-op Alchemy" in body, f"the home page answered {status}")
        status, body, _ = call("GET", "/static/app.js")
        check(status == 200 and len(body) > 1000, f"the game script answered {status}")

        sessions: list[str] = []
        for name in ("smoke1", "smoke2"):
            status, data, _ = call("POST", "/register", {"username": name, "password": "smoke-test-pass-1"})
            if not check(status == 200 and "session" in data, f"signing up {name} answered {status}: {data!r}"):
                return problems
            sessions.append(data["session"])
        status, data, _ = call("POST", "/games", {"name": "smoke test"}, sessions[0])
        if not check(status == 200 and "id" in data, f"creating a game answered {status}: {data!r}"):
            return problems
        slug = data["id"]
        players = []
        for session in sessions:
            status, data, _ = call("POST", f"/g/{slug}/join", {}, session)
            if not check(status == 200 and "token" in data, f"joining answered {status}: {data!r}"):
                return problems
            players.append(data)
        status, _, _ = call("GET", f"/g/{slug}")
        check(status == 200, f"the game page answered {status}")

        for session, player in zip(sessions, players):
            status, data, _ = call("POST", f"/g/{slug}/ticket", {"player": player["token"]}, session)
            if not check(status == 200 and "ticket" in data, f"asking for an event ticket answered {status}"):
                return problems
            stream = Stream(port, slug, data["ticket"])
            streams.append(stream)
            check(stream.status == 200, f"the event stream answered {stream.status}")
        check(streams[1].wait_for("snapshot") is not None, "the event stream sent no snapshot")

        status, _, _ = call("POST", f"/g/{slug}/cursor", {"player": players[0]["token"], "x": 0.25, "y": 0.75})
        check(status == 200, f"moving a cursor answered {status}")
        seen = streams[1].wait_for("cursor")
        check(seen is not None and seen.get("x") == 0.25, "the other player's stream never received the cursor")
        status, _, _ = call("POST", f"/g/{slug}/tile/add",
                            {"player": players[0]["token"], "element": "water", "x": 0.5, "y": 0.5})
        check(status == 200, f"adding a tile answered {status}")
        check(streams[1].wait_for("tile_added") is not None, "the other player's stream never received the new tile")

        status, data, _ = call("POST", f"/g/{slug}/elsewhere", {"junk": "x" * 300})  # an error reply to an unread body
        check(status == 404 and isinstance(data, dict), f"an unknown route answered {status}")
        status, _, _ = call("GET", "/auth-config")  # ... must not break the kept-alive connection
        check(status == 200, "the connection was unusable after an error reply")

        status, _, _ = call("POST", f"/g/{slug}/end", {"player": players[0]["token"], "save": False})
        check(status == 200, f"ending the game answered {status}")
        check(streams[1].wait_for("ended") is not None, "the other player was never told the game ended")
    except Exception as exc:  # a crash of the check itself is a failed check
        problems.append(f"the smoke test itself failed: {type(exc).__name__}: {exc}")
    finally:
        for stream in streams:
            stream.close()
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
        try:
            tmp.cleanup()
        except OSError:
            pass
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end smoke test of a server checkout")
    parser.add_argument("--server-dir", default=str(ROOT))
    args = parser.parse_args()
    started = time.monotonic()
    problems = run(Path(args.server_dir).resolve())
    for problem in problems:
        print(f"  FAIL  {problem}")
    print(f"smoke test: {'passed' if not problems else f'{len(problems)} problem(s)'} in {time.monotonic() - started:.1f} s")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
