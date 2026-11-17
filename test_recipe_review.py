"""Tests for the temporary once-over of every recipe (recipe_review.py and the /admin/recipes routes)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from games import GameRegistry
from recipe_review import RecipeReview, every_recipe, recipe_id
from recipes import load_recipes
from state import GameError
from test_coop import call, login_as, serve

BASE_DIR = Path(__file__).resolve().parent
BOOK = load_recipes(BASE_DIR / "test_recipes.json")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.review = RecipeReview(None)
        self.total = len(every_recipe(BOOK))

    def first(self) -> str:
        return self.review.queue(BOOK)["cards"][0]["id"]

    def test_every_recipe_is_a_card_and_the_counts_start_at_zero(self) -> None:
        q = self.review.queue(BOOK, limit=1000)
        self.assertEqual((q["total"], q["left"], q["ok"], q["flagged"]), (self.total, self.total, 0, 0))
        self.assertEqual(len(q["cards"]), min(self.total, 100))
        self.assertEqual(len({c["id"] for c in q["cards"]}), len(q["cards"]))

    def test_the_id_ignores_which_way_round_the_pair_is_written(self) -> None:
        self.assertEqual(recipe_id("water", "fire", "steam"), recipe_id("fire", "water", "steam"))

    def test_answered_recipes_leave_the_queue_and_are_counted(self) -> None:
        a, b = (c["id"] for c in self.review.queue(BOOK)["cards"][:2])
        self.review.handle("Rooty", BOOK, a, "ok", None)
        self.review.handle("Rooty", BOOK, b, "flag", "wrong ingredient")
        q = self.review.queue(BOOK)
        self.assertEqual((q["left"], q["ok"], q["flagged"]), (self.total - 2, 1, 1))
        self.assertNotIn(a, [c["id"] for c in q["cards"]])
        flagged = self.review.flagged(BOOK)
        self.assertEqual([(f["id"], f["note"], f["by"]) for f in flagged], [(b, "wrong ingredient", "Rooty")])

    def test_a_recipe_is_answered_once(self) -> None:
        rid = self.first()
        self.review.handle("Rooty", BOOK, rid, "ok", None)
        with self.assertRaises(GameError) as err:
            self.review.handle("Sam", BOOK, rid, "flag", None)
        self.assertEqual(err.exception.status, 409)

    def test_bad_answers_are_refused(self) -> None:
        for rid, verdict, note, status in ((self.first(), "maybe", None, 400), (self.first(), "ok", 5, 400),
                                           ("not a recipe", "ok", None, 404), (None, "ok", None, 404)):
            with self.assertRaises(GameError) as err:
                self.review.handle("Rooty", BOOK, rid, verdict, note)
            self.assertEqual(err.exception.status, status, (rid, verdict, note))

    def test_undo_is_for_your_own_answer_only(self) -> None:
        rid = self.first()
        self.review.handle("Rooty", BOOK, rid, "flag", "x")
        with self.assertRaises(GameError) as err:
            self.review.undo("Sam", rid)
        self.assertEqual(err.exception.status, 403)
        self.review.undo("rooty", rid)   # account names compare without regard to case
        self.assertEqual(self.review.queue(BOOK)["left"], self.total)
        with self.assertRaises(GameError) as err:
            self.review.undo("Rooty", rid)
        self.assertEqual(err.exception.status, 409)

    def test_comments_on_ok_recipes_are_listed_apart_from_flags(self) -> None:
        a, b, c = (card["id"] for card in self.review.queue(BOOK)["cards"][:3])
        self.review.handle("Rooty", BOOK, a, "ok", "check the spelling")
        self.review.handle("Rooty", BOOK, b, "ok", "")
        self.review.handle("Rooty", BOOK, c, "flag", "wrong")
        self.assertEqual([(x["id"], x["note"]) for x in self.review.commented(BOOK)], [(a, "check the spelling")])
        self.assertEqual([x["id"] for x in self.review.flagged(BOOK)], [c])

    def test_a_long_note_is_cut_and_control_characters_dropped(self) -> None:
        rid = self.first()
        self.review.handle("Rooty", BOOK, rid, "flag", "a\x00b" + "x" * 1000)
        note = self.review.flagged(BOOK)[0]["note"]
        self.assertEqual((len(note), note[:2]), (300, "ab"))

    def test_verdicts_survive_a_restart_and_a_damaged_file_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipe_review.json"
            rid = RecipeReview(path).queue(BOOK)["cards"][0]["id"]
            RecipeReview(path).handle("Rooty", BOOK, rid, "ok", None)
            self.assertEqual(RecipeReview(path).queue(BOOK)["ok"], 1)
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(GameError) as err:
                RecipeReview(path).handle("Rooty", BOOK, rid, "ok", None)
            self.assertEqual(err.exception.status, 500)
            self.assertEqual(path.read_text(encoding="utf-8"), "{not json")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(BOOK))
        cls.admin = login_as(cls.port, "Rooty")
        cls.server.accounts.set_admin("Rooty", True)
        cls.player = login_as(cls.port, "Annie")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, session: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        return call(self.port, method, path, body, headers={"X-Session": session})

    def test_login_and_admin_are_required(self) -> None:
        routes = (("GET", "/admin/recipes/queue"), ("GET", "/admin/recipes/flagged"),
                  ("POST", "/admin/recipes/handle"), ("POST", "/admin/recipes/undo"))
        for method, path in routes:
            body = {} if method == "POST" else None
            self.assertEqual(self.req("", method, path, body)[0], 401, path)
            self.assertEqual(self.req(self.player, method, path, body)[0], 403, path)

    def test_the_page_is_served(self) -> None:
        self.assertEqual(call(self.port, "GET", "/admin/recipes")[0], 200)

    def test_review_flow(self) -> None:
        status, q = self.req(self.admin, "GET", "/admin/recipes/queue")
        self.assertEqual(status, 200)
        card = q["cards"][0]
        self.assertEqual(set(card), {"id", "a", "b", "result", "also"})
        self.assertEqual(self.req(self.admin, "POST", "/admin/recipes/handle", {"id": card["id"], "verdict": "flag", "note": "odd"})[0], 200)
        self.assertEqual(self.req(self.admin, "POST", "/admin/recipes/handle", {"id": card["id"], "verdict": "ok"})[0], 409)
        flagged = self.req(self.admin, "GET", "/admin/recipes/flagged")[1]["flagged"]
        self.assertEqual([(f["id"], f["note"]) for f in flagged], [(card["id"], "odd")])
        self.assertEqual(self.req(self.admin, "POST", "/admin/recipes/undo", {"id": card["id"]})[1], {"id": card["id"], "verdict": None})
        self.assertEqual(self.req(self.admin, "GET", "/admin/recipes/flagged")[1], {"flagged": [], "commented": []})

    def test_a_comment_can_go_with_either_verdict(self) -> None:
        a, b = (c["id"] for c in self.req(self.admin, "GET", "/admin/recipes/queue")[1]["cards"][:2])
        self.req(self.admin, "POST", "/admin/recipes/handle", {"id": a, "verdict": "ok", "note": "fine, but the name is odd"})
        self.req(self.admin, "POST", "/admin/recipes/handle", {"id": b, "verdict": "ok"})   # no comment: not listed
        data = self.req(self.admin, "GET", "/admin/recipes/flagged")[1]
        self.assertEqual(data["flagged"], [])
        self.assertEqual([(c["id"], c["note"], c["by"]) for c in data["commented"]], [(a, "fine, but the name is odd", "Rooty")])
        for rid in (a, b):
            self.req(self.admin, "POST", "/admin/recipes/undo", {"id": rid})


if __name__ == "__main__":
    unittest.main()
