"""Deploy the latest commit on `main` to the live server, with checks first and an automatic undo if it goes wrong.

    python ops/deploy.py                 run the checks, back up the data, update the live folder, restart, verify
    python ops/deploy.py --dry-run       do the checks and say what would happen, changing nothing
    python ops/deploy.py --quick         skip the slow AI-tool tests (test_aigen.py)
    python ops/deploy.py rollback        put the live folder back on the commit it had before the last deploy
    python ops/deploy.py rollback abc123 ... or on a particular commit

The steps, in order (any failure stops everything before the live server is touched, except the last two):
  1. the development folder is clean and on `main`, and the live folder can simply move forward to it
  2. every test file passes
  3. a smoke test of the new code on a scratch copy of the server (ops/smoke_test.py)
  4. a "pre-deploy" backup of the live data (ops/backup.py)
  5. the live folder is moved forward to `main`
  6. the live server restarts
  7. the live server must answer correctly within 30 s; if it does not, the live folder goes back to the previous commit
     and the server restarts on that (data is never rolled back: step 4's backup is there if you ever need it).
Every deploy and rollback is appended to <live folder>\\deploys.log.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops import backup as backup_tools  # noqa: E402
from ops import supervise  # noqa: E402
from ops import smoke_test  # noqa: E402

SLOW_TESTS = ("test_aigen.py",)
# The private recipe book and pictures are not in git, but they were once, and an old commit still has them. Moving the live folder
# onto a commit that no longer has them would make git delete them, and moving it onto an old one would put old copies back.
PRIVATE_PATHS = ("recipes.json", "recipes.mmd", "recipes.svg", "recipes.html", "static/elements")
KEEP_DIR = ".deploy-keep"


@contextlib.contextmanager
def keep_private_files(live_dir: Path):
    """Whatever git does inside this block, the live folder's own recipe book and pictures are exactly as they were after it."""
    keep = live_dir / KEEP_DIR
    shutil.rmtree(keep, ignore_errors=True)
    saved = [name for name in PRIVATE_PATHS if (live_dir / name).exists()]
    for name in saved:
        target = keep / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if (live_dir / name).is_dir():
            shutil.copytree(live_dir / name, target)
        else:
            shutil.copy2(live_dir / name, target)
    try:
        yield
    finally:
        for name in saved:
            destination = live_dir / name
            if destination.is_dir():
                shutil.rmtree(destination)
            elif destination.exists():
                destination.unlink()
            destination.parent.mkdir(parents=True, exist_ok=True)
            if (keep / name).is_dir():
                shutil.copytree(keep / name, destination)
            else:
                shutil.copy2(keep / name, destination)
        shutil.rmtree(keep, ignore_errors=True)
HEALTH_SECONDS = 30.0


class DeployError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise DeployError(f"git {' '.join(args)} failed in {repo.name}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def run_all_tests(dev_dir: Path, quick: bool = False) -> list[str]:
    """Run every test_*.py file; returns one line per failing file (empty if all pass)."""
    failures = []
    for path in sorted(dev_dir.glob("test_*.py")):
        if quick and path.name in SLOW_TESTS:
            continue
        result = subprocess.run([sys.executable, str(path)], cwd=str(dev_dir), capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            tail = [line for line in (result.stderr or result.stdout).strip().splitlines() if line][-3:]
            failures.append(f"{path.name}: " + " | ".join(tail))
    return failures


def check_health(port: int, seconds: float = HEALTH_SECONDS) -> str | None:
    """None once the server at `port` answers its login page and its config properly; otherwise a description of the problem."""
    end = time.monotonic() + seconds
    problem = "no answer yet"
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=5) as page:
                page.read()
                if page.status != 200:
                    problem = f"/login answered {page.status}"
                else:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/auth-config", timeout=5) as config:
                        json.loads(config.read())
                        return None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            problem = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= end:
            return problem
        time.sleep(1.0)


def run_and_wait(command: list[str], timeout: float) -> tuple[int, str]:
    """Run `command`, wait for IT to finish (not for anything it started), and return (exit code, its error output or output).

    Output goes to temporary files, never pipes. A pipe stays open for as long as any process that inherited it lives, so reading
    one would hang forever when the command starts a server that keeps running: that is exactly what the restart script does
    (this hung the first real deploy until it was stopped by hand).
    """
    out_fd, out_name = tempfile.mkstemp(prefix="alchemy-out-")
    err_fd, err_name = tempfile.mkstemp(prefix="alchemy-err-")
    try:
        with os.fdopen(out_fd, "wb") as out, os.fdopen(err_fd, "wb") as err:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=timeout)
        text = Path(err_name).read_bytes() or Path(out_name).read_bytes()
        return result.returncode, text.decode("utf-8", "replace")
    finally:
        for name in (out_name, err_name):
            try:
                os.unlink(name)
            except OSError:
                pass  # a long-running child that inherited the file may still hold it; it is tiny and goes with that process


