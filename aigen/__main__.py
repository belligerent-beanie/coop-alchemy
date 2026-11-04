"""Command line: python3 -m aigen check | train | generate | target"""
from __future__ import annotations

import argparse
import collections
import os
import sys
from pathlib import Path

from accounts import AccountStore
from drafts import DraftStore
from recipes import RecipeError, load_recipes

from .embeddings import EmbeddingCache
from .generate import Settings, run, save_report
from .ratings import RatingCache
from .lmstudio import DEFAULT_URL, GROQ_MODEL, GROQ_URL, LMStudio, LMStudioError
from .planner import PlanSettings, plan_targets
from .scorer import Scorer, train

HERE = Path(__file__).resolve().parent
BASE_DIR = HERE.parent


def _client(args: argparse.Namespace, *, chat: bool, embed: bool) -> LMStudio:
    chat_url, chat_key, chat_model = args.chat_base_url, None, args.chat_model
    if args.groq:
        chat_url, chat_model = chat_url or GROQ_URL, chat_model or GROQ_MODEL
    if chat_url:
        chat_key = os.environ.get(args.chat_key_env)
        if not chat_key and chat:
            raise LMStudioError(f"the environment variable {args.chat_key_env} is not set. In the same Terminal "
                                f"window, run: export {args.chat_key_env}=your-key")
    client = LMStudio(args.base_url, chat_model, args.embed_model, chat_base_url=chat_url, chat_api_key=chat_key,
                      max_tokens=args.max_tokens or (2000 if chat_url else 3500))
    client.resolve_models(need_chat=chat, need_embed=embed)
    return client


def _draft_store(args: argparse.Namespace) -> DraftStore:
    """The drafts file, with bot accounts exempt from the draft and proposal limits."""
    accounts = AccountStore(args.accounts_file)
    if not accounts.is_bot(args.user):
        print(f"note: {args.user!r} is not a bot account, so the normal limits apply (200 drafts, 40 waiting proposals). "
              f"To lift them, sign up for that account on the site, then run: "
              f"python3 accounts_admin.py make-bot {args.user}", file=sys.stderr)
    return DraftStore(args.drafts_file, is_bot=accounts.is_bot)


