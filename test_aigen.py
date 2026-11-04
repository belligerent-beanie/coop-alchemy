"""Tests for the AI suggestion pipeline (aigen/), run against the fake LM Studio server."""
from __future__ import annotations

import json
import random
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from aigen import fake_lmstudio
from aigen.embeddings import EmbeddingCache
from aigen.generate import Settings, available_ids, run
from aigen.lmstudio import DailyLimitError, LMStudio, LMStudioError, is_daily_limit, retry_delay, strip_reasoning
from aigen.planner import PlanSettings, Planner, TargetResult, plan_targets
from aigen.ratings import RatingCache
from aigen.propose import (Judgement, NameIndex, ProposeError, name_key, normalize_name, parse_options, parse_profile,
                           parse_ratings, parse_reply)
from aigen.scorer import Scorer, auc, features, train
from drafts import DraftStore
from recipes import load_recipes

BASE_DIR = Path(__file__).resolve().parent
REAL = BASE_DIR / "recipes.json"


class NameTests(unittest.TestCase):
    def test_normalize_name(self) -> None:
        self.assertEqual(normalize_name('  "Lighthouse." '), "Lighthouse")
        self.assertEqual(normalize_name("ICE CASTLE"), "ice castle")
        self.assertEqual(normalize_name("baba yaga"), "baba yaga")
        for bad in (None, "", "a b c d", "x" * 40, "fire2", "hello! world?", 7):
            self.assertIsNone(normalize_name(bad), bad)

    def test_plural_and_case_count_as_the_same_name(self) -> None:
        self.assertEqual(name_key("Cats"), name_key("cat"))
        self.assertEqual(name_key("berries"), name_key("Berry"))
        self.assertNotEqual(name_key("glass"), name_key("glas"))
        index = NameIndex()
        index.add("Cat", "cat")
        self.assertEqual(index.lookup("cats"), "cat")
        self.assertIsNone(index.lookup("dog"))


class ParseTests(unittest.TestCase):
    def test_parse_reply(self) -> None:
        text = 'Sure!\n```json\n[{"n": 1, "result": "fog"}, {"n": 2, "result": null}, {"n": 9, "result": "x"}]\n```'
        self.assertEqual(parse_reply(text, 2), {1: "fog", 2: None})
        for bad in ("no json here", "[1, 2]", "[{broken"):
            with self.assertRaises(ProposeError):
                parse_reply(bad, 2)

    def test_parse_options_and_profile(self) -> None:
        options = parse_options('[{"a": "moon", "b": "bread", "link": "Shape", "why": " crescent  shaped ", "rating": "5"},'
                                ' {"a": "x", "b": "y"}, {"a": 1}]')
        self.assertEqual([(o.a, o.b, o.link, o.why, o.rating) for o in options],
                         [("moon", "bread", "shape", "crescent shaped", 5), ("x", "y", "", "", 0)])
        self.assertEqual(parse_options('[{"a": "p", "b": "q", "link": "weird", "rating": 99}]')[0][2:], ("", "", 5, "model"))
        with self.assertRaises(ProposeError):
            parse_options("[1, 2]")
        phrases = parse_profile('{"looks_like": ["Crescent", "crescent"], "resembles": ["moon", "a b c d"], "x": 3}')
        self.assertEqual(phrases, {"looks_like": ["crescent"], "resembles": ["moon"]})
        judged = parse_ratings('[{"n": 1, "rating": 4, "reads_as": " moon-shaped  bread", "others": ["bagel", 5]},'
                               ' {"n": 2, "rating": 9}, {"n": 7, "rating": 3}]', 3)
        self.assertEqual(judged, {1: (4, "moon-shaped bread", ("bagel",))})
        with self.assertRaises(ProposeError):
            parse_ratings("nothing", 2)
        with self.assertRaises(ProposeError):
            parse_profile('{"looks_like": []}')

    def test_cut_off_replies_keep_their_complete_entries(self) -> None:
        cut = ('```json\n[{"n": 1, "reads_as": "a", "others": [], "rating": 4},\n {"n": 2, "reads_as": "b", "others": ["x"], '
               '"rating": 5},\n {"n": 3, "reads_as": "unfinished')
        self.assertEqual(sorted(parse_ratings(cut, 3)), [1, 2])
        self.assertEqual([o.a for o in parse_options('[{"a": "x", "b": "y"}, {"a": "p", "b"')], ["x"])

    def test_strip_reasoning(self) -> None:
        self.assertEqual(strip_reasoning("<|channel|>analysis<|message|>hmm<|end|><|channel|>final<|message|>[1]<|return|>"), "[1]")
        self.assertEqual(strip_reasoning("  plain "), "plain")


