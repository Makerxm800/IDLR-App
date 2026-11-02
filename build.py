"""Build IDLR desktop binaries for the current operating system."""

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
IS_WINDOWS = sys.platform == "win32"


def run_pyinstaller(name, script):
    work = BUILD / name
    work.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
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
        "--collect-all",
        "static_ffmpeg",
        "--collect-all",
        "spotdl",
        "--collect-all",
        "ytmusicapi",
    ]
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
    import static_ffmpeg

    static_ffmpeg.add_paths()
    DIST.mkdir(exist_ok=True)
    run_pyinstaller("idlr-worker", "download_worker.py")
    run_pyinstaller("IDLR-spotdl", "spotdl_worker.py")

    if not (DIST / ("idlr-worker.exe" if IS_WINDOWS else "idlr-worker")).is_file():
        raise FileNotFoundError("PyInstaller did not produce the IDLR media worker.")

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        raise RuntimeError("Node.js and npm are required to build the Tauri desktop app.")
    bundles = "nsis" if IS_WINDOWS else "dmg" if sys.platform == "darwin" else "deb,appimage"
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

    print(f"IDLR native desktop build complete: {ROOT / 'src-tauri' / 'target' / 'release'}")


if __name__ == "__main__":
    main()
