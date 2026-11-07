"""categories.json sorts every element of recipes.json into exactly one category."""
import json
import unittest
from pathlib import Path

import categories
from games import GameRegistry
from test_coop import RECIPES, load_recipes

HERE = Path(__file__).parent


class CategoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.elements = json.loads((HERE / "recipes.json").read_text(encoding="utf-8"))["elements"]
        self.categories = json.loads((HERE / "categories.json").read_text(encoding="utf-8"))["categories"]

    def test_every_element_is_in_exactly_one_category(self) -> None:
        placed = [label for c in self.categories for label in c["elements"]]
        self.assertEqual(sorted(placed), sorted(self.elements), "add new elements to categories.json (and remove deleted ones)")

    def test_categories_are_named_and_not_empty(self) -> None:
        names = [c["name"] for c in self.categories]
        self.assertEqual(len(names), len(set(names)))
        for c in self.categories:
            self.assertTrue(c["name"] and c["description"] and c["elements"], c["name"])


class JournalCategoryTests(unittest.TestCase):
    def test_the_journal_counts_each_category_without_naming_what_is_missing(self) -> None:
        registry = GameRegistry(load_recipes(RECIPES))
        game = registry.create("Cats", "ann")
        token = game.join("ann").token
        journal = game.journal(token)
        tally = journal["progress"]["categories"]
        self.assertEqual(sum(c["total"] for c in tally), journal["progress"]["total"])
        self.assertEqual(sum(c["found"] for c in tally), journal["progress"]["found"])
        self.assertTrue(all(set(c) == {"name", "found", "total"} for c in tally))
        by_name = {c["name"]: c for c in tally}
        self.assertEqual(by_name["Materials & Substances"]["found"], 4, "the four starters")
        self.assertEqual({e["category"] for e in journal["elements"]}, {"Materials & Substances"})

    def test_an_unsorted_element_falls_into_other(self) -> None:
        self.assertEqual(categories.of("Fire"), "Materials & Substances")
        self.assertEqual(categories.of("something new"), categories.OTHER)


if __name__ == "__main__":
    unittest.main()
