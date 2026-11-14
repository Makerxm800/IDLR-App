"""Create the signed static manifest consumed by the Tauri updater."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def normalize_target(os_name: str, architecture: str) -> str:
    os_key = os_name.strip().lower()
    arch_key = architecture.strip().lower()
    arch = {"x64": "x86_64", "arm64": "aarch64"}.get(arch_key, arch_key)
    if os_key == "windows":
        return f"windows-{arch}"
    if os_key == "linux":
        return f"linux-{arch}"
    if os_key == "macos":
        return f"darwin-{arch}"
    raise ValueError(f"Unsupported updater platform: {os_name} {architecture}")


# Which manifest target prefix belongs to which platform, named the way a person
# would say it: `windows`/`linux`/`macos` rather than `darwin-aarch64`.
PLATFORM_ALIASES = {"windows": "windows-", "linux": "linux-", "macos": "darwin-"}

# Windows and Linux build on GitHub's free standard runners, so a release without
# one of them is a broken release, not an unavailable machine: no flag excuses
# them. macOS costs 10x the included minutes, so its leg can genuinely be
# cancelled for reasons that have nothing to do with this code — hence "optional"
# below, and hence the release publishing without it rather than not at all.
OPTIONAL_PLATFORMS = ("macos",)


def create_manifest(
    tag: str,
    repository: str,
    artifacts: Path,
    allow_missing: set[str] | None = None,
) -> dict[str, object]:
    version = tag.removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError(f"Release tag must contain a semantic version, got {tag!r}.")
    config_path = Path(__file__).resolve().parents[1] / "src-tauri" / "tauri.conf.json"
    configured_version = json.loads(config_path.read_text(encoding="utf-8"))["version"]
    if version != configured_version:
        raise ValueError(
            f"Release tag version {version} does not match tauri.conf.json ({configured_version})."
        )

    platforms: dict[str, dict[str, str]] = {}
    markers = sorted(artifacts.rglob("target-*.txt"))
    for marker in markers:
        os_name = marker.stem.removeprefix("target-")
        architecture = marker.read_text(encoding="utf-8").strip()
        target = normalize_target(os_name, architecture)
        if target in platforms:
            raise ValueError(f"Duplicate build artifact for updater platform {target}.")

        candidates = sorted(marker.parent.rglob("*.sig"))
        for signature in candidates:
            filename = signature.name.removesuffix(".sig")
            if target.startswith("windows-") and filename.lower().endswith("-setup.exe"):
                package = signature.with_name(filename)
            elif target.startswith("linux-") and filename.endswith(".AppImage"):
                package = signature.with_name(filename)
            elif target.startswith("darwin-") and filename.endswith(".app.tar.gz"):
                package = signature.with_name(filename)
            else:
                continue
            if not package.is_file():
                raise ValueError(f"Signature has no matching updater package: {signature}")
            release_filename = package.name.replace(" ", ".")
            platforms[target] = {
                "url": (
                    f"https://github.com/{repository}/releases/download/"
                    f"{quote(tag, safe='')}/{quote(release_filename, safe='')}"
                ),
                "signature": signature.read_text(encoding="utf-8").strip(),
            }
            break
        else:
            raise ValueError(f"No signed updater package found for {target}.")

    # A platform with no signed package at all is only tolerated when the caller
    # excuses it (see OPTIONAL_PLATFORMS). A *wrong architecture* for Windows or
    # Linux — which each publish exactly one x86_64 target — is never excusable:
    # that is a packaging bug, not an unavailable runner.
    absent = {alias for alias, prefix in PLATFORM_ALIASES.items()
              if not any(key.startswith(prefix) for key in platforms)}
    required = set(PLATFORM_ALIASES) - set(OPTIONAL_PLATFORMS)
    excused = allow_missing or set()
    wrong_arch = {f"{alias}-x86_64" for alias in sorted(required)
                  if alias not in absent and f"{alias}-x86_64" not in platforms}
    if absent & required:
        raise ValueError(
            "Release must include a signed Windows and Linux updater package; missing: "
            + ", ".join(sorted(absent & required))
            + "."
        )
    if wrong_arch:
        raise ValueError("Release is missing the published updater target(s): "
                         + ", ".join(sorted(wrong_arch)) + ".")
    if absent - excused:
        raise ValueError(
            "Release is missing a signed updater package for "
            + ", ".join(sorted(absent - excused))
            + ". Pass --allow-missing-platform " + " ".join(sorted(absent - excused))
            + " to publish without it (those platforms keep their current version)."
        )
    if excused:
        print("NOTE: publishing without " + ", ".join(sorted(excused))
              + "; they are absent from latest.json and will not be offered this update.")

    return {
        "version": version,
        "pub_date": datetime.now(timezone.utc).isoformat(),
        "platforms": platforms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-missing-platform",
        action="append",
        choices=sorted(OPTIONAL_PLATFORMS),
        default=[],
        metavar="PLATFORM",
        help="Publish even though this platform's signed package is absent "
             "(e.g. its runner was cancelled). Repeatable.",
    )
    args = parser.parse_args()

    if not args.artifacts.is_dir():
        parser.error(f"Artifacts directory does not exist: {args.artifacts}")
    manifest = create_manifest(
        args.tag, args.repository, args.artifacts, set(args.allow_missing_platform)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    for marker in args.artifacts.rglob("target-*.txt"):
        marker.unlink()
    print(f"Created signed updater manifest: {args.output}")


if __name__ == "__main__":
    main()
