"""The icon studio's server side: designs, proposals, approval straight into the game, undo; and who may do what."""

from __future__ import annotations

import base64
import http.client
import json
import struct
import tempfile
import time
import unittest
import zlib
from pathlib import Path

from games import GameRegistry
from iconstore import IconStore, check_doc, icon_png, png_size
from pictures import PictureIndex
from recipes import parse_recipes
from state import GameError
from test_coop import RECIPES, load_recipes, serve


def make_png(width: int = 128, height: int = 128, rgba: tuple[int, int, int, int] = (47, 58, 74, 255)) -> bytes:
    """A real PNG of one colour, made with the standard library."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgba) * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


DOC = {"version": 1, "layers": [{"id": "a", "name": "Shapes", "kind": "shapes", "visible": True, "shapes": [{"type": "rect"}]}],
       "reference": None}


class CheckTests(unittest.TestCase):
    def test_pngs_are_checked(self) -> None:
        self.assertEqual(png_size(make_png(128, 128)), (128, 128))
        self.assertEqual(icon_png(data_url(make_png())), make_png())
        for bad in ("not a url", "data:image/gif;base64,R0lG", data_url(b"\x89PNG\r\n\x1a\nshort"), data_url(make_png(64, 64)),
                    data_url(make_png()[:-12]), "data:image/png;base64,***", None):
            with self.assertRaises(GameError, msg=str(bad)[:40]):
                icon_png(bad)

    def test_designs_are_checked(self) -> None:
        self.assertEqual(check_doc(DOC), DOC)
        pixels = {"version": 1, "layers": [{"kind": "pixels", "name": "P", "pixels": data_url(make_png())}]}
        self.assertEqual(check_doc(pixels), pixels)
        for bad in ({}, {"layers": []}, {"layers": [{"kind": "svg"}]}, {"layers": [{"kind": "pixels", "pixels": "javascript:x"}]},
                    {"layers": [{"kind": "shapes", "shapes": "no"}]}, {"layers": [{"kind": "shapes", "shapes": [], "name": "x" * 41}]},
                    {"layers": [{"kind": "shapes", "shapes": []}], "reference": "x"}):
            with self.assertRaises(GameError, msg=json.dumps(bad)[:60]):
                check_doc(bad)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.pictures = root / "elements"
        self.pictures.mkdir()
        self.store = IconStore(root / "icons.json", root / "icon_files")

    def test_save_propose_approve_undo_a_redraw(self) -> None:
        old = make_png(rgba=(255, 0, 0, 255))
        target = self.pictures / "fire.png"
        target.write_bytes(old)
        saved = self.store.save_design("Mia", "fire", DOC, data_url(make_png()))
        self.assertEqual(saved["status"], "draft")
        self.assertEqual(self.store.design("mia", "fire")["doc"], DOC)          # account names ignore capitals
        self.assertEqual(self.store.propose("Mia", "fire", "rounder flame")["status"], "waiting")
        waiting = self.store.queue(lambda e: "/pic/x.png")["waiting"]
        self.assertEqual([(p["element"], p["author"], p["note"], p["current"]) for p in waiting], [("fire", "Mia", "rounder flame", "/pic/x.png")])
        self.store.approve("Rooty", waiting[0]["id"], "lovely", target)
        self.assertEqual(target.read_bytes(), make_png())                       # in the game
        self.assertEqual(self.store.design("Mia", "fire")["status"], "approved")
        recent = self.store.queue(lambda e: None)["recent"]
        self.assertTrue(recent[0]["can_undo"] and recent[0]["replaced"])
        self.assertEqual(self.store.undo("Rooty", waiting[0]["id"], self.pictures), {"restored": True})
        self.assertEqual(target.read_bytes(), old)                              # the old picture is back
        self.assertEqual(self.store.design("Mia", "fire")["status"], "undone")

    def test_undoing_a_new_icon_removes_it_and_a_hand_changed_picture_is_left_alone(self) -> None:
        target = self.pictures / "mud.png"
        self.store.save_design("Mia", "mud", DOC, data_url(make_png()))
        pid = self.store.propose("Mia", "mud", "")["png"] and self.store.queue(lambda e: None)["waiting"][0]["id"]
        self.store.approve("Rooty", pid, "", target)
        target.write_bytes(make_png(rgba=(0, 0, 0, 255)))                       # someone replaced it by hand since
        with self.assertRaises(GameError):
            self.store.undo("Rooty", pid, self.pictures)
        self.assertTrue(target.exists())
        target.write_bytes(make_png())
        self.assertEqual(self.store.undo("Rooty", pid, self.pictures), {"restored": False})
        self.assertFalse(target.exists())                                       # it had no picture before

    def test_only_the_latest_approval_of_an_element_can_be_undone(self) -> None:
        target = self.pictures / "air.png"
        ids = []
        for who in ("Mia", "Zed"):
            self.store.save_design(who, "air", DOC, data_url(make_png()))
            self.store.propose(who, "air", "")
            ids.append(next(p["id"] for p in self.store.queue(lambda e: None)["waiting"] if p["author"] == who))
            self.store.approve("Rooty", ids[-1], "", target)
        with self.assertRaises(GameError):
            self.store.undo("Rooty", ids[0], self.pictures)
        self.assertEqual([p["can_undo"] for p in self.store.queue(lambda e: None)["recent"]], [True, False])

    def test_reject_and_reply_and_editing_after_proposing(self) -> None:
        self.store.save_design("Mia", "air", DOC, data_url(make_png()))
        self.store.propose("Mia", "air", "")
        self.assertFalse(self.store.design("Mia", "air")["changed"])
        self.store.save_design("Mia", "air", DOC, data_url(make_png(rgba=(1, 2, 3, 255))))
        self.assertEqual((self.store.design("Mia", "air")["status"], self.store.design("Mia", "air")["changed"]), ("waiting", True))
        self.store.propose("Mia", "air", "again")                               # replaces the waiting one
        waiting = self.store.queue(lambda e: None)["waiting"]
        self.assertEqual(len(waiting), 1)
        self.store.reject("Rooty", waiting[0]["id"], "too busy at 28 px")
        design = self.store.design("Mia", "air")
        self.assertEqual((design["status"], design["reply"]), ("rejected", "too busy at 28 px"))
        self.store.save_design("Mia", "air", DOC, data_url(make_png()))
        self.assertEqual(self.store.design("Mia", "air")["status"], "draft")    # working on it again
        with self.assertRaises(GameError):
            self.store.reject("Rooty", waiting[0]["id"], "")                    # already handled

    def test_nothing_to_propose_and_files_by_name_only(self) -> None:
        with self.assertRaises(GameError):
            self.store.propose("Mia", "air", "")
        saved = self.store.save_design("Mia", "air", DOC, data_url(make_png()))
        self.assertEqual(self.store.file(saved["png"]), make_png())
        for name in ("../icons", "d-", "x-0123456789ab", saved["png"] + "/..", None):
            self.assertIsNone(self.store.file(name))

    def test_deleting_an_account_deletes_its_designs_but_keeps_proposals(self) -> None:
        self.store.save_design("Mia", "air", DOC, data_url(make_png()))
        self.store.save_design("Mia", "fire", DOC, data_url(make_png()))
        self.store.propose("Mia", "air", "")
        self.assertEqual(self.store.delete_user("MIA"), 2)
        self.assertEqual(self.store.mine("Mia"), [])
        self.assertEqual(len(self.store.queue(lambda e: None)["waiting"]), 1)


class PreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = IconStore(None, None)
        for who in ("Mia", "Zed"):
            self.store.save_design(who, "air", DOC, data_url(make_png()))
            self.store.propose(who, "air", "")
        self.ids = {p["author"]: p["id"] for p in self.store.queue(lambda e: None)["waiting"]}

    def test_its_designer_or_an_admin_previews_and_only_one_per_element(self) -> None:
        with self.assertRaises(GameError):
            self.store.set_preview("Zed", False, self.ids["Mia"], True)          # not his proposal
        self.store.set_preview("Mia", False, self.ids["Mia"], True)
        self.assertEqual(set(self.store.previews()), {"air"})
        self.assertTrue(self.store.design("Mia", "air")["preview"])
        self.store.set_preview("Rooty", True, self.ids["Zed"], True)             # an admin switches to Zed's
        self.assertFalse(self.store.design("Mia", "air")["preview"])
        self.assertTrue(self.store.design("Zed", "air")["preview"])
        self.assertEqual(self.store.previews()["air"], self.store.queue(lambda e: None)["waiting"][1]["png"])

    def test_a_preview_ends_when_the_proposal_is_handled(self) -> None:
        self.store.set_preview("Mia", False, self.ids["Mia"], True)
        self.store.reject("Rooty", self.ids["Mia"], "")
        self.assertEqual(self.store.previews(), {})
        with self.assertRaises(GameError):
            self.store.set_preview("Mia", False, self.ids["Mia"], True)          # only waiting ones


class ReplacedPictureTests(unittest.TestCase):
    def test_a_replaced_picture_gets_a_new_address_and_the_old_one_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "air.png").write_bytes(make_png(rgba=(255, 0, 0, 255)))
            index = PictureIndex(folder, parse_recipes({"starters": ["air"], "elements": {"air": {"label": "Air"}}, "recipes": []}))
            before = index.url_for("air")
            time.sleep(0.01)
            (folder / "air.png").write_bytes(make_png())
            index.refresh()
            after = index.url_for("air")
            self.assertNotEqual(before, after)                                  # browsers fetch it again
            for url in (before, after):                                         # and an open page's old address still works
                self.assertEqual(index.path_for(url.split("/")[-1][:-4]).read_bytes(), make_png())


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.folder = root / "elements"
        cls.folder.mkdir()
        book = load_recipes(RECIPES)
        cls.element = sorted(book.elements)[0]
        cls.index = PictureIndex(cls.folder, book)
        cls.server, cls.port = serve(GameRegistry(book, max_games=10, pictures=cls.index))
        cls.server.icons = IconStore(root / "icons.json", root / "icon_files")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, path: str, session: str = "", body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"X-Session": session} if session else {}
        payload, method = None, "GET"
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
            self.assertEqual({e["id"] for e in data["elements"]}, set(self.server.registry.book.elements))
            self.assertEqual(set(data["elements"][0]) - {"pic"}, {"id", "label", "emoji", "file"})

    def test_the_page_itself_is_served(self) -> None:
        status, body = self.request("/icons")
        self.assertEqual(status, 200)
        self.assertIn(b"Icon studio", body)
        status, body = self.request("/guides")
        self.assertEqual(status, 200)
        self.assertIn(b"Guides", body)
        status, body = self.request("/icons/set")
        self.assertEqual(status, 200)
        self.assertIn(b"Icon set", body)

    def test_a_designer_saves_and_proposes_and_an_admin_approves_it_into_the_game(self) -> None:
        designer, admin, player = self.session("drawer", designer=True), self.session("boss", admin=True), self.session("plain")
        png = make_png(rgba=(10, 200, 30, 255))
        save = {"element": self.element, "doc": DOC, "png": data_url(png)}
        self.assertEqual(self.request("/icons/save", player, save)[0], 403)
        self.assertEqual(self.request("/icons/save", designer, {**save, "element": "no-such-thing"})[0], 404)
        self.assertEqual(self.request("/icons/save", designer, {**save, "png": data_url(make_png(32, 32))})[0], 400)
        status, data = self.request("/icons/save", designer, save)
        self.assertEqual((status, data["design"]["status"]), (200, "draft"))
        self.assertEqual(self.request(f"/icons/design?element={self.element}", designer)[1]["design"]["doc"], DOC)
        self.assertEqual(self.request(f"/icons/design?element={self.element}", player)[0], 403)
        self.assertEqual(self.request(f"/icons/file/{data['design']['png']}.png", designer)[1], png)
        self.assertEqual(self.request(f"/icons/file/{data['design']['png']}.png", player)[0], 403)
        self.assertEqual(self.request("/icons/propose", designer, {"element": self.element, "note": "hi"})[1]["design"]["status"], "waiting")
        self.assertEqual(self.request("/icons/queue", designer)[0], 403)                 # deciding is for administrators
        queue = self.request("/icons/queue", admin)[1]
        proposal = next(p for p in queue["waiting"] if p["author"] == "drawer")
        self.assertEqual(self.request("/icons/handle", designer, {"id": proposal["id"], "action": "approve"})[0], 403)
        self.assertIsNone(self.index.url_for(self.element))
        status, _ = self.request("/icons/handle", admin, {"id": proposal["id"], "action": "approve", "reply": "yes"})
        self.assertEqual(status, 200)
        url = self.index.url_for(self.element)                                              # in the game at once
        self.assertTrue(url)
        self.assertEqual(self.request(url, player)[1], png)
        self.assertEqual(self.request("/icons/mine", designer)[1]["designs"][0]["status"], "approved")
        status, data = self.request("/icons/undo", admin, {"id": proposal["id"]})
        self.assertEqual((status, data), (200, {"restored": False}))
        self.assertIsNone(self.index.url_for(self.element))                                 # it had none before

    def test_previews_reach_admins_and_designers_only(self) -> None:
        designer, other = self.session("previewer", designer=True), self.session("otherdesigner", designer=True)
        player = self.session("previewplayer")
        self.request("/icons/save", designer, {"element": self.element, "doc": DOC, "png": data_url(make_png())})
        design = self.request("/icons/propose", designer, {"element": self.element})[1]["design"]
        self.assertEqual(self.request("/icons/preview", other, {"id": design["proposal"], "on": True})[0], 403)
        self.assertEqual(self.request("/icons/preview", player, {"id": design["proposal"], "on": True})[0], 403)
        self.assertEqual(self.request("/icons/preview", designer, {"id": design["proposal"], "on": True})[1], {"preview": True})
        for who, expected in (("", {}), (player, {}), (other, {self.element}), (designer, {self.element})):
            status, data = self.request("/icons/previews", who)
            self.assertEqual((status, set(data["previews"])), (200, set(expected)), who)
        url = self.request("/icons/previews", other)[1]["previews"][self.element]
        self.assertEqual(self.request(url, player)[0], 403)                      # the file itself is not for players either
        self.assertTrue(self.request("/icons/mine", designer)[1]["designs"][0]["preview"])

    def test_a_big_design_is_accepted_but_only_from_designers_and_only_on_save(self) -> None:
        designer = self.session("drawer2", designer=True)
        big = {"version": 1, "layers": [{"kind": "pixels", "name": f"P{i}", "pixels": data_url(make_png())} for i in range(8)]}
        status, _ = self.request("/icons/save", designer, {"element": self.element, "doc": big, "png": data_url(make_png())})
        self.assertEqual(status, 200)
        status, _ = self.request("/icons/propose", designer, {"element": self.element, "note": "x" * 5000})
        self.assertEqual(status, 400)                                                       # every other route keeps the small limit


if __name__ == "__main__":
    unittest.main()