class _FlakyChat(BaseHTTPRequestHandler):
    """A pretend remote chat service: answers 429 first, then needs the right API key."""
    calls: list[tuple[str, str | None]] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).calls.append((self.path, self.headers.get("Authorization")))
        if len(type(self).calls) == 1:
            body, status = b'{"error": {"message": "Rate limit reached. Please try again in 2.5s."}}', 429
        elif self.headers.get("Authorization") != "Bearer secret":
            body, status = b'{"error": "bad key"}', 401
        else:
            body, status = json.dumps({"choices": [{"message": {"content": "<think>hmm</think>[]"}}]}).encode(), 200
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RemoteChatTests(unittest.TestCase):
    def test_think_blocks_are_removed(self) -> None:
        self.assertEqual(strip_reasoning("<think>a\nb</think>\n[1]"), "[1]")
        self.assertEqual(strip_reasoning("half-open reasoning</think>[2]"), "[2]")

    def test_retry_delay(self) -> None:
        self.assertEqual(retry_delay({"Retry-After": "3"}, "", 0), 4.0)
        self.assertAlmostEqual(retry_delay({}, "Please try again in 2.5s.", 0), 3.5)
        self.assertAlmostEqual(retry_delay({}, "try again in 500ms", 0), 1.5)
        self.assertEqual(retry_delay({}, "no hint", 1), 10.0)

    def test_key_is_sent_and_rate_limits_are_waited_out(self) -> None:
        _FlakyChat.calls = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyChat)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        waits: list[float] = []
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/v1"
            client = LMStudio("http://127.0.0.1:9/v1", "openai/gpt-oss-20b", chat_base_url=url,
                              chat_api_key="secret", sleep=waits.append)
            self.assertTrue(client.remote_chat)
            self.assertEqual(client.chat("s", "u"), "[]")
            self.assertEqual(waits, [3.5])
            self.assertEqual([auth for _, auth in _FlakyChat.calls], ["Bearer secret"] * 2)
            wrong = LMStudio("http://127.0.0.1:9/v1", "m", chat_base_url=url, chat_api_key="nope", sleep=waits.append)
            with self.assertRaisesRegex(LMStudioError, "API key"):
                wrong.chat("s", "u")
        finally:
            server.shutdown()

    def test_remote_chat_needs_a_model_name(self) -> None:
        client = LMStudio("http://127.0.0.1:9/v1", chat_base_url="http://example.invalid/v1")
        with self.assertRaises(LMStudioError):
            client.resolve_models(need_chat=True, need_embed=False)


class ScorerTests(unittest.TestCase):
    def test_auc(self) -> None:
        self.assertEqual(auc([0.9, 0.8], [0.1, 0.2]), 1.0)
        self.assertEqual(auc([0.5], [0.5]), 0.5)

    def test_features_ignore_ingredient_order(self) -> None:
        va, vb, vr = [1.0, 0.0], [0.0, 1.0], [0.6, 0.8]
        self.assertEqual(features(va, vb, vr, "ice", "fire", "ice cube"), features(vb, va, vr, "fire", "ice", "ice cube"))
        self.assertEqual(features(va, vb, vr, "ice", "fire", "ice cube")[4], 1.0)  # shares a word

    def test_too_few_recipes(self) -> None:
        book = load_recipes(BASE_DIR / "test_recipes.json")
        with self.assertRaises(ValueError):
            train(book, lambda labels: [fake_lmstudio.fake_embedding(t) for t in labels], "fake", log=lambda s: None)

    def test_save_and_load(self) -> None:
        s = Scorer([1.0] * 6, 0.1, [0.0] * 6, [1.0] * 6, 0.4, "tag", {"auc": 1})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            s.save(path)
            self.assertEqual(Scorer.load(path).threshold, 0.4)
            with self.assertRaises(ValueError):
                Scorer.load(Path(tmp) / "missing.json")


