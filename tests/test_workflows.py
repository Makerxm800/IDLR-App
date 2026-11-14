"""Guards for the GitHub workflows, kept dependency-free (no PyYAML in CI).

These are text assertions rather than parsed-YAML ones for a reason: the release
process depends on them, and the whole point of a workflow check is to fail on a
typo in a file that Python never imports. They exist because the v2.0.5 attempt
lost two things at once — a test gate that only passed on LF checkouts, and a
release that could not publish because the macOS leg never got a runner.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = (ROOT / ".github" / "workflows" / "idlr-app-release.yml").read_text(encoding="utf-8")
TESTS = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")


def job_body(source, job_id):
    """One top-level job's whole body: every line indented at least four spaces,
    blank lines included, stopping at the next job (which is indented two)."""
    match = re.search(rf"^  {job_id}:\n((?:(?: {{4,}}|\t).*\n?|\n)+)", source, re.M)
    assert match, f"{job_id} is not a job in this workflow"
    return match.group(1)


class ReleaseWorkflowTests(unittest.TestCase):
    def test_the_suite_that_gates_a_release_is_the_suite_in_pr_checks(self):
        """`tests.yml` exists to catch a release-blocker early, which only works
        if it runs the identical command with the identical environment."""
        for source in (RELEASE, TESTS):
            step = re.search(r"- name: Run test suite\n((?: +[^\n]*\n)+)", source)
            self.assertIsNotNone(step, "a workflow lost its 'Run test suite' step")
            self.assertIn("python -m unittest discover -s tests", step.group(1))
            # Either on the step or for the whole file, but it has to be set: the
            # suite must not race `import app`'s optional downloads.
            self.assertIn('IDLR_NO_AUTO_INSTALL: "1"', step.group(1) + source,
                          "a workflow runs the suite without IDLR_NO_AUTO_INSTALL")

    def test_all_three_platforms_are_still_tested(self):
        for os_name in ("windows-latest", "ubuntu-24.04", "macos-14"):
            self.assertIn(os_name, RELEASE, f"{os_name} disappeared from the release matrix")
            self.assertIn(os_name, TESTS, f"{os_name} disappeared from the PR test matrix")

    def test_macos_cannot_block_a_windows_release(self):
        """macOS runners cost 10x the included minutes, so that leg can be
        cancelled with no runner ever assigned. It must be its own job, and
        publish must tolerate exactly that outcome — while a macOS build that
        actually fails still stops the release."""
        self.assertIn("  build-macos:\n", RELEASE, "macOS must not share a matrix with Windows/Linux")
        publish = job_body(RELEASE, "publish")
        condition = re.search(r"if: >-\n((?: +[^\n]*\n)+)", publish).group(1)
        self.assertIn("needs.build-macos.result == 'cancelled'", condition)
        self.assertIn("needs.build-macos.result == 'success'", condition)
        self.assertNotIn("needs.build-macos.result == 'failure'", condition,
                         "a broken macOS build has to stop the release")
        self.assertIn("needs.build-desktop.result == 'success'", condition,
                      "Windows/Linux must still be required outright")

    def test_publish_is_still_tag_or_reuse_gated(self):
        publish = job_body(RELEASE, "publish")
        self.assertIn("startsWith(github.ref, 'refs/tags/v')", publish)
        self.assertIn("inputs.publish_from_run_id != ''", publish)
        # A plain "Run workflow" click must build without publishing anything.
        build = job_body(RELEASE, "build-desktop")
        self.assertIn("if: github.event_name != 'workflow_dispatch' || inputs.publish_from_run_id == ''",
                      build)

    def test_manifest_may_be_told_to_skip_macos_only_when_it_is_absent(self):
        """Passing --allow-missing-platform whenever the job did not succeed keeps
        the strict rule in force for a normal three-platform release."""
        self.assertIn('if [ "${{ needs.build-macos.result }}" = "success" ]; then', RELEASE)
        self.assertIn('echo "flag=--allow-missing-platform macos"', RELEASE)
        self.assertIn("${{ steps.macos.outputs.flag }}", RELEASE)

    def test_the_bundled_ffmpeg_is_verified_not_assumed(self):
        """`dist/ffmpeg/` existing is not the same as the installer carrying it.

        A resource glob that matches nothing is a Tauri error, but a mapping that
        points at the right folder and lands in the wrong *layout* is not, and
        the symptom would be an installed IDLR that cannot convert MP3s. Both
        build jobs therefore look for the file inside the build tree.
        """
        for job in ("build-desktop", "build-macos"):
            body = job_body(RELEASE, job)
            self.assertIn("- name: Verify the shared FFmpeg was bundled", body,
                          f"{job} must check that the staged FFmpeg reached the bundle")
            self.assertIn("find src-tauri/target", body)
            self.assertIn("FFmpeg-not-staged.txt", body,
                          "a build without FFmpeg is allowed, it must not be a surprise")

    def test_the_stable_installer_name_is_still_produced(self):
        """The desktop app's updater and the README's download link both point at
        IDLR-App-Setup.exe, so a release without it is not a release."""
        self.assertIn("cp \"$installer\" release-publish/IDLR-App-Setup.exe", RELEASE)
        self.assertIn('test -n "$installer"', RELEASE)


if __name__ == "__main__":
    unittest.main()
