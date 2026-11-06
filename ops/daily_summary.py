"""A short daily summary of what happened on the live server, from its log files, for glancing at.

    python ops/daily_summary.py [--date YYYY-MM-DD] [--live-dir DIR]     (default: yesterday)

Reads <live folder>\\data\\live.log* and writes <live folder>\\summaries\\<date>.txt (and prints it). The summary has counts
only: no usernames and no addresses, so it can be kept longer than the log it comes from (which is deleted after 14 days).
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops import backup as backup_tools  # noqa: E402

# (label, text that identifies the log line)
EVENTS = (
    ("sign-ups", " alchemy.auth register ok"),
    ("logins", " alchemy.auth login ok"),
    ("failed logins", " alchemy.auth login failed"),
    ("rate-limited requests", " throttled "),
    ("emails confirmed", " alchemy.auth email verified"),
    ("passwords changed", " alchemy.auth password changed"),
    ("accounts deleted", " alchemy.auth account deleted"),
    ("administrator actions", " alchemy.auth admin "),
    ("server errors", " alchemy.server "),
    ("emails that could not be sent", " alchemy.mail could not send"),
    ("human-check problems", " alchemy.captcha "),
)
CLIENT = re.compile(r"\bclient=(\S+)")


def summarise(log_files: list[Path], day: str) -> dict[str, int]:
    """Counts of each event on `day` (YYYY-MM-DD) across the given log files, plus how many different addresses were seen."""
    counts = {label: 0 for label, _ in EVENTS}
    clients: set[str] = set()
    for path in log_files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.startswith(day):
                continue
            for label, marker in EVENTS:
                if marker in line:
                    counts[label] += 1
            found = CLIENT.search(line)
            if found:
                clients.add(found.group(1))
    counts["different network addresses"] = len(clients)
    return counts


def render(day: str, counts: dict[str, int], found_logs: bool) -> str:
    lines = [f"Co-op Alchemy summary for {day}", ""]
    if not found_logs:
        lines.append("(no log file found: logging to a file is not switched on, or the log is already deleted)")
    width = max(len(label) for label in counts)
    lines += [f"{label:<{width}}  {value}" for label, value in counts.items()]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise a day of the live server's log")
    parser.add_argument("--live-dir", default=str(backup_tools.default_live_dir()))
    parser.add_argument("--date", default=time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400)))
    args = parser.parse_args(argv)
    live = Path(args.live_dir)
    logs = sorted((live / "data").glob("live.log*"))
    text = render(args.date, summarise(logs, args.date), bool(logs))
    (live / "summaries").mkdir(parents=True, exist_ok=True)
    (live / "summaries" / f"{args.date}.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