class EmbeddingCacheTests(unittest.TestCase):
    def test_each_text_is_embedded_once_and_model_change_resets(self) -> None:
        calls: list[list[str]] = []

        def embed(texts: list[str]) -> list[list[float]]:
            calls.append(list(texts))
            return [fake_lmstudio.fake_embedding(t) for t in texts]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            cache = EmbeddingCache(embed, path, "m1")
            cache.get(["Water", "water", "fire"])
            cache.save()
            EmbeddingCache(embed, path, "m1").get(["fire"])
            self.assertEqual(calls, [["water", "fire"]])
            EmbeddingCache(embed, path, "m2").get(["fire"])
            self.assertEqual(len(calls), 2)


GROQ_DAILY = ('{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` '
              'service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199094, Requested 2749. '
              'Please try again in 13m16.17s.","type":"tokens","code":"rate_limit_exceeded"}}')
GROQ_MINUTE = '{"error":{"message":"Rate limit reached ... on tokens per minute (TPM): Please try again in 2.5s."}}'


def _http_429(body: str) -> urllib.error.HTTPError:
    import io
    return urllib.error.HTTPError("https://x/v1/chat/completions", 429, "Too Many Requests", {},  # type: ignore[arg-type]
                                  io.BytesIO(body.encode()))


class LimitAndUsageTests(unittest.TestCase):
    def test_daily_limit_is_told_apart_from_the_per_minute_one(self) -> None:
        self.assertTrue(is_daily_limit(GROQ_DAILY))
        self.assertTrue(is_daily_limit("... on requests per day (RPD) ..."))
        self.assertFalse(is_daily_limit(GROQ_MINUTE))

    def test_a_daily_limit_stops_at_once_without_waiting(self) -> None:
        waits: list[float] = []
        client = LMStudio("http://127.0.0.1:9/v1", "openai/gpt-oss-120b", sleep=waits.append)
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(_http_429(GROQ_DAILY))) as call:
            with self.assertRaises(DailyLimitError) as err:
                client.chat("s", "u")
        self.assertEqual((call.call_count, waits), (1, []))
        self.assertIn("13m16", str(err.exception))
        self.assertIn("daily limit", str(err.exception))

    def test_a_per_minute_limit_is_still_waited_out(self) -> None:
        waits: list[float] = []
        client = LMStudio("http://127.0.0.1:9/v1", "m", sleep=waits.append)
        good = mock.MagicMock()
        good.__enter__.return_value.read.return_value = json.dumps({"choices": [{"message": {"content": "[]"}}]}).encode()
        with mock.patch("urllib.request.urlopen", side_effect=[_http_429(GROQ_MINUTE), good]):
            self.assertEqual(client.chat("s", "u"), "[]")
        self.assertEqual(len(waits), 1)

    def test_token_usage_is_summed(self) -> None:
        client = LMStudio("http://127.0.0.1:9/v1", "m")
        replies = [{"choices": [{"message": {"content": "a"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}},
                   {"choices": [{"message": {"content": "b"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
                   {"choices": [{"message": {"content": "c"}}]}]  # no usage reported: still a call
        with mock.patch.object(client, "_call", side_effect=replies):
            for _ in replies:
                client.chat("s", "u")
        self.assertEqual(client.usage, {"calls": 3, "prompt": 110, "completion": 55, "total": 165})
        self.assertIn("165 tokens in 3 calls", client.usage_summary())


class RatingCacheTests(unittest.TestCase):
    def test_roundtrip_order_free_and_per_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            cache = RatingCache(path, "m1")
            cache.put("Waffles", "Batter", "Iron", Judgement(4, "batter in iron", ("pancake",)))
            cache.save()
            again = RatingCache(path, "m1")
            self.assertEqual(again.get("waffles", "iron", "batter"), Judgement(4, "batter in iron", ("pancake",)))
            self.assertEqual(again.hits, 1)
            self.assertIsNone(again.get("waffles", "iron", "egg"))
            self.assertIsNone(RatingCache(path, "other-model").get("waffles", "iron", "batter"))
            path.write_text("{broken")
            self.assertIsNone(RatingCache(path, "m1").get("waffles", "iron", "batter"))  # damaged: starts empty
            self.assertIsNone(RatingCache(None, "m1").get("a", "b", "c"))
            RatingCache(None, "m1").save()  # no file: nothing happens

    def test_rate_only_asks_about_pairs_it_has_not_seen(self) -> None:
        class Cand:
            def __init__(self, a: str, b: str) -> None:
                self.labels = [a, b]

        asked: list[str] = []

        class Client:
            def chat(self, system: str, prompt: str, **kw: object) -> str:
                asked.append(prompt)
                return json.dumps([{"n": i, "rating": 3, "reads_as": "x", "others": []} for i in range(1, 9)])

        planner = Planner.__new__(Planner)
        planner.client, planner.log = Client(), lambda s: None
        planner.ratings = RatingCache(None, "m")
        cands = [Cand("a", "b"), Cand("c", "d")]
        first = planner._rate("goal", cands)
        self.assertEqual((sorted(first), len(asked)), ([0, 1], 1))
        again = planner._rate("goal", cands + [Cand("e", "f")])
        self.assertEqual(sorted(again), [0, 1, 2])
        self.assertEqual(len(asked), 2)                     # only e + f was asked
        self.assertNotIn("a + b", asked[1])
        self.assertIn("e + f", asked[1])
        self.assertEqual(planner.ratings.hits, 2)
        planner._rate("goal", cands)
        self.assertEqual(len(asked), 2)                     # everything is known: no request at all


class _StubPlanner:
    """Stands in for Planner so plan_targets' stopping rules can be tried without a model."""
    client: LMStudio

    def __init__(self, settings: PlanSettings, **kw: object) -> None:
        self.client = kw["client"]  # type: ignore[assignment]
        self.trace: list[dict] = []
        self.profiles: list[dict] = []
        self.threshold = 0.0
        self.ratings = kw["ratings"] or RatingCache(None, "m")

    def plan(self, target: str) -> TargetResult:
        if target.startswith("old"):
            return TargetResult(target, "exists")
        self.client.usage["total"] += 1000
        if target == "boom":
            raise DailyLimitError("the daily limit is used up")
        return TargetResult(target, "no chain")


class StoppingRuleTests(unittest.TestCase):
    def _run(self, targets: list[str], **kw: object) -> dict:
        client = LMStudio("http://127.0.0.1:9/v1", "m")
        logs: list[str] = []
        with mock.patch("aigen.planner.Planner", _StubPlanner):
            report = plan_targets(targets, PlanSettings(**kw), book=None, client=client, cache=None,  # type: ignore[arg-type]
                                  scorer=None, store=None, log=logs.append)  # type: ignore[arg-type]
        return report

    def test_max_targets_counts_only_targets_that_needed_the_model(self) -> None:
        report = self._run(["old1", "a", "old2", "b", "c"], max_targets=2)
        self.assertEqual([t["target"] for t in report["targets"]], ["old1", "a", "old2", "b"])
        self.assertIn("--max-targets 2 reached", report["stopped"])
        self.assertIn("'c'", report["stopped"])
        self.assertEqual(self._run(["a", "b", "c"])["stopped"], "")  # 0 means no limit

    def test_token_budget_stops_before_the_next_target(self) -> None:
        report = self._run(["a", "b", "c"], token_budget=2000)
        self.assertEqual([t["target"] for t in report["targets"]], ["a", "b"])
        self.assertIn("--token-budget 2,000", report["stopped"])
        self.assertEqual(report["usage"]["total"], 2000)

    def test_a_daily_limit_keeps_what_is_done(self) -> None:
        report = self._run(["a", "boom", "c"])
        self.assertEqual([t["target"] for t in report["targets"]], ["a"])
        self.assertIn("daily limit", report["stopped"])


@unittest.skipUnless(REAL.exists(), "recipes.json not present")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, url = fake_lmstudio.start()
        cls.client = LMStudio(url)
        cls.client.resolve_models(need_chat=True, need_embed=True)
        cls.book = load_recipes(REAL)
        cls.cache = EmbeddingCache(cls.client.embed, None, "fake")
        cls.scorer = train(cls.book, cls.cache.get, "fake", log=lambda s: None)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_models_are_resolved(self) -> None:
        self.assertIn("gpt-oss", self.client.chat_model)
        self.assertIn("embed", self.client.embed_model)

    def test_unreachable_server(self) -> None:
        with self.assertRaises(LMStudioError):
            LMStudio("http://127.0.0.1:9/v1", timeout=2).models()

    def _run(self, store: DraftStore, **kw: object) -> dict:
        settings = Settings(new_elements=4, max_recipes=10, threshold=0.0, seed=1, **kw)
        return run(settings, book=self.book, client=self.client, cache=self.cache, scorer=self.scorer,
                   store=store, log=lambda s: None)

    def test_run_writes_drafts_and_proposals_in_order(self) -> None:
        store = DraftStore(None)
        report = self._run(store)
        self.assertTrue(report["accepted"])
        self.assertLessEqual(len(report["accepted"]), 10)
        round1 = [c for c in report["accepted"] if c["round"] == 1]
        self.assertTrue(all(not c["target"] for c in round1))          # round 1 is new elements only
        self.assertTrue(any(c["round"] == 2 for c in report["accepted"]))  # and something is built on them
        view = store.view("ai-suggester", self.book)
        self.assertEqual(len(view["recipes"]), len(report["written_drafts"]))
        proposals = store.queue(self.book)
        self.assertEqual(len(proposals), len(report["written_drafts"]))
        seen = set()
        for p in proposals:  # a new ingredient is always proposed before the recipes that use it
            for ingredient in (p["a"]["id"], p["b"]["id"]):
                if ingredient not in self.book.elements:
                    self.assertIn(ingredient, seen)
            seen.add(p["result"]["id"])
        for p in proposals:
            self.assertTrue(p["note"] == "" or p["note"].startswith("AI-suggested"))

    def test_dry_run_and_no_promote(self) -> None:
        store = DraftStore(None)
        self.assertEqual(self._run(store, dry_run=True)["written_drafts"], [])
        self.assertEqual(store.view("ai-suggester", self.book)["recipes"], [])
        report = self._run(store, promote=False)
        self.assertTrue(report["written_drafts"])
        self.assertEqual(store.queue(self.book), [])

    def test_second_run_does_not_repeat_names(self) -> None:
        store = DraftStore(None)
        first = {c["name"].casefold() for c in self._run(store)["accepted"] if not c["target"]}
        second = {c["name"].casefold() for c in self._run(store)["accepted"] if not c["target"]}
        self.assertFalse(first & second)


@unittest.skipUnless(REAL.exists(), "recipes.json not present")
class TargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, url = fake_lmstudio.start()
        cls.client = LMStudio(url)
        cls.client.resolve_models(need_chat=True, need_embed=True)
        cls.book = load_recipes(REAL)
        cls.cache = EmbeddingCache(cls.client.embed, None, "fake")
        cls.scorer = train(cls.book, cls.cache.get, "fake", log=lambda s: None)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def _plan(self, targets: list[str], store: DraftStore, **kw: object) -> dict:
        kw.setdefault("threshold", 0.0)
        kw.setdefault("min_rating", 3)  # the fake judge gives most pairs a 3
        settings = PlanSettings(seed=1, **kw)
        return plan_targets(targets, settings, book=self.book, client=self.client, cache=self.cache,
                            scorer=self.scorer, store=store, log=lambda s: None)

    def test_time_counts_as_available(self) -> None:
        ids = available_ids(self.book)
        self.assertTrue({"time", "ruins", "archeologist"} <= ids)
        self.assertEqual(len(ids), len(self.book.elements))

    def test_jacket_needs_a_helper_element(self) -> None:
        store = DraftStore(None)
        report = self._plan(["jacket"], store)
        result = report["targets"][0]
        self.assertEqual(result["status"], "planned")
        links = result["plan"]["links"]
        self.assertEqual([l["result"] for l in links], ["clothes", "jacket"])
        self.assertIn("clothes", (links[1]["a"], links[1]["b"]))
        for link in links:  # every ingredient exists, or was made by an earlier link
            made = {l["result"] for l in links[: links.index(link)]}
            for ingredient in (link["a"], link["b"]):
                self.assertTrue(ingredient in self.book.elements or ingredient in made, ingredient)
        proposals = store.queue(self.book)
        self.assertEqual([p["result"]["label"] for p in proposals], ["clothes", "jacket"])
        self.assertTrue(proposals[1]["note"].startswith("AI-suggested for the target 'jacket'"))

    def test_shape_recipe_is_built_by_the_code_and_won_by_the_rating(self) -> None:
        store = DraftStore(None)
        report = self._plan(["croissant"], store, threshold=0.4)  # what the model offered (rating 3) scores ~0.5
        result = report["targets"][0]
        self.assertEqual(result["status"], "planned")
        best = result["plan"]["links"][0]
        self.assertEqual({best["a"], best["b"]}, {"moon", "bread"})  # the model never suggested it: the code paired them
        self.assertEqual((best["link_type"], best["rating"]), ("shape", 5))
        self.assertIn("shaped like", best["why"])
        self.assertTrue(any(alt["links"][0]["rating"] == 3 for alt in result["alternatives"]))  # butter + flour
        self.assertIn("shape: bread shaped like moon", store.queue(self.book)[0]["note"])

    def test_variant_helpers_are_refused(self) -> None:
        report = self._plan(["croissant"], DraftStore(None), threshold=0.4)
        verdicts = [t["verdict"] for t in report["trace"]]
        self.assertTrue(any("just a variant of the existing 'moon'" in v for v in verdicts))
        self.assertFalse(any(t.get("new_helper") for t in report["trace"] if t["verdict"] == "kept"))

    def test_the_rating_replaces_the_scorer_as_the_main_signal(self) -> None:
        report = self._plan(["croissant"], DraftStore(None), threshold=0.7)
        kept = [t for t in report["trace"] if t["verdict"] == "kept"]
        self.assertEqual({(t["a"], t["b"]) for t in kept}, {("bread", "moon")})  # only the 5-rated pair clears 0.7
        self.assertGreaterEqual(kept[0]["score"], 0.7)
        self.assertTrue(any(t["verdict"].startswith("dropped: score") for t in report["trace"]))
        self.assertIn("moon", report["trace"][0]["hints"])

    def test_generated_pairs_do_not_need_the_full_list(self) -> None:
        report = self._plan(["croissant"], DraftStore(None), threshold=0.7, shortlist=30)
        link = report["targets"][0]["plan"]["links"][0]
        self.assertEqual({link["a"], link["b"]}, {"moon", "bread"})

    def test_kinds_that_are_not_elements_are_replaced_by_their_neighbours(self) -> None:
        from aigen.planner import Planner
        base = fake_lmstudio.fake_embedding

        def embed(texts: list[str]) -> list[list[float]]:  # 'baked good' means the same as 'bread' here
            return [base("bread" if t == "baked good" else t) for t in texts]

        cache = EmbeddingCache(embed, None, "stub")
        planner = Planner(PlanSettings(seed=1), book=self.book, client=self.client, cache=cache,
                          scorer=self.scorer, store=DraftStore(None), log=lambda s: None)
        profile = {"kind_of": ["baked good"], "made_of": ["flour", "butter"], "resembles": ["moon"]}
        pairs = {(o.a.lower(), o.b.lower()) for o in planner._generated(profile, "croissant")}
        self.assertIn(("bread", "moon"), pairs)
        self.assertFalse(any("flour" in p or "butter" in p for p in pairs))  # made_of is not a kind

    def test_listed_picks_are_used_as_they_are(self) -> None:
        from aigen.planner import Planner
        planner = Planner(PlanSettings(seed=1), book=self.book, client=self.client, cache=self.cache,
                          scorer=self.scorer, store=DraftStore(None), log=lambda s: None)
        profile = {"kind_elements": ["bread", "cake"], "look_elements": ["moon"]}
        pairs = {(o.a.lower(), o.b.lower()) for o in planner._generated(profile, "croissant")}
        self.assertEqual(pairs, {("bread", "moon"), ("cake", "moon")})  # no guessing from embeddings

    def test_generic_pairs_are_capped_and_the_specific_one_wins(self) -> None:
        store = DraftStore(None)
        report = self._plan(["croissant"], store, threshold=0.0)
        by_pair = {(t["a"], t["b"]): t for t in report["trace"] if "judge_rating" in t}
        generic = by_pair[("butter", "flour")]
        self.assertEqual((generic["judge_rating"], generic["rating"], generic.get("generic")), (5, 3, True))
        specific = by_pair[("bread", "moon")]
        self.assertEqual((specific["rating"], specific["reads_as"], specific.get("generic")), (5, "moon-shaped bread", None))
        best = report["targets"][0]["plan"]["links"][0]
        self.assertEqual({best["a"], best["b"]}, {"moon", "bread"})
        self.assertIn("reads as 'moon-shaped bread'", store.queue(self.book)[0]["note"])

    def test_without_a_working_judge_nothing_is_accepted(self) -> None:
        store = DraftStore(None)
        report = self._plan(["flibbertigibbet"], store, threshold=0.0, min_rating=1)
        self.assertEqual(report["targets"][0]["status"], "no chain")
        self.assertTrue(all(t["verdict"] == "dropped: the judging step did not rate it"
                            for t in report["trace"] if "a" in t and t["verdict"] != "kept" and "pair" not in t["verdict"]))
        self.assertEqual(store.queue(self.book), [])

    def test_a_weak_chain_is_not_written_at_the_default_bar(self) -> None:
        store = DraftStore(None)
        result = self._plan(["jacket"], store, min_rating=4)["targets"][0]  # the fake judge rates jacket pairs 3
        self.assertEqual(result["status"], "no chain")
        self.assertIn("Closest:", result["detail"])
        self.assertEqual(store.queue(self.book), [])
        good = self._plan(["croissant"], store, min_rating=4)["targets"][0]  # moon + bread is a 5
        self.assertEqual(good["status"], "planned")

    def test_a_pick_that_is_not_an_element_falls_back_to_its_nearest_element(self) -> None:
        from aigen.planner import Planner
        base = fake_lmstudio.fake_embedding
        cache = EmbeddingCache(lambda texts: [base("moon" if t == "crescent" else t) for t in texts], None, "stub")
        planner = Planner(PlanSettings(seed=1), book=self.book, client=self.client, cache=cache,
                          scorer=self.scorer, store=DraftStore(None), log=lambda s: None)
        profile = {"kind_elements": ["bread"], "look_elements": ["crescent"]}  # 'crescent' is not an element
        self.assertIn(("bread", "moon"), {(o.a.lower(), o.b.lower()) for o in planner._generated(profile, "croissant")})

    def test_low_ratings_are_not_rescued_by_the_scorer(self) -> None:
        report = self._plan(["croissant"], DraftStore(None), threshold=0.0)
        low = [t for t in report["trace"] if t.get("rating") in (1, 2)]
        self.assertTrue(low)
        self.assertTrue(all(t["verdict"].startswith("dropped: rated only") for t in low))

    def test_budget_zero_blocks_helpers(self) -> None:
        store = DraftStore(None)
        result = self._plan(["jacket"], store, max_intermediates=0)["targets"][0]
        self.assertEqual(result["status"], "no chain")
        self.assertEqual(store.queue(self.book), [])

    def test_existing_and_invalid_targets_are_skipped(self) -> None:
        store = DraftStore(None)
        report = self._plan(["armor", "x1"], store)
        self.assertEqual([t["status"] for t in report["targets"]], ["exists", "invalid"])
        self.assertEqual(report["written_drafts"], [])

    def test_target_made_by_an_earlier_run_is_not_repeated(self) -> None:
        store = DraftStore(None)
        self.assertEqual(self._plan(["jacket"], store)["targets"][0]["status"], "planned")
        self.assertEqual(self._plan(["jacket"], store)["targets"][0]["status"], "exists")

    def test_dry_run_writes_nothing(self) -> None:
        store = DraftStore(None)
        report = self._plan(["jacket"], store, dry_run=True)
        self.assertEqual(report["targets"][0]["status"], "planned")
        self.assertEqual(store.view("ai-suggester", self.book)["recipes"], [])


if __name__ == "__main__":
    unittest.main()
