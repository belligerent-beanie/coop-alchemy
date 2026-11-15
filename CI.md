# Continuous integration

Every pull request runs the checks below. Merging to `main` is blocked until they pass (branch protection is a GitHub setting, see "One-time setup").

| Check | What it does | Where it is configured |
|---|---|---|
| **Ruff** | Lint: bugs (pyflakes, bugbear), import order, security patterns (bandit rules). Any finding fails the build. | `pyproject.toml` |
| **Tests** | `run_tests.py` under `coverage`. Tests that need the private recipe book skip themselves when it is absent (as it is in CI). | `.github/workflows/ci.yml` |
| **SonarQube** | Deeper analysis: cognitive complexity, duplication, security hotspots, and a quality gate on new code. Imports the Ruff report and the coverage report. | `sonar-project.properties`, `.github/workflows/ci.yml` |
| **CodeQL** | GitHub's semantic security scan (data flows such as input reaching a file path or a command), Python and JavaScript. | `.github/workflows/codeql.yml` |
| **Dependabot** | Keeps the GitHub Actions and the dev tooling current. The game itself has no third-party packages. | `.github/dependabot.yml` |

## One rule: Ruff and SonarQube must agree

Ruff is the authority on Python lint. To stop the two tools disagreeing:

- CI feeds Ruff's JSON report into SonarQube (`sonar.python.ruff.reportPaths`), so a Ruff finding also shows in Sonar and counts against its gate.
- When we deliberately accept something in Ruff (an ignore in `pyproject.toml` or a `# noqa: CODE  (reason)` comment), the same decision is recorded for Sonar (`# NOSONAR` or a rule exclusion in `sonar-project.properties`), with the same reason. Never silence a rule in only one tool.
- Every `noqa` carries a reason in brackets. A bare `noqa` is a review comment waiting to happen.

Decisions made so far, and why:

| Ruff rule | Decision | Reason |
|---|---|---|
| S101 `assert` | ignored | used for internal invariants and in tests |
| S311 `random` | ignored | gameplay only (bots, picks, daily); tokens, captcha, invites and accounts never import `random` |
| S104 bind `0.0.0.0` | `noqa` at the two option defaults | the server is meant to listen on every interface behind a tunnel or proxy |
| S310 `urlopen` | `noqa` at each call | URLs are fixed https constants or the operator-configured local LM Studio address |
| S324 `sha1` | fixed (`usedforsecurity=False`) | a cache-busting fingerprint, not a secret |
| S606 `os.execv` | `noqa` | re-runs this same script in place for hot reload |
| S603/S607 in `ops/` | ignored | operator tools run fixed argument lists, never user input |
| B905 `zip` strictness | ignored | style opinion; adding `strict=` could start raising in live code |
| E701/E702, E741 | ignored | compact one-line style and short names are used on purpose |

## Running the checks locally

```
pip install -r requirements-dev.txt
ruff check .                 # lint (ruff check --fix . for the safe auto-fixes)
coverage run run_tests.py    # tests with coverage
pre-commit install           # optional: run Ruff before every commit
```

Careful with `ruff check --fix`: it treats a side-effect import such as `import alchemylog` as unused. Read the diff before keeping it.

## One-time setup (done in the GitHub and SonarQube Cloud web pages)

1. Import the repository at sonarcloud.io; confirm `sonar.organization` and `sonar.projectKey` in `sonar-project.properties` match.
2. Add the project's token as a repository secret named `SONAR_TOKEN`.
3. After the first CI run, protect `main`: Settings, Branches, require pull requests and the status checks `Ruff`, `Tests`, `SonarQube` and `CodeQL`.
4. Do a first-scan review in SonarQube and reconcile any rule it raises that Ruff deliberately ignores (see the table above).
