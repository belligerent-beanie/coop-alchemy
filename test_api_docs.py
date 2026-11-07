"""API.md must describe the server as it is: routes, events and options cannot drift apart from the documentation.

If one of these tests fails you changed an endpoint, event or server option: update API.md (and its change log) or the
README's options table in the same commit.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from games import GameRegistry
from test_coop import RECIPES, call, load_recipes, serve

ROOT = Path(__file__).resolve().parent
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
STATE = (ROOT / "state.py").read_text(encoding="utf-8")
API = (ROOT / "API.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

ROW = re.compile(r"^\| `(GET|POST) ([^`]+)` \|", re.M)


def documented_routes() -> set[tuple[str, str]]:
    return {(method, path) for method, path in ROW.findall(API)}


def routes_in_code() -> set[str]:
    """Every path the server's routing mentions, in the form API.md uses (<id> for a game or race)."""
    found: set[str] = set()
    literal = re.compile(r'"(/[A-Za-z0-9_/.-]*)"')
    in_run_action = False  # run_action() also has a parameter called `path`, but its values are in-game actions
    for line in SERVER.splitlines():
        if line.startswith("def run_action("):
            in_run_action = True
        elif line and not line[0].isspace() and not line.startswith(")"):
            in_run_action = False
        if re.search(r"\bpath (==|in \(|not in \()|\bpath\.startswith\(", line):
            found.update(("/g/<id>" + p if in_run_action else p) for p in literal.findall(line))
        if re.search(r"\brest (==|in \(|not in \()", line):
            found.update("/g/<id>" + p for p in literal.findall(line))
    actions = re.search(r"^ACTIONS = \((.*?)\)", SERVER, re.S | re.M)
    assert actions, "could not find ACTIONS in server.py"
    found.update("/g/<id>" + p for p in literal.findall(actions.group(1)))
    race = re.search(r"^RACE_PATH = .*\(\?:([a-z|]+)\)", SERVER, re.M)
    assert race, "could not find RACE_PATH in server.py"
    found.update("/races/<id>/" + name for name in race.group(1).split("|"))
    found.discard("/g/<id>/ticket-")  # (defensive: nothing legitimate ends like this)
    return found


class RouteDocumentationTests(unittest.TestCase):
    def test_every_route_in_the_code_is_documented(self) -> None:
        documented = {path for _, path in documented_routes()}
        missing = []
        for route in sorted(routes_in_code()):
            if route.endswith("/"):  # a prefix such as /static/ or /create/: some documented route must start with it
                ok = any(path.startswith(route) for path in documented)
            elif route == "/index.html":
                ok = "alias: `/index.html`" in API
            elif route == "/race/":
                ok = "/race/<id>" in documented
            else:
                ok = route in documented
            if not ok:
                missing.append(route)
        self.assertEqual(missing, [], "these routes exist in server.py but are not documented in API.md")

    def test_the_route_scan_finds_the_routes_we_know_about(self) -> None:
        """Guards the scan itself: if the code is restructured and the scan goes blind, this fails instead of passing quietly."""
        found = routes_in_code()
        for expected in ("/login", "/register", "/healthz", "/games", "/g/<id>/cursor", "/g/<id>/events", "/g/<id>/ticket",
                         "/g/<id>/tile/add", "/races/<id>/join", "/admin/handle", "/create/promote", "/static/"):
            self.assertIn(expected, found)
        self.assertGreater(len(found), 40)

    def test_every_documented_route_is_known_to_the_server(self) -> None:
        server, port = serve(GameRegistry(load_recipes(RECIPES), max_games=2))
        self.addCleanup(server.server_close)  # cleanups run last-in first-out: stop serving, then close
        self.addCleanup(server.shutdown)
        rows = documented_routes()
        self.assertGreater(len(rows), 50, "API.md has too few endpoint rows: did the table format change?")
        unknown = []
        for method, path in sorted(rows):
            url = path.replace("<id>", "nogame").replace("<file>", "app.js").replace("<token>", "x")
            status, body = call(port, method, url, {} if method == "POST" else None, headers={"X-Session": ""})
            if status == 404 and isinstance(body, dict) and body.get("error") == "not found":
                unknown.append(f"{method} {path}")
        self.assertEqual(unknown, [], "API.md documents these routes but the server does not have them")

    def test_no_route_is_listed_twice(self) -> None:
        rows = ROW.findall(API)
        self.assertEqual(len(rows), len(set(rows)))


class EventDocumentationTests(unittest.TestCase):
    def documented_events(self) -> set[str]:
        section = API.split("### Events", 1)[1].split("\n## ", 1)[0]
        return set(re.findall(r"^\| `([a-z_]+)` \|", section, re.M))

    def published_events(self) -> set[str]:
        names = set(re.findall(r'_publish\(\s*"([a-z_]+)"', STATE))
        names.update(re.findall(r'format_event\(\s*"([a-z_]+)"', STATE))
        names.discard("")
        return names

    def test_the_documented_events_are_exactly_the_published_ones(self) -> None:
        documented, published = self.documented_events(), self.published_events()
        self.assertEqual(sorted(published - documented), [], "events the server sends that API.md does not describe")
        self.assertEqual(sorted(documented - published), [], "events API.md describes that the server never sends")
        self.assertGreaterEqual(len(published), 10)


class OptionDocumentationTests(unittest.TestCase):
    def test_every_server_option_is_in_the_readme(self) -> None:
        options = re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', SERVER)
        self.assertGreater(len(options), 10)
        self.assertEqual([o for o in options if o not in README], [], "server options missing from the README options table")


if __name__ == "__main__":
    unittest.main()
