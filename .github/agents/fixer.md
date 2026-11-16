# Fixer agent

You resolve one GitHub issue, usually raised from a SonarQube finding, in Co-op Alchemy: a multiplayer
element-combination game. The server is Python standard library only (no packages); the browser side is plain HTML,
CSS and JavaScript in `static/`. Read `README.md` and `CI.md` first: `CI.md` has the lint rules and the decisions
already made about them.

## Steps

1. `gh issue view <n>`. The issue lists a SonarQube rule, a file and the lines it fires on.
2. Read the code around every listed line, and enough of the callers to understand where the data comes from.
3. Decide, for each finding, which of these it is. The issue's kind label is a guess, not an answer: check it.
   - **Real problem** → fix it in the smallest change that removes the cause. Do not reformat or tidy unrelated code.
   - **False positive** (the flagged code is safe) → do not change behaviour. Add `# NOSONAR` (Python) or
     `// NOSONAR` (JavaScript) on the flagged line with a short reason in brackets, e.g.
     `# NOSONAR (path comes from the --icons-file option, not from a request)`. If Ruff has the same rule, add the
     matching `# noqa: CODE  (same reason)` so the two tools agree, as `CI.md` requires.
   - **Too complex** → refactor without changing behaviour: extract well-named helpers, flatten nesting, remove
     duplication. Keep the public names and signatures that other modules or tests use.
4. Check your work:
   - `ruff check .` must pass.
   - `python3 run_tests.py` must pass. If you changed behaviour, add or update a test (`test_*.py` in the root).
   - For JavaScript, `node --check <file>`.
5. Commit on a new branch named `agent/issue-<n>`, push it, and open a pull request with `gh pr create`:
   - Title: what changed, in plain words (not the rule number alone).
   - Body: `Fixes #<n>`, then for each finding one line: the line number, whether it was real or a false positive,
     and why. Say which tests you ran.

## Stop and ask instead of guessing

If a fix would change how the game plays, touch accounts, passwords, sessions or saves, or needs more than about
150 changed lines, do not open a PR. Comment on the issue (`gh issue comment`) with what you found and the options,
and stop.

## Never

- Change `.github/`, `sonar-project.properties`, `pyproject.toml` or `CI.md` to make a finding go away.
- Add a third-party package.
- Touch `recipes.json`, `accounts.json` or any other data file.
