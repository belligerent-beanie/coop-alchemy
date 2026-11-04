"""Draw every recipe in a recipe file as one big Mermaid flowchart.

Usage:
    python3 recipe_flowchart.py [recipes.json] [-o recipes.mmd] [--html recipes.html]
                                [--direction LR|TD] [--tiers] [--focus ELEMENT]

Every recipe becomes a small "+" node: both ingredients point into it and it points
at whatever it makes. Starting elements are highlighted.

    --tiers   group the elements into boxes by how many steps it takes to reach them
    --focus   draw only the recipes that lead to one element (much smaller)
    --html    also write a page that draws the chart in your browser (needs internet
              once, to load the Mermaid library) with zoom and an SVG download button
    --svg     write the chart as an SVG file instead of drawing it in a browser. This
              is much faster for a big chart. It uses Graphviz (the `dot` program:
              install it from graphviz.org, or `brew install graphviz`); the picture
              looks a little different from Mermaid's but shows the same recipes.
    --dot     write the Graphviz source too (to draw it with other Graphviz tools)

A chart with thousands of recipes is bigger than the default limits of most Mermaid
viewers (mermaid.live, GitHub, VS Code previews). The --html page raises those
limits, so use it for the full chart; or paste the .mmd file into a viewer you have
configured with a high maxEdges setting.

The recipe file is only read, never changed.
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
from pathlib import Path

from hints import find_element
from recipes import RecipeBook, RecipeError, depths, load_recipes

MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs"


def label_text(text: str) -> str:
    """Escape a label for use inside "..." in Mermaid (entity codes are written #NN;)."""
    out = []
    for ch in text:
        if ch in '"#&<>`\\' or ord(ch) < 32:
            out.append(f"#{ord(ch)};")
        else:
            out.append(ch)
    return "".join(out)


def needed_for(book: RecipeBook, target: str) -> set[str]:
    """`target` and every element that can be used, directly or not, to make it."""
    makers: dict[str, set[str]] = {}
    for (a, b), results in book.lookup.items():
        for r in results:
            makers.setdefault(r, set()).update((a, b))
    seen = {target}
    todo = [target]
    while todo:
        for ingredient in makers.get(todo.pop(), ()):
            if ingredient not in seen:
                seen.add(ingredient)
                todo.append(ingredient)
    return seen


def build_flowchart(book: RecipeBook, *, direction: str = "LR", tiers: bool = False,
                    focus: str | None = None) -> str:
    """The flowchart as Mermaid text. `focus` is an element id to restrict the chart to."""
    if direction not in ("LR", "TD"):
        raise ValueError("direction must be LR or TD")
    depth = depths(book)
    wanted = needed_for(book, focus) if focus else set(book.elements)

    def order(eid: str) -> tuple[int, str]:
        return (depth.get(eid, 10**6), book.elements[eid].label.casefold())

    ids = sorted((e for e in book.elements if e in wanted), key=order)
    node = {eid: f"e{i}" for i, eid in enumerate(ids)}

    def element_line(eid: str) -> str:
        element = book.elements[eid]
        text = f"{element.emoji} {element.label}".strip()
        return f'{node[eid]}["{label_text(text)}"]'

    lines = [f"flowchart {direction}"]
    if tiers:
        groups: dict[int | None, list[str]] = {}
        for eid in ids:
            groups.setdefault(depth.get(eid), []).append(eid)
        for level in sorted(groups, key=lambda d: (d is None, d or 0)):
            title = "Unreachable" if level is None else ("Starting elements" if level == 0 else f"Tier {level}")
            lines.append(f'  subgraph tier{"X" if level is None else level}["{title}"]')
            lines.extend(f"    {element_line(eid)}" for eid in groups[level])
            lines.append("  end")
    else:
        lines.extend(f"  {element_line(eid)}" for eid in ids)

    combos: list[str] = []
    edges = 0
    for (a, b), results in sorted(book.lookup.items(), key=lambda item: (order(item[0][0]), order(item[0][1]))):
        results = tuple(r for r in results if r in node)
        if a not in node or b not in node or not results:
            continue
        combo = f"c{len(combos)}"
        combos.append(combo)
        lines.append(f'  {combo}(("+"))')
        if a == b:
            lines.append(f'  {node[a]} -- "x2" --> {combo}')
            edges += 1
        else:
            lines.append(f"  {node[a]} --> {combo}")
            lines.append(f"  {node[b]} --> {combo}")
            edges += 2
        for r in results:
            lines.append(f"  {combo} --> {node[r]}")
            edges += 1

    lines.append("  classDef starter fill:#ffe7a3,stroke:#b8860b,stroke-width:2px,color:#222")
    lines.append("  classDef combo fill:#eee,stroke:#999,color:#333")
    starters = [node[s] for s in book.starters if s in node]
    if starters:
        lines.append(f"  class {','.join(starters)} starter")
    if combos:
        lines.append(f"  class {','.join(combos)} combo")
    lines.append(f"  %% {len(ids)} elements, {len(combos)} combinations, {edges} arrows")
    return "\n".join(lines) + "\n"


def dot_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def build_dot(book: RecipeBook, *, direction: str = "LR", tiers: bool = False,
              focus: str | None = None) -> str:
    """The same chart as Graphviz source (draws far faster than Mermaid when it is huge)."""
    if direction not in ("LR", "TD"):
        raise ValueError("direction must be LR or TD")
    depth = depths(book)
    wanted = needed_for(book, focus) if focus else set(book.elements)
    ids = sorted((e for e in book.elements if e in wanted),
                 key=lambda e: (depth.get(e, 10**6), book.elements[e].label.casefold()))
    node = {eid: f"e{i}" for i, eid in enumerate(ids)}
    lines = ["digraph recipes {", f"  rankdir={'LR' if direction == 'LR' else 'TB'};",
             "  splines=line; nslimit=1; mclimit=0.2; ranksep=0.4; nodesep=0.1;  // favour speed on big charts",
             '  node [shape=box, style="rounded,filled", fillcolor="#ffffff", fontname="Helvetica"];',
             '  edge [color="#888888", arrowsize=0.6];']
    for eid in ids:
        element = book.elements[eid]
        text = f"{element.emoji} {element.label}".strip()
        fill = ', fillcolor="#ffe7a3", penwidth=2' if eid in book.starters else ""
        lines.append(f'  {node[eid]} [label="{dot_text(text)}"{fill}];')
    if tiers:
        groups: dict[int, list[str]] = {}
        for eid in ids:
            if eid in depth:
                groups.setdefault(depth[eid], []).append(node[eid])
        lines += [f"  {{ rank=same; {'; '.join(names)}; }}" for _, names in sorted(groups.items())]
    count = 0
    for (a, b), results in sorted(book.lookup.items()):
        results = tuple(r for r in results if r in node)
        if a not in node or b not in node or not results:
            continue
        combo = f"c{count}"
        count += 1
        lines.append(f'  {combo} [label="+", shape=circle, width=0.25, fixedsize=true, fillcolor="#eeeeee", '
                     f'color="#999999", fontsize=10];')
        doubled = ' [label="x2"]' if a == b else ""
        lines.append(f"  {node[a]} -> {combo}{doubled};")
        if a != b:
            lines.append(f"  {node[b]} -> {combo};")
        lines += [f"  {combo} -> {node[r]};" for r in results]
    lines.append("}")
    return "\n".join(lines) + "\n"


def html_page(chart: str, title: str) -> str:
    """A page that draws `chart` with high Mermaid limits, plus zoom and download buttons."""
    chart_json = json.dumps(chart).replace("</", "<\\/")  # keep "</script>" out of the page's script
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{ margin: 0; font: 15px system-ui, sans-serif; }}
  #bar {{ position: sticky; top: 0; z-index: 1; padding: 8px 12px; background: #fff; border-bottom: 1px solid #ccc; }}
  #bar button {{ font-size: 1rem; margin-right: 6px; }}
  #status {{ margin-left: 8px; color: #555; }}
  #stage {{ overflow: auto; height: calc(100vh - 50px); }}
  #chart {{ transform-origin: 0 0; display: inline-block; }}
