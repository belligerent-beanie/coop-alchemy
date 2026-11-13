"""The container files (Dockerfile, docker-compose.yml, .dockerignore, .env.example) agree with the server they start.

Docker itself is not needed here: the tests read the files, and rebuild what the image would contain in a scratch folder and start
the server from it. That does not replace building the image once on a machine that has Docker.
"""

from __future__ import annotations

import fnmatch
import http.client
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from bench_cursor import free_port, wait_up

ROOT = Path(__file__).resolve().parent
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


SUPERVISOR = (ROOT / "ops" / "supervise.py").read_text(encoding="utf-8")


def server_options() -> set[str]:
    return set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', SERVER))


def supervisor_options() -> set[str]:
    return set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', SUPERVISOR))


def entrypoint_parts() -> tuple[set[str], set[str]]:
    """The ENTRYPOINT runs the supervisor: (its own options, the server options it passes on after "--")."""
    block = re.search(r"^ENTRYPOINT \[(.*?)\]\s*$", DOCKERFILE, re.S | re.M)
    assert block, "no ENTRYPOINT in the Dockerfile"
    words = re.findall(r'"([^"]*)"', block.group(1))
    assert "--" in words and words[:2] == ["python", "ops/supervise.py"], "the ENTRYPOINT should run ops/supervise.py ... -- ..."
    cut = words.index("--")
    flags = lambda part: {w for w in part if re.fullmatch(r"--[a-z0-9-]+", w)}   # noqa: E731
    return flags(words[:cut]), flags(words[cut + 1:])


def entrypoint_flags() -> set[str]:
    return entrypoint_parts()[1]


def compose_flags() -> set[str]:
    return set(re.findall(r'^\s+- "(--[a-z0-9-]+)"\s*$', COMPOSE, re.M))


def image_contents(destination: Path) -> None:
    """Copy into `destination` what `docker build` would put in the image, following .dockerignore (names and paths)."""
    patterns = [line.strip().rstrip("/") for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]

    def ignored(relative: str) -> bool:
        return any(fnmatch.fnmatch(relative, pattern) or ("/" not in pattern and fnmatch.fnmatch(relative.rsplit("/", 1)[-1], pattern))
                   for pattern in patterns)

    def copy(folder: Path, target: Path, prefix: str) -> None:
        target.mkdir(exist_ok=True)
        for entry in folder.iterdir():
            relative = prefix + entry.name
            if ignored(relative) or entry.name in ("__pycache__",) or entry.suffix == ".pyc":
                continue
            if entry.is_dir():
                copy(entry, target / entry.name, relative + "/")
            else:
                shutil.copy2(entry, target / entry.name)

    copy(ROOT, destination, "")