def restart_supervised(data_dir: Path) -> None:
    """Restart the live server through its supervisor (ops/supervise.py), the same on every system: the server saves its running
    games as it stops and the new code picks them up. The health check that follows (step 7) confirms it came back."""
    if not supervise.supervisor_running(data_dir):
        raise DeployError(f"no supervisor is running for {data_dir}: install it first (python ops/install_supervisor.py install)")
    supervise.request(data_dir, "restart")
    deadline = time.time() + 30
    while (data_dir / supervise.REQUEST_FILE).exists():
        if time.time() > deadline:
            raise DeployError("the supervisor did not pick up the restart request within 30 s")
        time.sleep(0.5)


@dataclass
class Deployer:
    dev_dir: Path
    live_dir: Path
    port: int = 8000
    branch: str = "main"
    quick: bool = False
    dry_run: bool = False
    say: Callable[[str], None] = print
    run_tests: Callable[[], list[str]] | None = None
    smoke: Callable[[], list[str]] | None = None
    backup: Callable[[], object] | None = None
    restart: Callable[[], None] | None = None
    healthy: Callable[[], str | None] | None = None
    clock: Callable[[], float] = time.time
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.run_tests = self.run_tests or (lambda: run_all_tests(self.dev_dir, self.quick))
        self.smoke = self.smoke or (lambda: smoke_test.run(self.dev_dir))
        self.backup = self.backup or self._default_backup
        self.restart = self.restart or (lambda: restart_supervised(self.live_dir / "data"))
        self.healthy = self.healthy or (lambda: check_health(self.port))

    # ---- helpers ----------------------------------------------------------

    def _default_backup(self) -> object:
        made = backup_tools.make_backup(self.live_dir / "data", self.live_dir / "backups", "pre-deploy")
        backup_tools.prune(self.live_dir / "backups")
        return made

    def _log(self, line: str) -> None:
        try:
            with (self.live_dir / "deploys.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.clock()))} {line}\n")
        except OSError:
            self.say(f"(could not write deploys.log: {line})")

    def _short(self, commit: str) -> str:
        return commit[:7]

    # ---- the deploy -------------------------------------------------------

    def preflight(self) -> tuple[str, str]:
        """Check that a deploy is possible; returns (live commit now, commit to deploy)."""
        if not (self.live_dir / ".git").exists():
            raise DeployError(f"{self.live_dir} is not a git checkout (expected the live worktree)")
        if git(self.dev_dir, "status", "--porcelain"):
            raise DeployError("the development folder has uncommitted changes: commit or discard them, so that what is tested "
                              "is exactly what gets deployed")
        branch = git(self.dev_dir, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != self.branch:
            raise DeployError(f"the development folder is on {branch!r}, not {self.branch!r}")
        if git(self.live_dir, "status", "--porcelain", "--untracked-files=no"):
            raise DeployError("the live folder has local changes to tracked files; refusing to touch it")
        current = git(self.live_dir, "rev-parse", "HEAD")
        target = git(self.dev_dir, "rev-parse", self.branch)
        if subprocess.run(["git", "-C", str(self.live_dir), "merge-base", "--is-ancestor", current, target]).returncode != 0:
            raise DeployError("the live folder is not an ancestor of main (history diverged): sort that out by hand")
        return current, target

    def deploy(self) -> bool:
        """Run the whole deploy. Returns True if the live server is now running the new commit (or already was)."""
        assert self.run_tests and self.smoke and self.backup and self.restart and self.healthy
        try:
            current, target = self.preflight()
        except DeployError as exc:
            self.say(f"NOT DEPLOYED: {exc}")
            return False
        if current == target:
            self.say(f"Nothing to deploy: live is already on {self._short(target)}.")
            return True
        self.say(f"Deploying {self._short(current)} -> {self._short(target)} "
                 f"({git(self.dev_dir, 'log', '--oneline', f'{current}..{target}').count(chr(10)) + 1} commit(s))")

        self.say("1/6 running the tests ...")
        failures = self.run_tests()
        if failures:
            for line in failures:
                self.say(f"  FAIL {line}")
            self.say("NOT DEPLOYED: the tests fail. The live server was not touched.")
            return False
        self.say("2/6 smoke test of the new code ...")
        problems = self.smoke()
        if problems:
            for line in problems:
                self.say(f"  FAIL {line}")
            self.say("NOT DEPLOYED: the smoke test failed. The live server was not touched.")
            return False
        if self.dry_run:
            self.say("Dry run: everything checks out; nothing was changed.")
            return True

        self.say("3/6 backing up the live data ...")
        try:
            made = self.backup()
        except Exception as exc:  # a backup that cannot be made means no deploy
            self.say(f"NOT DEPLOYED: the backup failed ({exc}). The live server was not touched.")
            return False
        self.say(f"  backup: {made}")

        self.say("4/6 updating the live folder ...")
        try:
            with keep_private_files(self.live_dir):
                git(self.live_dir, "merge", "--ff-only", self.branch)
        except DeployError as exc:
            self.say(f"NOT DEPLOYED: {exc}")
            return False
        self.say("5/6 restarting the live server ...")
        problem: str | None
        try:
            self.restart()
            self.say("6/6 checking that it works ...")
            problem = self.healthy()
        except Exception as exc:
            problem = f"restart failed: {exc}"
        if problem is None:
            self._log(f"deployed {self._short(current)} -> {self._short(target)} ok")
            self.say(f"DEPLOYED {self._short(target)}. Players had to log in again; running games ended.")
            return True

        self.say(f"PROBLEM after the deploy: {problem}. Rolling back to {self._short(current)} ...")
        self._log(f"deploy {self._short(current)} -> {self._short(target)} FAILED ({problem}); rolling back")
        self._rollback_to(current, "automatic rollback")
        return False  # the deploy did not succeed, whether or not the undo did

    def _rollback_to(self, commit: str, why: str) -> bool:
        assert self.restart and self.healthy
        try:
            with keep_private_files(self.live_dir):
                git(self.live_dir, "reset", "--hard", commit)
            self.restart()
            problem = self.healthy()
        except Exception as exc:
            problem = f"{type(exc).__name__}: {exc}"
        if problem is None:
            self._log(f"rolled back to {self._short(commit)} ({why}) ok")
            self.say(f"ROLLED BACK to {self._short(commit)}; the live server is healthy again.")
            return True
        self._log(f"ROLLBACK to {self._short(commit)} FAILED: {problem}")
        self.say(f"!!! THE ROLLBACK ALSO FAILED: {problem}. The live server needs a person: see data\\live.err.log in the live folder.")
        return False

    def rollback(self, commit: str | None = None) -> bool:
        """Put the live folder back on `commit`, or on the commit it had before the last recorded deploy."""
        if commit is None:
            log = self.live_dir / "deploys.log"
            lines = [line for line in log.read_text(encoding="utf-8").splitlines() if " deployed " in line] if log.is_file() else []
            if not lines:
                self.say("There is no deploy on record to roll back; give a commit: ops/deploy.py rollback COMMIT")
                return False
            commit = lines[-1].split(" deployed ", 1)[1].split(" -> ", 1)[0]
        try:
            commit = git(self.live_dir, "rev-parse", "--verify", commit + "^{commit}")
        except DeployError as exc:
            self.say(str(exc))
            return False
        if self.dry_run:
            self.say(f"Dry run: would put live back on {self._short(commit)}.")
            return True
        return self._rollback_to(commit, "manual rollback")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy main to the live server, safely")
    parser.add_argument("command", nargs="?", default="deploy", choices=("deploy", "rollback"))
    parser.add_argument("commit", nargs="?", help="for rollback: the commit to go back to")
    parser.add_argument("--live-dir", default=str(backup_tools.default_live_dir()))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--quick", action="store_true", help="skip the slow AI-tool tests")
    parser.add_argument("--dry-run", action="store_true", help="check everything, change nothing")
    args = parser.parse_args(argv)
    deployer = Deployer(ROOT, Path(args.live_dir), port=args.port, quick=args.quick, dry_run=args.dry_run)
    ok = deployer.rollback(args.commit) if args.command == "rollback" else deployer.deploy()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