def cmd_check(args: argparse.Namespace) -> int:
    client = _client(args, chat=True, embed=True)
    print(f"chat model:      {client.chat_model}" + (f"  (remote: {client.chat_base_url})" if client.remote_chat else ""))
    print(f"embedding model: {client.embed_model}")
    reply = client.chat("Answer briefly.", "Say OK.", max_tokens=300)
    print(f"chat test:       {reply[:60]!r}")
    print(f"embedding test:  {len(client.embed(['water'])[0])} dimensions")
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    book = load_recipes(args.recipes_file)
    client = _client(args, chat=False, embed=True)
    cache = EmbeddingCache(client.embed, args.cache, client.embed_model or "")
    print(f"embedding {len(book.elements)} element names with {client.embed_model} (first run only) ...")
    scorer = train(book, cache.get, client.embed_model or "", seed=args.seed)
    cache.save()
    scorer.save(args.scorer)
    print("held-out results:")
    for key, value in scorer.metrics.items():
        print(f"  {key}: {value}")
    print(f"saved {args.scorer}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    book = load_recipes(args.recipes_file)
    scorer = Scorer.load(args.scorer)
    client = _client(args, chat=True, embed=True)
    if scorer.embed_tag != client.embed_model:
        print(f"the scorer was trained with {scorer.embed_tag!r} but LM Studio uses {client.embed_model!r}: "
              "run 'python3 -m aigen train' again or pass --embed-model", file=sys.stderr)
        return 2
    cache = EmbeddingCache(client.embed, args.cache, client.embed_model or "")
    store = _draft_store(args)
    settings = Settings(new_elements=args.new_elements, max_recipes=args.max_recipes, depth=args.depth,
                        partners=args.partners, per_call=args.per_call, user=args.user,
                        promote=not args.no_promote, dry_run=args.dry_run, threshold=args.threshold,
                        seed=args.seed)
    report = run(settings, book=book, client=client, cache=cache, scorer=scorer, store=store)
    cache.save()
    report["usage"] = dict(client.usage)
    log_path = save_report(report, HERE / "runs")

    print("\nAccepted:")
    for c in report["accepted"]:
        tag = "NEW " if not c["target"] else "    "
        print(f"  {tag}{c['a_label']} + {c['b_label']} = {c['name']}   (score {c['score']:.2f}, round {c['round']})")
    if not report["accepted"]:
        print("  nothing passed the filters")
    why = collections.Counter(c["verdict"].split(" ")[0] + " " + " ".join(c["verdict"].split(" ")[1:3])
                              for c in report["candidates"] if c["verdict"] != "accepted")
    if why:
        print("\nDropped:", ", ".join(f"{n}x {reason}" for reason, n in why.most_common(6)))
    if args.dry_run:
        print("\nDry run: nothing was written to the drafts.")
    else:
        sent = "and sent to the admins" if settings.promote else "(not sent: they stay drafts)"
        print(f"\nWrote {len(report['written_drafts'])} drafts {sent}.")
    if client.usage["calls"]:
        print(client.usage_summary())
    print(f"Full log: {log_path}")
    return 0


def cmd_target(args: argparse.Namespace) -> int:
    targets = list(args.targets)
    if args.targets_file:
        lines = Path(args.targets_file).read_text(encoding="utf-8").splitlines()
        targets += [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    if not targets:
        print("error: give at least one target noun, or --targets-file", file=sys.stderr)
        return 2
    book = load_recipes(args.recipes_file)
    scorer = Scorer.load(args.scorer)
    client = _client(args, chat=True, embed=True)
    if scorer.embed_tag != client.embed_model:
        print(f"the scorer was trained with {scorer.embed_tag!r} but LM Studio uses {client.embed_model!r}: "
              "run 'python3 -m aigen train' again or pass --embed-model", file=sys.stderr)
        return 2
    cache = EmbeddingCache(client.embed, args.cache, client.embed_model or "")
    store = _draft_store(args)
    settings = PlanSettings(user=args.user, promote=not args.no_promote, dry_run=args.dry_run,
                            threshold=args.threshold, seed=args.seed, max_intermediates=args.max_intermediates,
                            options=args.options, beam=args.beam, shortlist=args.shortlist, verbose=args.verbose,
                            min_rating=args.min_rating, max_targets=args.max_targets, token_budget=args.token_budget)
    ratings = RatingCache(None if args.no_rating_cache else args.ratings_cache, client.chat_model or "")
    report = plan_targets(targets, settings, book=book, client=client, cache=cache, scorer=scorer, store=store,
                          ratings=ratings)
    cache.save()
    log_path = save_report(report, HERE / "runs")

    print()
    for t in report["targets"]:
        if t["plan"] is None:
            print(f"{t['target']}: {t['status']} - {t['detail']}")
            continue
        print(f"{t['target']}: planned (weakest link {t['plan']['score']:.2f})")
        for link in t["plan"]["links"]:
            reason = f"  [{link['link_type'] or 'link'}: {link['why']}]" if link.get("why") else ""
            print(f"    {link['a_label']} + {link['b_label']} = {link['result']}   ({link['score']:.2f}){reason}")
        for alt in t["alternatives"]:
            steps = "; ".join(f"{l['a_label']} + {l['b_label']} = {l['result']}" for l in alt["links"])
            print(f"    (alternative, {alt['score']:.2f}: {steps})")
    if args.dry_run:
        print("\nDry run: nothing was written to the drafts.")
    else:
        sent = "and sent to the admins" if settings.promote else "(not sent: they stay drafts)"
        print(f"\nWrote {len(report['written_drafts'])} drafts {sent}.")
    if client.usage["calls"] or ratings.hits:
        print(client.usage_summary() + (f" {ratings.hits} ratings came from the cache." if ratings.hits else ""))
    if report["stopped"]:
        print(report["stopped"][:300])
    print(f"Full log: {log_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m aigen", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--base-url", default=DEFAULT_URL, help="LM Studio server (default %(default)s)")
        p.add_argument("--chat-model", help="chat model id (default: gpt-oss if present, else the first one)")
        p.add_argument("--groq", action="store_true",
                       help="use Groq's free API for the chat model (needs GROQ_API_KEY; embeddings stay local)")
        p.add_argument("--chat-base-url", help="another OpenAI-compatible service for chat (embeddings stay local)")
        p.add_argument("--chat-key-env", default="GROQ_API_KEY",
                       help="environment variable holding that service's API key (default %(default)s)")
        p.add_argument("--max-tokens", type=int, default=None,
                       help="longest chat answer to allow (default 3500 locally, 2000 for a remote service)")
        p.add_argument("--embed-model", help="embedding model id (default: the first one with 'embed' in its name)")
        p.add_argument("--recipes-file", default=str(BASE_DIR / "recipes.json"))
        p.add_argument("--cache", default=str(HERE / "cache" / "embeddings.json"))
        p.add_argument("--scorer", default=str(HERE / "model.json"))
        p.add_argument("--seed", type=int, default=None)

    p = sub.add_parser("check", help="test the connection to LM Studio")
    common(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("train", help="train the plausibility scorer on recipes.json")
    common(p)
    p.set_defaults(func=cmd_train, seed=0)

    p = sub.add_parser("generate", help="propose new elements and recipes and add them as drafts")
    common(p)
    p.add_argument("--drafts-file", default=str(BASE_DIR / "drafts.json"))
    p.add_argument("--accounts-file", default=str(BASE_DIR / "accounts.json"))
    p.add_argument("--user", default="ai-suggester", help="account name the drafts and proposals are credited to")
    p.add_argument("--new-elements", type=int, default=8, help="new elements to aim for in round 1")
    p.add_argument("--max-recipes", type=int, default=30, help="most recipes to write in total")
    p.add_argument("--depth", type=int, default=2, help="rounds: 1 = new elements only; 2+ = build with them too")
    p.add_argument("--partners", type=int, default=6, help="existing elements tried with each new element")
    p.add_argument("--per-call", type=int, default=8, help="pairs per request to the model")
    p.add_argument("--threshold", type=float, default=None, help="minimum score (default: the trained one)")
    p.add_argument("--no-promote", action="store_true", help="write drafts but do not send them to the admins")
    p.add_argument("--dry-run", action="store_true", help="show what would be added, write nothing")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("target", help="find a chain of recipes that ends in each given noun")
    common(p)
    p.add_argument("targets", nargs="*", help='target nouns, e.g. jacket "ice cream truck"')
    p.add_argument("--targets-file", help="a text file with one target per line (# starts a comment)")
    p.add_argument("--drafts-file", default=str(BASE_DIR / "drafts.json"))
    p.add_argument("--accounts-file", default=str(BASE_DIR / "accounts.json"))
    p.add_argument("--user", default="ai-suggester", help="account name the drafts and proposals are credited to")
    p.add_argument("--max-intermediates", type=int, default=2, help="new helper elements allowed per target")
    p.add_argument("--options", type=int, default=6, help="ingredient pairs requested per question")
    p.add_argument("--shortlist", type=int, default=0,
                   help="elements shown to the model: 0 = all of them (default), else only the N nearest the target "
                        "(use this with a small local model whose context is short)")
    p.add_argument("--min-rating", type=int, default=4, choices=[1, 2, 3, 4, 5],
                   help="every link of a chain must be rated at least this by the judging step (default 4 of 5)")
    p.add_argument("--verbose", action="store_true", help="print every option the model gave and why it was kept or dropped")
    p.add_argument("--max-targets", type=int, default=0,
                   help="stop after this many targets that needed the model (0 = no limit); run again for the rest")
    p.add_argument("--token-budget", type=int, default=0,
                   help="stop before the next target once this many chat tokens are used (0 = no limit); "
                        "Groq's free gpt-oss-120b allows about 200,000 a day")
    p.add_argument("--ratings-cache", default=str(HERE / "cache" / "ratings.json"),
                   help="remembered ratings, so re-running a target (for example with a lower --min-rating) is cheap")
    p.add_argument("--no-rating-cache", action="store_true", help="ask the judging model again every time")
    p.add_argument("--beam", type=int, default=3, help="chains kept per target (the best is written)")
    p.add_argument("--threshold", type=float, default=None, help="minimum link score (default: the trained one)")
    p.add_argument("--no-promote", action="store_true", help="write drafts but do not send them to the admins")
    p.add_argument("--dry-run", action="store_true", help="show the chains, write nothing")
    p.set_defaults(func=cmd_target)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (LMStudioError, RecipeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
