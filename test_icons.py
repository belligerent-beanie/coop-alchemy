"""The icon studio's server side: who may see every element, and what they get."""

from __future__ import annotations

import http.client
import json
import unittest

from games import GameRegistry
from test_coop import RECIPES, load_recipes, serve


class IconElementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES), max_games=10))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, path: str, session: str = "", body: dict | None = None, method: str = "GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Session": session} if session else {}
        payload = None
        if body is not None:
            payload, headers["Content-Type"], method = json.dumps(body).encode(), "application/json", "POST"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if resp.getheader("Content-Type", "").startswith("application/json") else data)

    def session(self, name: str, admin: bool = False, designer: bool = False) -> str:
        body = {"username": name, "password": f"password-{name}"}
        status, data = self.request("/register", body=body)
        if status == 409:
            status, data = self.request("/login", body=body)
        self.assertEqual(status, 200)
        self.server.accounts.set_admin(name, admin)
        self.server.accounts.set_designer(name, designer)
        return data["session"]

    def test_only_designers_and_admins_see_every_element(self) -> None:
        self.assertEqual(self.request("/icons/elements")[0], 401)
        self.assertEqual(self.request("/icons/elements", self.session("iconplayer"))[0], 403)
        for who in (self.session("icondesigner", designer=True), self.session("iconadmin", admin=True)):
            status, data = self.request("/icons/elements", who)
            self.assertEqual(status, 200)
            ids = {e["id"] for e in data["elements"]}
            self.assertEqual(ids, set(self.server.registry.book.elements))   # found or not: all of them
            sample = data["elements"][0]
            self.assertEqual(set(sample) - {"pic"}, {"id", "label", "emoji", "file"})
            self.assertTrue(sample["file"].endswith(".png"))

    def test_the_page_itself_is_served(self) -> None:
        status, body = self.request("/icons")
        self.assertEqual(status, 200)
        self.assertIn(b"Icon studio", body)


if __name__ == "__main__":
    unittest.main()
