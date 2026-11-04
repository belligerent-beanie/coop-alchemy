# aigen: AI-suggested elements and recipes

An LLM (LM Studio) invents element names; a small scorer trained on `recipes.json` filters them.
Results go into the normal draft system and are sent to the admin queue, credited to the account
`ai-suggester` (set up once: sign up for that account on the site, then
`python3 accounts_admin.py make-bot ai-suggester` so the draft and proposal limits do not apply). Nothing touches `recipes.json`; an admin still approves each recipe.

## One-time setup (LM Studio)

1. Keep a chat model (gpt-oss-20b works). Set its **context length to at least 8192**.
2. Download an embedding model: search for `nomic-embed-text-v1.5` (about 80 MB).
3. Developer tab, **Start Server** (default `http://localhost:1234`).

```
python3 -m aigen check      # confirms both models answer
python3 -m aigen train      # embeds the element names once, trains the scorer, prints held-out results
```

## No room for a big model? Use Groq's free API for the chat part

If your Mac can't run a 20B model, let Groq (free tier, no local model needed) do the naming. Embeddings stay
local: only the small `nomic-embed-text` model needs to be available in LM Studio, and the chat model should
**not** be loaded there.

```
export GROQ_API_KEY=your-key        # in the same Terminal window you run aigen in; never put the key in a file
python3 -m aigen check --groq
python3 -m aigen train              # embeddings only, no --groq needed
python3 -m aigen target jacket --groq --dry-run
```

`--groq` uses `openai/gpt-oss-20b` at `https://api.groq.com/openai/v1`. Other OpenAI-compatible services work
with `--chat-base-url URL --chat-model NAME --chat-key-env VARIABLE`. The free tier is limited (about 8,000
tokens a minute for gpt-oss-20b at the time of writing), so when a limit is hit the tool waits and retries by
itself; a run just takes longer. Only element names and sample recipe lines are sent. A small local model
works too (for example a 3-4B model in LM Studio, `--chat-model NAME`); `<think>` blocks are ignored.

## Making suggestions

```
python3 -m aigen generate --dry-run     # look at what it would add; writes nothing
python3 -m aigen generate               # writes drafts and sends them to the admin queue
```

Useful options: `--new-elements 8` (new elements in round 1), `--max-recipes 30` (total),
`--depth 2` (round 2 builds recipes with the new elements; 3 builds on those too),
`--no-promote` (drafts only), `--threshold 0.5` (raise to be stricter), `--user NAME`.
Every run writes a full log (every suggestion and why it was dropped) to `aigen/runs/`.

## Targets: "make me a jacket"

```
python3 -m aigen target jacket "ice cream truck" --dry-run
python3 -m aigen target --targets-file wishlist.txt        # one noun per line, # for comments
```

For each target noun it works backwards: the model is shown **every existing element** (A-Z) and picks
ingredient pairs from it, so it can spot look-alikes by itself. If a pair needs an element that does not
exist yet (for example `clothes`), that element becomes a sub-goal. Up to `--max-intermediates 2` new helper
elements per target; chains are ranked by their weakest link and only the best is written (the others are
in the run log). Targets that already exist are skipped, and no alternative recipes are added for them.
Later targets in the same run, and later runs, can build on helpers created earlier.

**How candidates are found and judged.** For each goal the tool makes three kinds of request:

1. *Describe the goal*: which listed elements are the same sort of thing and which it looks like (a croissant:
   bread, cake; moon), plus what it is made of, used for and comes from.
2. *Propose pairs*: the model sees every existing element and proposes ingredient pairs. On top of that the
   **code itself** pairs elements of the same *kind* as the goal (bread, cake) with elements it *looks like* (moon).
   The model picks both groups directly from the element list it is shown (with `--shortlist N` it describes
   the goal in free text instead and the phrases are matched to elements), so shape recipes such as `moon + bread = croissant` are always among the candidates.
3. *Judge*: one request judges all candidates together. For each pair it must read the two ingredients as a
   phrase (`bread` + `moon` reads as "moon-shaped bread"), list other well-known things the same pair could just
   as easily make, and rate from 1 to 5 how specifically the pair points to the goal. Generic pairs of common
   ingredients (`dough` + `butter` fits croissant, brioche, cookies ...) are capped at 3, and the code enforces
   that cap whenever three or more rivals are listed.

The final score is mostly that rating (0.14 per point) plus 0.3 times the trained scorer's value, which now
acts as a tiebreaker (it compares names by meaning and cannot see shapes or ingredients). A rating of 1 or 2 always drops a
candidate (the scorer cannot rescue it); 3 passes the default threshold; 4 and 5 rank higher. A helper element is refused if it is just an existing element with
a word added (`crescent moon` when `moon` exists), and chains that invent fewer elements are preferred. The
reason and rating are shown in the output and in the note the admins see (`shape: bread shaped like moon`).
Use `--verbose` to see every candidate and why it was kept or dropped; the same trace is in the run log.
Every link of a chain must be rated at least 4 of 5 (`--min-rating`, lower it to accept weaker
recipes); if nothing clears the bar the target is reported as "no chain" with the closest candidates, instead
of sending a mediocre recipe to the admins. The describing and judging steps use medium reasoning effort on
gpt-oss models; naming uses low. With a small local model whose context is short, `--shortlist N` shows only the N nearest elements (plus
elements matching the description) instead of all of them. Each goal costs three requests (four with a helper
element), so runs on a free API are slower.

Judging is done 8 candidates at a time. A reply that is cut off still counts for its complete entries, missing
ones are asked again once, and a candidate the judge never rates is dropped (there is no fallback to the proposer's
own rating, which is what let generic pairs win).

## Limits, budgets and cost

- Every run prints how many chat tokens it used (taken from the service's own counts; also saved as `usage` in the
  run log). The log has no token count for older runs.
- **Ratings are remembered** in `aigen/cache/ratings.json` (per chat model, per target and pair). Re-running a
  target, for example after lowering `--min-rating`, asks the judging model only about pairs it has not rated before.
  `--no-rating-cache` turns this off; delete the file to start again.
- `--max-targets N` stops after N targets that needed the model (targets that already exist don't count);
  `--token-budget N` stops before the next target once N chat tokens are used. Run again for the rest.
  Groq's free `gpt-oss-120b` allows about 200,000 tokens a day (per model, over a rolling 24 hours), roughly
  6 to 10 targets.
- If the service says the **daily** limit is used up, the run stops at once with its message (and the time to
  try again) instead of waiting; what was already planned is saved. A per-minute limit is still waited out.

## Notes

- Time is unlocked by the game rather than made by a recipe, so the generators treat it as available
  (otherwise `time`, `ruins` and `archeologist` would look unreachable).

- Round 1 pairs random existing elements and only keeps names that do not already exist.
  Round 2+ pairs each new element with its nearest existing elements (by meaning) and a few random
  ones; those results may be new or existing elements.
- A bot account has no limit on drafts or waiting proposals. Without `make-bot`, the normal limits
  apply (200 drafts, 40 waiting proposals) and `generate` and `target` print a note saying so.
- `generate` edits `drafts.json` at the end of a run, in milliseconds. A live server re-reads that
  file on every request, so this is safe, but avoid pressing "Promote" at the very same moment.
- To try the pipeline without LM Studio: `python3 -m aigen.fake_lmstudio 1235` and add
  `--base-url http://127.0.0.1:1235/v1` (names will be nonsense pseudo-words).
- Tests: `python3 -m unittest test_aigen`.
