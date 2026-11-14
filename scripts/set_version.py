"""Set or verify the version IDLR ships with, across every file that must agree.

Seven places carry the same number and each one breaks differently when they
drift apart:

* ``package.json``           — what ``build.py`` stamps into the Windows version
                               resource of the helper executables, and what the
                               ``v<version>`` tag is compared against.
* ``src-tauri/tauri.conf.json`` — the NSIS installer name/size and the updater
                               manifest's ``version``; a mismatch means clients
                               either never see the update or update forever.
* ``src-tauri/Cargo.toml``/``.lock`` — the crate version cargo publishes.
* ``package-lock.json`` — ``npm ci`` refuses a lockfile that disagrees.
* ``app.py``                 — ``/api/info`` version, the console banner, the
                               "What's New" gate and the update check.
* ``src-tauri/src/lib.rs``   — the shell's own ``APP_VERSION``.

Hand-editing them is how a release ends up published under last version's
number, so this script is the only sanctioned way to change it::

    python scripts/set_version.py 2.0.5     # write
    python scripts/set_version.py --check   # verify (also run by the tests)

The user-visible *prose* is deliberately not generated: refresh the "What's
New" list in ``index.html`` and add a README changelog entry for the number.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _read(path: Path) -> str:
    # newline="" keeps every byte's line ending exactly as stored, so a run on
    # Windows cannot rewrite the whole file with CRLF.
    with open(path, encoding="utf-8", newline="") as stream:
        return stream.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)


def _sub_once(path: Path, pattern: str, repl: str, text: str) -> str:
    match = re.search(pattern, text, re.M | re.S)
    if not match:
        raise SystemExit(f"{path.relative_to(ROOT)}: no version entry matched {pattern!r}")
    return text[:match.start()] + match.expand(repl) + text[match.end():]


def _cargo_lock_text(version: str | None, text: str) -> str:
    """Rewrite (or read) the version of the `idlr` package in Cargo.lock.

    The lock has hundreds of `version = ".."` lines, so it needs the block
    walk instead of a regex over the whole file.
    """
    blocks = text.split("\n[[package]]\n")
    prefix = ""
    if blocks and 'name = "idlr"' not in blocks[0]:
        prefix, blocks = blocks[0], blocks[1:]
    for index, block in enumerate(blocks):
        if 'name = "idlr"' not in block:
            continue
        match = re.search(r"^version = \"[^\"]+\"$", block, re.M)
        if not match:
            raise SystemExit("src-tauri/Cargo.lock: the idlr package has no version line")
        replacement = f'version = "{version}"' if version else match.group(0)
        blocks[index] = block[:match.start()] + replacement + block[match.end():]
        break
    else:
        raise SystemExit('src-tauri/Cargo.lock: no `name = "idlr"` package block')
    joined = "\n[[package]]\n".join(blocks)
    body = prefix + "\n[[package]]\n" + joined if prefix else "[[package]]\n" + joined
    return body


NPM_ROOT_MARKER = '"packages": {\n    "": {'


def _npm_lock_span(text: str) -> tuple[int, int]:
    """Where the root package's own version lives inside package-lock.json.

    The file repeats `"version"` for every dependency, so the value is anchored
    on the `packages[""]` entry npm writes first. A lockfile left behind is what
    makes `npm ci` fail or rebuild with the previous release's number.
    """
    start = text.find(NPM_ROOT_MARKER)
    if start < 0:
        raise SystemExit("package-lock.json: no `packages[\"\"]` entry to anchor on")
    window = text[start:start + 400]
    match = re.search(r'"version":\s*"([^"]+)"', window)
    if not match:
        raise SystemExit("package-lock.json: the root package entry has no version")
    return start + match.start(1) - 1, start + match.end(1) + 1


def read_versions() -> dict[str, str]:
    """The version each release-critical file currently carries."""
    files = {
        "package.json": (ROOT / "package.json", r'"version":\s*"([^"]+)"'),
        "src-tauri/tauri.conf.json": (ROOT / "src-tauri" / "tauri.conf.json",
                                      r'"version":\s*"([^"]+)"'),
        "src-tauri/Cargo.toml": (ROOT / "src-tauri" / "Cargo.toml",
                                 r'\[package\][^\[]*?^version = "([^"]+)"'),
        "app.py": (ROOT / "app.py", r'^APP_VERSION = "([^"]+)"'),
        "src-tauri/src/lib.rs": (ROOT / "src-tauri" / "src" / "lib.rs",
                                 r'^const APP_VERSION: &str = "([^"]+)";'),
    }
    versions = {}
    for label, (path, pattern) in files.items():
        match = re.search(pattern, _read(path), re.M)
        if not match:
            raise SystemExit(f"{label}: could not find the version ({pattern!r})")
        versions[label] = match.group(1)
    lock = re.search(r'name = "idlr"\nversion = "([^"]+)"',
                     _read(ROOT / "src-tauri" / "Cargo.lock"))
    if not lock:
        # Older locks order the keys differently; fall back to the block walk.
        block = re.search(r'name = "idlr"\n(?:.+\n)*?version = "([^"]+)"',
                          _read(ROOT / "src-tauri" / "Cargo.lock"))
        if not block:
            raise SystemExit('src-tauri/Cargo.lock: no `name = "idlr"` package block')
        versions["src-tauri/Cargo.lock"] = block.group(1)
    else:
        versions["src-tauri/Cargo.lock"] = lock.group(1)
    lock_text = _read(ROOT / "package-lock.json")
    begin, end = _npm_lock_span(lock_text)
    versions["package-lock.json"] = lock_text[begin + 1:end - 1]
    return versions


def apply(version: str) -> list[str]:
    """Write `version` into every file; returns the ones that changed."""
    changed = []

    def update(relative: str, pattern: str, repl: str) -> None:
        path = ROOT / relative
        before = _read(path)
        after = _sub_once(path, pattern, repl, before)
        if after != before:
            _write(path, after)
            changed.append(relative)

    update("package.json", r'"version":\s*"[^"]+"', f'"version": "{version}"')
    update("src-tauri/tauri.conf.json", r'"version":\s*"[^"]+"', f'"version": "{version}"')
    update("src-tauri/Cargo.toml", r'(\[package\][^\[]*?^version = )"[^"]+"',
           f'\\1"{version}"')
    update("app.py", r'^APP_VERSION = "[^"]+"', f'APP_VERSION = "{version}"')
    update("src-tauri/src/lib.rs", r'^const APP_VERSION: &str = "[^"]+";',
           f'const APP_VERSION: &str = "{version}";')

    npm_lock = ROOT / "package-lock.json"
    before = _read(npm_lock)
    begin, end = _npm_lock_span(before)
    after = before[:begin] + f'"{version}"' + before[end:]
    # the top-level "version" mirrors the root package
    after = re.sub(r'^(  "version":\s*)"[^"]+"', rf'\1"{version}"', after, count=1, flags=re.M)
    if after != before:
        _write(npm_lock, after)
        changed.append("package-lock.json")

    lock_path = ROOT / "src-tauri" / "Cargo.lock"
    before = _read(lock_path)
    after = _cargo_lock_text(version, before)
    if after != before:
        _write(lock_path, after)
        changed.append("src-tauri/Cargo.lock")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", nargs="?", help='e.g. 2.0.5 (no leading "v")')
    parser.add_argument("--check", action="store_true",
                        help="verify every file agrees and change nothing")
    args = parser.parse_args(argv)

    if args.version and args.check:
        parser.error("give either a version or --check, not both")
    if not args.version and not args.check:
        parser.error("usage: set_version.py 2.0.5   |   set_version.py --check")

    if args.version:
        if not VERSION_RE.match(args.version):
            parser.error(f"{args.version!r} is not X.Y.Z (no leading v, no suffix)")
        current = read_versions()
        if len(set(current.values())) > 1:
            for label, value in current.items():
                if value != args.version:
                    print(f"  ~ {label}: {value}")
        changed = apply(args.version)
        for relative in changed:
            print(f"updated {relative}")
        if not changed:
            print(f"already at {args.version}")
        print("\nNext: refresh the What's New list in index.html and add a "
              f"README changelog entry, then publish with\n    git tag v{args.version} && git push origin v{args.version}")
        return 0

    versions = read_versions()
    distinct = set(versions.values())
    if len(distinct) > 1:
        print("version sources disagree:", file=sys.stderr)
        for label, value in versions.items():
            print(f"  {value:<10} {label}", file=sys.stderr)
        return 1
    version = distinct.pop()
    if not VERSION_RE.match(version):
        print(f"version {version!r} is not X.Y.Z", file=sys.stderr)
        return 1
    print(f"all {len(versions)} version sources agree on {version} "
          f"— tag v{version} for a release")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
