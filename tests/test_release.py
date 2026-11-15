"""Release guards: the version must be one number, stated once per file.

A release is published by pushing a `v<version>` tag, and the updater decides
whether to offer a package by comparing that tag against the version compiled
into the app. So a file left behind by a hand-edit does not look like a typo —
it either re-offers an update that is already installed, or hides the one users
need (which, for the antivirus-quarantine fix, is the release that matters).
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.set_version import read_versions  # noqa: E402


class VersionAgreementTests(unittest.TestCase):
    def test_every_version_source_carries_the_same_number(self):
        versions = read_versions()
        self.assertEqual(len(set(versions.values())), 1,
                         "version sources disagree: " + json.dumps(versions, indent=2)
                         + "\nfix it with: python scripts/set_version.py X.Y.Z")

    def test_version_is_release_shaped(self):
        version = next(iter(read_versions().values()))
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        # Four dotted parts is what PyInstaller's version resource needs.
        import build
        tuple_version, text = build._version_fields()
        self.assertEqual(len(tuple_version), 4)
        self.assertEqual(text, version)

    def test_ui_reads_its_version_from_the_backend(self):
        """index.html must not hard-code a version: it would drift every release.

        Only the `vX.Y.Z` form is checked — SVG path data such as `a1.65 1.65`
        matches a bare three-part number and is not a version.
        """
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        hard_coded = re.findall(r"\bv\d+\.\d+\.\d+\b", html)
        self.assertEqual(hard_coded, [],
                         "hard-coded version(s) in index.html: "
                         f"{hard_coded[:5]} — add class=\"js-appver\" and let /api/info fill it in")
        self.assertIn('class="js-appver"', html)
        self.assertIn("function setAppVersion(version)", html)
        # The old per-release localStorage key is gone, so the What's New modal
        # cannot silently stop showing (or show forever).
        self.assertNotIn("idlr_whatsnew_v", html)
        self.assertIn("function whatsNewKey()", html)

    def test_changelog_documents_the_current_version(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        version = next(iter(read_versions().values()))
        self.assertRegex(readme, r"### v" + re.escape(version) + r" — ")
        entries = re.findall(r"^### v(\d+\.\d+\.\d+) —", readme, re.M)
        self.assertEqual(entries[0], version,
                         "the newest changelog entry should be the version being shipped")

    def test_frontend_version_placeholders_are_wired_up(self):
        """Empty spans are fine before /api/info answers, but every element that
        displays the version has to opt in, or it stays blank forever."""
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        for element_id in ("info-ver", "settings-ver", "settings-ver-foot"):
            tag = re.search(r"<[a-z]+[^>]*id=\"" + element_id + r"\"[^>]*>", html)
            self.assertIsNotNone(tag, f"#{element_id} disappeared from the markup")
            self.assertIn("js-appver", tag.group(0),
                          f"#{element_id} shows the version but is not filled by setAppVersion()")


VERSION_FILES = ("package.json", "package-lock.json", "app.py",
                 "src-tauri/tauri.conf.json", "src-tauri/Cargo.toml",
                 "src-tauri/Cargo.lock", "src-tauri/src/lib.rs")


class CheckoutFlavourTests(unittest.TestCase):
    """The tooling has to work on a Windows checkout, not just this container.

    `git config core.autocrlf true` (the default on Windows machines) hands the
    working tree CRLF files, and the v2.0.5 release build died exactly there:
    `name = "idlr"\nversion = …` does not match `…\r\nversion = …`, so
    set_version.py raised and every platform's test step failed.
    """

    def _crlf_tree(self, target):
        source = ROOT
        for relative in VERSION_FILES:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = (source / relative).read_bytes().replace(b"\r\n", b"\n")
            (destination).write_bytes(data.replace(b"\n", b"\r\n"))

    def test_reads_and_writes_a_crlf_checkout(self):
        from scripts import set_version

        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            self._crlf_tree(tree)
            original = set_version.ROOT
            set_version.ROOT = tree
            try:
                versions = set_version.read_versions()
                self.assertEqual(len(set(versions.values())), 1,
                                 "CRLF checkout parsed inconsistently: " + repr(versions))
                self.assertTrue(set_version.apply("99.98.97"))
                self.assertEqual(set(set_version.read_versions().values()), {"99.98.97"})
                for relative in VERSION_FILES:
                    raw = (tree / relative).read_bytes()
                    self.assertIn(b"\r\n", raw, f"{relative} lost its CRLF endings")
                    self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"),
                                     f"{relative} ended up with mixed line endings")
            finally:
                set_version.ROOT = original

    def test_repository_pins_line_endings(self):
        attributes = ROOT / ".gitattributes"
        self.assertTrue(attributes.is_file(),
                        ".gitattributes is what keeps a Windows checkout from rewriting these files")
        self.assertIn("text=auto eol=lf", attributes.read_text(encoding="utf-8"))


class StartupCostTests(unittest.TestCase):
    """`import app` is the packaged worker's whole startup, so it must stay cheap."""

    def test_optional_packages_are_probed_not_imported_at_startup(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        startup = source.split('log("SETUP", "Application modules ready.')
        self.assertEqual(len(startup), 2, "the startup marker moved")
        startup = startup[0]
        self.assertIn("importlib.util.find_spec(i)", startup,
                      "startup should probe optional packages with find_spec()")
        self.assertNotIn('("static-ffmpeg", "static_ffmpeg"), ("spotdl", "spotdl")]:\n    try:\n        __import__(i)',
                         startup, "the startup loop must not import every package serially")
        # Importing static-ffmpeg and looking for FFmpeg both cost time and can
        # touch the network, so neither may run before the window is up.
        for offender in ("    import static_ffmpeg", "\n_setup_ffmpeg()\n"):
            self.assertNotIn(offender, startup,
                             "an eager module-level import/call belongs in "
                             "_finish_startup_checks(), not in `import app`")

    def test_broken_optional_package_is_still_reported(self):
        """Deferring the import must not hide an installed-but-broken spotdl —
        that is the pydantic-core clash case from the changelog."""
        import importlib

        import app as idlr

        # Let the import-time checks thread finish before anything is counted,
        # and drop any spotdl entry it already recorded: otherwise this test is a
        # race between its own sys.path insert and that thread's `import spotdl`.
        idlr._STARTUP_CHECKS.wait(timeout=60)
        saved_problems = list(idlr.SETUP_PROBLEMS)
        idlr.SETUP_PROBLEMS[:] = [i for i in idlr.SETUP_PROBLEMS
                                  if not (isinstance(i, dict) and i.get("package") == "spotdl")]
        problems_before = len(idlr.SETUP_PROBLEMS)
        # The same thread also prepares FFmpeg; that download is not what this
        # test is about (and would leave a background fetch running).
        saved_setup, idlr._setup_ffmpeg = idlr._setup_ffmpeg, lambda: None
        idlr._STARTUP_CHECKS.clear()
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "spotdl"
            package.mkdir()
            (package / "__init__.py").write_text('raise RuntimeError("simulated broken install")\n',
                                                 encoding="utf-8")
            saved = sys.modules.pop("spotdl", None)
            broken_before = set(idlr.BROKEN_PKGS)
            sys.path.insert(0, tmp)
            try:
                importlib.invalidate_caches()
                idlr._finish_startup_checks()
            finally:
                sys.path.remove(tmp)
                sys.modules.pop("spotdl", None)
                if saved is not None:
                    sys.modules["spotdl"] = saved
                idlr._setup_ffmpeg = saved_setup
        self.assertTrue(idlr._STARTUP_CHECKS.is_set(),
                        "a job that starts immediately waits on this event to learn "
                        "whether FFmpeg turned up, so it has to be released")
        self.assertIn("spotdl", idlr.BROKEN_PKGS - broken_before,
                      "a spotdl that fails to import must land in BROKEN_PKGS")
        self.assertGreater(len(idlr.SETUP_PROBLEMS), problems_before,
                           "…and be mentioned in /api/info's problems")
        # leave the module's shared state exactly as found
        idlr.BROKEN_PKGS.clear()
        idlr.BROKEN_PKGS.update(broken_before)
        idlr.SETUP_PROBLEMS[:] = saved_problems


if __name__ == "__main__":
    unittest.main()
