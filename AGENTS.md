# Agents: SonarQube findings fixed by Claude, reviewed by Claude Haiku

Small problems SonarQube finds (real but minor bugs, false positives that need recording, functions that are too
complex) are worked through by Claude agents in GitHub Actions. A person decides what gets worked on and merges.

```
SonarQube finding ──(daily)──▶ GitHub issue ──(you add `agent`)──▶ Sonnet fixes it, opens a PR
                                                                          │
                                         you merge ◀── checks pass ◀── Haiku reviews it ("Agent review")
```

## The pieces

| Step | Workflow | What it does |
|---|---|---|
| Findings → issues | `.github/workflows/sonar-issues.yml` (daily, or *Run workflow*) | `ops/sonar_issues.py` groups the open findings on main by rule and file, opens up to 15 new issues a run (most severe first), labelled `sonar` plus one of `sonar:small-fix`, `sonar:likely-false-positive`, `sonar:complexity`. Issues whose findings disappear are closed. |
| Fix | `.github/workflows/agent-fix.yml` | Adding the **`agent`** label to an open issue starts Claude **Sonnet 5.5**, following `.github/agents/fixer.md`: fix a real problem, record a false positive with `NOSONAR` (and the matching Ruff `noqa`), or refactor. It runs Ruff and the tests, then opens a PR on `agent/issue-<n>` that says `Fixes #<n>`. If the fix is risky or large it comments on the issue instead. |
| Review | `.github/workflows/agent-review.yml` | Every PR (the agent's or a person's) is reviewed by Claude **Haiku 5.5**, following `.github/agents/reviewer.md` **as it is on main** (a PR cannot rewrite its own reviewer). It leaves inline comments and a summary; the **Agent review** check is green only on an *approve* verdict. |

## Day to day

- See what is waiting: the issue list filtered by `label:sonar`.
- Hand one over: add the `agent` label. Label a few at a time; each run uses your Claude plan.
- Leave one alone: close it. Closed issues are never reopened or recreated.
- Agent stuck or wrong: comment on its PR and fix by hand, or close the PR and remove/re-add `agent` to retry.
- Run the import now: Actions → *Sonar issues* → *Run workflow*. Preview locally with `python3 ops/sonar_issues.py`.

## One-time setup (done in the browser)

1. **Install the Claude GitHub App** on this repository: run `/install-github-app` in Claude Code, or install it from
   https://github.com/apps/claude. PRs opened with the default Actions token would not trigger CI; the app's do.
2. **Token**: run `claude setup-token` and save the result as the repository secret `CLAUDE_CODE_OAUTH_TOKEN`
   (Settings → Secrets and variables → Actions → Secrets). Add the same value under the **Dependabot** secrets tab,
   or Dependabot's PRs cannot be reviewed.
3. **Make the review required**, once it has run on one PR after this is merged: Settings → Branches → `main` →
   add the status check **Agent review**. An admin can still override a red review.

## Limits worth knowing

- Pull requests from forks get no secrets, so their Agent review fails; a maintainer reviews and overrides.
- The review workflow only runs as it is on main: while a PR changes `agent-review.yml` itself, its review is
  skipped and the check is red. That is deliberate.
- Haiku is a second pair of eyes, not the last one: you still merge.
