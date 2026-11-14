"""Build IDLR desktop binaries for the current operating system.

Two things matter a lot here, because they are what makes Windows Defender
(or SmartScreen) quarantine a fresh install and leave the app "deleted itself":

1. Runtime self-extraction. A PyInstaller onefile binary unpacks itself into a
   random %TEMP% folder and runs the result, which is the single most reliable
   way to look like malware to an antivirus. So we pre-place everything that
   would otherwise be created at runtime — including FFmpeg — inside the bundle.
   The app then never writes or downloads an .exe during a session.
2. Metadata. A signed, versioned, description-carrying PE file is judged far
   more leniently than a nameless one. We always embed version info, and sign
   both the workers and the installer when IDLR_SIGN_THUMBPRINT is configured.
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

# Never imported by a headless yt-dlp/spotdl worker; see run_pyinstaller().
BUNDLE_EXCLUDES = (
    "tkinter",
    "matplotlib",
    "IPython",
    "pytest",
    "notebook",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
)
DIST = ROOT / "dist"
BUILD = ROOT / "build"
IS_WINDOWS = sys.platform == "win32"
WORKERS = (("idlr-worker", "download_worker.py"), ("IDLR-spotdl", "spotdl_worker.py"))


def _version_fields():
    """Version tuple for the Windows version resource (major,minor,patch,build).

    Read from `package.json`, which `scripts/set_version.py` keeps in step with
    the Tauri config, the Rust constants and the git tag. There is deliberately
    no fallback constant here: stamping the exe with whatever version was hard-
    coded at release time is how a build advertises the update it already
    installed, so a missing manifest is a build error instead.
    """
    manifest = ROOT / "package.json"
    try:
        raw = str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(
            f"build.py: cannot read the version from {manifest} ({exc!r}). "
            "Run `python scripts/set_version.py X.Y.Z` before packaging."
        )
    parts = []
    for chunk in str(raw).split(".")[:4]:
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4]), ".".join(str(part) for part in parts[:3])


def write_windows_version_file(path, name, version_text):
    """Emit a PyInstaller version-info file so the exe carries real metadata.

    The names below are PyInstaller's `FixedFileInfo` keyword arguments (the
    file is `eval()`'d against PyInstaller's own versioninfo classes), so an
    unknown kwarg is a hard build error — `tests/test_build.py` locks this.
    """
    version_tuple, _ = _version_fields()
    description = f"IDLR App — {name} (local media downloader helper)"
    content = (
        "# UTF-8\n"
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers={version_tuple},\n"
        f"    prodvers={version_tuple},\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0)\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable('040904B0', [\n"
        "        StringStruct('CompanyName', 'IDLR'),\n"
        f"        StringStruct('FileDescription', '{description}'),\n"
        f"        StringStruct('FileVersion', '{version_text}'),\n"
        f"        StringStruct('InternalName', '{name}'),\n"
        "        StringStruct('LegalCopyright', 'MIT License'),\n"
        f"        StringStruct('OriginalFilename', '{name}.exe'),\n"
        "        StringStruct('ProductName', 'IDLR App'),\n"
        f"        StringStruct('ProductVersion', '{version_text}')\n"
        "      ])\n"
        "    ]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n"
    )
    Path(path).write_text(content, encoding="utf-8")
    return path


def sign_windows(paths):
    """Authenticode-sign built binaries when a certificate is configured.

    Set IDLR_SIGN_THUMBPRINT (and optionally IDLR_SIGN_TIMESTAMP_URL) to use the
    Windows signtool that ships with the SDK. Unsigned builds still work, but a
    signed installer is the only real cure for SmartScreen reputation warnings.
    """
    thumbprint = os.environ.get("IDLR_SIGN_THUMBPRINT", "").strip()
    if not IS_WINDOWS or not thumbprint:
        return False
    tool = shutil.which("signtool.exe") or shutil.which("signtool")
    if tool is None:
        print("IDLR: IDLR_SIGN_THUMBPRINT is set but signtool.exe was not found; skipping signing.")
        return False
    timestamp = os.environ.get("IDLR_SIGN_TIMESTAMP_URL", "http://timestamp.digicert.com")
    for target in paths:
        if not Path(target).is_file():
            continue
        subprocess.run(
            [tool, "sign", "/fd", "SHA256", "/tr", timestamp, "/td", "SHA256",
             "/sha1", thumbprint, str(target)],
            check=True,
        )
        print(f"IDLR: signed {Path(target).name}")
    return True


def fetch_ffmpeg_for_bundling():
    """Make static-ffmpeg download FFmpeg NOW, so PyInstaller bundles it.

    If we do not, the installed app has to fetch FFmpeg itself on someone's
    first download — into a folder that may be read-only, on a network that may
    be blocked, and looking to antivirus like a dropper pulling in a binary.
    """
    try:
        import static_ffmpeg
    except Exception as exc:  # not installed: the app still works, just warns
        print(f"IDLR: static-ffmpeg unavailable ({exc}); FFmpeg will not be bundled.")
        return False
    try:
        static_ffmpeg.add_paths()
        found = shutil.which("ffmpeg")
        print(f"IDLR: FFmpeg ready for bundling: {found}" if found
              else "IDLR: FFmpeg could not be resolved; continuing without it.")
        return bool(found)
    except Exception as exc:
        print(f"IDLR: could not prepare FFmpeg for bundling ({exc}). "
              "The app will use a system FFmpeg, or explain that it needs one.")
        return False


def ffmpeg_staging_dir():
    return DIST / "ffmpeg"


def stage_ffmpeg_for_bundling():
    """Copy the FFmpeg binaries into `dist/ffmpeg/` so they ship *once*.

    Until now every helper embedded its own copy: `--collect-all static_ffmpeg`
    pulled `static_ffmpeg/bin/<platform>/ffmpeg.exe` (~80 MB) plus ffprobe into
    the worker AND into the spotdl helper, which is most of why the Windows
    installer is 235 MB and the Linux AppImage 494 MB. With --onefile that also
    meant every launch re-extracted ~160 MB into %TEMP% before doing anything —
    the single biggest reason IDLR feels slow to open, and the behaviour
    antivirus likes to read as a dropper unpacking binaries.

    tauri.conf.json maps `../dist/ffmpeg/*` to `binaries/ffmpeg/`, so the files
    land next to the helpers in the install folder, are written once by the
    installer, and app.py finds them by looking beside its own executable.
    """
    target = ffmpeg_staging_dir()
    target.mkdir(parents=True, exist_ok=True)
    staged = []
    for tool in ("ffmpeg", "ffprobe"):
        source = shutil.which(tool)
        if not source:
            continue
        destination = target / (tool + (".exe" if IS_WINDOWS else ""))
        shutil.copy2(source, destination)
        staged.append(f"{tool}: {destination.stat().st_size / 1048576:.1f} MB")
    if len(staged) < 2:
        # Tauri refuses a resource glob that matches no file, and an IDLR build
        # without FFmpeg is still a working app — it says so in the UI instead
        # of crashing. A placeholder keeps the bundle building either way.
        (target / "FFmpeg-not-staged.txt").write_text(
            "IDLR expects ffmpeg and ffprobe next to this file. This build could "
            "not stage them (no FFmpeg was available at build time), so IDLR will "
            "fall back to a system FFmpeg and say so in the banner instead of "
            "failing.\n",
            encoding="utf-8",
        )
    print("IDLR: staged shared FFmpeg for bundling — " + (", ".join(staged) if staged
          else "nothing found; the app will use a system FFmpeg"))
    return len(staged) == 2


def run_pyinstaller(name, script):
    work = BUILD / name
    work.mkdir(parents=True, exist_ok=True)
    _, version_text = _version_fields()
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        # Onefile + a random %TEMP% extraction is the top cause of Defender
        # deleting an IDLR install. --noupx keeps the PE unpacked and scannable,
        # and the version resource below describes what the file actually is.
        "--noupx",
        "--onefile",
        "--name",
        name,
        "--distpath",
        str(DIST),
        "--workpath",
        str(work),
        "--specpath",
        str(work),
        "--collect-all",
        "yt_dlp",
        # Only the module: `--collect-all` would also embed the ~80 MB of FFmpeg
        # binaries it downloaded into its package folder, per helper. The shared
        # copy staged by stage_ffmpeg_for_bundling() is what ships now.
        "--collect-submodules",
        "static_ffmpeg",
        "--collect-all",
        "spotdl",
        "--collect-all",
        "ytmusicapi",
    ]
    # --collect-all drags in each package's optional extras, and PyInstaller's
    # hooks happily add GUI/plotting/test stacks that a headless downloader
    # never imports. With --onefile, every byte of that is unpacked into
    # %TEMP% on every launch (and scanned by antivirus), so excluding them is
    # both a startup win and a smaller, more boring PE.
    for module in BUNDLE_EXCLUDES:
        command += ["--exclude-module", module]
    if IS_WINDOWS:
        version_file = write_windows_version_file(work / f"{name}.version.txt", name, version_text)
        command += ["--version-file", str(version_file)]
    command.append(str(ROOT / script))
    subprocess.run(command, cwd=ROOT, check=True)


def windows_short_path(path):
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(
        str(path), buffer, len(buffer)
    )
    if not length or length >= len(buffer) or " " in buffer.value:
        raise RuntimeError(f"Could not resolve a cmd-safe path for {path}.")
    return buffer.value


def main():
    DIST.mkdir(exist_ok=True)
    fetch_ffmpeg_for_bundling()
    stage_ffmpeg_for_bundling()
    run_pyinstaller("idlr-worker", "download_worker.py")
    run_pyinstaller("IDLR-spotdl", "spotdl_worker.py")

    if not (DIST / ("idlr-worker.exe" if IS_WINDOWS else "idlr-worker")).is_file():
        raise FileNotFoundError("PyInstaller did not produce the IDLR media worker.")

    sign_windows([DIST / ("idlr-worker.exe" if IS_WINDOWS else "idlr-worker"),
                  DIST / ("IDLR-spotdl.exe" if IS_WINDOWS else "IDLR-spotdl")])

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        raise RuntimeError("Node.js and npm are required to build the Tauri desktop app.")
    if IS_WINDOWS:
        bundles = "nsis"
    elif sys.platform == "darwin":
        bundles = "app,dmg"
    else:
        bundles = "deb,appimage"
    if IS_WINDOWS:
        vs_dev_cmd = (
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Microsoft Visual Studio"
            / "2022"
            / "BuildTools"
            / "Common7"
            / "Tools"
            / "VsDevCmd.bat"
        )
        if vs_dev_cmd.is_file():
            env = os.environ.copy()
            cargo_bin = Path.home() / ".cargo" / "bin"
            if cargo_bin.is_dir():
                env["PATH"] = f"{cargo_bin}{os.pathsep}{env.get('PATH', '')}"
            command = (
                f"call {windows_short_path(vs_dev_cmd)} -arch=x64 -host_arch=x64 >nul"
                f" && npm run tauri -- build --bundles {bundles}"
            )
            subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", command],
                cwd=ROOT,
                env=env,
                check=True,
            )
        else:
            subprocess.run([npm, "run", "tauri", "--", "build", "--bundles", bundles], cwd=ROOT, check=True)
    else:
        subprocess.run([npm, "run", "tauri", "--", "build", "--bundles", bundles], cwd=ROOT, check=True)

    if IS_WINDOWS:
        sign_windows(sorted(ROOT.glob("src-tauri/target/release/bundle/nsis/*-setup.exe")))

    print(f"IDLR native desktop build complete: {ROOT / 'src-tauri' / 'target' / 'release'}")


if __name__ == "__main__":
    main()
