"""A stand-in for LM Studio's server, for tests and for trying the pipeline without a model.

    python3 -m aigen.fake_lmstudio 1235

Embeddings are hashed character trigrams (crude but deterministic); the "chat model" invents pseudo-words.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIMS = 96
_PAIR = re.compile(r"^(\d+)\. (.+) \+ (.+)$", re.M)


def fake_embedding(text: str) -> list[float]:
    padded = f"  {text.casefold()} "
    vec = [0.0] * DIMS
    for i in range(len(padded) - 2):
        vec[zlib.crc32(padded[i:i + 3].encode()) % DIMS] += 1.0
    return vec


_GOAL = re.compile(r'Goal: make a new element called "([^"]+)"')
_AVAILABLE = re.compile(r"^Available elements: (.+)$", re.M)


_ELEMENT = re.compile(r'^Element: "([^"]+)"', re.M)


def fake_profile_answer(prompt: str) -> str:
    """What the goal looks like, etc. A croissant resembles a moon (crescent) and bread."""
    goal = _ELEMENT.search(prompt).group(1)
    if "Available elements:" in prompt:  # the model picks from the list it was shown
        if goal == "croissant":
            return json.dumps({"kind_elements": ["bread", "cake"], "look_elements": ["moon"], "made_of": ["dough"],
                               "used_for": [], "comes_from": ["france"]})
        return json.dumps({"kind_elements": [], "look_elements": [], "made_of": [], "used_for": [], "comes_from": []})
    if goal == "croissant":
        return json.dumps({"kind_of": ["bread", "pastry"], "looks_like": ["crescent"], "resembles": ["moon", "horn"],
                           "made_of": ["dough"], "used_for": [], "comes_from": ["france"]})
    return json.dumps({"looks_like": ["round"], "resembles": [], "made_of": [], "used_for": [], "comes_from": []})


_JUDGE_GOAL = re.compile(r'Goal: a new element called "([^"]+)"')


def fake_judge_answer(prompt: str) -> str:
    """Ratings for the numbered pairs. For a croissant only moon + bread is great and butter + flour is okay."""
    goal = _JUDGE_GOAL.search(prompt).group(1)
    if goal == "flibbertigibbet":  # a judge that never answers in JSON
        return "I am sorry, I cannot do that."
    rows = []
    for n, a, b in _PAIR.findall(prompt.split("Pairs:", 1)[-1]):  # not the instructions above the list
        pair = {a, b}
        rating, reads_as, others = 3, f"{a} {b}", []
        if goal == "croissant":
            if pair == {"moon", "bread"}:
                rating, reads_as, others = 5, "moon-shaped bread", ["bagel"]
            elif pair == {"butter", "flour"}:  # generic: the judge likes it but names three rivals
                rating, reads_as, others = 5, "buttery dough", ["pastry", "brioche", "cookie"]
            else:
                rating = 1
        rows.append({"n": int(n), "reads_as": reads_as, "others": others, "rating": rating})
    return json.dumps(rows)


def fake_goal_answer(prompt: str) -> str:
    """Options for a goal. 'jacket' can only be made with a helper called 'clothes' (like the real example)."""
    goal = _GOAL.search(prompt).group(1)
    avail = _AVAILABLE.search(prompt).group(1).split("; ")
    if goal == "croissant":  # the model itself never thinks of moon + bread, and invents a look-alike helper
        rows = [{"a": "butter", "b": "flour", "link": "material", "why": "core ingredients", "rating": 5},
                {"a": "butter", "b": "crescent moon", "link": "shape", "why": "crescent look", "rating": 4}]
    elif goal == "jacket":
        rows = [{"a": avail[0], "b": "clothes"}, {"a": avail[1], "b": "clothes"}]
    else:
        rows = [{"a": avail[0], "b": avail[1]}, {"a": avail[2], "b": avail[3]}]
        if "OR be a new intermediate" in prompt:
            rows.append({"a": avail[4], "b": goal[:4] + "part"})
    return json.dumps(rows)


def fake_answer(prompt: str) -> str:
    if "A good recipe is SPECIFIC" in prompt:
        return fake_judge_answer(prompt)
    if "Describe it for a recipe-design game" in prompt:
        return fake_profile_answer(prompt)
    if _GOAL.search(prompt):
        return fake_goal_answer(prompt)
    rows = []
    for n, a, b in _PAIR.findall(prompt):
        n = int(n)
        result = None if n % 5 == 0 else (a.split()[0][:4] + b.split()[-1][-4:]).lower()
        rows.append({"n": n, "result": result})
    return "```json\n" + json.dumps(rows) + "\n```"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # quiet
        pass

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.endswith("/models"):
            self._send({"data": [{"id": "fake-gpt-oss-20b"}, {"id": "fake-embed-model"}]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path.endswith("/embeddings"):
            texts = payload["input"]
            self._send({"data": [{"index": i, "embedding": fake_embedding(t)} for i, t in enumerate(texts)]})
        elif self.path.endswith("/chat/completions"):
            prompt = payload["messages"][-1]["content"]
            self._send({"choices": [{"finish_reason": "stop", "message": {"content": fake_answer(prompt)}}]})
        else:
            self.send_error(404)


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start in a background thread; returns the server and its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


if __name__ == "__main__":
    srv, url = start(int(sys.argv[1]) if len(sys.argv) > 1 else 1235)
    print("fake LM Studio at", url)
    threading.Event().wait()
