"""ops/sonar_issues.py: grouping, labelling and the create/close plan (no network)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "ops"))
import sonar_issues as si  # noqa: E402  (ops/ is not a package)


def finding(rule: str, path: str, line: int, severity: str = "MAJOR", key: str = "k") -> dict:
    return {"rule": rule, "component": f"{si.PROJECT}:{path}", "line": line, "severity": severity,
            "message": "a | b", "key": key}


class SonarIssuesTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(si.kind_of("python:S3776"), "sonar:complexity")
        self.assertEqual(si.kind_of("pythonsecurity:S2083"), "sonar:likely-false-positive")
        self.assertEqual(si.kind_of("css:S4657"), "sonar:small-fix")

    def test_groups_by_rule_and_file(self):
        groups = si.group_findings([finding("css:S4657", "static/style.css", 9),
                                    finding("css:S4657", "static/style.css", 3, "CRITICAL"),
                                    finding("css:S4657", "static/other.css", 1)], {"css:S4657": "Shorthand"})
        self.assertEqual(len(groups), 2)
        group = groups["css:S4657|static/style.css"]
        self.assertEqual(group.severity, "CRITICAL")
        self.assertIn("(2)", group.title())
        body = group.body()
        self.assertEqual(si.parse_marker(body), group.key)
        self.assertLess(body.index("| 3 |"), body.index("| 9 |"))
        self.assertIn("a \\| b", body)   # a pipe in a message must not break the table

    def test_plan_creates_missing_and_closes_resolved(self):
        groups = si.group_findings([finding("css:S4657", "a.css", 1, "MINOR"),
                                    finding("python:S3776", "b.py", 1, "BLOCKER")], {})
        tracked = [
            {"number": 1, "state": "open", "title": "old", "body": si.MARKER.format("gone|x.py")},
            {"number": 2, "state": "closed", "title": "left alone", "body": si.MARKER.format("css:S4657|a.css")},
            {"number": 3, "state": "closed", "title": "also gone", "body": si.MARKER.format("gone|y.py")},
        ]
        new, resolved = si.plan(groups, tracked, max_new=10)
        self.assertEqual([g.key for g in new], ["python:S3776|b.py"])   # the closed one is never recreated
        self.assertEqual([i["number"] for i in resolved], [1])
        new, _ = si.plan(groups, [], max_new=1)
        self.assertEqual(new[0].severity, "BLOCKER")   # most severe first when capped


if __name__ == "__main__":
    unittest.main()