</style>
</head>
<body>
<div id="bar">
  <button id="out" type="button">&minus;</button><button id="in" type="button">+</button>
  <button id="fit" type="button">Fit width</button><button id="save" type="button">Download SVG</button>
  <span id="status">Drawing the chart (a big one can take a minute)&hellip;</span>
</div>
<div id="stage"><div id="chart"></div></div>
<script type="module">
import mermaid from "{MERMAID_URL}";
const source = {chart_json};
const status = document.getElementById("status");
const holder = document.getElementById("chart");
let zoom = 1;
const apply = () => {{ holder.style.transform = `scale(${{zoom}})`; }};
document.getElementById("in").onclick = () => {{ zoom *= 1.25; apply(); }};
document.getElementById("out").onclick = () => {{ zoom /= 1.25; apply(); }};
document.getElementById("fit").onclick = () => {{
  const svg = holder.querySelector("svg");
  if (svg) {{ zoom = document.getElementById("stage").clientWidth / svg.getBoundingClientRect().width * zoom; apply(); }}
}};
document.getElementById("save").onclick = () => {{
  const svg = holder.querySelector("svg");
  if (!svg) return;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([svg.outerHTML], {{ type: "image/svg+xml" }}));
  link.download = "recipes.svg";
  link.click();
}};
mermaid.initialize({{ startOnLoad: false, maxTextSize: 100000000, maxEdges: 1000000,
                      flowchart: {{ useMaxWidth: false, htmlLabels: false }} }});
