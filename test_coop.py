"""Tests for the recipe book, the game state and the HTTP/SSE server.

Run:  python3 -m unittest -v test_coop

These tests always use the bundled dummy test_recipes.json (water, fire, earth and
air plus a few dozen combinations), never the real recipes.json, so they give the
same result whichever recipe set the game is running with.
"""
from __future__ import annotations

import http.client
import json
import os
import random
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
import contextlib
import io

from games import GameRegistry, clean_game_name, slugify
from hints import find_element, hint_can_make, hint_recipe, hint_try, new_combinations
import accounts_admin
from accounts import AccountStore, LOCKOUT_SECONDS, MAX_FAILURES, SESSION_SECONDS, clean_username
from recipe_flowchart import build_flowchart, html_page, label_text, needed_for
from recipe_report import build_report
from saves import SaveStore
from recipes import RecipeError, depths, find_problems, load_recipes, parse_recipes, reachable
from server import AlchemyServer, BASE_DIR
from throttle import Throttle
from state import DJINN, MAX_TILES, PALETTE, RECENT_SECONDS, TIME_UNLOCK_AT, UNLOCKED_BY, GameError, GameState

RECIPES = BASE_DIR / "test_recipes.json"  # fixed test data, independent of the real recipes.json


def parse_event(text: str) -> tuple[str, Any]:
    name, data = "message", ""
    for line in text.strip().splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            data = line[6:]
    return name, json.loads(data)


def drain(sub: Any) -> list[tuple[str, Any]]:
    out = []
    while not sub.messages.empty():
        out.append(parse_event(sub.messages.get_nowait()))
    return out


def names(events: list[tuple[str, Any]]) -> list[str]:
    return [name for name, _ in events]


class RecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = load_recipes(RECIPES)

    def test_dummy_recipes_are_fully_reachable(self) -> None:
        self.assertEqual(reachable(self.book), set(self.book.elements))

    def test_combine_is_order_independent(self) -> None:
        self.assertEqual(self.book.combine("water", "fire"), ("steam",))
        self.assertEqual(self.book.combine("fire", "water"), ("steam",))
        self.assertEqual(self.book.combine("water", "air"), ())

    def test_emoji_is_optional(self) -> None:
        data = {
            "starters": ["a", "b"],
            "elements": {"a": {"label": "Alpha"}, "b": {"label": "Beta", "emoji": ""}, "c": {"label": "Gamma"}},
            "recipes": [{"a": "a", "b": "b", "result": "c"}],
        }
        self.assertEqual(find_problems(data), [])
        book = parse_recipes(data)
        self.assertEqual([e.emoji for e in book.elements.values()], ["", "", ""])
        state = GameState(book)
        p = state.join("P")
        tile = state.add_tile(p.token, "b", 0.5, 0.5)
        res = state.combine(p.token, tile["id"], element="a")
        self.assertEqual(res["results"][0]["emoji"], "")
        bad = dict(data, elements={"a": {"label": "Alpha", "emoji": 5}})
        self.assertIn("'emoji' must be a string", "\n".join(find_problems(bad)))

    def test_credit_marks_player_made_elements(self) -> None:
        data = {
            "starters": ["a", "b"],
            "elements": {"a": {"label": "Alpha"}, "b": {"label": "Beta"}, "c": {"label": "Gamma", "credit": " Ann "}},
            "recipes": [{"a": "a", "b": "b", "result": "c", "credit": "Ann"}],
        }
        self.assertEqual(find_problems(data), [])
        book = parse_recipes(data)
        self.assertEqual([e.credit for e in book.elements.values()], ["", "", "Ann"])
        state = GameState(book)
        p = state.join("P")
        tile = state.add_tile(p.token, "a", 0.5, 0.5)
        made = state.combine(p.token, tile["id"], element="b")["results"][0]
        self.assertEqual(made["credit"], "Ann")
        self.assertNotIn("credit", state._element_json("a"))  # original recipes carry no mark
        bad = dict(data, elements={"a": {"label": "Alpha", "credit": 5}})
        self.assertIn("'credit' must be a string", "\n".join(find_problems(bad)))

    def test_unknown_names_get_suggestions(self) -> None:
        data = json.loads(RECIPES.read_text(encoding="utf-8"))
        data["recipes"].append({"a": "watr", "b": "fire", "result": "steam"})
        self.assertIn("did you mean 'water'", "\n".join(find_problems(data)))

    def test_repeated_recipes_are_cleaned_up_not_rejected(self) -> None:
        self.assertEqual(self.book.notes, ())
        data = {
            "starters": ["a", "b"],
            "elements": {k: {"label": k} for k in "abcde"},
            "recipes": [
                {"a": "a", "b": "b", "result": "c"},
                {"a": "b", "b": "a", "result": "c"},
                {"a": "a", "b": "b", "result": "d"},
                {"a": "b", "b": "a", "result": ["c", "e"]},
            ],
        }
        self.assertEqual(find_problems(data), [])
        book = parse_recipes(data)
        self.assertEqual(book.combine("a", "b"), ("c", "d", "e"))
        self.assertEqual(len(book.lookup), 1)
        self.assertEqual(len(book.notes), 4)

    def test_ids_may_contain_spaces_capitals_and_punctuation(self) -> None:
        data = {
            "starters": ["Fire!", "Water (H2O)"],
            "elements": {"Fire!": {"label": "Fire"}, "Water (H2O)": {"label": "Water"}, "Big Steam": {"label": "S"}},
            "recipes": [{"a": "Fire!", "b": "Water (H2O)", "result": "Big Steam"}],
        }
        self.assertEqual(find_problems(data), [])

    def test_bad_ids_are_rejected(self) -> None:
        def problems(eid: str) -> str:
            data = {"starters": [eid], "elements": {eid: {"label": "x"}}, "recipes": []}
            return "\n".join(find_problems(data))

        self.assertIn("empty", problems("  "))
        self.assertIn("whitespace", problems("pad "))
        self.assertIn("control characters", problems("ta\tb"))
        self.assertIn("at most", problems("x" * 65))

    def test_case_only_collisions_are_rejected(self) -> None:
        data = {
            "starters": ["Fire"],
            "elements": {"Fire": {"label": "a"}, "fire": {"label": "b"}},
            "recipes": [],
        }
        self.assertIn("capitalisation", "\n".join(find_problems(data)))

    def test_invalid_file_raises(self) -> None:
        with self.assertRaises(RecipeError):
            parse_recipes({"starters": [], "elements": {}, "recipes": []})


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = load_recipes(RECIPES)
        self.state = GameState(self.book)
        self.alice = self.state.join("Alice")
        self.bob = self.state.join("Bob")
        self.sub_a = self.state.subscribe(self.alice.token)
        self.sub_b = self.state.subscribe(self.bob.token)
        assert self.sub_a and self.sub_b
        drain(self.sub_a)
        drain(self.sub_b)

    def add(self, who: Any, element: str, x: float = 0.5, y: float = 0.5) -> dict[str, Any]:
        return self.state.add_tile(who.token, element, x, y)

    # ---- joining and snapshots ----

    def test_join_validates_names(self) -> None:
        with self.assertRaises(GameError):
            self.state.join("   ")
        with self.assertRaises(GameError):
            self.state.join(123)
        self.assertEqual(self.state.join("  Zed \n  Q ").name, "Zed Q")
        self.assertEqual(len(self.state.join("x" * 100).name), 24)

    def test_unknown_token_cannot_subscribe_or_act(self) -> None:
        self.assertIsNone(self.state.subscribe("nope"))
        with self.assertRaises(GameError) as ctx:
            self.state.add_tile("nope", "water", 0.5, 0.5)
        self.assertEqual(ctx.exception.status, 403)

    def test_snapshot_includes_self_colours_and_starters(self) -> None:
        carol = self.state.join("Carol")
        sub = self.state.subscribe(carol.token)
        assert sub
        name, snap = drain(sub)[0]
        self.assertEqual(name, "snapshot")
        self.assertEqual(snap["you"], carol.pid)
        self.assertEqual({p["name"] for p in snap["players"]}, {"Alice", "Bob", "Carol"})
        colours = [p["color"] for p in snap["players"]]
        self.assertEqual(len(set(colours)), 3)
        self.assertTrue(set(colours) <= set(PALETTE))
        starters = {e["id"]: e["by"] for e in snap["elements"]}
        self.assertEqual(starters, {s: None for s in self.book.starters})
        self.assertIn("players", names(drain(self.sub_a)))  # existing players hear about her

    def test_colour_is_stable_across_reconnects(self) -> None:
        before = {p["pid"]: p["color"] for p in self.state._online_players()}
        self.state.unsubscribe(self.sub_a)
        again = self.state.subscribe(self.alice.token)
        assert again
        after = {p["pid"]: p["color"] for p in self.state._online_players()}
        self.assertEqual(before[self.alice.pid], after[self.alice.pid])

    def test_at_most_five_players(self) -> None:
        for name in ("Carol", "Dave", "Erin"):
            self.state.join(name)
        with self.assertRaises(GameError) as ctx:
            self.state.join("Frank")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("full", ctx.exception.message)

    def test_names_must_be_unique(self) -> None:
        for name in ("Alice", "alice", "  ALICE ", "Al\u200bice".replace("\u200b", "")):
            with self.assertRaises(GameError) as ctx:
                self.state.join(name)
            self.assertEqual(ctx.exception.status, 409)
            self.assertIn("taken", ctx.exception.message)
        self.state.join("Alicia")  # a different name is fine

    def test_joined_but_not_yet_connected_players_hold_a_seat_briefly(self) -> None:
        now = [0.0]
        state = GameState(self.book, clock=lambda: now[0], grace=30.0)
        for i in range(5):
            state.join(f"P{i}")  # nobody has opened a stream yet
        now[0] = 29.0
        with self.assertRaises(GameError):
            state.join("Late")
        now[0] = 31.0
        state.join("Late")  # the pending players never connected, so their seats expired

    def test_disconnecting_frees_the_name_and_seat_immediately(self) -> None:
        for name in ("Carol", "Dave", "Erin"):
            self.state.subscribe(self.state.join(name).token)
        with self.assertRaises(GameError):
            self.state.join("Frank")  # five players are connected
        self.state.unsubscribe(self.sub_a)
        self.assertEqual([p["name"] for p in self.state._online_players()].count("Alice"), 0)
        self.state.join("Alice")  # her name is free again right away

    def test_recently_left(self) -> None:
        now = [0.0]
        state = GameState(self.book, clock=lambda: now[0], grace=30.0)
        ann = state.join("Ann")
        sub = state.subscribe(ann.token)
        assert sub
        self.assertFalse(state.recently_left("Ann"))  # still in the game
        self.assertFalse(state.recently_left("Bob"))  # never was
        now[0] = 100.0
        state.unsubscribe(sub)
        self.assertTrue(state.recently_left("Ann"))
        self.assertTrue(state.recently_left("ANN"))
        self.assertFalse(state.recently_left("Bob"))
        sub = state.subscribe(state.join("Ann").token)  # back in
        self.assertFalse(state.recently_left("Ann"))
        now[0] = 200.0
        assert sub
        state.unsubscribe(sub)
        now[0] = 200.0 + RECENT_SECONDS - 1
        self.assertTrue(state.recently_left("Ann"))
        now[0] = 200.0 + RECENT_SECONDS + 1
        self.assertFalse(state.recently_left("Ann"))  # too long ago

    def test_old_token_cannot_return_to_a_taken_name_or_full_game(self) -> None:
        self.state.unsubscribe(self.sub_a)
        self.state.join("Alice")  # someone else takes the name
        self.assertIsNone(self.state.subscribe(self.alice.token))  # must rejoin, and will be told why

        self.state.unsubscribe(self.sub_b)  # Bob drops, the game fills up without him
        for name in ("Carol", "Dave", "Erin", "Frank", "Gina"):
            try:
                self.state.subscribe(self.state.join(name).token)
            except GameError:
                break
        self.assertIsNone(self.state.subscribe(self.bob.token))

    def test_dropped_token_expires_after_the_grace_period(self) -> None:
        now = [0.0]
        state = GameState(self.book, clock=lambda: now[0], grace=30.0)
        p = state.join("Pat")
        sub = state.subscribe(p.token)
        assert sub
        state.unsubscribe(sub)
        now[0] = 29.0
        self.assertIsNotNone(state.subscribe(p.token))  # quick reconnect keeps the identity
        state.unsubscribe(state._subs[0])
        now[0] = 100.0
        self.assertIsNone(state.subscribe(p.token))  # too long: must join again

    def test_reconnecting_with_the_same_token_keeps_the_slot(self) -> None:
        self.state.unsubscribe(self.sub_a)
        self.assertIsNotNone(self.state.subscribe(self.alice.token))

    # ---- tiles ----

    def test_add_tile_broadcasts_to_others_only(self) -> None:
        tile = self.add(self.alice, "water", 0.25, 0.75)
        self.assertEqual((tile["element"], tile["x"], tile["y"]), ("water", 0.25, 0.75))
        self.assertEqual(names(drain(self.sub_a)), [])
        (name, data), = drain(self.sub_b)
        self.assertEqual((name, data["tile"]["id"]), ("tile_added", tile["id"]))

    def test_add_requires_discovered_element(self) -> None:
        with self.assertRaises(GameError):
            self.add(self.alice, "steam")
        with self.assertRaises(GameError):
            self.add(self.alice, "unicorn")
        with self.assertRaises(GameError):
            self.add(self.alice, 5)  # type: ignore[arg-type]

    def test_coordinates_are_validated_and_clamped(self) -> None:
        tile = self.add(self.alice, "water", -3, 7)
        self.assertEqual((tile["x"], tile["y"]), (0.0, 1.0))
        for bad in ("0.5", None, float("nan"), True, float("inf")):
            with self.assertRaises(GameError):
                self.add(self.alice, "water", bad, 0.5)  # type: ignore[arg-type]

    def test_workspace_is_full(self) -> None:
        for _ in range(MAX_TILES):
            self.add(self.alice, "water")
        with self.assertRaises(GameError) as ctx:
            self.add(self.alice, "water")
        self.assertEqual(ctx.exception.status, 409)

    def test_grab_is_exclusive(self) -> None:
        tile = self.add(self.alice, "water")
        self.state.grab_tile(self.alice.token, tile["id"])
        self.state.grab_tile(self.alice.token, tile["id"])  # re-grab by the holder is fine
        with self.assertRaises(GameError) as ctx:
            self.state.grab_tile(self.bob.token, tile["id"])
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(GameError):
            self.state.remove_tile(self.bob.token, tile["id"])
        with self.assertRaises(GameError):
            self.state.move_tile(self.bob.token, tile["id"], 0.1, 0.1)

    def test_move_and_drop_release_the_tile(self) -> None:
        tile = self.add(self.alice, "water")
        with self.assertRaises(GameError):  # must grab first
            self.state.move_tile(self.alice.token, tile["id"], 0.1, 0.1)
        self.state.grab_tile(self.alice.token, tile["id"])
        drain(self.sub_b)
        self.state.move_tile(self.alice.token, tile["id"], 0.2, 0.3, 0.25, 0.35)
        self.state.drop_tile(self.alice.token, tile["id"], 0.4, 0.5)
        events = drain(self.sub_b)
        self.assertEqual(names(events), ["tile_moved", "tile_dropped"])
        self.assertEqual((events[0][1]["cx"], events[0][1]["cy"]), (0.25, 0.35))
        self.state.grab_tile(self.bob.token, tile["id"])  # free again
        self.assertEqual(self.state.tiles()[0]["x"], 0.4)

    def test_missing_tile_is_404(self) -> None:
        with self.assertRaises(GameError) as ctx:
            self.state.grab_tile(self.alice.token, "t999")
        self.assertEqual(ctx.exception.status, 404)

    def test_clear_tiles(self) -> None:
        self.add(self.alice, "water")
        self.add(self.alice, "fire")
        drain(self.sub_b)
        self.assertEqual(self.state.clear_tiles(self.bob.token), {"cleared": 2})
        self.assertEqual(self.state.tiles(), [])
        events = drain(self.sub_a)
        self.assertEqual(names(events), ["tiles_cleared"])
        self.assertEqual(events[0][1], {"pid": self.bob.pid, "name": "Bob"})  # everyone is told who sent the cat
        self.assertEqual(drain(self.sub_b), [])  # the person who sent it already knows

    # ---- combining ----

    def test_combine_two_tiles_creates_discovery_for_everyone(self) -> None:
        a = self.add(self.alice, "water", 0.2, 0.2)
        b = self.add(self.bob, "fire", 0.6, 0.6)
        drain(self.sub_a)
        drain(self.sub_b)
        self.state.grab_tile(self.alice.token, a["id"])
        res = self.state.combine(self.alice.token, b["id"], tile=a["id"])
        self.assertEqual(res["new"], ["steam"])
        self.assertEqual(sorted(res["removed"]), sorted([a["id"], b["id"]]))
        self.assertEqual(res["tiles"][0]["element"], "steam")
        self.assertEqual((res["tiles"][0]["x"], res["tiles"][0]["y"]), (0.6, 0.6))
        self.assertEqual([t["element"] for t in self.state.tiles()], ["steam"])
        self.assertEqual(self.state.discovered()["steam"], "Alice")

        events_b = drain(self.sub_b)
        self.assertIn("discovered", names(events_b))
        self.assertEqual(names(events_b).count("tile_removed"), 2)
        self.assertEqual(names(events_b).count("tile_added"), 1)
        discovered = dict(events_b)["discovered"]
        self.assertEqual((discovered["by"], discovered["found"]), ("Alice", len(self.book.starters) + 1))
        # The combiner learns about the discovery through the event too, but not about her own tile changes.
        self.assertEqual(names(drain(self.sub_a)), ["discovered"])

    def test_combine_element_with_tile(self) -> None:
        target = self.add(self.alice, "water")
        res = self.state.combine(self.bob.token, target["id"], element="fire")
        self.assertEqual(res["new"], ["steam"])
        self.assertEqual(res["removed"], [target["id"]])
        self.assertEqual(self.state.discovered()["steam"], "Bob")

    def test_combine_rediscovery_is_not_new(self) -> None:
        for _ in range(2):
            t = self.add(self.alice, "water")
            res = self.state.combine(self.alice.token, t["id"], element="fire")
        self.assertEqual(res["new"], [])
        self.assertEqual(self.state.discovered()["steam"], "Alice")  # first discoverer keeps the credit
        self.assertEqual(len(self.state.tiles()), 2)

    def test_self_combination(self) -> None:
        a = self.add(self.alice, "water")
        b = self.add(self.alice, "water")
        self.state.grab_tile(self.alice.token, a["id"])
        res = self.state.combine(self.alice.token, b["id"], tile=a["id"])
        self.assertEqual(res["new"], ["puddle"])

    def test_combine_without_recipe_drops_and_keeps_tiles(self) -> None:
        a = self.add(self.alice, "water")
        b = self.add(self.alice, "air", 0.8, 0.8)
        self.state.grab_tile(self.alice.token, a["id"])
        drain(self.sub_b)
        res = self.state.combine(self.alice.token, b["id"], tile=a["id"], x=0.7, y=0.7)
        self.assertEqual((res["results"], res["removed"], res["tiles"]), ([], [], []))
        tiles = {t["id"]: t for t in self.state.tiles()}
        self.assertEqual(len(tiles), 2)
        self.assertIsNone(tiles[a["id"]]["held_by"])
        self.assertEqual(tiles[a["id"]]["x"], 0.7)
        self.assertEqual(names(drain(self.sub_b)), ["tile_dropped"])

    def test_combine_rules(self) -> None:
        a = self.add(self.alice, "water")
        b = self.add(self.alice, "fire")
        with self.assertRaises(GameError):  # a tile must be grabbed first
            self.state.combine(self.alice.token, b["id"], tile=a["id"])
        self.state.grab_tile(self.alice.token, a["id"])
        with self.assertRaises(GameError):  # not with itself
            self.state.combine(self.alice.token, a["id"], tile=a["id"])
        with self.assertRaises(GameError):  # exactly one of tile/element
            self.state.combine(self.alice.token, b["id"])
        with self.assertRaises(GameError):
            self.state.combine(self.alice.token, b["id"], tile=a["id"], element="water")
        with self.assertRaises(GameError):  # undiscovered element
            self.state.combine(self.alice.token, b["id"], element="steam")
        self.state.grab_tile(self.bob.token, b["id"])
        with self.assertRaises(GameError) as ctx:  # target is being moved
            self.state.combine(self.alice.token, b["id"], tile=a["id"])
        self.assertEqual(ctx.exception.status, 409)

    def test_failed_combine_changes_nothing(self) -> None:
        a = self.add(self.alice, "water")
        before = (self.state.discovered(), self.state.tiles())
        with self.assertRaises(GameError):
            self.state.combine(self.alice.token, "t999", element="fire")
        with self.assertRaises(GameError):
            self.state.combine(self.alice.token, a["id"], element="nonsense")
        self.assertEqual(before, (self.state.discovered(), self.state.tiles()))

    def test_multi_result_recipe_creates_several_tiles(self) -> None:
        data = {
            "starters": ["a", "b"],
            "elements": {k: {"label": k} for k in "abcd"},
            "recipes": [{"a": "a", "b": "b", "result": ["c", "d"]}],
        }
        state = GameState(parse_recipes(data))
        p = state.join("P")
        sub = state.subscribe(p.token)
        assert sub
        t = state.add_tile(p.token, "b", 0.5, 0.5)
        res = state.combine(p.token, t["id"], element="a")
        self.assertEqual(res["new"], ["c", "d"])
        self.assertEqual([x["element"] for x in res["tiles"]], ["c", "d"])
        self.assertNotEqual((res["tiles"][0]["x"], res["tiles"][0]["y"]), (res["tiles"][1]["x"], res["tiles"][1]["y"]))

    # ---- cursors and disconnects ----

    def test_cursor_is_relayed_to_others_and_clamped(self) -> None:
        self.state.set_cursor(self.alice.token, 1.5, 0.25)
        self.assertEqual(names(drain(self.sub_a)), [])
        (name, data), = drain(self.sub_b)
        self.assertEqual((name, data), ("cursor", {"pid": self.alice.pid, "x": 1.0, "y": 0.25}))
        self.state.set_cursor(self.alice.token, None, None)
        self.assertEqual(drain(self.sub_b)[0][1]["x"], None)
        with self.assertRaises(GameError):
            self.state.set_cursor(self.alice.token, 0.5, None)

    def test_disconnect_releases_tiles_and_hides_cursor(self) -> None:
        tile = self.add(self.alice, "water")
        self.state.grab_tile(self.alice.token, tile["id"])
        drain(self.sub_b)
        self.state.unsubscribe(self.sub_a)
        self.assertEqual(sorted(names(drain(self.sub_b))), ["cursor", "players", "tile_dropped"])
        self.assertIsNone(self.state.tiles()[0]["held_by"])
        self.state.grab_tile(self.bob.token, tile["id"])

    def test_second_tab_keeps_player_online_and_tiles_held(self) -> None:
        tile = self.add(self.alice, "water")
        self.state.grab_tile(self.alice.token, tile["id"])
        second = self.state.subscribe(self.alice.token)
        assert second
        self.state.unsubscribe(self.sub_a)
        self.assertEqual(self.state.tiles()[0]["held_by"], self.alice.pid)
        self.assertEqual(len([p for p in self.state._online_players() if p["pid"] == self.alice.pid]), 1)

    def test_slow_subscriber_is_dropped(self) -> None:
        for _ in range(300):
            self.state.set_cursor(self.alice.token, 0.1, 0.1)
        self.assertTrue(self.sub_b.dead)
        self.state.set_cursor(self.alice.token, 0.2, 0.2)  # must not raise


class HintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = load_recipes(RECIPES)
        self.rng = random.Random(7)
        self.starters = set(self.book.starters)

    def test_new_combinations_from_the_starters(self) -> None:
        results = {r for _, _, r in new_combinations(self.book, self.starters)}
        self.assertEqual(results, {"steam", "mud", "lava", "dust", "puddle"})

    def test_can_make_names_an_undiscovered_element_that_is_makeable(self) -> None:
        for _ in range(20):
            self.assertIn(hint_can_make(self.book, self.starters, self.rng), {"steam", "mud", "lava", "dust", "puddle"})
        self.assertIsNone(hint_can_make(self.book, set(self.book.elements), self.rng))

    def test_try_gives_a_pair_that_really_makes_something_new(self) -> None:
        for _ in range(20):
            pair = hint_try(self.book, self.starters, self.rng)
            assert pair is not None
            self.assertTrue(set(self.book.combine(*pair)) - self.starters)
        self.assertIsNone(hint_try(self.book, set(self.book.elements), self.rng))

    def test_find_element_ignores_case_and_spaces(self) -> None:
        self.assertEqual(find_element(self.book, "  STEAM "), "steam")
        self.assertEqual(find_element(self.book, "Fire"), "fire")
        self.assertIsNone(find_element(self.book, "unicorn"))
        self.assertIsNone(find_element(self.book, "   "))

    def test_recipe_prefers_ingredients_you_already_have(self) -> None:
        depth = depths(self.book)
        self.assertEqual(sorted(hint_recipe(self.book, self.starters, depth, "ash") or ()), ["fire", "tree"])
        self.assertEqual(hint_recipe(self.book, self.starters, depth, "water"), None)
        for have in (self.starters, self.starters | {"lava"}):
            pair = hint_recipe(self.book, have, depth, "stone")
            self.assertIn(set(pair or ()), ({"lava", "water"}, {"lava", "air"}))
        self.assertEqual(
            set(hint_recipe(self.book, self.starters | {"lava"}, depth, "stone") or ()) - {"lava"} <= self.starters, True
        )


class JournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState(load_recipes(RECIPES), rng=random.Random(3))
        self.alice = self.state.join("Alice")
        self.bob = self.state.join("Bob")
        self.sub_b = self.state.subscribe(self.bob.token)
        assert self.sub_b
        drain(self.sub_b)

    def entry(self, element: str) -> dict[str, Any]:
        entries = self.state.journal(self.alice.token)["elements"]
        return next(e for e in entries if e["id"] == element)

    def make(self, a: str, b: str) -> None:
        tile = self.state.add_tile(self.alice.token, a, 0.2, 0.2)
        other = self.state.add_tile(self.alice.token, b, 0.6, 0.6)
        self.state.grab_tile(self.alice.token, tile["id"])
        self.state.combine(self.alice.token, other["id"], tile=tile["id"], x=0.6, y=0.6)

    def test_starts_with_only_the_starting_elements_and_no_recipes(self) -> None:
        entries = self.state.journal(self.alice.token)["elements"]
        self.assertEqual(sorted(e["id"] for e in entries), ["air", "earth", "fire", "water"])
        for e in entries:
            self.assertTrue(e["starter"])
            self.assertEqual((e["made_from"], e["used_in"]), ([], []))
            self.assertEqual(set(e), {"id", "label", "emoji", "by", "starter", "made_from", "used_in"})

    def test_a_discovery_links_its_ingredients_and_result(self) -> None:
        self.make("water", "fire")
        steam = self.entry("steam")
        self.assertFalse(steam["starter"])
        self.assertEqual(steam["by"], "Alice")
        self.assertEqual([sorted(pair) for pair in steam["made_from"]], [["fire", "water"]])
        self.assertEqual(self.entry("water")["used_in"], [["fire", "steam"]])
        self.assertEqual(self.entry("fire")["used_in"], [["water", "steam"]])

    def test_undiscovered_recipes_and_elements_are_never_revealed(self) -> None:
        self.make("water", "fire")
        everything = json.dumps(self.state.journal(self.alice.token))
        for hidden in ("mud", "lava", "dust", "puddle", "cloud"):  # nobody has made these yet
            self.assertNotIn(f'"{hidden}"', everything)
        self.assertEqual(self.entry("earth")["used_in"], [])  # its recipes all give undiscovered results

    def test_a_pair_of_the_same_element_is_listed_once(self) -> None:
        self.make("water", "water")
        self.assertEqual(self.entry("puddle")["made_from"], [["water", "water"]])
        self.assertEqual(self.entry("water")["used_in"], [["water", "puddle"]])

    def test_the_journal_is_private_and_needs_a_player(self) -> None:
        self.state.journal(self.alice.token)
        self.assertEqual(drain(self.sub_b), [])
        with self.assertRaises(GameError):
            self.state.journal("not-a-token")


class HintStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState(load_recipes(RECIPES), rng=random.Random(3))
        self.alice = self.state.join("Alice")
        self.bob = self.state.join("Bob")
        self.sub_b = self.state.subscribe(self.bob.token)
        assert self.sub_b
        drain(self.sub_b)

    def test_hints_are_private(self) -> None:
        self.state.hint(self.alice.token, "can_make")
        self.state.hint(self.alice.token, "try")
        self.assertEqual(drain(self.sub_b), [])

    def test_can_make_and_try_shapes(self) -> None:
        made = self.state.hint(self.alice.token, "can_make")
        self.assertEqual(made["kind"], "can_make")
        self.assertIn(made["result"]["id"], {"steam", "mud", "lava", "dust", "puddle"})
        self.assertEqual(set(made["result"]), {"id", "label", "emoji"})
        pair = self.state.hint(self.alice.token, "try")["pair"]
        self.assertEqual(len(pair), 2)
        self.assertTrue(self.state.book.combine(pair[0]["id"], pair[1]["id"]))

    def test_nothing_left_to_make(self) -> None:
        state = GameState(parse_recipes({
            "starters": ["a", "b"], "elements": {"a": {"label": "A"}, "b": {"label": "B"}}, "recipes": [],
        }))
        p = state.join("P")
        self.assertEqual(state.hint(p.token, "can_make")["result"], None)
        self.assertEqual(state.hint(p.token, "try")["pair"], None)

    def test_find_flags_which_ingredients_you_have(self) -> None:
        hint = self.state.hint(self.alice.token, "find", "Steam")
        self.assertEqual(hint["target"]["id"], "steam")
        self.assertFalse(hint["have"])
        self.assertEqual({e["id"]: e["known"] for e in hint["recipe"]}, {"water": True, "fire": True})
        deep = self.state.hint(self.alice.token, "find", "ash")
        self.assertEqual({e["id"]: e["known"] for e in deep["recipe"]}, {"tree": False, "fire": True})

    def test_find_starter_and_unknown(self) -> None:
        hint = self.state.hint(self.alice.token, "find", "WATER")
        self.assertEqual((hint["starter"], hint["have"], hint["recipe"]), (True, True, None))
        with self.assertRaises(GameError) as ctx:
            self.state.hint(self.alice.token, "find", "unicorn")
        self.assertEqual(ctx.exception.status, 404)

    def test_bad_requests(self) -> None:
        for kind, target in (("nope", None), ("find", None), ("find", "  "), ("find", 5), ("find", "x" * 300)):
            with self.assertRaises(GameError) as ctx:
                self.state.hint(self.alice.token, kind, target)
            self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(GameError) as ctx:
            self.state.hint("nope", "can_make")
        self.assertEqual(ctx.exception.status, 403)


# Steps that lead to the Genie in the dummy recipes: (element, tile it is dropped on).
GENIE_PATH = [
    ("fire", "earth"), ("water", "fire"), ("air", "steam"), ("cloud", "water"), ("cloud", "rain"),
    ("lava", "water"), ("storm", "stone"), ("stone", "air"), ("sand", "fire"), ("glass", "fire"),
    ("water", "earth"), ("mud", "rain"), ("plant", "earth"), ("mud", "tree"),
    ("lamp", "magic"), ("human", "magic lamp"),
]


def unlock_genie(state: GameState, token: str) -> None:
    for element, tile_element in GENIE_PATH:
        tile = state.add_tile(token, tile_element, 0.5, 0.5)
        state.combine(token, tile["id"], element=element)
    state.clear_tiles(token)


class GenieTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = load_recipes(RECIPES)
        self.state = GameState(self.book)
        self.alice = self.state.join("Alice")
        self.bob = self.state.join("Bob")
        self.sub_a = self.state.subscribe(self.alice.token)
        self.sub_b = self.state.subscribe(self.bob.token)
        assert self.sub_a and self.sub_b

    def events(self, sub: Any, name: str) -> list[Any]:
        return [data for event, data in drain(sub) if event == name]

    def test_locked_until_the_genie_element_is_discovered(self) -> None:
        snapshot = parse_event(self.sub_a.messages.get_nowait())[1]
        self.assertEqual(snapshot["genie"], {"available": False})  # nothing to show until it is unlocked
        with self.assertRaises(GameError) as ctx:
            self.state.genie_use(self.alice.token, "fire")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("not unlocked", ctx.exception.message)

    def test_unlocking_gives_three_wishes_and_tells_everyone(self) -> None:
        drain(self.sub_b)
        unlock_genie(self.state, self.alice.token)
        statuses = [e["status"] for e in self.events(self.sub_b, "genie")]
        self.assertEqual(statuses[-1]["left"], 3)
        self.assertTrue(statuses[-1]["unlocked"])
        self.assertEqual(self.state.discovered()[DJINN], "Alice")

    def test_a_wish_combines_with_the_whole_shared_pool_and_shows_every_result(self) -> None:
        unlock_genie(self.state, self.alice.token)
        have = set(self.state.discovered())
        expected = {r for partner in have for r in self.book.combine("fire", partner)}
        drain(self.sub_b)
        res = self.state.genie_use(self.bob.token, "fire")  # a different player than the one who unlocked it
        self.assertEqual({e["id"] for e in res["results"]}, expected)
        self.assertEqual(sorted(t["element"] for t in res["tiles"]), sorted(expected))
        self.assertEqual(sorted(t["element"] for t in self.state.tiles()), sorted(expected))
        self.assertEqual(len({(t["x"], t["y"]) for t in res["tiles"]}), len(expected))  # no stacking
        self.assertEqual(res["genie"]["left"], 2)
        self.assertTrue(set(res["new"]) <= expected - have)
        for eid in res["new"]:
            self.assertEqual(self.state.discovered()[eid], "Bob")

        events = drain(self.sub_b)  # Bob is the one who used it; Alice is the other player
        self.assertEqual([n for n, _ in events].count("tile_added"), 0)
        alice_events = [(n, d) for n, d in drain(self.sub_a)]
        self.assertEqual(len([1 for n, _ in alice_events if n == "tile_added"]), len(expected))
        used = [d for n, d in alice_events if n == "genie"][-1]
        self.assertEqual((used["used_by"]["name"], used["used_by"]["element"]["id"]), ("Bob", "fire"))

    def test_a_wish_that_makes_nothing_is_not_spent(self) -> None:
        unlock_genie(self.state, self.alice.token)
        res = self.state.genie_use(self.alice.token, "djinn")
        self.assertEqual((res["results"], res["tiles"], res["genie"]["left"]), ([], [], 3))

    def test_wishes_run_out(self) -> None:
        unlock_genie(self.state, self.alice.token)
        for _ in range(3):
            self.state.clear_tiles(self.alice.token)
            self.state.genie_use(self.alice.token, "water")
        with self.assertRaises(GameError) as ctx:
            self.state.genie_use(self.alice.token, "water")
        self.assertIn("no wishes left", ctx.exception.message)

    def test_one_extra_wish_per_fifty_discoveries_after_unlocking(self) -> None:
        book = parse_recipes({
            "starters": ["a", "b"],
            "elements": {k: {"label": k} for k in ("a", "b", "djinn")},
            "recipes": [{"a": "a", "b": "b", "result": "djinn"}],
        })
        state = GameState(book)
        p = state.join("P")
        tile = state.add_tile(p.token, "b", 0.5, 0.5)
        state.combine(p.token, tile["id"], element="a")
        self.assertEqual((state._genie_status()["total"], state._genie_status()["next_in"]), (3, 50))
        for i in range(49):
            state._discovered[f"extra{i}"] = None
        self.assertEqual((state._genie_status()["total"], state._genie_status()["next_in"]), (3, 1))
        state._discovered["extra49"] = None
        self.assertEqual((state._genie_status()["total"], state._genie_status()["left"]), (4, 4))
        for i in range(50, 100):
            state._discovered[f"extra{i}"] = None
        state._genie_used = 2
        self.assertEqual((state._genie_status()["total"], state._genie_status()["left"]), (5, 3))

    def test_the_wish_needs_room_for_all_the_tiles_and_is_kept_if_not(self) -> None:
        unlock_genie(self.state, self.alice.token)
        with mock.patch("state.MAX_TILES", 2):
            with self.assertRaises(GameError) as ctx:
                self.state.genie_use(self.alice.token, "fire")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.state._genie_status()["left"], 3)

    def test_only_discovered_elements_and_known_players(self) -> None:
        unlock_genie(self.state, self.alice.token)
        with self.assertRaises(GameError):
            self.state.genie_use(self.alice.token, "unicorn")
        with self.assertRaises(GameError) as ctx:
            self.state.genie_use("nope", "fire")
        self.assertEqual(ctx.exception.status, 403)

    def test_the_djinn_is_found_by_id_or_label_in_any_case(self) -> None:
        for eid, label in (("Djinn", "Whatever"), ("x7", "DJINN"), ("djinn", "Djinn")):
            book = parse_recipes({
                "starters": ["a"], "elements": {"a": {"label": "A"}, eid: {"label": label}},
                "recipes": [{"a": "a", "b": "a", "result": eid}],
            })
            self.assertIsNotNone(GameState(book)._djinn, (eid, label))

    def test_games_without_a_genie_element_have_no_genie(self) -> None:
        book = parse_recipes({
            "starters": ["a"], "elements": {"a": {"label": "A"}, "b": {"label": "B"}},
            "recipes": [{"a": "a", "b": "a", "result": "b"}],
        })
        state = GameState(book)
        p = state.join("P")
        self.assertEqual(state._genie_status(), {"available": False})
        with self.assertRaises(GameError):
            state.genie_use(p.token, "a")


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = load_recipes(RECIPES)
        self.now = [0.0]
        self.registry = GameRegistry(self.book, idle_seconds=600, clock=lambda: self.now[0])

    def test_slugs_and_names(self) -> None:
        self.assertEqual(slugify("Friday Night!  Game"), "friday-night-game")
        self.assertEqual(slugify("???"), "")
        self.assertEqual(clean_game_name("  a \n  b\t"), "a b")
        self.assertEqual(len(clean_game_name("x" * 100)), 30)
        self.assertEqual(clean_game_name(None), "")
        with self.assertRaises(GameError):
            clean_game_name(5)

    def test_create_get_and_list(self) -> None:
        game = self.registry.create("Friday Night")
        self.assertEqual((game.slug, game.name), ("friday-night", "Friday Night"))
        self.assertIs(self.registry.get("friday-night"), game)
        self.assertIsNone(self.registry.get("nope"))
        (summary,) = self.registry.list()
        self.assertEqual((summary["id"], summary["players"], summary["found"], summary["max_players"]),
                         ("friday-night", 0, 4, 5))

    def test_unnamed_games_get_numbers_and_unicode_names_still_work(self) -> None:
        names = [self.registry.create(None).name, self.registry.create("  ").name]
        self.assertEqual(len(set(names)), 2)
        for name in names:
            self.assertRegex(name, r"^[a-z0-9]{6}$")
        odd = self.registry.create("ゲーム")
        self.assertRegex(odd.slug, r"^[0-9a-f]{6}$")
        self.assertEqual(odd.name, "ゲーム")

    def test_at_most_three_games(self) -> None:
        for name in ("one", "two", "three"):
            self.registry.create(name)
        with self.assertRaises(GameError) as ctx:
            self.registry.create("four")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("3 games are already running", ctx.exception.message)

    def test_names_must_be_unique_ignoring_case_and_punctuation(self) -> None:
        self.registry.create("Party")
        for name in ("party", " PARTY ", "Party!"):
            with self.assertRaises(GameError) as ctx:
                self.registry.create(name)
            self.assertEqual(ctx.exception.status, 409)

    def test_empty_games_close_after_the_idle_time_and_free_the_slot(self) -> None:
        for name in ("one", "two", "three"):
            self.registry.create(name)
        self.now[0] = 599.0
        self.assertEqual(len(self.registry.list()), 3)
        self.now[0] = 601.0
        self.assertEqual(self.registry.list(), [])
        self.registry.create("four")

    def test_games_with_players_stay_open_and_the_clock_restarts_when_they_leave(self) -> None:
        game = self.registry.create("busy")
        player = game.join("Pat")
        sub = game.subscribe(player.token)
        assert sub
        self.now[0] = 5000.0
        self.assertEqual([g["id"] for g in self.registry.list()], ["busy"])
        game.unsubscribe(sub)
        self.now[0] = 5500.0
        self.assertEqual(len(self.registry.list()), 1)
        self.now[0] = 5601.0
        self.assertEqual(self.registry.list(), [])

    def test_games_are_independent(self) -> None:
        one, two = self.registry.create("one"), self.registry.create("two")
        a, b = one.join("Same"), two.join("Same")  # the same name is fine in different games
        tile = one.add_tile(a.token, "water", 0.5, 0.5)
        one.combine(a.token, tile["id"], element="fire")
        self.assertIn("steam", one.discovered())
        self.assertNotIn("steam", two.discovered())
        self.assertEqual(two.tiles(), [])
        with self.assertRaises(GameError):
            one.add_tile(b.token, "water", 0.5, 0.5)  # a token from another game means nothing here


class SaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = load_recipes(RECIPES)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wall = [1000.0]  # wall-clock time, used for save expiry
        self.store = SaveStore(self.tmp.name, now=lambda: self.wall[0])
        self.registry = GameRegistry(self.book, max_games=2, saves=self.store)

    def play(self, name: str = "Friday", player: str = "Ann") -> tuple[GameState, Any]:
        game = self.registry.create(name)
        who = game.join(player)
        tile = game.add_tile(who.token, "water", 0.2, 0.2)
        other = game.add_tile(who.token, "fire", 0.6, 0.6)
        game.grab_tile(who.token, tile["id"])
        game.combine(who.token, other["id"], tile=tile["id"])  # water + fire = steam
        return game, who

    def test_end_with_save_then_resume_by_game_or_player_name(self) -> None:
        game, who = self.play()
        found = len(game.export()["discovered"])
        self.assertGreater(found, len(self.book.starters))
        self.registry.end("friday", who.token, True)
        self.assertIsNone(self.registry.get("friday"))
        for user in ("Ann", "ann", " ANN "):
            (match,) = self.registry.find_saved(user)
            self.assertEqual((match["id"], match["found"]), ("friday", found))
        self.assertEqual(self.registry.find_saved("Nobody"), [])
        self.assertEqual(self.registry.find_saved("friday"), [])  # the game's name finds nothing: saves belong to players
        resumed = self.registry.resume("friday", "Ann")
        self.assertEqual(resumed.name, "Friday")
        self.assertEqual(len(resumed.export()["discovered"]), found)
        self.assertEqual(resumed.export()["discovered"]["steam"], "Ann")  # who found it is kept
        self.assertEqual(self.registry.find_saved("ann"), [])  # running games are not offered again
        with self.assertRaises(GameError):
            self.registry.resume("friday", "Ann")  # already running

    def test_end_without_saving_leaves_nothing(self) -> None:
        game, who = self.play()
        self.registry.end("friday", who.token, False)
        self.assertEqual(self.registry.find_saved("Ann"), [])
        with self.assertRaises(GameError) as ctx:
            self.registry.resume("friday", "Ann")
        self.assertEqual(ctx.exception.status, 404)

    def test_only_players_of_a_saved_game_can_find_or_resume_it(self) -> None:
        game, who = self.play()
        self.registry.end("friday", who.token, True)
        self.assertEqual(self.registry.find_saved("Mallory"), [])
        for outsider in ("Mallory", "", "Ann Smith"):
            with self.assertRaises(GameError) as ctx:
                self.registry.resume("friday", outsider)
            self.assertEqual(ctx.exception.status, 403)
        self.assertIsNone(self.registry.get("friday"))
        self.assertEqual(self.registry.resume("friday", "ann").slug, "friday")

    def test_only_a_player_of_that_game_can_end_it(self) -> None:
        game, who = self.play()
        other = self.registry.create("Other")
        stranger = other.join("Zed")
        for bad in (None, "nope", stranger.token):
            with self.assertRaises(GameError) as ctx:
                self.registry.end("friday", bad, True)
            self.assertEqual(ctx.exception.status, 403)
        self.assertIsNotNone(self.registry.get("friday"))
        with self.assertRaises(GameError) as ctx:
            self.registry.end("missing", who.token, True)
        self.assertEqual(ctx.exception.status, 404)

    def test_a_resumed_game_is_not_recent_for_players_of_the_saved_one(self) -> None:
        game, who = self.play()
        self.registry.end("friday", who.token, True)
        self.registry.resume("friday", "Ann")
        (entry,) = self.registry.list("Ann")
        self.assertFalse(entry["recent"])  # they have not just left it: it was only just reopened
        self.assertFalse(self.registry.list()[0]["recent"])  # no username, nothing is marked

    def test_only_the_creator_can_save(self) -> None:
        game = self.registry.create("Mine", "Ann")
        ann, bob = game.join("Ann"), game.join("Bob")
        self.assertEqual(game.creator, "Ann")
        with self.assertRaises(GameError) as ctx:
            self.registry.end("mine", bob.token, True)
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("only Ann", ctx.exception.message)
        self.assertIsNotNone(self.registry.get("mine"))  # still running, nothing saved
        self.assertEqual(self.store.for_player("Ann"), [])
        self.assertEqual(game.export()["creator"], "Ann")
        self.registry.end("mine", ann.token, True)
        resumed = self.registry.resume("mine", "Bob")  # Bob played in it, so he can bring it back
        self.assertEqual(resumed.creator, "Ann")  # but it is still Ann's game to save
        bob_again = resumed.join("Bob")
        with self.assertRaises(GameError):
            self.registry.end("mine", bob_again.token, True)
        self.registry.end("mine", bob_again.token, False)  # anyone may end it without saving
        self.assertIsNone(self.registry.get("mine"))

    def test_creator_comparison_ignores_case_and_old_saves_go_to_the_resumer(self) -> None:
        game = self.registry.create("Case", "Ann Lee")
        who = game.join("Ann Lee")
        self.registry.end("case", who.token, True)
        data = json.loads((Path(self.tmp.name) / "case.json").read_text(encoding="utf-8"))
        del data["creator"]  # a save made before games had a creator
        (Path(self.tmp.name) / "case.json").write_text(json.dumps(data), encoding="utf-8")
        resumed = self.registry.resume("case", "ann lee")
        self.assertEqual(resumed.creator, "ann lee")
        again = resumed.join("Ann Lee")
        self.registry.end("case", again.token, True)  # same account, different capitals

    def test_resume_ignores_the_game_limit(self) -> None:
        game, who = self.play()
        self.registry.end("friday", who.token, True)
        self.registry.create("A")
        self.registry.create("B")
        with self.assertRaises(GameError) as ctx:
            self.registry.create("C")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.registry.resume("friday", "Ann").slug, "friday")
        self.assertEqual(len(self.registry.list()), 3)

    def test_saves_expire_after_three_days(self) -> None:
        game, who = self.play()
        self.registry.end("friday", who.token, True)
        self.wall[0] += 3 * 24 * 3600 - 1
        self.assertEqual(len(self.registry.find_saved("Ann")), 1)
        self.wall[0] += 2
        self.assertEqual(self.registry.find_saved("Ann"), [])
        self.assertFalse(list(Path(self.tmp.name).glob("*.json")))  # the file is deleted too
        with self.assertRaises(GameError):
            self.registry.resume("friday", "Ann")

    def test_a_game_that_closes_by_itself_is_not_saved(self) -> None:
        now = [0.0]
        registry = GameRegistry(self.book, idle_seconds=60, clock=lambda: now[0], saves=self.store)
        busy = registry.create("Busy")
        who = busy.join("Bob")
        a = busy.add_tile(who.token, "water", 0.2, 0.2)
        b = busy.add_tile(who.token, "fire", 0.6, 0.6)
        busy.grab_tile(who.token, a["id"])
        busy.combine(who.token, b["id"], tile=a["id"])
        busy.unsubscribe(busy.subscribe(who.token))
        now[0] = 3000
        self.assertEqual(registry.list(), [])
        self.assertEqual(self.store.for_player("Bob"), [])

    def test_djinn_state_survives_and_is_clamped(self) -> None:
        game = self.registry.create("Wishes")
        who = game.join("Ann")
        unlock_genie(game, who.token)
        before = game.export()
        self.assertEqual(before["genie"]["used"], 0)
        self.registry.end("wishes", who.token, True)
        resumed = self.registry.resume("wishes", "Ann")
        self.assertEqual(resumed.export()["genie"], before["genie"])
        tampered = {"discovered": before["discovered"], "genie": {"unlocked_at": "x", "used": 99},
                    "players": ["Ann", 5, ""]}
        fresh = GameState(self.book)
        fresh.restore(tampered)
        self.assertEqual(fresh._genie_used, 3)  # never more than the wishes available
        self.assertEqual(fresh.export()["players"], ["Ann"])

    def test_unknown_elements_in_a_save_are_ignored(self) -> None:
        state = GameState(self.book)
        state.restore({"discovered": {"steam": "Ann", "dragon": "Bob", "water": "Eve"}, "players": "x"})
        discovered = state.export()["discovered"]
        self.assertIn("steam", discovered)
        self.assertNotIn("dragon", discovered)
        self.assertIsNone(discovered["water"])  # starters stay starters

    def test_bad_files_and_names_are_harmless(self) -> None:
        directory = Path(self.tmp.name)
        (directory / "broken.json").write_text("{not json", encoding="utf-8")
        (directory / "list.json").write_text("[]", encoding="utf-8")
        (directory / "mismatch.json").write_text(json.dumps({"slug": "other", "saved_at": self.wall[0]}))
        self.assertEqual(self.store.for_player("broken"), [])
        self.assertIsNone(self.store.load("../etc/passwd"))
        self.assertIsNone(self.store.load(""))
        with self.assertRaises(GameError):
            self.registry.resume(5, "Ann")

    def test_saving_is_off_without_a_directory(self) -> None:
        registry = GameRegistry(self.book)  # no store
        game = registry.create("NoSave")
        who = game.join("Ann")
        with self.assertRaises(GameError):
            registry.end("nosave", who.token, True)
        self.assertIsNotNone(registry.get("nosave"))  # the game stays open
        registry.end("nosave", who.token, False)

    def test_deleting_an_accounts_saves_removes_every_game_it_played_in(self) -> None:
        for name, players in (("Solo", ["Ann"]), ("Shared", ["Ann", "Bob"]), ("Bobs", ["Bob"])):
            game, first = self.play(name, players[0])
            for other in players[1:]:
                game.join(other)
            self.registry.end(slugify(name), first.token, True)
        self.assertEqual(sorted(self.store.delete_for_player("ann")), ["shared", "solo"])
        self.assertEqual([g["id"] for g in self.store.for_player("Bob")], ["bobs"])  # the shared one went too
        self.assertEqual(self.store.for_player("Ann"), [])
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).glob("*.json")), ["bobs.json"])
        self.assertEqual(self.store.delete_for_player("Nobody"), [])

    def test_saving_again_replaces_the_old_save(self) -> None:
        game, who = self.play()
        self.registry.end("friday", who.token, True)
        again = self.registry.resume("friday", "Ann")
        again_player = again.join("Ann")  # the creator, who is the one allowed to save
        again.join("Bea")
        again.add_tile(again_player.token, "earth", 0.1, 0.1)
        self.registry.end("friday", again_player.token, True)
        (match,) = self.registry.find_saved("Bea")
        self.assertEqual(match["players"], ["Ann", "Bea"])  # earlier players are remembered


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = load_recipes(RECIPES)
        self.data = json.loads(RECIPES.read_text(encoding="utf-8"))

    def test_flowchart_has_every_element_and_recipe(self) -> None:
        chart = build_flowchart(self.book)
        self.assertTrue(chart.startswith("flowchart LR\n"))
        self.assertEqual(chart.count('["'), len(self.book.elements))
        self.assertEqual(chart.count('(("+"))'), len(self.book.lookup))
        arrows = sum(2 if a != b else 1 for a, b in self.book.lookup) + sum(len(r) for r in self.book.lookup.values())
        self.assertEqual(chart.count("-->"), arrows)
        self.assertIn(f"{len(self.book.elements)} elements", chart)
        self.assertIn("class e", chart)

    def test_flowchart_tiers_direction_and_focus(self) -> None:
        tiers = build_flowchart(self.book, tiers=True, direction="TD")
        self.assertTrue(tiers.startswith("flowchart TD"))
        self.assertIn('subgraph tier0["Starting elements"]', tiers)
        self.assertIn('subgraph tier1["Tier 1"]', tiers)
        focused = build_flowchart(self.book, focus="djinn")
        self.assertLess(focused.count('["'), len(self.book.elements))
        wanted = needed_for(self.book, "djinn")
        self.assertIn("djinn", wanted)
        self.assertIn("water", wanted)
        self.assertNotIn("tower", wanted)
        with self.assertRaises(ValueError):
            build_flowchart(self.book, direction="UP")

    def test_flowchart_escapes_labels_and_marks_self_combinations(self) -> None:
        data = {"starters": ["a"], "elements": {"a": {"label": 'Say "hi" <b> #1'}, "b": {"label": "B", "emoji": "🅱"}},
                "recipes": [{"a": "a", "b": "a", "result": "b"}]}
        chart = build_flowchart(parse_recipes(data))
        self.assertIn("Say #34;hi#34; #60;b#62; #35;1", chart)
        self.assertIn('-- "x2" -->', chart)
        self.assertNotIn('"hi"', chart)
        self.assertEqual(label_text("a&b"), "a#38;b")

    def test_html_page_embeds_the_chart_safely(self) -> None:
        page = html_page("flowchart LR\n  a[\"</script>\"]\n", "T <x>")
        self.assertNotIn("</script>\"]", page)  # the chart can't close the script tag early
        self.assertNotIn("&quot;", page)  # and is not HTML-escaped, which broke Mermaid's parser
        self.assertIn('-->', html_page("a --> b\n", "T"))
        self.assertIn("maxEdges", page)
        self.assertIn("T &lt;x&gt;", page)

    def test_report_is_clean_for_the_dummy_set_and_finds_planted_problems(self) -> None:
        lines, errors = build_report(self.data)
        self.assertEqual(errors, 0)
        self.assertTrue(all(not line.startswith(("1. **ERROR", "2. **ERROR")) for line in lines))
        broken = json.loads(json.dumps(self.data))
        broken["recipes"].append({"a": "water", "b": "fire", "result": "stem"})
        broken["elements"]["orphan"] = {"label": "Orphan"}
        lines, errors = build_report(broken)
        self.assertEqual(errors, 1)  # structural problems are reported on their own first
        self.assertTrue(any("did you mean 'steam'" in line for line in lines))
        self.assertTrue(any("Suggested change" in line for line in lines))
        del broken["recipes"][-1]
        lines, errors = build_report(broken)
        text = "\n".join(lines)
        self.assertEqual(errors, 1)
        self.assertIn("'Orphan' is an orphan", text)
        self.assertIn("Suggested change: Add a recipe that makes 'Orphan'", text)


def chain_book(length: int = 130, time_at: int | None = None, with_time: bool = True) -> Any:
    """Starters s1 and s2; e1 = s1+s2, e<i> = e<i-1>+s1. Optionally 'Time' = e<time_at>+s2."""
    elements = {"s1": {"label": "S1"}, "s2": {"label": "S2"}}
    recipes = [{"a": "s1", "b": "s2", "result": "e1"}]
    for i in range(1, length + 1):
        elements[f"e{i}"] = {"label": f"E{i}"}
        if i > 1:
            recipes.append({"a": f"e{i - 1}", "b": "s1", "result": f"e{i}"})
    if with_time:
        elements["time"] = {"label": "Time", "emoji": "⏳"}
        if time_at is not None:
            recipes.append({"a": f"e{time_at}", "b": "s2", "result": "time"})
    return parse_recipes({"starters": ["s1", "s2"], "elements": elements, "recipes": recipes})