class ContainerFileTests(unittest.TestCase):
    def test_every_option_the_container_passes_exists_in_the_server(self) -> None:
        known = server_options()
        supervisor_flags, passed_on = entrypoint_parts()
        self.assertGreaterEqual(len(supervisor_flags), 6, "found too few supervisor options in the ENTRYPOINT")
        self.assertEqual(sorted(supervisor_flags - supervisor_options()), [], "ENTRYPOINT options ops/supervise.py does not accept")
        for where, flags in (("Dockerfile ENTRYPOINT (after --)", passed_on), ("docker-compose.yml command", compose_flags())):
            self.assertGreaterEqual(len(flags), 2, f"found too few options in the {where}: did its layout change?")
            self.assertEqual(sorted(flags - known), [], f"options in the {where} that server.py does not accept")
        from ops.supervise import DATA_OPTIONS   # the data files the supervisor adds itself
        self.assertEqual(sorted({option for option, _ in DATA_OPTIONS} - known), [], "supervisor data options server.py does not accept")

    def test_the_health_check_uses_a_documented_endpoint(self) -> None:
        self.assertIn("/healthz", DOCKERFILE)
        self.assertIn("`GET /healthz`", (ROOT / "API.md").read_text(encoding="utf-8"))

    def test_the_tunnel_token_file_is_never_committed(self) -> None:
        ignored = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        self.assertIn(".env", ignored)
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for line in example.splitlines():
            if line.startswith("TUNNEL_TOKEN="):
                self.assertEqual(line.strip(), "TUNNEL_TOKEN=", "the example must not contain a real token")

    def test_the_game_publishes_no_port_so_the_tunnel_is_the_only_way_in(self) -> None:
        self.assertNotRegex(COMPOSE, r"(?m)^\s+ports:")
        self.assertIn("alchemy:8000", COMPOSE)  # the address to give the tunnel is stated

    def test_the_data_stays_out_of_the_image(self) -> None:
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for private in ("accounts.json", "reports.json", ".env", "data", "backups", "*.pem", "recipes.json", "static/elements"):
            self.assertIn(private, ignore)
        for flag in ("--data-dir", "--backups-dir", "--recipes", "--elements-dir"):
            self.assertRegex(DOCKERFILE, rf'"{flag}", "/data')  # pointed at the volume (the supervisor puts every data file there)

    def test_the_recipe_book_and_pictures_are_not_in_git_and_not_in_the_image(self) -> None:
        ignored = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        for private in ("recipes.json", "static/elements/"):
            self.assertIn(private, ignored)
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "recipes.json", "recipes.mmd", "recipes.svg", "static/elements"],
                                 capture_output=True, text=True)
        if tracked.returncode == 0:   # a copy of the sources that is not a git checkout cannot be asked
            self.assertEqual(tracked.stdout.strip(), "", "the private recipe book and pictures must not be tracked by git")
        with tempfile.TemporaryDirectory() as tmp:
            image_contents(Path(tmp))
            self.assertFalse((Path(tmp) / "recipes.json").exists())
            self.assertFalse((Path(tmp) / "static" / "elements").exists())
            self.assertTrue((Path(tmp) / "static" / "app.js").is_file())

    def test_the_files_the_image_would_hold_are_enough_to_run_and_serve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image, data = Path(tmp) / "app", Path(tmp) / "data"
            image.mkdir()
            data.mkdir()
            image_contents(image)
            self.assertTrue((image / "server.py").is_file() and (image / "static").is_dir())
            self.assertFalse((image / "recipes.json").exists())   # the book comes from the data volume
            shutil.copy2(ROOT / "test_recipes.json", data / "recipes.json")
            self.assertFalse((image / "test_coop.py").exists() or (image / "ops" / "deploy.py").exists() or (image / "accounts.json").exists())
            imports = subprocess.run([sys.executable, "-c", "import server, accounts_admin, ops.supervise"], cwd=image, capture_output=True, text=True)
            self.assertEqual(imports.returncode, 0, imports.stderr)
            port = free_port()
            args = [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(port)]
            for flag in ("accounts-file", "daily-file", "drafts-file", "invites-file", "reports-file", "solo-file", "profile-file"):
                args += [f"--{flag}", str(data / f"{flag}.json")]
            args += ["--recipes", str(data / "recipes.json"), "--elements-dir", str(data / "elements"),
                     "--saves-dir", str(data / "saves"), "--snapshot-file", str(data / "snap.json"),
                     "--pid-file", str(data / "pid"), "--log-file", str(data / "live.log"), "--log-days", "14",
                     "--client-ip-header", "CF-Connecting-IP", "--trusted-proxy", "127.0.0.1", "--max-games", "10"]
            server = subprocess.Popen(args, cwd=image, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait_up(port)
                for path, expect in (("/healthz", b'"ok": true'), ("/", b"Co-op Alchemy"), ("/static/app.js", b"sendCursorTick"),
                                     ("/privacy", b"Privacy")):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    conn.request("GET", path)
                    response = conn.getresponse()
                    body = response.read()
                    conn.close()
                    self.assertEqual(response.status, 200, path)
                    self.assertIn(expect, body, path)
            finally:
                server.terminate()
                try:
                    server.wait(5)
                except subprocess.TimeoutExpired:
                    server.kill()
                time.sleep(0.2)  # let Windows release the log file before the folder is removed


if __name__ == "__main__":
    unittest.main()
