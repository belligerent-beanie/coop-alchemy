"""Run the test suite and say plainly which tests failed.

    python run_tests.py              everything
    python run_tests.py --failed     only what failed last time (fast: re-check a fix)
    python run_tests.py test_bots    named modules, classes or tests, as for `python -m unittest`
    python run_tests.py -x           stop at the first failure

The last lines are the verdict: the count, then one line per failing test with the reason. Everything else (the output of each
test, full tracebacks) is in test-output.log, so nothing has to be searched for. Exit code 0 only if everything passed.
"""
from __future__ import annotations

import contextlib
import sys
import time
import unittest
import warnings
from pathlib import Path

HERE = Path(__file__).parent
LOG = HERE / "test-output.log"
LAST_FAILED = HERE / ".last-failed"


def flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def reason(trace: str) -> str:
    """The error line of a traceback, trimmed to one line."""
    for marker in (chr(10) + "Stdout:", chr(10) + "Stderr:"):   # unittest appends what the test printed after the traceback
        trace = trace.split(marker)[0]
    lines = [line for line in trace.strip().splitlines() if line.strip()]
    for line in lines:   # the first unindented line after "Traceback" is the exception (a long message continues on later lines)
        if not line.startswith(("  ", "\t", "Traceback")):
            return line.strip()[:200]
    return lines[-1].strip()[:200] if lines else "?"


def main(argv: list[str]) -> int:
    warnings.simplefilter("ignore", ResourceWarning)
    failfast = "-x" in argv
    only_failed = "--failed" in argv
    names = [a for a in argv if not a.startswith("-")]
    loader = unittest.TestLoader()
    if only_failed:
        names = LAST_FAILED.read_text(encoding="utf-8").split() if LAST_FAILED.exists() else []
        if not names:
            print("Nothing failed last time (no .last-failed): running everything.")
    suite = loader.loadTestsFromNames(names) if names else loader.discover(str(HERE), pattern="test_*.py")
    total = sum(1 for _ in flatten(suite))
    with LOG.open("w", encoding="utf-8") as log:
        runner = unittest.TextTestRunner(stream=log, verbosity=2, buffer=True, failfast=failfast)
        started = time.time()
        print(f"Running {total} tests ...", flush=True)
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):   # what tests print goes to the log, not the screen
            result = runner.run(suite)
        seconds = time.time() - started
    problems = [(t.id(), "FAIL", reason(tb)) for t, tb in result.failures] + \
               [(t.id(), "ERROR", reason(tb)) for t, tb in result.errors]
    LAST_FAILED.write_text("\n".join(p[0] for p in problems), encoding="utf-8")
    print()
    if not problems:
        print(f"PASSED  {result.testsRun} tests in {seconds:.0f}s ({len(result.skipped)} skipped)")
        return 0
    print(f"FAILED  {len(problems)} of {result.testsRun} tests in {seconds:.0f}s ({len(result.skipped)} skipped):\n")
    for test_id, kind, why in problems:
        print(f"  {kind}  {test_id}\n         {why}")
    print(f"\nFull tracebacks and output: {LOG.name}.  Re-run just these: python run_tests.py --failed")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
