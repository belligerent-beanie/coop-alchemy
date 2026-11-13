"""Design patches: named UI changes that can be put on the site and taken off again without touching the real styles.

A patch is a file in patches/:

    patches/<name>.css         the style changes (required). Its first /* comment */ is the patch's title. Rules can be
                               scoped to one page with the attribute look.js puts on <html>: [data-page="home"] .card { … }
                               (pages: home, game, race, create, admin, ops, login, verify, privacy, terms).
                               The default look styles pages with id selectors (html[data-look="euler"] #home h1), which beat a plain
                               [data-page="home"] h1: repeat the ids (html[data-page="home"] #home h1) or the rule will not show.
                               Unbalanced braces are refused when the patch is switched on, and the patch is left out of what is served.
    patches/<name>.text.json   text changes (optional): [{"page": "home", "selector": "h1", "text": "New words"}, …]
                               ("page" may be left out to mean every page). Only elements with no children are changed.

Each patch is in one of three states, kept in patches_state.json (data, not code: it is not in git):

    off       nobody sees it (the default for a new patch)
    preview   only administrators and designers see it, so it can be tried on the live site first
    on        everybody sees it

The server serves the patches that apply to the caller at /patches.css (linked from every page's <head>, so they apply
before the page is drawn) and /patches/text (applied by look.js). Administrators switch them on the operations page,
designers in the UI picker's "Patches" panel, and anyone with access to the server with:

    python3 patches.py list
    python3 patches.py on|preview|off <name>

When a patch should stay for good, fold it into static/style.css and delete its files; when it is not wanted, switch it
off or delete it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
PATCH_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")   # also what makes a name safe to use as a file name
STATES = ("off", "preview", "on")
MAX_TEXT_LEN = 500


COMMENT = re.compile(r"/\*.*?\*/", re.S)
STRING = re.compile(r'"(?:\\.|[^"\\\n])*"' + r"|'(?:\\.|[^'\\\n])*'")


def css_problem(css: str) -> str:
    """"" when the braces balance, else what is wrong. One unclosed { swallows the rules of every patch served after it, so a
    patch like that is refused when it is switched on and left out when it is served."""
    text = STRING.sub("", COMMENT.sub("", css))   # a comment or a string may hold any brace
    depth = 0
    for char in text:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return "a } closes nothing"
    return f"{depth} unclosed {{" if depth else ""


class PatchError(ValueError):
    """A request about patches that cannot be done (an unknown patch, a bad state, a broken patch file)."""


class PatchStore:
    def __init__(self, patches_dir: str | Path, state_file: str | Path | None) -> None:
        self.dir = Path(patches_dir)
        self.state_file = Path(state_file) if state_file else None
        self._lock = threading.Lock()
        self._memory: dict[str, str] = {}   # the states when there is no state file (tests)

    # ---- what exists, and in what state ----

    def names(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.css") if PATCH_NAME.match(p.stem))

    def _states(self) -> dict[str, str]:
        if self.state_file is None:
            return dict(self._memory)
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}   # unreadable: everything off, which is the safe way to fail
        return {k: v for k, v in raw.items() if isinstance(k, str) and v in STATES} if isinstance(raw, dict) else {}

    def state(self, name: str) -> str:
        return self._states().get(name, "off")

    def set_state(self, name: str, state: str) -> None:
        if not isinstance(name, str) or name not in self.names():
            raise PatchError(f"there is no patch called {name!r}")
        if state not in STATES:
            raise PatchError(f"the state must be one of {', '.join(STATES)}")
        problem = css_problem(self._css_of(name))
        if problem and state != "off":
            raise PatchError(f"{name}.css cannot be switched on: {problem}")
        with self._lock:
            states = self._states()
            if state == "off":
                states.pop(name, None)
            else:
                states[name] = state
            if self.state_file is None:
                self._memory = states
                return
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(json.dumps(states, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, self.state_file)   # all or nothing: a crash never leaves half a file

    def title(self, name: str) -> str:
        m = re.match(r"\s*/\*\s*(.*?)\s*\*/", self._css_of(name), re.S)
        return m.group(1).splitlines()[0].strip() if m else name

    def listing(self) -> list[dict[str, Any]]:
        states = self._states()
        rows = []
        for name in self.names():
            problem = css_problem(self._css_of(name))
            problem = f"{name}.css: {problem}" if problem else ""
            try:
                texts = len(self._text_of(name))
            except PatchError as exc:
                texts, problem = 0, problem or str(exc)
            rows.append({"name": name, "title": self.title(name), "state": states.get(name, "off"),
                         "text_changes": texts, "problem": problem})
        return rows

    # ---- what a visitor gets ----

    def _active(self, include_preview: bool) -> list[str]:
        wanted = ("on", "preview") if include_preview else ("on",)
        states = self._states()
        return [n for n in self.names() if states.get(n) in wanted]

    def _css_of(self, name: str) -> str:
        try:
            return (self.dir / f"{name}.css").read_text(encoding="utf-8")
        except OSError:
            return ""

    def _text_of(self, name: str) -> list[dict[str, str]]:
        path = self.dir / f"{name}.text.json"
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PatchError(f"{path.name} is not valid JSON: {exc}") from None
        if not isinstance(raw, list):
            raise PatchError(f"{path.name} must be a list of changes")
        changes = []
        for item in raw:
            if not (isinstance(item, dict) and isinstance(item.get("selector"), str) and isinstance(item.get("text"), str)
                    and isinstance(item.get("page", "*"), str) and len(item["text"]) <= MAX_TEXT_LEN):
                raise PatchError(f"{path.name}: each change needs a \"selector\" and a \"text\" (and may have a \"page\")")
            changes.append({"page": item.get("page", "*"), "selector": item["selector"], "text": item["text"]})
        return changes

    def css(self, include_preview: bool) -> str:
        parts = []
        for name in self._active(include_preview):
            if css_problem(self._css_of(name)):
                continue   # edited into a broken state after it was switched on: it must not swallow the patches after it
            parts.append(f"/* ---- patch {name} ({self.state(name)}) ---- */\n{self._css_of(name).strip()}\n")
        return "\n".join(parts)

    def text(self, include_preview: bool) -> list[dict[str, str]]:
        changes = []
        for name in self._active(include_preview):
            try:
                changes += [{**change, "patch": name} for change in self._text_of(name)]
            except PatchError:
                continue   # a broken text file leaves that patch's text alone; the listing shows the problem
        return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Switch design patches on, to preview, or off")
    parser.add_argument("--dir", default=str(BASE_DIR / "patches"), help="where the patch files are (default ./patches)")
    parser.add_argument("--state-file", default=str(BASE_DIR / "patches_state.json"),
                        help="where the on/off state is kept (default ./patches_state.json; the server's --patches-state-file)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="every patch with its state")
    for state in STATES:
        sub.add_parser(state, help=f"switch a patch to {state}").add_argument("name")
    args = parser.parse_args(argv)
    store = PatchStore(args.dir, args.state_file)
    if args.command == "list":
        rows = store.listing()
        if not rows:
            print(f"No patches in {store.dir}.")
        for row in rows:
            extra = f", {row['text_changes']} text change(s)" if row["text_changes"] else ""
            problem = f"  PROBLEM: {row['problem']}" if row["problem"] else ""
            print(f"{row['state']:8} {row['name']}  ({row['title']}{extra}){problem}")
        return 0
    try:
        store.set_state(args.name, args.command)
    except PatchError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{args.name} is now {args.command}. Pages pick it up when they are next loaded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