try {{
  const {{ svg }} = await mermaid.render("recipes", source);
  holder.innerHTML = svg;
  status.textContent = "";
}} catch (err) {{
  status.textContent = "Could not draw the chart: " + err.message;
}}
</script>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Draw a recipe file as a Mermaid flowchart")
    parser.add_argument("recipes", nargs="?", default=str(Path(__file__).resolve().parent / "recipes.json"))
    parser.add_argument("-o", "--output", default="recipes.mmd", help="Mermaid file to write (default recipes.mmd)")
    parser.add_argument("--html", help="also write a page that draws the chart in a browser")
    parser.add_argument("--svg", help="write an SVG file using Graphviz (fast, no browser needed)")
    parser.add_argument("--dot", help="write the Graphviz source to this file")
    parser.add_argument("--direction", choices=("LR", "TD"), default="LR")
    parser.add_argument("--tiers", action="store_true", help="group elements by how many steps they take to reach")
    parser.add_argument("--focus", metavar="ELEMENT", help="only draw what is needed to make this element")
    args = parser.parse_args(argv[1:])

    try:
        book = load_recipes(args.recipes)
    except RecipeError as exc:
        print(exc, file=sys.stderr)
        return 1
    target = None
    if args.focus:
        target = find_element(book, args.focus)
        if target is None:
            print(f"there is no element called {args.focus!r}", file=sys.stderr)
            return 1
    chart = build_flowchart(book, direction=args.direction, tiers=args.tiers, focus=target)
    if args.svg or args.dot:
        dot = build_dot(book, direction=args.direction, tiers=args.tiers, focus=target)
        try:
            if args.dot:
                Path(args.dot).write_text(dot, encoding="utf-8")
            if args.svg:
                program = shutil.which("dot")
                if program is None:
                    print("Graphviz is not installed, so the SVG can't be made. Install it from graphviz.org "
                          "(macOS: brew install graphviz), then run this again. The .mmd file is still written.",
                          file=sys.stderr)
                    Path(args.output).write_text(chart, encoding="utf-8")
                    return 1
                done = subprocess.run([program, "-Tsvg", "-o", args.svg], input=dot, text=True,
                                      encoding="utf-8", capture_output=True)
                if done.returncode != 0:
                    print(f"Graphviz failed: {done.stderr.strip()[:500]}", file=sys.stderr)
                    return 1
        except OSError as exc:
            print(f"cannot write the output: {exc}", file=sys.stderr)
            return 1
    try:
        Path(args.output).write_text(chart, encoding="utf-8")
        if args.html:
            Path(args.html).write_text(html_page(chart, "Recipe flowchart"), encoding="utf-8")
    except OSError as exc:
        print(f"cannot write the output: {exc}", file=sys.stderr)
        return 1
    written = [args.output] + [x for x in (args.html, args.svg, args.dot) if x]
    print(chart.splitlines()[-1].strip("% ").strip(), "->", ", ".join(written))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