class TimeUnlockTests(unittest.TestCase):
    def climb(self, state: GameState, token: str, steps: int) -> None:
        """Make the next `steps` elements of the chain."""
        for _ in range(steps):
            found = state.export()["discovered"]
            have = max((int(k[1:]) for k in found if k[0] == "e"), default=0)  # highest e<i> found so far
            first = "s1" if have == 0 else f"e{have}"
            a = state.add_tile(token, first, 0.2, 0.2)
            b = state.add_tile(token, "s2" if have == 0 else "s1", 0.6, 0.6)
            state.grab_tile(token, a["id"])
            state.combine(token, b["id"], tile=a["id"])
            state.clear_tiles(token)

    def test_time_is_given_when_the_room_reaches_100_elements(self) -> None:
        state = GameState(chain_book())
        ann = state.join("Ann")
        sub = state.subscribe(ann.token)
        assert sub
        self.climb(state, ann.token, TIME_UNLOCK_AT - 3)  # 2 starters + 97 = 99 found
        self.assertEqual(len(state.export()["discovered"]), TIME_UNLOCK_AT - 1)
        self.assertNotIn("time", state.export()["discovered"])
        drain(sub)
        self.climb(state, ann.token, 1)  # the 100th element
        events = drain(sub)
        unlocked = [d for n, d in events if n == "discovered" and d.get("unlocked")]
        self.assertEqual(len(unlocked), 1)
        self.assertEqual((unlocked[0]["id"], unlocked[0]["by"], unlocked[0]["unlocked"]),
                         ("time", UNLOCKED_BY, TIME_UNLOCK_AT))
        self.assertEqual(state.export()["discovered"]["time"], UNLOCKED_BY)
        self.assertEqual(len(state.export()["discovered"]), TIME_UNLOCK_AT + 1)
        self.climb(state, ann.token, 3)
        self.assertEqual(sum(1 for n, d in drain(sub) if d.get("unlocked")), 0)  # only once

    def test_making_time_by_recipe_first_gives_no_extra_unlock(self) -> None:
        state = GameState(chain_book(time_at=5))
        ann = state.join("Ann")
        sub = state.subscribe(ann.token)
        assert sub
        self.climb(state, ann.token, 5)
        a = state.add_tile(ann.token, "e5", 0.2, 0.2)
        b = state.add_tile(ann.token, "s2", 0.6, 0.6)
        state.grab_tile(ann.token, a["id"])
        state.combine(ann.token, b["id"], tile=a["id"])
        self.assertEqual(state.export()["discovered"]["time"], "Ann")  # a normal discovery
        drain(sub)
        self.climb(state, ann.token, 100)
        self.assertEqual(sum(1 for n, d in drain(sub) if d.get("unlocked")), 0)
        self.assertEqual(state.export()["discovered"]["time"], "Ann")

    def test_no_time_element_means_nothing_happens(self) -> None:
        state = GameState(chain_book(with_time=False))
        ann = state.join("Ann")
        self.climb(state, ann.token, TIME_UNLOCK_AT + 5)
        self.assertEqual(len(state.export()["discovered"]), TIME_UNLOCK_AT + 7)

    def test_a_restored_room_with_enough_elements_gets_time(self) -> None:
        book = chain_book()
        full = {"s1": None, "s2": None, **{f"e{i}": "Ann" for i in range(1, 120)}}
        state = GameState(book)
        state.restore({"discovered": full})
        self.assertEqual(state.export()["discovered"]["time"], UNLOCKED_BY)  # restore keeps the empty string
        again = GameState(book)
        again.restore(state.export())
        self.assertEqual(again.export()["discovered"]["time"], UNLOCKED_BY)
        small = GameState(book)
        small.restore({"discovered": {"s1": None, "e1": "Ann"}})
        self.assertNotIn("time", small.export()["discovered"])

    def test_time_is_in_the_snapshot_with_its_unlock_marker(self) -> None:
        state = GameState(chain_book())
        ann = state.join("Ann")
        self.climb(state, ann.token, TIME_UNLOCK_AT)
        sub = state.subscribe(ann.token)
        assert sub
        snapshot = next(d for n, d in drain(sub) if n == "snapshot")
        self.assertEqual({e["id"]: e["by"] for e in snapshot["elements"]}["time"], UNLOCKED_BY)


class AccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "accounts.json"
        self.now = [1000.0]
        self.store = AccountStore(self.path, clock=lambda: self.now[0], iterations=1000)

    def test_register_login_and_names_are_unique_ignoring_case(self) -> None:
        self.assertEqual(self.store.register("  Ann   Lee ", "correct horse"), "Ann Lee")
        with self.assertRaises(GameError) as ctx:
            self.store.register("ann lee", "another password")
        self.assertEqual(ctx.exception.status, 409)
        token, name = self.store.login("ANN LEE", "correct horse")
        self.assertEqual(name, "Ann Lee")  # shown with the capitals it was created with
        self.assertEqual(self.store.username(token), "Ann Lee")

    def test_username_and_password_rules(self) -> None:
        for bad in ("ab", "x" * 25, "bad/name", "<script>", "bad\x00name", "", 5, None):
            with self.assertRaises(GameError) as ctx:
                clean_username(bad)
            self.assertEqual(ctx.exception.status, 400, bad)
        self.assertEqual(clean_username("Jo_o-1.x y"), "Jo_o-1.x y")
        for bad in ("short", "x" * 129, 12345678, None):
            with self.assertRaises(GameError) as ctx:
                self.store.register("Valid Name", bad)
            self.assertEqual(ctx.exception.status, 400, bad)
        with self.assertRaises(GameError):
            self.store.register("Valid Name", "valid name")  # password equal to the username
        self.assertEqual(self.store.usernames(), [])

    def test_wrong_password_and_unknown_user_look_the_same(self) -> None:
        self.store.register("Ann", "correct horse")
        errors = []
        for user, password in (("Ann", "wrong password"), ("Nobody", "correct horse"), ("Ann", 5), (None, None)):
            with self.assertRaises(GameError) as ctx:
                self.store.login(user, password)
            errors.append((ctx.exception.status, ctx.exception.message))
        self.assertEqual(set(errors), {(401, "wrong username or password")})

    def test_too_many_wrong_passwords_lock_the_account_for_a_minute(self) -> None:
        self.store.register("Ann", "correct horse")
        for _ in range(MAX_FAILURES):
            with self.assertRaises(GameError) as ctx:
                self.store.login("Ann", "nope nope nope")
            self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(GameError) as ctx:
            self.store.login("Ann", "correct horse")  # even the right password waits
        self.assertEqual(ctx.exception.status, 429)
        self.now[0] += LOCKOUT_SECONDS + 1
        self.assertEqual(self.store.login("Ann", "correct horse")[1], "Ann")
        for _ in range(MAX_FAILURES - 1):  # a success cleared the count
            with self.assertRaises(GameError):
                self.store.login("Ann", "nope nope nope")
        self.assertEqual(self.store.login("Ann", "correct horse")[1], "Ann")

    def test_sessions_expire_when_unused_and_end_on_logout(self) -> None:
        self.store.register("Ann", "correct horse")
        token, _ = self.store.login("Ann", "correct horse")
        for _ in range(3):  # using a session keeps it alive
            self.now[0] += SESSION_SECONDS - 10
            self.assertEqual(self.store.username(token), "Ann")
        self.now[0] += SESSION_SECONDS + 1
        self.assertIsNone(self.store.username(token))
        token, _ = self.store.login("Ann", "correct horse")
        self.store.logout(token)
        self.assertIsNone(self.store.username(token))
        for junk in (None, "", "nope", 5):
            self.assertIsNone(self.store.username(junk))

    def test_reset_and_delete_end_existing_sessions(self) -> None:
        self.store.register("Ann", "correct horse")
        token, _ = self.store.login("Ann", "correct horse")
        self.store.set_password("ann", "new password 1")
        self.assertIsNone(self.store.username(token))
        with self.assertRaises(GameError):
            self.store.login("Ann", "correct horse")
        token, _ = self.store.login("Ann", "new password 1")
        self.store.delete("Ann")
        self.assertIsNone(self.store.username(token))
        for action in (lambda: self.store.delete("Ann"), lambda: self.store.set_password("Ann", "whatever 123")):
            with self.assertRaises(GameError) as ctx:
                action()
            self.assertEqual(ctx.exception.status, 404)

    def test_passwords_are_stored_hashed_and_accounts_persist(self) -> None:
        self.store.register("Ann", "correct horse")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("correct horse", text)
        self.assertIn("salt", text)
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        again = AccountStore(self.path, clock=lambda: self.now[0], iterations=1000)
        self.assertEqual(again.login("ann", "correct horse")[1], "Ann")
        self.store.register("Bob", "correct horse")  # same password, different salt and hash
        data = json.loads(self.path.read_text(encoding="utf-8"))["accounts"]
        self.assertNotEqual(data["ann"]["hash"], data["bob"]["hash"])

    def test_a_damaged_accounts_file_is_never_overwritten(self) -> None:
        self.path.write_text("{broken", encoding="utf-8")
        for action in (lambda: self.store.register("Ann", "correct horse"), lambda: self.store.login("Ann", "x" * 8)):
            with self.assertRaises(GameError) as ctx:
                action()
            self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")


class AdminToolTests(unittest.TestCase):
    def test_deleting_an_account_removes_its_solo_saves_and_its_name_from_shared_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            accounts_file, saves_dir = Path(tmp) / "accounts.json", Path(tmp) / "saves"
            store = AccountStore(accounts_file, iterations=1000)
            for name in ("Ann", "Bob"):
                store.register(name, f"password-{name}")
            book = load_recipes(RECIPES)
            saves = SaveStore(saves_dir)
            registry = GameRegistry(book, saves=saves)
            for slug, players in (("solo", ["Ann"]), ("duo", ["Ann", "Bob"]), ("other", ["Bob"])):
                game = registry.create(slug)
                tokens = [game.join(p) for p in players]
                tile = game.add_tile(tokens[0].token, "water", 0.3, 0.3)
                other = game.add_tile(tokens[0].token, "fire", 0.6, 0.6)
                game.grab_tile(tokens[0].token, tile["id"])
                game.combine(tokens[0].token, other["id"], tile=tile["id"])
                registry.end(slug, tokens[0].token, True)
            args = ["admin", "--file", str(accounts_file), "--saves-dir", str(saves_dir), "delete", "ann"]
            out = io.StringIO()
            with mock.patch("builtins.input", return_value="no"), contextlib.redirect_stdout(out):
                self.assertEqual(accounts_admin.main(args), 1)
            self.assertIn("'duo' (also played by Bob", out.getvalue())  # the admin sees what will happen to each
            self.assertEqual(len(saves.for_player("Ann")), 2)
            self.assertEqual(store.usernames(), ["Ann", "Bob"])  # answering no changes nothing
            out = io.StringIO()
            with mock.patch("builtins.input", return_value="yes"), contextlib.redirect_stdout(out):
                self.assertEqual(accounts_admin.main(args), 0)
            self.assertIn("1 saved game(s) deleted, 1 kept for the others", out.getvalue())
            self.assertEqual(store.usernames(), ["Bob"])
            self.assertEqual(sorted(g["id"] for g in saves.for_player("Bob")), ["duo", "other"])  # Bob keeps the shared game
            duo = saves.load("duo")
            assert duo is not None
            self.assertEqual(duo["players"], ["deleted player", "Bob"])
            self.assertNotIn("Ann", json.dumps(duo))  # her name is gone from the creator and discoverer fields too
            self.assertFalse(saves.exists("solo"))
            self.assertEqual(saves.for_player("Ann"), [])


