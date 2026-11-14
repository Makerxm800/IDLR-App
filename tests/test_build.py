"""Tests for build.py — the packaging step nobody can re-run in a code review.

PyInstaller reads the `--version-file` we generate by `eval()`-ing it against its
own classes, so a wrong keyword name is a build-time crash on Windows only. Check
it here against the same signature PyInstaller defines.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build  # noqa: E402


class VersionFileTests(unittest.TestCase):
    def test_version_tuple_is_four_numbers_from_package_json(self):
        declared = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        version_tuple, version_text = build._version_fields()
        self.assertEqual(len(version_tuple), 4)
        self.assertTrue(all(isinstance(part, int) for part in version_tuple))
        self.assertEqual(version_text, ".".join(declared.split(".")[:3]))

    def test_generated_version_file_matches_pyinstaller_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build.write_windows_version_file(Path(tmp) / "v.txt", "idlr-worker", "2.0.4")
            text = Path(path).read_text(encoding="utf-8")

        captured = {}

        class FixedFileInfo:
            # Must accept exactly what PyInstaller's FixedFileInfo.__init__ takes.
            def __init__(self, filevers=(0, 0, 0, 0), prodvers=(0, 0, 0, 0), mask=0x3f,
                         flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)):
                captured["fixed"] = dict(filevers=filevers, prodvers=prodvers, mask=mask,
                                         flags=flags, OS=OS, fileType=fileType,
                                         subtype=subtype, date=date)

        class StringStruct:
            def __init__(self, key, value):
                captured.setdefault("strings", {})[key] = value

        class StringTable:
            def __init__(self, name=None, kids=None):
                captured["table"] = (name, kids)

        class StringFileInfo:
            def __init__(self, kids=None):
                captured["stringFileInfo"] = kids

        class VarStruct:
            def __init__(self, key, value):
                captured.setdefault("vars", {})[key] = value

        class VarFileInfo:
            def __init__(self, kids=None):
                captured["varFileInfo"] = kids

        class VSVersionInfo:
            def __init__(self, ffi=None, kids=None):
                captured["root"] = (ffi, kids)

        namespace = dict(VSVersionInfo=VSVersionInfo, FixedFileInfo=FixedFileInfo,
                         StringFileInfo=StringFileInfo, StringTable=StringTable,
                         StringStruct=StringStruct, VarFileInfo=VarFileInfo,
                         VarStruct=VarStruct)
        eval(compile(text, "version-file", "eval"), namespace)  # noqa: S307

        self.assertIn("filevers", captured["fixed"])
        strings = captured["strings"]
        self.assertEqual(strings["ProductName"], "IDLR App")
        self.assertEqual(strings["FileVersion"], "2.0.4")
        self.assertEqual(strings["OriginalFilename"], "idlr-worker.exe")
        self.assertIn("media downloader helper", strings["FileDescription"])
        self.assertNotIn("\n\n", text)

    def test_a_missing_version_manifest_is_a_build_error_not_a_default(self):
        """Guessing a version would stamp the exe with an old number, which makes
        the updater offer an update that is already installed."""
        with tempfile.TemporaryDirectory() as tmp:
            original = build.ROOT
            try:
                build.ROOT = Path(tmp)  # no package.json here
                with self.assertRaises(SystemExit) as caught:
                    build._version_fields()
                self.assertIn("set_version.py", str(caught.exception))
            finally:
                build.ROOT = original

    def test_workers_are_not_upx_packed_and_are_onefile(self):
        """`--noupx` keeps the binaries scannable; onefile keeps the bundle layout
        that src-tauri/tauri.conf.json maps into `binaries/`."""
        source = (ROOT / "build.py").read_text(encoding="utf-8")
        self.assertIn('"--noupx"', source)
        self.assertIn('"--onefile"', source)
        self.assertNotIn('"--onedir"', source)
        self.assertIn("--version-file", source)

    def test_bundled_ffmpeg_is_prepared_before_pyinstaller_runs(self):
        source = (ROOT / "build.py").read_text(encoding="utf-8")
        main_body = source.split("def main():", 1)[1]
        self.assertLess(main_body.index("fetch_ffmpeg_for_bundling()"),
                        main_body.index('run_pyinstaller("idlr-worker"'),
                        "FFmpeg must be fetched before PyInstaller collects it")

    def test_startup_cannot_be_killed_by_optional_setup(self):
        """FFmpeg prep must be guarded, and a packaged build must not install or
        create anything next to its own exe (Program Files is read-only, and a
        runtime download of an .exe is what antivirus reacts to)."""
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("def _setup_ffmpeg():", app_source)
        # Every call into static-ffmpeg must be inside a try, and only ever the
        # `weak=True` variant, which never touches the network when the
        # binaries are already on disk.
        self.assertEqual(app_source.count("static_ffmpeg.add_paths("),
                         app_source.count("static_ffmpeg.add_paths(weak=True)"))
        self.assertIn("except Exception as e:", app_source)
        # No unguarded fatal exits during startup.
        self.assertNotIn("sys.exit(1)", app_source)
        # pip is a source-install repair only, and it is opt-out-able.
        self.assertIn("if not IS_FROZEN and os.environ.get(\"IDLR_NO_AUTO_INSTALL\") != \"1\":",
                      app_source)
        self.assertIn("if IS_FROZEN:", app_source)


class SharedFFmpegBundlingTests(unittest.TestCase):
    """FFmpeg is staged once beside the helpers, not embedded inside each one.

    `--collect-all static_ffmpeg` used to copy the ~80 MB of binaries it
    downloads into its package folder into *both* onefile exes — which is what
    made the Windows installer 235 MB and made every launch re-extract ~160 MB
    into a random %TEMP% folder before doing anything.
    """

    def test_pyinstaller_collects_the_module_not_its_binaries(self):
        source = (ROOT / "build.py").read_text(encoding="utf-8")
        self.assertNotIn('"--collect-all",\n        "static_ffmpeg"', source,
                         "collecting static-ffmpeg wholesale embeds its downloaded FFmpeg per helper")
        self.assertIn('"--collect-submodules",\n        "static_ffmpeg"', source)

    def test_staging_copies_both_tools_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            for tool in ("ffmpeg", "ffprobe"):
                (bin_dir / tool).write_text("#!/bin/sh\n", encoding="utf-8")
            stage = Path(tmp) / "dist" / "ffmpeg"
            with (
                unittest.mock.patch.object(build.shutil, "which",
                                           side_effect=lambda name, *a, **k: str(bin_dir / name)
                                           if name in ("ffmpeg", "ffprobe") else None),
                unittest.mock.patch.object(build, "ffmpeg_staging_dir", lambda: stage),
            ):
                self.assertTrue(build.stage_ffmpeg_for_bundling())
            self.assertEqual(sorted(p.name for p in stage.iterdir()), ["ffmpeg", "ffprobe"])
            self.assertFalse((stage / "FFmpeg-not-staged.txt").exists())

    def test_a_build_without_ffmpeg_still_produces_a_bundle(self):
        """Tauri refuses a resource glob that matches nothing, so the folder has
        to exist even when the builder had no FFmpeg — and IDLR must still run
        (it tells the user instead of crashing)."""
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "dist" / "ffmpeg"
            with (
                unittest.mock.patch.object(build.shutil, "which", return_value=None),
                unittest.mock.patch.object(build, "ffmpeg_staging_dir", lambda: stage),
            ):
                self.assertFalse(build.stage_ffmpeg_for_bundling())
            self.assertTrue((stage / "FFmpeg-not-staged.txt").is_file())

    def test_staging_happens_before_the_bundler_runs(self):
        main_body = (ROOT / "build.py").read_text(encoding="utf-8").split("def main():", 1)[1]
        self.assertLess(main_body.index("stage_ffmpeg_for_bundling()"),
                        main_body.index('run_pyinstaller("idlr-worker"'),
                        "the shared copy must exist before Tauri collects resources")

    def test_installer_maps_the_folder_into_the_resources(self):
        config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        self.assertEqual(config["bundle"]["resources"].get("../dist/ffmpeg/*"), "binaries/ffmpeg/")

    def test_the_frozen_helper_finds_it_next_to_its_exe(self):
        sys.path.insert(0, str(ROOT))
        import app as idlr

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binaries = root / "resources" / "binaries"
            exe = binaries / ("idlr-worker.exe" if os.name == "nt" else "idlr-worker")
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            # Whatever this platform calls them; the layout is the point.
            names = [tool + (".exe" if os.name == "nt" else "") for tool in ("ffmpeg", "ffprobe")]
            for layout in ("folder", "flat"):
                target = binaries / "ffmpeg" if layout == "folder" else binaries
                target.mkdir(parents=True, exist_ok=True)
                for name in names:
                    (target / name).write_bytes(b"")
                with unittest.mock.patch.object(idlr, "IS_FROZEN", True):
                    found = idlr._shipped_ffmpeg_dir(str(exe))
                self.assertTrue(found, f"the shipped FFmpeg was not found in the {layout} layout")
                shutil.rmtree(target, ignore_errors=True)

    def test_a_source_checkout_is_unaffected(self):
        sys.path.insert(0, str(ROOT))
        import app as idlr

        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(idlr, "IS_FROZEN", False):
                self.assertEqual(idlr._shipped_ffmpeg_dir(str(Path(tmp) / "python")), "")


if __name__ == "__main__":
    unittest.main()
