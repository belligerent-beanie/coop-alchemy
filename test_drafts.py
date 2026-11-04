"""Tests for draft recipes, promotion proposals and admin accounts (drafts.py and the /create and /admin routes)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from accounts import AccountStore
from drafts import MAX_DRAFT_RECIPES, MAX_PENDING_PROPOSALS, DraftStore
from games import GameRegistry
from recipes import load_recipes, parse_recipes
from state import GameError
from test_coop import call, login_as, serve

BASE_DIR = Path(__file__).resolve().parent
BOOK = load_recipes(BASE_DIR / "test_recipes.json")
# Pairs of starters that make nothing in the fixture.
FREE = [("air", "air"), ("air", "fire"), ("air", "water"), ("fire", "fire"), ("earth", "earth")]


def status_of(err: Any) -> int:
    return err.exception.status


class DraftStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DraftStore(None)

    def test_combine_uses_real_recipes_then_drafts(self) -> None:
        self.assertEqual([e["id"] for e in self.store.combine("Ann", BOOK, "water", "fire")], ["steam"])
        self.assertEqual(self.store.combine("Ann", BOOK, "air", "air"), [])
        self.store.create("Ann", BOOK, "air", "air", "Wind Storm")
        self.assertEqual([e["label"] for e in self.store.combine("Ann", BOOK, "air", "air")], ["Wind Storm"])
        # Drafts belong to the account that made them.
        self.assertEqual(self.store.combine("Bob", BOOK, "air", "air"), [])
        with self.assertRaises(GameError) as err:
            self.store.combine("Ann", BOOK, "air", "nonsense")
        self.assertEqual(status_of(err), 400)
        with self.assertRaises(GameError):  # Bob has no "Wind Storm"
            self.store.combine("Bob", BOOK, "air", "Wind Storm")

    def test_create_new_element_and_alternative_recipe(self) -> None:
        made = self.store.create("Ann", BOOK, "air", "air", "  Wind   Storm ")
        self.assertEqual(made["element"], {"id": "Wind Storm", "label": "Wind Storm", "emoji": "", "draft": True})
        self.assertEqual(made["recipe"]["status"], "draft")
        self.assertTrue(made["recipe"]["new_element"])
        # A name that matches a real element (by label, ignoring case) is another recipe for it.
        alt = self.store.create("Ann", BOOK, "air", "fire", "STEAM")
        self.assertEqual(alt["element"]["id"], "steam")
        self.assertFalse(alt["element"]["draft"])
        self.assertFalse(alt["recipe"]["new_element"])
        # ...and so does a name that matches one of the account's own new elements.
        again = self.store.create("Ann", BOOK, "air", "water", "wind storm")
        self.assertEqual(again["element"]["id"], "Wind Storm")
        self.assertEqual([e["id"] for e in self.store.view("Ann", BOOK)["elements"]], ["Wind Storm"])

    def test_create_rejects_bad_requests(self) -> None:
        cases: list[tuple[tuple[Any, ...], int]] = [
            (("water", "fire", "Anything"), 409),        # a real recipe already exists
            (("air", "air", ""), 400),
            (("air", "air", "   "), 400),
            (("air", "air", "x" * 65), 400),
            (("air", "air", "bad\x00name"), 400),        # control characters are dropped, leaving a fine name...
            (("air", "air", 5), 400),
            (("air", "ghost", "Name"), 400),
            ((None, "air", "Name"), 400),
        ]
        for args, expected in cases:
            if args[2] == "bad\x00name":
                self.assertEqual(self.store.create("Ann", BOOK, *args)["element"]["label"], "badname")
                self.store.delete("Ann", BOOK, "r1")
                continue
            with self.subTest(args=args), self.assertRaises(GameError) as err:
                self.store.create("Ann", BOOK, *args)
            self.assertEqual(status_of(err), expected)
        self.store.create("Ann", BOOK, "air", "air", "One")
        with self.assertRaises(GameError) as err:  # b + a is the same pair
            self.store.create("Ann", BOOK, "air", "air", "Two")
        self.assertEqual(status_of(err), 409)

    def test_limits(self) -> None:
        book = parse_recipes({"starters": ["a"], "elements": {f"e{i}": {"label": f"E{i}"} for i in range(30)} | {"a": {"label": "A"}},
                              "recipes": []})
        ids = list(book.elements)
        made = 0
        for i, x in enumerate(ids):
            for y in ids[i:]:
                if made == MAX_DRAFT_RECIPES:
                    break
                self.store.create("Ann", book, x, y, f"N{made}")
                made += 1
        with self.assertRaises(GameError) as err:
            self.store.create("Ann", book, ids[-1], ids[-2], "One more")
        self.assertEqual(status_of(err), 409)

    def test_delete_rules(self) -> None:
        first = self.store.create("Ann", BOOK, "air", "air", "Gale")["recipe"]["id"]
        second = self.store.create("Ann", BOOK, "Gale", "fire", "Firestorm")["recipe"]["id"]
        with self.assertRaises(GameError) as err:  # Firestorm is built on Gale
            self.store.delete("Ann", BOOK, first)
        self.assertEqual(status_of(err), 409)
        self.store.promote("Ann", BOOK, second, "")
        with self.assertRaises(GameError) as err:  # waiting for an admin
            self.store.delete("Ann", BOOK, second)
        self.assertEqual(status_of(err), 409)
        self.store.handle("Root", "p2", "reject", "not now")
        view = self.store.delete("Ann", BOOK, second)
        self.assertEqual([e["id"] for e in view["elements"]], ["Gale"])
        with self.assertRaises(GameError) as err:  # Gale's own proposal is still waiting
            self.store.delete("Ann", BOOK, first)
        self.assertEqual(status_of(err), 409)
        self.store.handle("Root", "p1", "reject", "")
        self.assertEqual(self.store.delete("Ann", BOOK, first), {"elements": [], "recipes": []})
        with self.assertRaises(GameError) as err:
            self.store.delete("Ann", BOOK, "r99")
        self.assertEqual(status_of(err), 404)

    def test_promote_sends_one_proposal_per_recipe_with_credit(self) -> None:
        self.store.create("Ann", BOOK, "air", "air", "Gale")
        top = self.store.create("Ann", BOOK, "Gale", "fire", "Firestorm")["recipe"]["id"]
        self.store.create("Ann", BOOK, "air", "water", "Mist Cloud")  # unrelated draft, not sent
        sent = self.store.promote("Ann", BOOK, top, "  please  ")
        self.assertEqual(sent["proposals"], ["p1", "p2"])
        self.assertEqual({r["id"]: r["status"] for r in sent["recipes"]},
                         {"r1": "requested", "r2": "requested", "r3": "draft"})
        first, second = self.store.queue(BOOK)
        self.assertEqual([(p["a"]["label"], p["b"]["label"], p["result"]["label"]) for p in (first, second)],
                         [("Air", "Air", "Gale"), ("Gale", "Fire", "Firestorm")])  # ingredients first
        self.assertEqual((first["note"], second["note"]), ("", "please"))  # the note rides on the draft you promoted
        self.assertEqual((first["user"], first["new_element"]), ("Ann", True))
        self.assertEqual([n["label"] for n in second["needs"]], ["Gale"])  # built on a proposal still waiting
        self.assertEqual([e["label"] for e in first["dependents"]["elements"]], ["Firestorm"])
        self.assertEqual(second["dependents"], {"total": 0, "elements": []})
        with self.assertRaises(GameError) as err:
            self.store.promote("Ann", BOOK, top, "")
        self.assertEqual(status_of(err), 409)
        with self.assertRaises(GameError) as err:
            self.store.promote("Bob", BOOK, top, "")  # not Bob's draft
        self.assertEqual(status_of(err), 404)

    def test_dependents_of_a_real_element_and_of_proposals(self) -> None:
        self.store.create("Ann", BOOK, "air", "air", "Gale")
        self.store.create("Ann", BOOK, "Gale", "fire", "Firestorm")
        top = self.store.create("Ann", BOOK, "Firestorm", "water", "Rainstorm")["recipe"]["id"]
        self.store.promote("Ann", BOOK, top, "")
        gale = self.store.queue(BOOK)[0]
        self.assertEqual([(e["label"], e["via"], e["status"]) for e in gale["dependents"]["elements"]],
                         [("Firestorm", "Gale + Fire", "pending"), ("Rainstorm", "Firestorm + Water", "pending")])
        # Another proposal for an element the game already has lists what the game builds on it.
        alt = self.store.create("Bob", BOOK, "air", "fire", "Steam")["recipe"]["id"]  # label of a real element
        self.store.promote("Bob", BOOK, alt, "")
        steam = next(p for p in self.store.queue(BOOK) if p["user"] == "Bob")
        self.assertFalse(steam["new_element"])
        self.assertGreater(steam["dependents"]["total"], 0)
        self.assertTrue(all(e["status"] == "in the game" for e in steam["dependents"]["elements"]))

    def test_players_may_propose_different_results_and_admins_see_the_competition(self) -> None:
        ann = self.store.create("Ann", BOOK, "air", "air", "Gale")["recipe"]["id"]
        bob = self.store.create("Bob", BOOK, "air", "air", "Breeze")["recipe"]["id"]  # same pair, other result
        self.store.promote("Ann", BOOK, ann, "")
        self.store.promote("Bob", BOOK, bob, "")
        by_user = {p["user"]: p for p in self.store.queue(BOOK)}
        self.assertEqual(by_user["Ann"]["competing"],
                         [{"proposal": "p2", "user": "Bob", "status": "pending", "result_label": "Breeze", "same_result": False}])
        self.assertEqual([c["user"] for c in by_user["Bob"]["competing"]], ["Ann"])
        # The admin picks one; the other stays open (with the choice visible) until it is answered too.
        self.store.handle("Root", "p2", "approve", "Better name")
        (ann_left,) = self.store.queue(BOOK)
        self.assertEqual(ann_left["competing"][0]["status"], "approved")
        self.assertEqual(self.store.handle("Root", "p1", "reject", "Bob's was better")["status"], "rejected")
        self.assertEqual(self.store.queue(BOOK), [])

    def test_handle_updates_the_authors_drafts_and_can_be_undone(self) -> None:
        rid = self.store.create("Ann", BOOK, "air", "air", "Gale")["recipe"]["id"]
        self.store.promote("Ann", BOOK, rid, "")
        with self.assertRaises(GameError) as err:
            self.store.handle("Root", "p1", "maybe", "")
        self.assertEqual(status_of(err), 400)
        with self.assertRaises(GameError) as err:
            self.store.handle("Root", "p9", "approve", "")
        self.assertEqual(status_of(err), 404)
        self.assertEqual(self.store.handle("Root", "p1", "reject", "Too similar to Wind"), {"id": "p1", "status": "rejected"})
        (mine,) = self.store.view("Ann", BOOK)["recipes"]
        self.assertEqual((mine["status"], mine["note"]), ("rejected", "Too similar to Wind"))
        with self.assertRaises(GameError) as err:  # answered already, by Root
            self.store.handle("Other", "p1", "approve", "")
        self.assertEqual(status_of(err), 409)
        # Undo: only the admin who answered can take it back, and the draft goes back to waiting.
        with self.assertRaises(GameError) as err:
            self.store.reopen("Other", "p1")
        self.assertEqual(status_of(err), 403)
        self.assertEqual(self.store.reopen("root", "p1"), {"id": "p1", "status": "pending"})
        (mine,) = self.store.view("Ann", BOOK)["recipes"]
        self.assertEqual((mine["status"], mine["note"]), ("requested", ""))
        with self.assertRaises(GameError) as err:
            self.store.reopen("Root", "p1")
        self.assertEqual(status_of(err), 409)
        self.store.handle("Root", "p1", "reject", "no")
        # A rejected draft can be promoted again, as a new proposal.
        self.assertEqual(self.store.promote("Ann", BOOK, rid, "second try")["proposals"], ["p2"])
        self.store.handle("Root", "p2", "approve", "Added!")
        (mine,) = self.store.view("Ann", BOOK)["recipes"]
        self.assertEqual((mine["status"], mine["note"]), ("approved", "Added!"))
        done = self.store.approved(BOOK)
        self.assertEqual([p["id"] for p in done["proposals"]], ["p2"])
        self.assertEqual(done["snippet"], {"elements": {"Gale": {"label": "Gale", "credit": "Ann"}},
                                           "recipes": [{"a": "air", "b": "air", "result": "Gale", "credit": "Ann"}]})
        self.assertEqual(self.store.queue(BOOK), [])

    def test_waiting_proposals_close_when_the_game_gains_the_recipe(self) -> None:
        rid = self.store.create("Ann", BOOK, "air", "air", "Gale")["recipe"]["id"]
        self.store.promote("Ann", BOOK, rid, "")
        data = json.loads((BASE_DIR / "test_recipes.json").read_text())
        data["recipes"].append({"a": "air", "b": "air", "result": "dust"})
        self.assertEqual(self.store.queue(parse_recipes(data)), [])
        self.assertEqual(self.store.approved(BOOK)["proposals"], [])

    def test_pending_proposal_limit(self) -> None:
        book = parse_recipes({"starters": ["e0"], "elements": {f"e{i}": {"label": f"E{i}"} for i in range(10)},
                              "recipes": []})
        ids = list(book.elements)
        pairs = [(x, y) for i, x in enumerate(ids) for y in ids[i:]][: MAX_PENDING_PROPOSALS + 1]
        rids = [self.store.create("Ann", book, x, y, f"N{i}")["recipe"]["id"] for i, (x, y) in enumerate(pairs)]
        for rid in rids[:MAX_PENDING_PROPOSALS]:
            self.store.promote("Ann", book, rid, "")
        with self.assertRaises(GameError) as err:
            self.store.promote("Ann", book, rids[-1], "")
        self.assertEqual(status_of(err), 409)

    def test_drafts_catch_up_with_the_real_recipes(self) -> None:
        self.store.create("Ann", BOOK, "air", "air", "Gale")
        self.store.create("Ann", BOOK, "Gale", "fire", "Firestorm")
        self.store.create("Ann", BOOK, "air", "fire", "Smoke Signal")
        data = json.loads((BASE_DIR / "test_recipes.json").read_text())
        # An admin added "Gale" (under another id, same label) and the air + fire recipe.
        data["elements"]["gale"] = {"label": "Gale"}
        data["recipes"] += [{"a": "air", "b": "air", "result": "gale"}, {"a": "air", "b": "fire", "result": "dust"}]
        newer = parse_recipes(data)
        view = self.store.view("Ann", newer)
        self.assertEqual([e["id"] for e in view["elements"]], ["Firestorm", "Smoke Signal"])
        self.assertEqual([(r["a"], r["b"], r["result"]) for r in view["recipes"]], [("gale", "fire", "Firestorm")])
        self.assertEqual(view["recipes"][0]["a_label"], "Gale")

    def test_file_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "drafts.json"
            DraftStore(path).create("Ann", BOOK, "air", "air", "Gale")
            self.assertEqual([r["result"] for r in DraftStore(path).view("Ann", BOOK)["recipes"]], ["Gale"])
            self.assertEqual(DraftStore(path).delete_user("ann"), 1)  # account names ignore capitals
            self.assertEqual(DraftStore(path).view("Ann", BOOK)["recipes"], [])
            path.write_text("{broken")
            with self.assertRaises(GameError) as err:
                DraftStore(path).create("Ann", BOOK, "air", "air", "Gale")
            self.assertEqual(status_of(err), 500)
            self.assertEqual(path.read_text(), "{broken")  # never overwritten


class AdminAccountTests(unittest.TestCase):
    def test_admin_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / "accounts.json", iterations=1000)
            store.register("Annie", "password-Annie")
            store.register("Bobby", "password-Bobby")
            self.assertFalse(store.is_admin("Annie"))
            store.set_admin("annie", True)
            self.assertEqual((store.is_admin("Annie"), store.is_admin("Bobby"), store.admins()), (True, False, ["Annie"]))
            store.set_admin("Annie", False)
            self.assertEqual(store.admins(), [])
            with self.assertRaises(GameError):
                store.set_admin("Nobody", True)
            store.set_admin("Annie", True)
            store.set_password("Annie", "another-password")  # keeps the flag
            self.assertTrue(store.is_admin("Annie"))


class BotAccountTests(unittest.TestCase):
    def test_bot_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / "accounts.json", iterations=1000)
            store.register("Robo", "password-Robo")
            store.register("Bobby", "password-Bobby")
            self.assertEqual((store.is_bot("Robo"), store.bots()), (False, []))
            store.set_bot("robo", True)
            self.assertEqual((store.is_bot("Robo"), store.is_bot("Bobby"), store.bots()), (True, False, ["Robo"]))
            self.assertFalse(store.is_admin("Robo"))  # a different flag
            store.set_password("Robo", "another-password")  # keeps the flag
            self.assertTrue(store.is_bot("Robo"))
            store.set_bot("Robo", False)
            self.assertEqual(store.bots(), [])
            with self.assertRaises(GameError):
                store.set_bot("Nobody", True)

    def test_bots_have_no_draft_or_proposal_limit(self) -> None:
        store = DraftStore(None, is_bot=lambda name: name == "Robo")
        book = parse_recipes({"starters": ["a"], "elements": {f"e{i}": {"label": f"E{i}"} for i in range(30)} | {"a": {"label": "A"}},
                              "recipes": []})
        ids = list(book.elements)
        pairs = [(x, y) for i, x in enumerate(ids) for y in ids[i:]][: MAX_DRAFT_RECIPES + 5]
        for name in ("Robo", "Ann"):
            made, refused = 0, 0
            rids = []
            for i, (x, y) in enumerate(pairs):
                try:
                    rids.append(store.create(name, book, x, y, f"{name}{i}")["recipe"]["id"])
                    made += 1
                except GameError as exc:
                    self.assertEqual(exc.status, 409)
                    refused += 1
            promoted = 0
            for rid in rids[: MAX_PENDING_PROPOSALS + 5]:
                try:
                    store.promote(name, book, rid, "")
                    promoted += 1
                except GameError as exc:
                    self.assertEqual(exc.status, 409)
            if name == "Robo":
                self.assertEqual((made, refused, promoted), (MAX_DRAFT_RECIPES + 5, 0, MAX_PENDING_PROPOSALS + 5))
            else:
                self.assertEqual((made, refused, promoted), (MAX_DRAFT_RECIPES, 5, MAX_PENDING_PROPOSALS))


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(BOOK))
        cls.admin = login_as(cls.port, "Rooty")
        cls.server.accounts.set_admin("Rooty", True)
        cls.ann = login_as(cls.port, "Annie")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, session: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        return call(self.port, method, path, body, headers={"X-Session": session})

    def test_pages_and_login_required(self) -> None:
        for path in ("/create", "/admin"):
            self.assertEqual(call(self.port, "GET", path)[0], 200)
        for method, path in (("GET", "/create/data"), ("POST", "/create/combine"), ("GET", "/admin/queue"), ("GET", "/admin/approved"), ("POST", "/admin/reopen"),
                             ("POST", "/admin/handle")):
            self.assertEqual(self.req("", method, path, {} if method == "POST" else None)[0], 401, path)

    def test_me_reports_admins(self) -> None:
        self.assertEqual(self.req(self.admin, "GET", "/me")[1], {"username": "Rooty", "admin": True})
        self.assertEqual(self.req(self.ann, "GET", "/me")[1], {"username": "Annie", "admin": False})

    def test_create_and_promote_flow(self) -> None:
        status, data = self.req(self.ann, "GET", "/create/data")
        self.assertEqual(status, 200)
        self.assertEqual({e["id"] for e in data["elements"]}, set(BOOK.elements))  # everything is unlocked
        self.assertEqual(data["drafts"], {"elements": [], "recipes": []})

        self.assertEqual(self.req(self.ann, "POST", "/create/combine", {"a": "water", "b": "fire"})[1]["results"][0]["id"], "steam")
        self.assertEqual(self.req(self.ann, "POST", "/create/combine", {"a": "air", "b": "air"})[1], {"results": []})
        status, made = self.req(self.ann, "POST", "/create/recipe", {"a": "air", "b": "air", "name": "Gale"})
        self.assertEqual((status, made["element"]["draft"]), (200, True))
        self.assertEqual(self.req(self.ann, "POST", "/create/recipe", {"a": "air", "b": "air", "name": "Again"})[0], 409)
        self.assertEqual(self.req(self.ann, "POST", "/create/recipe", {"a": "air", "b": "fire"})[0], 400)
        # Drafts are private to the account.
        self.assertEqual(self.req(self.admin, "POST", "/create/combine", {"a": "air", "b": "air"})[1], {"results": []})

        status, sent = self.req(self.ann, "POST", "/create/promote", {"id": made["recipe"]["id"], "note": "so windy"})
        self.assertEqual(status, 200)
        self.assertEqual(sent["recipes"][0]["status"], "requested")

        (pid,) = sent["proposals"]
        for method, path in (("GET", "/admin/queue"), ("GET", "/admin/approved")):
            self.assertEqual(self.req(self.ann, method, path)[0], 403, path)
        for path in ("/admin/handle", "/admin/reopen"):
            self.assertEqual(self.req(self.ann, "POST", path, {"id": pid, "action": "approve"})[0], 403, path)
        status, listing = self.req(self.admin, "GET", "/admin/queue")
        self.assertEqual(status, 200)
        (proposal,) = [p for p in listing["proposals"] if p["id"] == pid]
        self.assertEqual((proposal["user"], proposal["note"]), ("Annie", "so windy"))
        self.assertEqual((proposal["a"]["id"], proposal["result"]["label"]), ("air", "Gale"))

        status, done = self.req(self.admin, "POST", "/admin/handle", {"id": pid, "action": "approve", "reply": "Nice"})
        self.assertEqual((status, done["status"]), (200, "approved"))
        status, data = self.req(self.ann, "GET", "/create/data")
        self.assertEqual([(r["status"], r["note"]) for r in data["drafts"]["recipes"]], [("approved", "Nice")])
        self.assertEqual(self.req(self.admin, "POST", "/admin/handle", {"id": pid, "action": "reject"})[0], 409)
        self.assertEqual(self.req(self.admin, "GET", "/admin/queue")[1]["proposals"], [])
        status, approved = self.req(self.admin, "GET", "/admin/approved")
        self.assertEqual(approved["snippet"]["recipes"], [{"a": "air", "b": "air", "result": "Gale", "credit": "Annie"}])

        status, left = self.req(self.ann, "POST", "/create/delete", {"id": made["recipe"]["id"]})
        self.assertEqual((status, left["recipes"]), (200, []))


if __name__ == "__main__":
    unittest.main()
