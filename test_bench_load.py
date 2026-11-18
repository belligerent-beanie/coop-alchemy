"""bench_load.main() exits 0 when every scenario ran without errors and 1 when any scenario recorded errors (table and --json output)."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from typing import Any
from unittest import mock

import bench_load


def result(scenario: str, errors: int) -> dict[str, Any]:
    """The numbers one scenario reports: every row of the table, with the errors set as asked."""
    row: dict[str, Any] = {key: 0 for key, _label, _digits in bench_load.ROWS}
    row.update(scenario=scenario, errors=errors, error_kinds={"timeout": errors} if errors else {})
    return row


def run_main(results: list[dict[str, Any]], *flags: str) -> tuple[int, str]:
    """Run main() with run_scenario answering from `results`; returns (exit code, what it printed)."""
    answers = iter(results)
    argv = ["bench_load.py", "--scenarios", ",".join(r["scenario"] for r in results), *flags]
    out = io.StringIO()
    with mock.patch.object(bench_load, "run_scenario", side_effect=lambda spec, seconds: next(answers)), \
            mock.patch("sys.argv", argv), contextlib.redirect_stdout(out):
        code = bench_load.main()
    return code, out.getvalue()


class ExitCodeTests(unittest.TestCase):
    def test_no_errors_exits_0(self) -> None:
        for flags in ((), ("--json",)):
            with self.subTest(flags=flags):
                code, _ = run_main([result("1x5", 0), result("3x5", 0)], *flags)
                self.assertEqual(code, 0)

    def test_any_scenario_with_errors_exits_1_in_table_mode(self) -> None:
        code, printed = run_main([result("1x5", 0), result("3x5", 2)])
        self.assertEqual(code, 1)
        self.assertIn("errors in 3x5", printed)
        self.assertNotIn("errors in 1x5", printed)

    def test_any_scenario_with_errors_exits_1_in_json_mode(self) -> None:
        code, printed = run_main([result("1x5", 0), result("3x5", 2)], "--json")
        self.assertEqual(code, 1)
        self.assertEqual([r["errors"] for r in json.loads(printed)], [0, 2])


if __name__ == "__main__":
    unittest.main()
