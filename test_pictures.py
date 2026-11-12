"""Element pictures: served only at secret addresses, and only sent with the elements a player may see."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from games import GameRegistry
from pictures import PictureIndex, slugify
from recipes import RecipeBook, parse_recipes
from state import GameState
from test_coop import RECIPES, call, load_recipes, login_as, serve

PNG = b"\x89PNG\r\n\x1a\n" + b"fake picture"


def book_with(*elements: tuple[str, str]) -> RecipeBook:
    data = {"starters": [elements[0][0]],
            "elements": {eid: {"label": label} for eid, label in elements},
            "recipes": []}
    return parse_recipes(data)


class SlugTests(unittest.TestCase):
    def test_slugs_match_the_file_names_the_pages_used_to_ask_for(self) -> None:
        self.assertEqual(slugify("Alarm Clock"), "alarm-clock")
        self.assertEqual(slugify("Philosopher's Stone"), "philosophers-stone")
        self.assertEqual(slugify("Café au lait"), "cafe-au-lait")
        self.assertEqual(slugify("  --Rock 'n' Roll!  "), "rock-n-roll")
        self.assertEqual(slugify(None), "")


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        for name in ("air", "alarm-clock"):
            (self.folder / f"{name}.png").write_bytes(PNG)
        self.book = book_with(("air", "Air"), ("clock", "Alarm Clock"), ("mud", "Mud"))
        self.now = [0.0]
        self.index = PictureIndex(self.folder, self.book, secret=b"k" * 32, clock=lambda: self.now[0])

    def test_pictures_are_found_by_id_or_label_and_the_rest_have_none(self) -> None:
        self.assertTrue(self.index.url_for("air"))
        self.assertTrue(self.index.url_for("clock"))      # by its label
        self.assertIsNone(self.index.url_for("mud"))
        self.assertIsNone(self.index.url_for("no-such-element"))

    def test_addresses_hide_the_name_and_resolve_back_to_the_file(self) -> None:
        url = self.index.url_for("air")
        assert url
        self.assertRegex(url, r"^/pic/[0-9a-f]{32}\.png$")
        self.assertNotIn("air", url)
        token = url[len("/pic/"):-len(".png")]
        self.assertEqual(self.index.path_for(token), self.folder / "air.png")
        for bad in ("air", "../air", "", "0" * 32, token.upper()):
            self.assertIsNone(self.index.path_for(bad), bad)

    def test_each_secret_gives_different_addresses(self) -> None:
        other = PictureIndex(self.folder, self.book, secret=b"z" * 32)
        self.assertNotEqual(self.index.url_for("air"), other.url_for("air"))
        self.assertEqual(self.index.url_for("air"), PictureIndex(self.folder, self.book, secret=b"k" * 32).url_for("air"))

    def test_a_new_file_is_picked_up_without_a_restart(self) -> None:
        self.assertIsNone(self.index.url_for("mud"))
        (self.folder / "mud.png").write_bytes(PNG)
        self.assertIsNone(self.index.url_for("mud"))   # looked at again only every few seconds
        self.now[0] += 10
        self.assertTrue(self.index.url_for("mud"))

    def test_a_missing_folder_just_means_no_pictures(self) -> None:
        gone = PictureIndex(self.folder / "nowhere", self.book)
        self.assertIsNone(gone.url_for("air"))
        self.assertEqual(gone.all_urls(), {})


class GameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = load_recipes(RECIPES)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for eid in self.book.elements:   # a picture for every element, so any leak would show
            (Path(self.tmp.name) / f"{slugify(eid)}.png").write_bytes(PNG)
        self.index = PictureIndex(self.tmp.name, self.book)

    def test_nothing_about_an_unfound_elements_picture_reaches_a_player(self) -> None:
        game = GameState(self.book, rng=random.Random(1), pictures=self.index)
        player = game.join("Alice")
        sub = game.subscribe(player.token)
        assert sub
        seen = sub.messages.get_nowait() + json.dumps(game.journal(player.token))
        for eid in self.book.elements:
            token = self.index.url_for(eid).split("/")[-1][:-4]   # type: ignore[union-attr]
            if eid in game.discovered():
                self.assertIn(token, seen)
            else:
                self.assertNotIn(token, seen, eid)

    def test_a_found_element_comes_with_its_picture(self) -> None:
        game = GameState(self.book, rng=random.Random(1), pictures=self.index)
        player = game.join("Alice")
        first = game.add_tile(player.token, "water", 0.2, 0.2)
        other = game.add_tile(player.token, "fire", 0.6, 0.6)
        game.grab_tile(player.token, first["id"])
        result = game.combine(player.token, other["id"], tile=first["id"], x=0.6, y=0.6)
        self.assertEqual(result["results"][0]["pic"], self.index.url_for("steam"))

    def test_without_an_index_there_is_just_no_pic_field(self) -> None:
        game = GameState(self.book, rng=random.Random(1))
        player = game.join("Alice")
        self.assertTrue(all("pic" not in e for e in game.journal(player.token)["elements"]))


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.book = load_recipes(RECIPES)
        cls.tmp = tempfile.TemporaryDirectory()
        for eid in cls.book.elements:
            (Path(cls.tmp.name) / f"{slugify(eid)}.png").write_bytes(PNG)
        cls.index = PictureIndex(cls.tmp.name, cls.book)
        cls.server, cls.port = serve(GameRegistry(cls.book, pictures=cls.index))
        cls.admin = login_as(cls.port, "Rooty")
        cls.server.accounts.set_admin("Rooty", True)
        cls.ann = login_as(cls.port, "Annie")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path: str, session: str = "") -> tuple[int, object]:
        return call(self.port, "GET", path, headers={"X-Session": session})

    def test_a_picture_is_served_at_its_secret_address_only(self) -> None:
        url = self.index.url_for("water")
        assert url
        status, body = self.get(url)
        self.assertEqual(status, 200)
        self.assertEqual(self.get("/pic/" + "0" * 32 + ".png")[0], 404)
        self.assertEqual(self.get("/pic/water.png")[0], 404)
        self.assertEqual(self.get("/pic/.png")[0], 404)

    def test_the_picture_folder_is_not_served_by_file_name(self) -> None:
        for path in ("/static/elements/air.png", "/static/elements/../elements/air.png", "/static/elements/",
                     "/static/elements/%61ir.png", "/static//elements/air.png"):
            self.assertEqual(self.get(path)[0], 404, path)
        self.assertEqual(self.get("/static/style.css")[0], 200)   # the rest of static/ still is

    def test_the_picture_list_is_for_administrators(self) -> None:
        self.assertEqual(self.get("/images")[0], 401)
        self.assertEqual(self.get("/images", self.ann)[0], 403)
        status, data = self.get("/images", self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(data["pictures"]["water"], self.index.url_for("water"))   # type: ignore[index]
        self.assertNotIn("water.png", json.dumps(data))

    @unittest.skipUnless((Path(__file__).resolve().parent / "recipes.json").is_file() and (Path(__file__).resolve().parent / "static" / "elements").is_dir(),
                         "the private recipe book and pictures are not in this checkout")
    def test_the_real_pictures_folder_has_pictures_under_secret_names_only(self) -> None:
        real = load_recipes(Path(__file__).resolve().parent / "recipes.json")
        index = PictureIndex(Path(__file__).resolve().parent / "static" / "elements", real)
        urls = index.all_urls()
        self.assertGreater(len(urls), 100)
        self.assertEqual(len(set(urls.values())), len(urls))
        self.assertTrue(all(url.startswith("/pic/") and len(url) == len("/pic/") + 32 + len(".png") for url in urls.values()))


if __name__ == "__main__":
    unittest.main()
