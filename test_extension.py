"""The Chrome extension (extension/) is built from the same picker as the game's pages: these tests guard that build. The end-to-end test that
loads it into a browser is extension/test/e2e.mjs (it opens a window, so it is not part of this suite)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXT = ROOT / "extension"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class ExtensionBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "dist"
        subprocess.run([NODE, str(EXT / "build.mjs"), "--out", str(cls.out)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_the_picker_is_the_games_own_file_not_a_copy_that_can_drift(self) -> None:
        self.assertEqual((self.out / "designer.js").read_bytes(), (ROOT / "static" / "designer.js").read_bytes())
        sheet = (self.out / "designer-css.js").read_text(encoding="utf-8")
        self.assertTrue(sheet.startswith("window.__designerCss = "))
        self.assertIn(".dz-dock", sheet)

    def test_the_manifest_asks_for_nothing_broad_and_names_files_that_exist(self) -> None:
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(sorted(manifest["permissions"]), ["activeTab", "scripting", "storage"])
        self.assertNotIn("host_permissions", manifest)   # all sites is optional, asked for by the designer in the options page
        self.assertEqual(manifest["optional_host_permissions"], ["<all_urls>"])
        named = [manifest["background"]["service_worker"], manifest["options_ui"]["page"], *manifest["icons"].values()]
        for name in named:
            self.assertTrue((self.out / name).is_file(), name)
        for name in ("host.js", "designer-css.js", "designer.js"):   # the files the service worker injects, in this order
            self.assertTrue((self.out / name).is_file(), name)
        self.assertIn('const FILES = ["host.js", "designer-css.js", "designer.js"]', (self.out / "background.js").read_text(encoding="utf-8"))

    def test_the_icons_are_real_pngs(self) -> None:
        for size in (16, 48, 128):
            data = (self.out / "icons" / f"{size}.png").read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(int.from_bytes(data[16:20], "big"), size)

    def test_every_script_parses(self) -> None:
        for name in ("host.js", "background.js", "designer.js", "options.js", "designer-css.js"):
            result = subprocess.run([NODE, "--check", str(self.out / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    def test_the_test_build_is_the_only_one_with_host_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([NODE, str(EXT / "build.mjs"), "--test", "--out", tmp], check=True, capture_output=True)
            self.assertEqual(json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))["host_permissions"], ["<all_urls>"])


class PickerHostSeamTests(unittest.TestCase):
    """The places the picker depends on where it runs go through `host`; the extension replaces them, so they must stay the only ones."""

    def setUp(self) -> None:
        self.source = (ROOT / "static" / "designer.js").read_text(encoding="utf-8")

    def test_storage_goes_through_the_host(self) -> None:
        body = self.source.split("}, pre || {});", 1)[1]   # everything after the host defaults
        self.assertNotIn("localStorage.", body)
        self.assertNotIn("Auth.", body)

    def test_events_and_focus_are_read_the_shadow_dom_safe_way(self) -> None:
        body = self.source.split("const eventTarget", 1)[1].split("\n", 1)[1]   # after the helper that reads event.target itself
        self.assertNotIn("event.target", body)
        self.assertNotIn("document.activeElement", body.replace("document.activeElement)", ""))


if __name__ == "__main__":
    unittest.main()
