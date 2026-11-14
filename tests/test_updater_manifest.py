import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from scripts.create_updater_manifest import create_manifest, normalize_target


TAURI_CONFIG = Path(__file__).resolve().parents[1] / "src-tauri" / "tauri.conf.json"
VERSION = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))["version"]


class UpdaterManifestTests(unittest.TestCase):
    def test_normalizes_runner_architectures(self):
        self.assertEqual(normalize_target("Windows", "X64"), "windows-x86_64")
        self.assertEqual(normalize_target("Linux", "X64"), "linux-x86_64")
        self.assertEqual(normalize_target("macOS", "ARM64"), "darwin-aarch64")

    def test_creates_manifest_from_signed_platform_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts = Path(temp_dir)
            packages = {
                "Windows": ("X64", f"nsis/IDLR App_{VERSION}_x64-setup.exe"),
                "Linux": ("X64", f"appimage/IDLR App_{VERSION}_amd64.AppImage"),
                "macOS": ("ARM64", "macos/IDLR.app.tar.gz"),
            }
            for os_name, (architecture, relative_package) in packages.items():
                platform_dir = artifacts / os_name
                platform_dir.mkdir()
                (platform_dir / f"target-{os_name}.txt").write_text(
                    architecture,
                    encoding="utf-8",
                )
                package = platform_dir / relative_package
                package.parent.mkdir(parents=True)
                package.write_bytes(b"test updater package")
                package.with_name(package.name + ".sig").write_text(
                    f"signature-{os_name}",
                    encoding="utf-8",
                )

            manifest = create_manifest(
                f"v{VERSION}",
                "Makerxm800/IDLR-App",
                artifacts,
            )

        self.assertEqual(manifest["version"], VERSION)
        self.assertEqual(
            set(manifest["platforms"]),
            {"windows-x86_64", "linux-x86_64", "darwin-aarch64"},
        )
        self.assertTrue(
            unquote(manifest["platforms"]["windows-x86_64"]["url"]).endswith(
                f"IDLR.App_{VERSION}_x64-setup.exe"
            )
        )
        self.assertTrue(
            unquote(manifest["platforms"]["linux-x86_64"]["url"]).endswith(
                f"IDLR.App_{VERSION}_amd64.AppImage"
            )
        )
        self.assertTrue(
            unquote(manifest["platforms"]["darwin-aarch64"]["url"]).endswith(
                "IDLR.app.tar.gz"
            )
        )
        self.assertEqual(
            manifest["platforms"]["darwin-aarch64"]["signature"],
            "signature-macOS",
        )

    def write_platform(self, artifacts, os_name, architecture, package):
        platform_dir = artifacts / os_name
        platform_dir.mkdir(parents=True)
        (platform_dir / f"target-{os_name}.txt").write_text(architecture, encoding="utf-8")
        installer = platform_dir / package
        installer.parent.mkdir(parents=True, exist_ok=True)
        installer.write_bytes(b"test updater package")
        installer.with_name(installer.name + ".sig").write_text("signature", encoding="utf-8")

    def windows_and_linux(self, artifacts):
        self.write_platform(artifacts, "Windows", "X64", "IDLR_x64-setup.exe")
        self.write_platform(artifacts, "Linux", "X64", "IDLR_amd64.AppImage")

    def test_rejects_artifacts_without_a_macos_updater(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts = Path(temp_dir)
            self.windows_and_linux(artifacts)

            with self.assertRaisesRegex(ValueError, "missing a signed"):
                create_manifest(
                    f"v{VERSION}",
                    "Makerxm800/IDLR-App",
                    artifacts,
                )

    def test_may_publish_without_the_macos_package(self):
        """The macOS leg costs 10x the included runner minutes, so it can be
        cancelled for reasons unrelated to this release. That must not hold the
        Windows and Linux installers hostage — and the manifest has to stay
        *valid*, because the updater reads it on every check for updates."""
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts = Path(temp_dir)
            self.windows_and_linux(artifacts)
            manifest = create_manifest(
                f"v{VERSION}",
                "Makerxm800/IDLR-App",
                artifacts,
                {"macos"},
            )
        self.assertEqual(set(manifest["platforms"]), {"windows-x86_64", "linux-x86_64"})
        self.assertEqual(manifest["version"], VERSION)

    def test_nothing_excuses_a_missing_windows_or_linux_package(self):
        for os_name, package in (("Windows", "IDLR_x64-setup.exe"),
                                 ("Linux", "IDLR_amd64.AppImage")):
            with self.subTest(missing=os_name), tempfile.TemporaryDirectory() as temp_dir:
                artifacts = Path(temp_dir)
                other = "Linux" if os_name == "Windows" else "Windows"
                self.write_platform(artifacts, other, "X64",
                                    f"IDLR_{'amd64.AppImage' if other == 'Linux' else 'x64-setup.exe'}")
                with self.assertRaisesRegex(ValueError, "must include a signed Windows and Linux"):
                    create_manifest(f"v{VERSION}", "Makerxm800/IDLR-App", artifacts, {"macos"})


if __name__ == "__main__":
    unittest.main()
