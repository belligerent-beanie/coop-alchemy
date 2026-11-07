"""categories.json sorts every element of recipes.json into exactly one category."""
import json
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
