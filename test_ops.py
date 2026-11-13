"""The operations tools: backups, the health probe, and safe deploys with automatic rollback (the supervisor: test_supervise.py)."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ops import backup, supervise
from ops.deploy import Deployer, git, keep_private_files


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        (self.data / "saves").mkdir(parents=True)
        (self.data / "accounts.json").write_text('{"accounts": {"ann": {}}}', encoding="utf-8")
        (self.data / "daily.json").write_text('{"days": {}}', encoding="utf-8")
        (self.data / "saves" / "g1.json").write_text('{"slug": "g1"}', encoding="utf-8")
        (self.data / "reload_snapshot.json").write_text('{"sessions": ["secret-token"]}', encoding="utf-8")  # must never be copied
        (self.data / "live.log").write_text("a log line\n", encoding="utf-8")
        self.backups = self.root / "backups"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_backup_copies_exactly_the_data_files(self) -> None:
        made = backup.make_backup(self.data, self.backups, "pre-deploy", now=1_700_000_000)
        names = sorted(p.name for p in made.iterdir())
        self.assertEqual(names, ["accounts.json", "backup.json", "daily.json", "saves"])
        self.assertTrue((made / "saves" / "g1.json").is_file())
        manifest = json.loads((made / "backup.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["label"], "pre-deploy")
        self.assertIn("accounts.json", manifest["contents"])
        self.assertTrue(made.name.endswith("-pre-deploy"))

    def test_damaged_data_stops_the_backup_and_leaves_nothing_behind(self) -> None:
        (self.data / "accounts.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(backup.BackupError):
            backup.make_backup(self.data, self.backups)
        self.assertEqual(list(self.backups.iterdir()), [])  # not even a partial folder

    def test_a_missing_data_folder_and_a_double_backup_are_refused(self) -> None:
        with self.assertRaises(backup.BackupError):
            backup.make_backup(self.root / "nowhere", self.backups)
        backup.make_backup(self.data, self.backups, now=1_700_000_000)
        with self.assertRaises(backup.BackupError):
            backup.make_backup(self.data, self.backups, now=1_700_000_000)

    def test_retention_keeps_the_newest_of_each_kind(self) -> None:
        for n in range(20):
            backup.make_backup(self.data, self.backups, now=1_700_000_000 + n * 100)
        for n in range(12):
            backup.make_backup(self.data, self.backups, "pre-deploy", now=1_700_100_000 + n * 100)
        removed = backup.prune(self.backups)
        remaining = [p.name for p in backup.list_backups(self.backups)]
        self.assertEqual(len(removed), 6 + 2)
        self.assertEqual(sum(1 for n in remaining if n.endswith("pre-deploy")), 10)
        self.assertEqual(sum(1 for n in remaining if not n.endswith("pre-deploy")), 14)
        newest = time.strftime("%Y%m%d-%H%M%S", time.localtime(1_700_000_000 + 19 * 100))
        self.assertIn(newest, remaining)  # the newest scheduled backup survived
        oldest = time.strftime("%Y%m%d-%H%M%S", time.localtime(1_700_000_000))
        self.assertNotIn(oldest, remaining)

    def test_restore_puts_data_back_after_saving_what_was_there(self) -> None:
        made = backup.make_backup(self.data, self.backups, now=1_700_000_000)
        (self.data / "accounts.json").write_text('{"accounts": {"changed": {}}}', encoding="utf-8")
        (self.data / "saves" / "g1.json").unlink()
        port = socket.socket(); port.bind(("127.0.0.1", 0)); free = port.getsockname()[1]; port.close()
        backup.restore(self.backups, made.name, self.data, port=free)
        self.assertIn('"ann"', (self.data / "accounts.json").read_text(encoding="utf-8"))
        self.assertTrue((self.data / "saves" / "g1.json").is_file())
        safety = [p for p in backup.list_backups(self.backups) if p.name.endswith("pre-restore")]
        self.assertEqual(len(safety), 1)
        self.assertIn('"changed"', (safety[0] / "accounts.json").read_text(encoding="utf-8"))

    def test_restore_refuses_while_a_server_is_running_and_for_unknown_names(self) -> None:
        made = backup.make_backup(self.data, self.backups, now=1_700_000_000)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        with self.assertRaises(backup.BackupError):
            backup.restore(self.backups, made.name, self.data, port=listener.getsockname()[1])
        backup.restore(self.backups, made.name, self.data, port=listener.getsockname()[1], force=True)  # explicit override
        with self.assertRaises(backup.BackupError):
            backup.restore(self.backups, "no-such-backup", self.data, port=1)

    def test_the_command_line(self) -> None:
        base = ["--data-dir", str(self.data), "--backups-dir", str(self.backups)]
        self.assertEqual(backup.main(base), 0)
        self.assertEqual(len(backup.list_backups(self.backups)), 1)
        self.assertEqual(backup.main(["restore", "x", *base]), 2)  # restoring needs --yes and a name
        (self.data / "accounts.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(backup.main(base), 1)  # a damaged file is reported, not preserved


class ProbeTests(unittest.TestCase):
    def serve(self, routes: dict[str, tuple[int, bytes]]) -> int:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                status, body = routes.get(self.path, (404, b"{}"))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return int(server.server_address[1])

    def test_probe_understands_healthy_old_and_broken_servers(self) -> None:
        self.assertTrue(supervise.probe(self.serve({"/healthz": (200, b'{"ok": true}')})))
        self.assertTrue(supervise.probe(self.serve({"/login": (200, b"<html>")})))  # a version from before /healthz
        self.assertFalse(supervise.probe(self.serve({"/healthz": (500, b"{}"), "/login": (200, b"x")})))
        self.assertFalse(supervise.probe(self.serve({"/healthz": (200, b'{"ok": false}')})))
        self.assertFalse(supervise.probe(self.serve({"/healthz": (200, b"not json")})))
        self.assertFalse(supervise.probe(self.serve({})))  # neither answers
        free = socket.socket(); free.bind(("127.0.0.1", 0)); port = free.getsockname()[1]; free.close()
        self.assertFalse(supervise.probe(port, timeout=1))  # nothing listening


def make_repos(root: Path) -> tuple[Path, Path]:
    """A development repo on main with one commit, and a live worktree on branch `live` at the same commit."""
    dev, live = root / "dev", root / "live"
    dev.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.org"), ("config", "user.name", "T"),
                 ("config", "commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(dev), *args], check=True, capture_output=True)
    (dev / "app.txt").write_text("v1", encoding="utf-8")
    commit(dev, "v1")
    subprocess.run(["git", "-C", str(dev), "worktree", "add", "-q", str(live), "-b", "live"], check=True, capture_output=True)
    return dev, live


def commit(repo: Path, text: str) -> str:
    (repo / "app.txt").write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", text], check=True, capture_output=True)
    return git(repo, "rev-parse", "HEAD")


class DeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dev, self.live = make_repos(Path(self.tmp.name))
        self.old = git(self.live, "rev-parse", "HEAD")
        self.new = commit(self.dev, "v2")
        self.calls: list[str] = []
        self.messages: list[str] = []
        self.tests_result: list[str] = []
        self.smoke_result: list[str] = []
        self.health: list[str | None] = [None]  # successive answers of the health check; the last one repeats
        self.backup_error: Exception | None = None
        self.restart_error: Exception | None = None

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def deployer(self, **kwargs: object) -> Deployer:
        def run_tests() -> list[str]:
            self.calls.append("tests")
            return self.tests_result

        def smoke() -> list[str]:
            self.calls.append("smoke")
            return self.smoke_result

        def backup_data() -> str:
            self.calls.append("backup")
            if self.backup_error:
                raise self.backup_error
            return "backups/x"

        def restart() -> None:
            self.calls.append("restart")
            if self.restart_error:
                raise self.restart_error

        def healthy() -> str | None:
            self.calls.append("health")
            return self.health.pop(0) if len(self.health) > 1 else self.health[0]

        return Deployer(self.dev, self.live, say=self.messages.append, run_tests=run_tests, smoke=smoke, backup=backup_data,
                        restart=restart, healthy=healthy, **kwargs)  # type: ignore[arg-type]

    def untrack_private_files(self) -> str:
        """The change that moves the recipe book out of git: the commit removes it, the live folder's copy should survive."""
        for args in (("rm", "-r", "-q", "--cached", "recipes.json", "static"), ):
            subprocess.run(["git", "-C", str(self.dev), *args], check=True, capture_output=True)
        return commit(self.dev, "v3 (book no longer in git)")

    def track_private_files_in_live_history(self) -> None:
        """Before: both folders hold a recipe book and pictures that git tracks (as the project did until the book moved out)."""
        for folder in (self.dev,):
            (folder / "static" / "elements").mkdir(parents=True)
            (folder / "recipes.json").write_text('{"book": "old"}', encoding="utf-8")
            (folder / "static" / "elements" / "water.png").write_bytes(b"png-old")
            subprocess.run(["git", "-C", str(folder), "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(folder), "commit", "-q", "-m", "book tracked"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.live), "merge", "-q", "--ff-only", "main"], check=True, capture_output=True)

    def test_a_deploy_that_removes_the_recipe_book_from_git_keeps_the_live_copy(self) -> None:
        self.track_private_files_in_live_history()
        (self.live / "recipes.json").write_text('{"book": "edited live"}', encoding="utf-8")   # untracked changes are not a git concern
        subprocess.run(["git", "-C", str(self.live), "update-index", "--assume-unchanged", "recipes.json"], check=True)
        target = self.untrack_private_files()
        self.assertTrue(self.deployer().deploy())
        self.assertEqual(self.live_commit(), target)
        self.assertEqual((self.live / "recipes.json").read_text(encoding="utf-8"), '{"book": "edited live"}')
        self.assertEqual((self.live / "static" / "elements" / "water.png").read_bytes(), b"png-old")
        self.assertFalse((self.live / ".deploy-keep").exists())

    def test_a_rollback_to_a_commit_that_still_tracked_the_book_does_not_bring_back_the_old_book(self) -> None:
        self.track_private_files_in_live_history()
        before = self.live_commit()
        self.untrack_private_files()
        self.assertTrue(self.deployer().deploy())
        (self.live / "recipes.json").write_text('{"book": "new"}', encoding="utf-8")
        self.assertTrue(self.deployer().rollback(before))
        self.assertEqual(self.live_commit(), before)
        self.assertEqual((self.live / "recipes.json").read_text(encoding="utf-8"), '{"book": "new"}')

    def test_files_that_are_not_there_are_not_invented(self) -> None:
        with keep_private_files(self.live):
            pass
        self.assertFalse((self.live / "recipes.json").exists())
        self.assertFalse((self.live / ".deploy-keep").exists())

    def live_commit(self) -> str:
        return git(self.live, "rev-parse", "HEAD")

    def log_text(self) -> str:
        path = self.live / "deploys.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_a_good_deploy_runs_every_step_in_order_and_moves_live_forward(self) -> None:
        self.assertTrue(self.deployer().deploy())
        self.assertEqual(self.calls, ["tests", "smoke", "backup", "restart", "health"])
        self.assertEqual(self.live_commit(), self.new)
        self.assertIn(f"deployed {self.old[:7]} -> {self.new[:7]} ok", self.log_text())

    def test_nothing_to_deploy(self) -> None:
        git(self.live, "merge", "--ff-only", "main")
        self.assertTrue(self.deployer().deploy())
        self.assertEqual(self.calls, [])

    def test_failing_tests_or_smoke_leave_live_completely_alone(self) -> None:
        self.tests_result = ["test_x.py: FAILED (errors=1)"]
        self.assertFalse(self.deployer().deploy())
        self.assertEqual(self.calls, ["tests"])
        self.assertEqual(self.live_commit(), self.old)
        self.tests_result, self.smoke_result, self.calls = [], ["/healthz answered 404"], []
        self.assertFalse(self.deployer().deploy())
        self.assertEqual(self.calls, ["tests", "smoke"])
        self.assertEqual(self.live_commit(), self.old)
        self.assertEqual(self.log_text(), "")

    def test_a_dry_run_changes_nothing(self) -> None:
        self.assertTrue(self.deployer(dry_run=True).deploy())
        self.assertEqual(self.calls, ["tests", "smoke"])
        self.assertEqual(self.live_commit(), self.old)

    def test_a_failed_backup_means_no_deploy(self) -> None:
        self.backup_error = OSError("disk full")
        self.assertFalse(self.deployer().deploy())
        self.assertEqual(self.calls, ["tests", "smoke", "backup"])
        self.assertEqual(self.live_commit(), self.old)

    def test_preflight_refuses_unsafe_situations(self) -> None:
        (self.dev / "untracked.txt").write_text("x", encoding="utf-8")
        self.assertFalse(self.deployer().deploy())  # an untracked file in dev: what is tested may differ from what is deployed
        self.assertTrue(any("uncommitted" in m for m in self.messages))
        (self.dev / "untracked.txt").unlink()
        self.messages.clear()
        git(self.dev, "checkout", "-q", "-b", "feature")
        self.assertFalse(self.deployer().deploy())
        self.assertTrue(any("'feature'" in m for m in self.messages))
        git(self.dev, "checkout", "-q", "main")
        (self.live / "app.txt").write_text("hand edit", encoding="utf-8")
        self.messages.clear()
        self.assertFalse(self.deployer().deploy())
        self.assertTrue(any("local changes" in m for m in self.messages))
        git(self.live, "checkout", "--", "app.txt")
        self.assertEqual(self.calls, [])  # none of the steps ran
        self.assertEqual(self.live_commit(), self.old)

    def test_a_diverged_live_branch_is_refused(self) -> None:
        commit(self.live, "hotfix made directly on live")
        self.assertFalse(self.deployer().deploy())
        self.assertTrue(any("diverged" in m for m in self.messages))
        self.assertEqual(self.calls, [])

    def test_an_unhealthy_server_after_deploy_is_rolled_back(self) -> None:
        self.health = ["/login answered 500", None]  # broken after the deploy, fine after the rollback
        self.assertFalse(self.deployer().deploy())
        self.assertEqual(self.calls, ["tests", "smoke", "backup", "restart", "health", "restart", "health"])
        self.assertEqual(self.live_commit(), self.old)
        self.assertEqual((self.live / "app.txt").read_text(encoding="utf-8"), "v1")
        log = self.log_text()
        self.assertIn("FAILED", log)
        self.assertIn(f"rolled back to {self.old[:7]}", log)
        self.assertTrue(any("ROLLED BACK" in m for m in self.messages))

    def test_a_restart_that_crashes_is_rolled_back_too(self) -> None:
        errors = [RuntimeError("powershell exploded"), None]

        def flaky_restart() -> None:
            self.calls.append("restart")
            error = errors.pop(0)
            if error:
                raise error

        deployer = self.deployer()
        deployer.restart = flaky_restart
        self.assertFalse(deployer.deploy())
        self.assertEqual(self.live_commit(), self.old)
        self.assertEqual(self.calls.count("restart"), 2)

    def test_if_the_rollback_fails_too_it_says_so_loudly(self) -> None:
        self.health = ["down", "still down"]
        self.assertFalse(self.deployer().deploy())
        self.assertTrue(any("ROLLBACK ALSO FAILED" in m for m in self.messages))
        self.assertIn("ROLLBACK to", self.log_text())
        self.assertEqual(self.live_commit(), self.old)  # the code is back even though the server is not

    def test_manual_rollback_goes_to_the_commit_before_the_last_deploy(self) -> None:
        self.assertTrue(self.deployer().deploy())
        self.calls.clear()
        self.assertTrue(self.deployer().rollback())
        self.assertEqual(self.live_commit(), self.old)
        self.assertEqual(self.calls, ["restart", "health"])
        self.assertIn("manual rollback", self.log_text())

    def test_rollback_to_a_named_commit_and_to_nonsense(self) -> None:
        self.assertTrue(self.deployer().deploy())
        self.assertTrue(self.deployer().rollback(self.old[:10]))
        self.assertEqual(self.live_commit(), self.old)
        self.assertFalse(self.deployer().rollback("no-such-commit"))

    def test_rollback_without_any_deploy_on_record_asks_for_a_commit(self) -> None:
        self.assertFalse(self.deployer().rollback())
        self.assertTrue(any("no deploy on record" in m for m in self.messages))


if __name__ == "__main__":
    unittest.main()
