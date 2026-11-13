"""Design patches (patches.py): the store, the command line, and the server's /patches endpoints."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path

import patches
from games import GameRegistry
from patches import PatchError, PatchStore
from test_coop import RECIPES, load_recipes, serve


def make_patches(root: Path) -> Path:
    folder = root / "patches"
    folder.mkdir()
    (folder / "cards.css").write_text("/* Rounder cards on the home page\n   (from the designer) */\n"
                                      "[data-page=\"home\"] .card { border-radius: 16px; }\n")
    (folder / "title.css").write_text("/* A new title */\nh1 { letter-spacing: 1px; }\n")
    (folder / "title.text.json").write_text(json.dumps([{"page": "home", "selector": "h1", "text": "Alchemy, together"}]))
    (folder / "broken.css").write_text("/* Broken text file */\n")
    (folder / "broken.text.json").write_text("{not json")
    (folder / "Not-A-Name.css").write_text("/* ignored: names are lower case */\n")
    return folder


class PatchStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "patches_state.json"
        self.store = PatchStore(make_patches(root), self.state)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_patches_are_the_css_files_with_good_names_and_start_off(self) -> None:
        self.assertEqual(self.store.names(), ["broken", "cards", "title"])
        self.assertEqual({row["name"]: row["state"] for row in self.store.listing()}, {"broken": "off", "cards": "off", "title": "off"})
        self.assertEqual(self.store.title("cards"), "Rounder cards on the home page")
        self.assertEqual(self.store.css(include_preview=True), "")

    def test_on_is_for_everyone_and_preview_only_for_those_who_see_previews(self) -> None:
        self.store.set_state("cards", "on")
        self.store.set_state("title", "preview")
        everyone, insiders = self.store.css(False), self.store.css(True)
        self.assertIn("border-radius: 16px", everyone)
        self.assertNotIn("letter-spacing", everyone)
        self.assertIn("letter-spacing", insiders)
        self.assertEqual(self.store.text(False), [])
        self.assertEqual(self.store.text(True), [{"page": "home", "selector": "h1", "text": "Alchemy, together", "patch": "title"}])

    def test_the_state_survives_a_new_store_and_off_removes_the_entry(self) -> None:
        self.store.set_state("cards", "on")
        again = PatchStore(self.store.dir, self.state)
        self.assertEqual(again.state("cards"), "on")
        again.set_state("cards", "off")
        self.assertEqual(json.loads(self.state.read_text()), {})

    def test_unknown_patches_and_states_are_refused(self) -> None:
        for name, state in (("nope", "on"), ("../accounts", "on"), ("Not-A-Name", "on"), ("cards", "maybe"), (None, "on")):
            with self.assertRaises(PatchError):
                self.store.set_state(name, state)

    def test_a_broken_text_file_is_reported_and_skipped_not_fatal(self) -> None:
        self.store.set_state("broken", "on")
        row = next(r for r in self.store.listing() if r["name"] == "broken")
        self.assertIn("not valid JSON", row["problem"])
        self.assertEqual(self.store.text(True), [])
        self.assertIn("patch broken", self.store.css(False))

    def test_an_unreadable_state_file_means_everything_off(self) -> None:
        self.state.write_text("{garbage")
        self.assertEqual(self.store.css(True), "")

    def test_the_command_line(self) -> None:
        out = io.StringIO()
        args = ["--dir", str(self.store.dir), "--state-file", str(self.state)]
        with contextlib.redirect_stdout(out):
            self.assertEqual(patches.main([*args, "preview", "cards"]), 0)
            self.assertEqual(patches.main([*args, "list"]), 0)
        self.assertIn("preview  cards  (Rounder cards on the home page)", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(patches.main([*args, "on", "nope"]), 1)


class PatchEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES), max_games=10))
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.server.patches = PatchStore(make_patches(root), root / "patches_state.json")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        for name in self.server.patches.names():
            self.server.patches.set_state(name, "off")

    def request(self, method: str, path: str, session: str = "", body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Session": session} if session else {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        ctype = resp.getheader("Content-Type", "")
        conn.close()
        return resp.status, (json.loads(data) if ctype.startswith("application/json") else data.decode()), ctype

    def session(self, name: str, admin: bool = False, designer: bool = False) -> str:
        body = {"username": name, "password": f"password-{name}"}
        status, data, _ = self.request("POST", "/register", body=body)
        if status == 409:
            status, data, _ = self.request("POST", "/login", body=body)
        self.assertEqual(status, 200)
        self.server.accounts.set_admin(name, admin)
        self.server.accounts.set_designer(name, designer)
        return data["session"]

    def test_patches_css_has_on_patches_for_everyone_and_previews_only_for_admins_and_designers(self) -> None:
        self.server.patches.set_state("cards", "on")
        self.server.patches.set_state("title", "preview")
        status, css, ctype = self.request("GET", "/patches.css")
        self.assertEqual((status, ctype), (200, "text/css; charset=utf-8"))
        self.assertIn("border-radius", css)
        self.assertNotIn("letter-spacing", css)
        self.assertNotIn("letter-spacing", self.request("GET", "/patches.css", self.session("player1"))[1])
        for who in (self.session("designer1", designer=True), self.session("admin1", admin=True)):
            self.assertIn("letter-spacing", self.request("GET", "/patches.css", who)[1])
        self.assertEqual(self.request("GET", "/patches/text")[1], {"changes": []})
        self.assertEqual(len(self.request("GET", "/patches/text", self.session("designer1", designer=True))[1]["changes"]), 1)

    def test_admins_and_designers_can_list_and_switch_but_players_cannot(self) -> None:
        self.assertEqual(self.request("GET", "/patches/list")[0], 401)
        player = self.session("player2")
        self.assertEqual(self.request("GET", "/patches/list", player)[0], 403)
        self.assertEqual(self.request("POST", "/patches/set", player, {"name": "cards", "state": "on"})[0], 403)
        self.assertEqual(self.server.patches.state("cards"), "off")
        for who, state in ((self.session("designer2", designer=True), "preview"), (self.session("admin2", admin=True), "on")):
            status, data, _ = self.request("POST", "/patches/set", who, {"name": "cards", "state": state})
            self.assertEqual(status, 200)
            self.assertEqual(next(r for r in data["patches"] if r["name"] == "cards")["state"], state)
        self.assertEqual(self.request("GET", "/patches/list", self.session("designer2", designer=True))[0], 200)

    def test_a_bad_patch_or_state_is_a_400(self) -> None:
        admin = self.session("admin3", admin=True)
        for body in ({"name": "nope", "state": "on"}, {"name": "cards", "state": "sideways"}, {"name": "../x", "state": "on"}, {}):
            self.assertEqual(self.request("POST", "/patches/set", admin, body)[0], 400)


if __name__ == "__main__":
    unittest.main()
