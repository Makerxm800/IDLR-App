"""Tests for build.py — the packaging step nobody can re-run in a code review.

PyInstaller reads the `--version-file` we generate by `eval()`-ing it against its
own classes, so a wrong keyword name is a build-time crash on Windows only. Check
it here against the same signature PyInstaller defines.
"""

import json
import sys
import tempfile
import unittest
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

    def test_version_file_survives_a_missing_package_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = build.ROOT
            try:
                build.ROOT = Path(tmp)  # no package.json here
                self.assertEqual(build._version_fields()[1], "2.0.4")
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


if __name__ == "__main__":
    unittest.main()
