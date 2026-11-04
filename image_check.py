"""Show which elements have a picture in static/elements/ and which pictures match nothing.

Usage:
    python3 image_check.py recipes.json [--images static/elements]

A picture belongs to an element when its file name (without .png) equals the element's
id or label, lower-cased, with every run of other characters turned into "-" and
apostrophes dropped ("Alarm Clock" -> alarm-clock.png). This only reads files.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path


def slugify(text: str) -> str:
    """Same rule as slugify() in static/app.js."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    plain = plain.lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Match element pictures to elements")
    parser.add_argument("recipes")
    parser.add_argument("--images", default=str(Path(__file__).resolve().parent / "static" / "elements"))
    args = parser.parse_args(argv[1:])
    try:
        data = json.loads(Path(args.recipes).read_text(encoding="utf-8"))
        elements = data["elements"]
        pictures = {p.stem for p in Path(args.images).glob("*.png")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"could not read the files: {exc}", file=sys.stderr)
        return 1
    used: set[str] = set()
    missing = []
    for eid, info in elements.items():
        label = info.get("label", eid) if isinstance(info, dict) else eid
        match = next((s for s in (slugify(eid), slugify(label)) if s in pictures), None)
        if match:
            used.add(match)
        else:
            missing.append(f"{eid}  (looks for {slugify(eid)}.png" +
                           (f" or {slugify(label)}.png)" if slugify(label) != slugify(eid) else ")"))
    unused = sorted(pictures - used)
    print(f"{len(elements) - len(missing)} of {len(elements)} elements have a picture; "
          f"{len(pictures)} picture files, {len(unused)} unused.")
    if missing:
        print(f"\nElements without a picture ({len(missing)}):")
        print("\n".join(f"  {line}" for line in missing))
    if unused:
        print(f"\nPictures that match no element ({len(unused)}):")
        print("\n".join(f"  {name}.png" for name in unused))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
