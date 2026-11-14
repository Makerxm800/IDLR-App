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


if __name__ == "__main__":
    unittest.main()
