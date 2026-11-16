"""Turn open SonarQube Cloud findings on main into GitHub issues that an agent can pick up.

Usage:
    python3 ops/sonar_issues.py            print what would change (no token needed)
    python3 ops/sonar_issues.py --apply    create and close GitHub issues (needs GITHUB_TOKEN and GITHUB_REPOSITORY)

Findings are grouped by rule and file, so one issue holds every place one rule fires in one file. Each issue
gets the `sonar` label and one kind label:

    sonar:small-fix              a real, local problem with an obvious fix
    sonar:likely-false-positive  rules that, in this code base, mostly flag safe code (the agent must still check)
    sonar:complexity             a function that is too complex: a behaviour-preserving refactor

Nothing starts an agent by itself: a person adds the `agent` label to an issue (see AGENTS.md).
An issue whose findings are gone from SonarQube is closed with a comment. An issue someone closed is never reopened,
so closing one is how to say "leave this alone".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field

SONAR = "https://sonarcloud.io/api"
GITHUB = "https://api.github.com"
PROJECT = "belligerent-beanie_coop-alchemy"
MARKER = "<!-- sonar-group: {} -->"

LIKELY_FALSE_POSITIVE = {
    # "Agentic workflow" injections: command-line arguments of operator scripts treated as if a language model wrote them.
    "pythonsecurity:S8701", "pythonsecurity:S8703", "pythonsecurity:S8705", "pythonsecurity:S8707",
    "pythonsecurity:S2083",   # paths come from the server's own configuration, not from requests
    "pythonsecurity:S5131",   # flags every response write; responses carry explicit content types and nosniff
    "pythonsecurity:S6350", "pythonsecurity:S6549",
    "jssecurity:S6105", "jssecurity:S8480",
    "python:S4790",           # sha1 used as a cache-busting fingerprint (usedforsecurity=False)
    "python:S1313", "python:S5863", "javascript:S4036", "javascript:S3403",
}
COMPLEXITY = {
    "python:S3776", "javascript:S3776",   # cognitive complexity
    "python:S1541", "javascript:S1541",   # cyclomatic complexity
    "python:S138", "javascript:S138",     # function too long
    "python:S107", "javascript:S107",     # too many parameters
}
KINDS = {
    "sonar:small-fix": ("0e8a16", "A real, local problem with an obvious fix"),
    "sonar:likely-false-positive": ("fbca04", "Probably safe code; confirm, then record the decision"),
    "sonar:complexity": ("5319e7", "Too complex; refactor without changing behaviour"),
}
LABELS = {
    "sonar": ("1d76db", "Raised by SonarQube"),
    "agent": ("d93f0b", "Hand this issue to the fixer agent"),
    **KINDS,
}
SEVERITY_ORDER = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]


def kind_of(rule: str) -> str:
    if rule in COMPLEXITY:
        return "sonar:complexity"
    if rule in LIKELY_FALSE_POSITIVE:
        return "sonar:likely-false-positive"
    return "sonar:small-fix"


@dataclass
class Group:
    rule: str
    rule_name: str
    path: str
    findings: list[dict] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.rule}|{self.path}"

    @property
    def severity(self) -> str:
        return min((f.get("severity", "INFO") for f in self.findings), key=SEVERITY_ORDER.index)

    def title(self) -> str:
        count = f" ({len(self.findings)})" if len(self.findings) > 1 else ""
        return f"Sonar {self.rule}: {self.rule_name} in {self.path}{count}"[:250]

    def body(self) -> str:
        rule_url = f"https://sonarcloud.io/organizations/belligerent-beanie/rules?open={urllib.parse.quote(self.rule)}&rule_key={urllib.parse.quote(self.rule)}"
        lines = [
            f"SonarQube rule [`{self.rule}`]({rule_url}): **{self.rule_name}**",
            f"File: `{self.path}` · highest severity: {self.severity} · kind: `{kind_of(self.rule)}`",
            "",
            "| Line | Severity | Message |",
            "|---|---|---|",
        ]
        for f in sorted(self.findings, key=lambda f: f.get("line") or 0):
            message = f.get("message", "").replace("|", "\\|").replace("\n", " ")
            link = f"https://sonarcloud.io/project/issues?id={PROJECT}&open={f['key']}"
            lines.append(f"| {f.get('line', '–')} | {f.get('severity', '')} | [{message}]({link}) |")
        lines += ["", "Add the `agent` label to hand this to the fixer agent. Close it to leave it alone.",
                  MARKER.format(self.key)]
        return "\n".join(lines)


def group_findings(issues: list[dict], rule_names: dict[str, str]) -> dict[str, Group]:
    groups: dict[str, Group] = {}
    for issue in issues:
        path = issue["component"].split(":", 1)[-1]
        group = Group(issue["rule"], rule_names.get(issue["rule"], issue["rule"]), path)
        groups.setdefault(group.key, group).findings.append(issue)
    return groups


def parse_marker(body: str | None) -> str | None:
    head, sep, rest = (body or "").partition("<!-- sonar-group: ")
    return rest.split(" -->", 1)[0] if sep else None


def _get_json(url: str, token: str | None = None, method: str = "GET", payload: dict | None = None) -> object:
    headers = {"Accept": "application/json", "User-Agent": "coop-alchemy-sonar-issues"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310  (fixed https API hosts)
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  (fixed https API hosts)
        text = response.read().decode()
    return json.loads(text) if text else None


def sonar_findings(branch: str) -> tuple[list[dict], dict[str, str]]:
    issues: list[dict] = []
    names: dict[str, str] = {}
    page = 1
    while True:
        query = urllib.parse.urlencode({"componentKeys": PROJECT, "branch": branch, "resolved": "false",
                                        "ps": 500, "p": page, "additionalFields": "rules"})
        data = _get_json(f"{SONAR}/issues/search?{query}")
        issues += data["issues"]
        names.update({r["key"]: r["name"] for r in data.get("rules", [])})
        if page * 500 >= min(data["paging"]["total"], 10000):
            return issues, names
        page += 1


class GitHub:
    def __init__(self, repo: str, token: str):
        self.base, self.token = f"{GITHUB}/repos/{repo}", token

    def call(self, path: str, method: str = "GET", payload: dict | None = None) -> object:
        return _get_json(self.base + path, self.token, method, payload)

    def sonar_issues(self) -> list[dict]:
        found, page = [], 1
        while batch := self.call(f"/issues?labels=sonar&state=all&per_page=100&page={page}"):
            found += [i for i in batch if "pull_request" not in i]
            page += 1
        return found

    def ensure_labels(self) -> None:
        existing = {label["name"] for label in self.call("/labels?per_page=100")}
        for name, (color, description) in LABELS.items():
            if name not in existing:
                self.call("/labels", "POST", {"name": name, "color": color, "description": description})


def plan(groups: dict[str, Group], tracked: list[dict], max_new: int) -> tuple[list[Group], list[dict]]:
    """Which groups need a new issue (most severe first, at most max_new) and which open issues are now resolved."""
    known = {parse_marker(i.get("body")): i for i in tracked}
    new = [g for key, g in groups.items() if key not in known]
    new.sort(key=lambda g: (SEVERITY_ORDER.index(g.severity), g.rule, g.path))
    resolved = [i for key, i in known.items() if key and key not in groups and i["state"] == "open"]
    return new[:max_new], resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="change GitHub (default: only print the plan)")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--max-new", type=int, default=15, help="most new issues per run (default 15)")
    args = parser.parse_args(argv)

    groups = group_findings(*sonar_findings(args.branch))
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    github = GitHub(repo, token) if token and repo else None
    if args.apply and github is None:
        print("--apply needs GITHUB_TOKEN and GITHUB_REPOSITORY", file=sys.stderr)
        return 2
    tracked = github.sonar_issues() if github else []
    new, resolved = plan(groups, tracked, args.max_new)

    counts: dict[str, int] = defaultdict(int)
    for g in groups.values():
        counts[kind_of(g.rule)] += 1
    print(f"{sum(len(g.findings) for g in groups.values())} findings in {len(groups)} groups: "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"{len(new)} new issue(s), {len(resolved)} to close" + ("" if github else " (no token: existing issues not checked)"))
    for g in new:
        print(f"  + [{kind_of(g.rule)}] {g.title()}")
    for issue in resolved:
        print(f"  - #{issue['number']} {issue['title']}")
    if not args.apply:
        return 0

    github.ensure_labels()
    for g in new:
        github.call("/issues", "POST", {"title": g.title(), "body": g.body(), "labels": ["sonar", kind_of(g.rule)]})
    for issue in resolved:
        github.call(f"/issues/{issue['number']}/comments", "POST",
                    {"body": "SonarQube no longer reports these findings on main, so this is done."})
        github.call(f"/issues/{issue['number']}", "PATCH", {"state": "closed", "state_reason": "completed"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
