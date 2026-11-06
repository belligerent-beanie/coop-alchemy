"""run_and_wait: the restart helper must wait for the command it runs, not for a server that command leaves running."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ops.deploy import run_and_wait

# A command that starts a long-lived child which inherits its output handles (as restart-live.ps1 does with the server), then exits.
# argv: [pid file, how many seconds the child lives]
SPAWN_AND_EXIT = ("import subprocess, sys; "
                  "child = subprocess.Popen([sys.executable, '-c', 'import time, sys; time.sleep(float(sys.argv[1]))', sys.argv[2]]); "
                  "open(sys.argv[1], 'w').write(str(child.pid))")


class RunAndWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.pidfile = Path(self.tmp.name) / "child.pid"
        self.addCleanup(self.kill_child)

    def kill_child(self) -> None:
        try:
            os.kill(int(self.pidfile.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def test_it_does_not_wait_for_a_process_the_command_left_running(self) -> None:
        started = time.monotonic()
        code, _ = run_and_wait([sys.executable, "-c", SPAWN_AND_EXIT, str(self.pidfile), "120"], timeout=60)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - started, 20, "it waited for the long-lived child")
        self.assertTrue(self.pidfile.exists())  # the child really was started, and is still running

    def test_the_pipe_based_way_does_hang_which_is_why_files_are_used(self) -> None:
        """The premise of the test above: reading pipes blocks while a child that inherited them lives (the first real deploy hung)."""
        with self.assertRaises(subprocess.TimeoutExpired):
            # the child lives 8 s: long enough to outlast the 3 s timeout, short enough that the test ends soon after
            subprocess.run([sys.executable, "-c", SPAWN_AND_EXIT, str(self.pidfile), "8"], capture_output=True, timeout=3)

    def test_error_output_comes_back_with_the_exit_code(self) -> None:
        code, text = run_and_wait([sys.executable, "-c", "import sys; sys.stderr.write('bad news'); sys.exit(3)"], timeout=30)
        self.assertEqual(code, 3)
        self.assertIn("bad news", text)

    def test_ordinary_output_is_used_when_there_is_no_error_output(self) -> None:
        code, text = run_and_wait([sys.executable, "-c", "print('all good')"], timeout=30)
        self.assertEqual((code, text.strip()), (0, "all good"))

    def test_a_command_that_runs_too_long_times_out(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            run_and_wait([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)

    def test_it_leaves_no_temporary_files_behind(self) -> None:
        temp = Path(tempfile.gettempdir())
        before = {p.name for p in temp.glob("alchemy-*")}
        run_and_wait([sys.executable, "-c", "print('x')"], timeout=30)
        self.assertEqual({p.name for p in temp.glob("alchemy-*")} - before, set())


if __name__ == "__main__":
    unittest.main()