class SSEClient:
    """Minimal server-sent-events reader for the integration tests."""

    def __init__(self, port: int, slug: str, token: str) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        status, data = call(port, "POST", f"/g/{slug}/ticket", {"player": token})
        ticket = data["ticket"] if status == 200 else "no-such-ticket"  # an unknown player gets the stream's 403
        self.conn.request("GET", f"/g/{slug}/events?ticket={ticket}")
        self.resp = self.conn.getresponse()
        self.status = self.resp.status

    def next(self) -> tuple[str, Any]:
        lines: list[str] = []
        while True:
            line = self.resp.fp.readline().decode("utf-8")  # type: ignore[union-attr]
            if line in ("\n", "\r\n"):
                if lines and not all(x.startswith(("retry:", ":")) for x in lines):
                    return parse_event("".join(lines))
                lines = []
            elif line == "":
                raise EOFError("stream closed")
            else:
                lines.append(line)

    def until(self, wanted: str) -> Any:
        for _ in range(50):
            name, data = self.next()
            if name == wanted:
                return data
        raise AssertionError(f"never saw {wanted}")

    def close(self) -> None:
        self.resp.close()
        self.conn.close()


SESSIONS: dict[int, str] = {}  # port -> login session that call() sends by default (the "Host" account)


def serve(registry: GameRegistry) -> tuple[AlchemyServer, int]:
    tmp = tempfile.TemporaryDirectory()
    accounts = AccountStore(Path(tmp.name) / "accounts.json", iterations=1000)  # few iterations: fast tests
    server = AlchemyServer(("127.0.0.1", 0), registry, BASE_DIR / "static", accounts)
    server.register_throttle = Throttle(10_000, 1.0)  # the tests make many accounts from one address
    close = server.server_close

    def close_and_clean() -> None:
        close()
        tmp.cleanup()

    server.server_close = close_and_clean  # type: ignore[method-assign]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    SESSIONS[port] = login_as(port, "Host")
    return server, port


def login_as(port: int, name: str) -> str:
    """A login session for `name`, creating the account if it does not exist yet."""
    body = {"username": name, "password": f"password-{name}"}
    status, data = call(port, "POST", "/register", body, headers={"X-Session": ""})
    if status == 409:
        status, data = call(port, "POST", "/login", body, headers={"X-Session": ""})
    assert status == 200, data
    return data["session"]


def call(port: int, method: str, path: str, body: Any = None, raw: bytes | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        sent = {"Content-Type": "application/json"} if payload is not None else {}
        if port in SESSIONS:
            sent["X-Session"] = SESSIONS[port]
        sent.update(headers or {})
        conn.request(method, path, body=payload, headers=sent)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, (json.loads(data) if "json" in resp.getheader("Content-Type", "") else data)
    finally:
        conn.close()


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = GameRegistry(load_recipes(RECIPES), max_games=50)
        cls.server, cls.port = serve(cls.registry)
        cls.counter = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def new_game(self) -> str:
        type(self).counter += 1
        status, data = call(self.port, "POST", "/games", {"name": f"test {self.counter}"})
        self.assertEqual(status, 200)
        return data["id"]

    def join(self, slug: str, name: str) -> dict[str, Any]:
        status, data = call(self.port, "POST", f"/g/{slug}/join", {},
                            headers={"X-Session": login_as(self.port, name)})
        self.assertEqual(status, 200)
        self.assertEqual(data["name"], name)  # the player's name is the account's username
        return data

    def post(self, slug: str, who: dict[str, Any], path: str, **body: Any) -> tuple[int, Any]:
        return call(self.port, "POST", f"/g/{slug}{path}", {"player": who["token"], **body})

    def test_home_page_game_list_and_creating_a_game(self) -> None:
        status, body = call(self.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Start a new game", body)
        status, data = call(self.port, "POST", "/games", {"name": "Friday Night"})
        self.assertEqual((status, data), (200, {"id": "friday-night", "name": "Friday Night", "url": "/g/friday-night"}))
        listing = call(self.port, "GET", "/games")[1]
        self.assertEqual(listing["max_games"], 50)
        self.assertIn("friday-night", [g["id"] for g in listing["games"]])
        self.assertEqual(call(self.port, "POST", "/games", {"name": "friday night"})[0], 409)
        self.assertEqual(call(self.port, "POST", "/games", {"name": 5})[0], 400)

    def test_game_pages_and_static_files(self) -> None:
        slug = self.new_game()
        status, body = call(self.port, "GET", f"/g/{slug}")
        self.assertEqual(status, 200)
        self.assertIn(b"Co-op Alchemy", body)
        self.assertEqual(call(self.port, "GET", f"/g/{slug}/")[0], 200)
        status, body = call(self.port, "GET", "/g/never-existed")
        self.assertEqual(status, 404)
        self.assertIn(b"isn't running", body)
        for name in ("app.js", "style.css", "home.js", "home.html"):
            self.assertEqual(call(self.port, "GET", f"/static/{name}")[0], 200)
        self.assertEqual(call(self.port, "GET", "/static/../server.py")[0], 404)
        self.assertEqual(call(self.port, "GET", "/static/%2e%2e/server.py")[0], 404)
        self.assertEqual(call(self.port, "GET", "/nope")[0], 404)
        self.assertEqual(call(self.port, "GET", "/events?player=x")[0], 404)  # the old unscoped route is gone

    def test_bad_requests(self) -> None:
        slug = self.new_game()
        self.assertEqual(call(self.port, "POST", f"/g/{slug}/join", raw=b"not json")[0], 400)
        self.assertEqual(call(self.port, "POST", f"/g/{slug}/join", raw=b"[1]")[0], 400)
        self.assertEqual(call(self.port, "POST", f"/g/{slug}/tile/add", {"element": "water"})[0], 400)  # no token
        body = {"player": "zzz", "element": "water", "x": 0, "y": 0}
        self.assertEqual(call(self.port, "POST", f"/g/{slug}/tile/add", body)[0], 403)
        self.assertEqual(call(self.port, "POST", f"/g/{slug}/elsewhere", {})[0], 404)
        self.assertEqual(call(self.port, "POST", "/g/missing/join", {})[0], 404)
        self.assertEqual(call(self.port, "POST", "/join", {})[0], 404)
        self.assertEqual(SSEClient(self.port, slug, "bogus").status, 403)
        self.assertEqual(SSEClient(self.port, "missing", "bogus").status, 404)

    def test_duplicate_name_is_refused_and_five_players_at_most(self) -> None:
        slug = self.new_game()
        self.join(slug, "Dupe")
        status, data = call(self.port, "POST", f"/g/{slug}/join", {},
                            headers={"X-Session": login_as(self.port, "Dupe")})  # the same account again
        self.assertEqual(status, 409)
        self.assertIn("taken", data["error"])
        for name in ("Bob", "Cat", "Dan", "Eve"):
            self.join(slug, name)
        status, data = call(self.port, "POST", f"/g/{slug}/join", {}, headers={"X-Session": login_as(self.port, "Fay")})
        self.assertEqual(status, 409)
        self.assertIn("full", data["error"])
        other = self.new_game()
        self.join(other, "Dupe")  # another game has its own seats and names

    def test_hint_and_genie_endpoints(self) -> None:
        slug = self.new_game()
        who = self.join(slug, "Hinty")
        status, data = self.post(slug, who, "/hint", kind="find", target="steam")
        self.assertEqual((status, data["target"]["id"]), (200, "steam"))
        self.assertEqual(self.post(slug, who, "/hint", kind="find", target="unicorn")[0], 404)
        self.assertEqual(self.post(slug, who, "/hint", kind="bogus")[0], 400)
        self.assertEqual(self.post(slug, who, "/hint", kind="can_make")[1]["kind"], "can_make")
        status, data = self.post(slug, who, "/genie", element="fire")
        self.assertEqual(status, 409)
        self.assertIn("not unlocked", data["error"])

    def test_journal_endpoint(self) -> None:
        slug = self.new_game()
        who = self.join(slug, "Writer")
        status, data = self.post(slug, who, "/journal")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(e["id"] for e in data["elements"]), ["air", "earth", "fire", "water"])
        self.assertEqual(call(self.port, "POST", f"/g/{slug}/journal", {"player": "zzz"})[0], 403)
        self.assertEqual(call(self.port, "POST", f"/g/{slug}/journal", {})[0], 400)  # no token
        for name in ("journal.js",):
            self.assertEqual(call(self.port, "GET", f"/static/{name}")[0], 200)

    def test_the_game_list_shows_players_and_the_genie(self) -> None:
        slug = self.new_game()
        who = self.join(slug, "Lister")
        sse = SSEClient(self.port, slug, who["token"])
        try:
            sse.until("snapshot")
            game = next(g for g in call(self.port, "GET", "/games")[1]["games"] if g["id"] == slug)
            self.assertEqual((game["players"], game["found"]), (1, 4))
            self.assertEqual(game["genie"], {"available": False})  # still locked, so nothing is revealed
        finally:
            sse.close()

    def test_two_players_share_a_workspace(self) -> None:
        slug = self.new_game()
        alice, bob = self.join(slug, "Alice"), self.join(slug, "Bob")
        sse_a, sse_b = SSEClient(self.port, slug, alice["token"]), SSEClient(self.port, slug, bob["token"])
        try:
            snap_a = sse_a.until("snapshot")
            self.assertEqual(snap_a["you"], alice["pid"])
            self.assertEqual(snap_a["game"]["id"], slug)
            self.assertEqual(snap_a["genie"], {"available": False})  # hidden until the Djinn is unlocked
            self.assertIn(alice["pid"], [p["pid"] for p in snap_a["players"]])
            snap_b = sse_b.until("snapshot")
            self.assertEqual(snap_b["you"], bob["pid"])
            self.assertEqual({alice["pid"], bob["pid"]} - {p["pid"] for p in snap_b["players"]}, set())

            status, tile_w = self.post(slug, alice, "/tile/add", element="water", x=0.3, y=0.3)
            self.assertEqual(status, 200)
            status, tile_f = self.post(slug, alice, "/tile/add", element="fire", x=0.6, y=0.6)
            self.assertEqual(sse_b.until("tile_added")["tile"]["id"], tile_w["id"])

            self.assertEqual(self.post(slug, bob, "/cursor", x=0.4, y=0.5)[0], 200)
            cursor = sse_a.until("cursor")
            self.assertEqual((cursor["pid"], cursor["x"], cursor["y"]), (bob["pid"], 0.4, 0.5))

            self.assertEqual(self.post(slug, bob, "/tile/grab", tile=tile_w["id"])[0], 200)
            status, err = self.post(slug, alice, "/tile/grab", tile=tile_w["id"])
            self.assertEqual(status, 409)
            self.assertIn("someone else", err["error"])

            self.assertEqual(self.post(slug, bob, "/tile/move", tile=tile_w["id"], x=0.55, y=0.6, cx=0.56, cy=0.61)[0], 200)
            moved = sse_a.until("tile_moved")
            self.assertEqual((moved["pid"], moved["x"]), (bob["pid"], 0.55))

            status, res = self.post(slug, bob, "/combine", a=tile_w["id"], b=tile_f["id"], x=0.6, y=0.6)
            self.assertEqual(status, 200)
            self.assertEqual(res["new"], ["steam"])
            found = sse_a.until("discovered")
            self.assertEqual((found["id"], found["by"], found["pid"]), ("steam", "Bob", bob["pid"]))
            self.assertEqual(sse_a.until("tile_added")["tile"]["element"], "steam")

            self.assertEqual(self.post(slug, alice, "/tiles/clear")[0], 200)
            sse_b.until("tiles_cleared")
        finally:
            sse_a.close()
            sse_b.close()

    def test_creating_a_fourth_game_over_http_is_refused(self) -> None:
        server, port = serve(GameRegistry(load_recipes(RECIPES)))
        try:
            for name in ("a", "b", "c"):
                self.assertEqual(call(port, "POST", "/games", {"name": name})[0], 200)
            status, data = call(port, "POST", "/games", {"name": "d"})
            self.assertEqual(status, 409)
            self.assertIn("3 games are already running", data["error"])
            self.assertEqual(call(port, "GET", "/games")[1]["max_games"], 3)
        finally:
            server.shutdown()
            server.server_close()


    def test_ending_a_game_notifies_players_and_closes_their_streams(self) -> None:
        slug = self.new_game()
        a, b = self.join(slug, "Ann"), self.join(slug, "Bob")
        stream = SSEClient(self.port, slug, b["token"])
        stream.until("snapshot")
        self.assertEqual(self.post(slug, a, "/end")[0], 400)  # must say whether to save
        self.assertEqual(self.post(slug, {"token": "bad"}, "/end", save=False)[0], 403)
        self.assertEqual(self.post(slug, a, "/end", save=False), (200, {"saved": False}))
        self.assertEqual(stream.until("ended"), {"by": "Ann", "saved": False})
        with self.assertRaises(EOFError):
            stream.next()
        stream.close()
        self.assertEqual(call(self.port, "GET", f"/g/{slug}")[0], 404)
        self.assertEqual(self.post(slug, a, "/end", save=False)[0], 404)

    def test_element_pictures_are_listed_and_served(self) -> None:
        status, data = call(self.port, "GET", "/images")
        self.assertEqual(status, 200)
        names = data["images"]
        self.assertEqual(names, sorted(names))
        folder = BASE_DIR / "static" / "elements"
        self.assertEqual(names, sorted(p.stem for p in folder.glob("*.png")))
        if names:  # the folder holds whatever pictures the project has
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                conn.request("GET", f"/static/elements/{names[0]}.png")
                res = conn.getresponse()
                body = res.read()
            finally:
                conn.close()
            self.assertEqual(res.status, 200)
            self.assertEqual(res.getheader("Content-Type"), "image/png")
            self.assertTrue(body.startswith(b"\x89PNG"))
            self.assertIn("max-age", res.getheader("Cache-Control"))
        self.assertEqual(call(self.port, "GET", "/static/elements/../server.py")[0], 404)

    def test_lan_address_for_invite_links(self) -> None:
        self.assertEqual(call(self.port, "GET", "/lan", headers={"X-Session": ""})[0], 401)
        self.assertEqual(call(self.port, "GET", "/lan"), (200, {"base": ""}))  # tests do not set one
        self.server.public_base = "http://192.168.1.20:8000"
        try:
            self.assertEqual(call(self.port, "GET", "/lan")[1], {"base": "http://192.168.1.20:8000"})
        finally:
            self.server.public_base = ""

    def test_games_list_marks_recently_left_games(self) -> None:
        slug = self.new_game()
        ann = self.join(slug, "Annie")
        stream = SSEClient(self.port, slug, ann["token"])
        stream.until("snapshot")
        mine = {"X-Session": login_as(self.port, "Annie")}

        def entry(headers: dict[str, str]) -> dict[str, Any]:
            return next(g for g in call(self.port, "GET", "/games", headers=headers)[1]["games"] if g["id"] == slug)

        self.assertFalse(entry(mine)["recent"])  # she is in it right now
        stream.close()
        for _ in range(60):  # the server notices the closed connection within a few seconds
            if entry(mine)["recent"]:
                break
            threading.Event().wait(0.2)
        self.assertTrue(entry(mine)["recent"])
        self.assertFalse(entry({"X-Session": login_as(self.port, "Other Person")})["recent"])
        self.assertFalse(entry({})["recent"])  # the default Host account was never in it

    def test_saving_over_http_is_for_the_creator_only(self) -> None:
        slug = self.new_game()  # created by the default Host account
        host = self.join(slug, "Host")
        guest = self.join(slug, "Guest")
        snapshot = SSEClient(self.port, slug, guest["token"])
        self.assertEqual(snapshot.until("snapshot")["game"]["creator"], "Host")
        snapshot.close()
        status, data = self.post(slug, guest, "/end", save=True)
        self.assertEqual(status, 403)
        self.assertIn("only Host", data["error"])
        self.assertEqual(call(self.port, "GET", f"/g/{slug}")[0], 200)  # still running
        self.assertEqual(self.post(slug, guest, "/end", save=False), (200, {"saved": False}))

    def test_saved_games_can_be_found_and_resumed_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = GameRegistry(load_recipes(RECIPES), max_games=1, saves=SaveStore(tmp))
            server, port = serve(registry)
            try:
                ann = {"X-Session": login_as(port, "Ann")}
                _, game = call(port, "POST", "/games", {"name": "Keep Me"}, headers=ann)
                _, who = call(port, "POST", f"/g/{game['id']}/join", {}, headers=ann)
                tile = call(port, "POST", f"/g/{game['id']}/tile/add",
                            {"player": who["token"], "element": "water", "x": 0.2, "y": 0.2})[1]
                other = call(port, "POST", f"/g/{game['id']}/tile/add",
                             {"player": who["token"], "element": "fire", "x": 0.6, "y": 0.6})[1]
                call(port, "POST", f"/g/{game['id']}/tile/grab", {"player": who["token"], "tile": tile["id"]})
                call(port, "POST", f"/g/{game['id']}/combine",
                     {"player": who["token"], "a": tile["id"], "b": other["id"]})
                self.assertEqual(call(port, "POST", f"/g/{game['id']}/end",
                                      {"player": who["token"], "save": True}), (200, {"saved": True}))
                call(port, "POST", "/games", {"name": "Filler"})  # the only slot is taken again
                self.assertEqual(call(port, "POST", "/games", {"name": "One more"})[0], 409)
                status, data = call(port, "GET", "/saved", headers=ann)
                self.assertEqual(status, 200)
                self.assertEqual([g["id"] for g in data["games"]], ["keep-me"])
                self.assertEqual(call(port, "GET", "/saved")[1], {"games": []})  # Host was not in that game
                self.assertEqual(call(port, "POST", "/games/resume", {"id": "keep-me"})[0], 403)
                self.assertEqual(call(port, "POST", "/games/resume", {"id": "keep-me"}, headers=ann)[1]["url"],
                                 "/g/keep-me")
                self.assertEqual(call(port, "POST", "/games/resume", {"id": "keep-me"}, headers=ann)[0], 409)
                self.assertEqual(call(port, "POST", "/games/resume", {"id": "nope"}, headers=ann)[0], 404)
                self.assertEqual(call(port, "POST", "/games/resume", {}, headers=ann)[0], 400)
                _, again = call(port, "POST", "/g/keep-me/join", {}, headers=ann)
                snapshot = SSEClient(port, "keep-me", again["token"])
                names = [e["id"] for e in snapshot.until("snapshot")["elements"]]
                snapshot.close()
                self.assertIn("steam", names)
            finally:
                server.shutdown()
                server.server_close()

    def test_phones_and_tablets_get_the_same_pages_and_api_as_laptops(self) -> None:
        phone = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"}
        android = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}
        desktop = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"}
        slug = self.new_game()
        for headers in (phone, android, desktop):
            for path in ("/", "/login", "/privacy", f"/g/{slug}", "/static/app.js", "/static/style.css"):
                status, body = call(self.port, "GET", path, headers=headers)
                self.assertEqual(status, 200, (headers["User-Agent"][:20], path))
            self.assertEqual(call(self.port, "GET", "/games", headers=headers)[0], 200)
            status, game = call(self.port, "POST", "/games", {"name": f"phone game {headers['User-Agent'][13:17]}"}, headers=headers)
            self.assertEqual(status, 200, game)
            call(self.port, "POST", "/games/resume", {"id": "nothing"}, headers=headers)  # reachable (a 404 for the id, not a 403)
        self.assertEqual(call(self.port, "GET", "/laptop")[0], 404)  # the page phones used to be sent to is gone

    def test_everything_needs_a_login(self) -> None:
        slug = self.new_game()
        none = {"X-Session": ""}
        bogus = {"X-Session": "not-a-session"}
        for headers in (none, bogus):
            for method, path, body in (("GET", "/games", None), ("GET", "/saved", None), ("GET", "/me", None),
                                       ("POST", "/games", {"name": "x"}), ("POST", "/games/resume", {"id": slug}),
                                       ("POST", f"/g/{slug}/join", {})):
                self.assertEqual(call(self.port, method, path, body, headers=headers)[0], 401, (method, path))
        for page in ("/", f"/g/{slug}", "/login", "/privacy", "/static/auth.js"):
            self.assertEqual(call(self.port, "GET", page, headers=none)[0], 200, page)  # pages hold nothing private

    def test_register_login_me_and_logout_over_http(self) -> None:
        none = {"X-Session": ""}
        status, data = call(self.port, "POST", "/register", {"username": "Zoe Z", "password": "zoe-pass-1"}, headers=none)
        self.assertEqual((status, data["username"]), (200, "Zoe Z"))
        session = {"X-Session": data["session"]}
        self.assertEqual(call(self.port, "GET", "/me", headers=session)[1], {"username": "Zoe Z", "admin": False, "must_change_password": False})
        self.assertEqual(call(self.port, "POST", "/register", {"username": "zoe z", "password": "zoe-pass-2"},
                              headers=none)[0], 409)
        self.assertEqual(call(self.port, "POST", "/register", {"username": "Q", "password": "zoe-pass-2"},
                              headers=none)[0], 400)
        self.assertEqual(call(self.port, "POST", "/login", {"username": "zoe z", "password": "bad"}, headers=none)[0], 401)
        status, data = call(self.port, "POST", "/login", {"username": "ZOE Z", "password": "zoe-pass-1"}, headers=none)
        self.assertEqual((status, data["username"]), (200, "Zoe Z"))
        self.assertEqual(call(self.port, "POST", "/logout", {}, headers=session), (200, {}))
        self.assertEqual(call(self.port, "GET", "/me", headers=session)[0], 401)
        self.assertEqual(call(self.port, "GET", "/me", headers={"X-Session": data["session"]})[0], 200)

    def test_the_game_name_comes_from_the_account_not_the_request(self) -> None:
        slug = self.new_game()
        session = {"X-Session": login_as(self.port, "Real Name")}
        status, data = call(self.port, "POST", f"/g/{slug}/join", {"name": "Someone Else"}, headers=session)
        self.assertEqual((status, data["name"]), (200, "Real Name"))


if __name__ == "__main__":
    unittest.main()
