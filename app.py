#!/usr/bin/env python3
"""
IDLR App — Internet Downloader
YouTube · Spotify · SoundCloud · Twitter · Instagram · TikTok and more
Fast · Private · Unlimited
"""
import argparse, base64, hashlib, json, os, re, shutil, subprocess, sys, tempfile, urllib.request, zipfile
import io
import importlib.util
from pathlib import Path

APP_VERSION = "2.0.12"
APP_DIR = os.path.join(os.path.expanduser("~"), ".idlr_app")
os.makedirs(APP_DIR, exist_ok=True)
_YTDLP_UPDATE_STATE = os.path.join(APP_DIR, "ytdlp-update.json")
_YTDLP_UPDATE_PACKAGES = os.path.join(APP_DIR, "packages")
_ACTIVE_UPDATED_YTDLP = ""
_PENDING_UPDATED_YTDLP = ""


def _activate_updated_ytdlp():
    global _ACTIVE_UPDATED_YTDLP
    try:
        with open(_YTDLP_UPDATE_STATE, encoding="utf-8") as stream:
            state = json.load(stream)
        version = str(state.get("version", ""))
        if not re.fullmatch(r"[0-9A-Za-z.+_-]{1,80}", version):
            raise ValueError("Invalid yt-dlp update version in local update state.")
        package_dir = os.path.join(_YTDLP_UPDATE_PACKAGES, "yt-dlp-" + version)
        if os.path.isfile(os.path.join(package_dir, "yt_dlp", "__init__.py")):
            sys.path.insert(0, package_dir)
            _ACTIVE_UPDATED_YTDLP = version
    except FileNotFoundError:
        return
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log("UPDATE", f"Ignoring invalid local yt-dlp update: {exc}")

LOG_FILE = os.path.join(APP_DIR, "idlr.log")
_LOG_LIMIT = 1024 * 1024  # keep idlr.log under ~1 MB so it never fills the disk


def log(tag, msg):
    """Pretty timestamped log, e.g. [12:01:44] [IDLR:DL] message.

    Always appended to ~/.idlr_app/idlr.log *and* echoed to the console. The
    bundled desktop worker has no console at all, so the file is the only place
    a user (or a bug report) can find out why something failed.
    """
    import datetime as _dt
    ts = _dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{tag}] {msg}"
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > _LOG_LIMIT:
            with open(LOG_FILE, "rb") as stream:
                stream.seek(-_LOG_LIMIT // 2, os.SEEK_END)
                tail = stream.read()
            with open(LOG_FILE, "wb") as stream:
                stream.write(b"[...] " + tail.split(b"\n", 1)[-1])
        with open(LOG_FILE, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is None:
        return
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        try:
            print(line.encode("ascii", "replace").decode(), flush=True)
        except Exception:
            pass


_activate_updated_ytdlp()

# spotdl is OPTIONAL: the app must start even if the Spotify engine is broken
# (e.g. a pydantic/pydantic-core version clash after an upgrade). In that case
# YouTube etc. keep working and Spotify downloads show the exact repair command.
OPTIONAL_PKGS = {"spotdl", "static-ffmpeg"}
BROKEN_PKGS = set()
# Human-readable startup issues, surfaced through /api/info so the UI can
# explain itself instead of dying silently. Each entry is a small dict because
# "spotdl is missing" and "flask is missing" are not the same news: a *notice*
# is something IDLR worked around (Spotify links just match on YouTube) and the
# app still downloads, a *blocking* problem stops downloads. Rendering both as
# the same orange "IDLR needs one more thing" alarm — which is what used to
# happen — taught people to ignore the one banner that really means trouble.
SETUP_PROBLEMS = []


def report_issue(package, title, detail, severity, reason=""):
    """Record one startup issue for /api/info, at most once per package+title.

    `reason` carries the exception text that used to disappear into the log
    file; it is what makes "spotdl is unavailable" actionable instead of a
    shrug, and the banner's Copy button hands it over verbatim.
    """
    if any(isinstance(item, dict) and item.get("package") == package
           and item.get("title") == title for item in SETUP_PROBLEMS):
        return
    SETUP_PROBLEMS.append({"package": package, "title": title, "detail": detail,
                           "severity": severity, "reason": str(reason or "")[:300]})
    log("SETUP", f"  [{'WARN' if severity == 'notice' else '!!'}] {package}: {title}"
                 + (f" ({str(reason)[:110]})" if reason else ""))


IS_FROZEN = bool(getattr(sys, "frozen", False))


def clear_issue(package, title=None):
    """Forget recorded problems for a package, because reality changed its mind.

    `report_issue` is built to survive a noisy startup — it dedupes and the list
    only grows — which is exactly wrong for a notice raised by *one* failed
    download: after twenty good Spotify links the banner would still claim the
    downloader is unavailable, and a warning that outlives its evidence is the
    thing users learn to ignore. Kept as a slice assignment so a UI polling
    `/api/info` from another thread never sees a half-written list.
    """
    kept = [item for item in SETUP_PROBLEMS
            if not (isinstance(item, dict)
                    and item.get("package") == package
                    and (title is None or item.get("title") == title))]
    if len(kept) != len(SETUP_PROBLEMS):
        SETUP_PROBLEMS[:] = kept
        log("SETUP", f"  [OK] {package}: cleared the earlier notice")


def _find_bundled_binary(name):
    """Locate a helper executable IDLR ships next to its own binary.

    The bundled worker may end up in `.../binaries/` (one-file layout) or
    `.../binaries/idlr-worker/` (folder layout, which antivirus heuristics
    complain about far less). Search both instead of assuming the file sits
    right next to us — a missing helper must never look like a crash.
    """
    try:
        base = Path(os.path.dirname(sys.executable)).resolve()
    except Exception:
        return None
    candidates = []
    for folder in [base, *list(base.parents)[:3]]:
        candidates.append(folder / name)
        candidates.append(folder / "binaries" / name)
        candidates.append(folder / Path(name).stem / name)
    seen = set()
    for candidate in candidates:
        key = str(candidate).lower() if os.name == "nt" else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return str(candidate)
    return None


# The helper that owns spotdl in a packaged build. Presence is the only thing this
# process may ask it at startup — see _looks_like_engine_failure() for when the app
# learns more.
SPOTDL_HELPER = "IDLR-spotdl.exe" if os.name == "nt" else "IDLR-spotdl"


# What a spotdl run has to print for it to be about *spotdl* rather than about
# the track that was requested.
_ENGINE_MARKERS = ("Traceback (most recent call last)", "ImportError",
                   "ModuleNotFoundError", "No module named", "pydantic",
                   "Fatal Python error", "EntryPointNotFound", "frozen importlib")


def _looks_like_engine_failure(output):
    """True when spotdl's own output says the engine cannot run.

    `_spotify_run` falls back to a YouTube match for every failure, so without this
    a private or DRM'd track would raise a banner about IDLR's downloader. The
    marker list is deliberately about import/traceback noise: that is what a broken
    or quarantined helper prints, and it is what no per-track error looks like.
    """
    text = output or ""
    return any(marker in text for marker in _ENGINE_MARKERS)


def _dependency_present(pkg, module):
    """Cheap, import-time answer to "can this optional feature possibly work?".

    Returns (ok, reason). Never imports anything and never runs a subprocess:
    this sits on the startup path, on a cold Windows start, inside files
    antivirus is scanning.
    """
    if IS_FROZEN and pkg == "spotdl":
        found = _find_bundled_binary(SPOTDL_HELPER) is not None
        return found, "" if found else "IDLR-spotdl is not next to the app"
    try:
        if importlib.util.find_spec(module) is None:
            return False, f"{pkg} is not installed"
        return True, ""
    except (ImportError, ValueError) as exc:
        # A half-removed distribution can raise here rather than return None.
        return False, f"{type(exc).__name__}: {exc}"


def _pip_install(pkg, timeout=180):
    """Best-effort dependency repair for source installs only.

    Never used in the bundled app (pip inside a frozen build writes next to
    the exe and is exactly what antivirus heuristics love to quarantine), and
    never fatal: a blocked network or a PEP-668 managed environment must not
    stop IDLR from starting.
    """
    log("SETUP", f"Installing {pkg} ... (first run only)")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", pkg, "-q"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=timeout)
    log("SETUP", f"Installed {pkg} OK")


log("SETUP", "Checking dependencies (flask, yt-dlp, static-ffmpeg, spotdl) ...")
OPTIONAL_IMPORTS = {"static-ffmpeg": "static_ffmpeg", "spotdl": "spotdl"}
for p, i in [("flask", "flask"), ("yt-dlp", "yt_dlp"),
             ("static-ffmpeg", "static_ffmpeg"), ("spotdl", "spotdl")]:
    try:
        if p in OPTIONAL_PKGS:
            # Presence check only — see _finish_startup_checks(). Importing
            # spotdl costs ~1.3 s (fastapi, mutagen, aiohttp, ytmusicapi behind
            # it) on every single launch, and it is only ever needed for a
            # Spotify link, which runs in its own child process anyway. On a
            # cold Windows start with antivirus scanning the extracted files
            # that import was a visible chunk of "IDLR takes forever to open".
            # In the packaged build the answer also has to come from the helper
            # that owns the dependency, not from this process — see
            # _dependency_present().
            ok, reason = _dependency_present(p, i)
            if not ok:
                raise ImportError(reason or f"{p} is not installed")
        else:
            __import__(i)
        log("SETUP", f"  [OK] {p} already installed")
    except Exception as e:
        # NOTE: except Exception (not just ImportError) — a broken install can
        # raise SystemError and friends at import time; that must never kill
        # startup on its own.
        log("SETUP", f"  [!!] {p} import failed ({type(e).__name__}: {str(e)[:110]})")
        if not IS_FROZEN and os.environ.get("IDLR_NO_AUTO_INSTALL") != "1":
            try:
                _pip_install(p)
                __import__(i)
                log("SETUP", f"  [OK] {p} repaired")
                continue
            except Exception as e2:
                log("SETUP", f"  [!!] automatic repair of {p} failed: {str(e2)[:110]}")
        if p in OPTIONAL_PKGS:
            BROKEN_PKGS.add(p)
            if p == "spotdl":
                report_issue("spotdl", "Spotify's own downloader is unavailable",
                             "Spotify links still download — IDLR matches the track on "
                             "YouTube instead, so quality and metadata may differ.",
                             "notice", f"{type(e).__name__}: {e}")
            else:
                report_issue("static-ffmpeg", "FFmpeg helper is unavailable",
                             "IDLR is using the FFmpeg already on your PATH. MP3 "
                             "conversion and merging need FFmpeg to work.",
                             "notice", f"{type(e).__name__}: {e}")
            continue
        # A required package is missing. Raising (rather than sys.exit) keeps the
        # traceback in the log and lets the desktop worker answer with a real
        # error message instead of vanishing mid-handshake.
        report_issue(p, f"{p} is required but broken",
                     "IDLR cannot start until this is repaired — reinstall the app, "
                     "and check Windows Defender's protection history for a "
                     "quarantined file.",
                     "blocking", f"{type(e).__name__}: {e}")
        raise RuntimeError(
            f"IDLR cannot start: the required component '{p}' is missing or broken "
            f"({type(e).__name__}: {str(e)[:160]}). "
            "If you installed IDLR manually, reinstall it; Windows Defender sometimes "
            "quarantines IDLR's bundled files — restore them and add an exclusion."
        )

log("SETUP", "Loading application modules ...")
from flask import Flask, request, jsonify, Response, send_from_directory
import yt_dlp, shutil, json, hashlib, datetime
import threading, uuid, re, time, socket, webbrowser

# `import static_ffmpeg` costs ~120 ms (its package __init__ wires up a download
# helper and a file lock) and the only thing that needs it is the FFmpeg check
# below — which is why that check now runs in the background too. Bound lazily by
# _finish_startup_checks(); every use site treats None as "not available yet".
static_ffmpeg = None

log("SETUP", "Preparing FFmpeg in the background ...")

_FFMPEG_READY = False
# Set once the background startup checks have resolved FFmpeg and the optional
# packages. A job that starts a fraction of a second after launch waits on this
# instead of concluding that FFmpeg is missing.
_STARTUP_CHECKS = threading.Event()


def _shipped_ffmpeg_dir(exe_path=None):
    """The FFmpeg folder the installer ships *next to* this helper, if present.

    build.py stages one copy of FFmpeg into `dist/ffmpeg/` and tauri.conf.json
    maps it to `binaries/ffmpeg/`, so an installed IDLR has ffmpeg/ffprobe as a
    sibling of idlr-worker.exe — written once by the installer, never re-extracted
    on launch, and not something the helper has to download or write anywhere.
    Several layouts are searched for the same reason `_find_bundled_binary` is:
    a resource glob may keep the folder or flatten it into `binaries/`, and the
    worker may be run from inside or next to that folder.
    """
    if not IS_FROZEN:
        return ""
    suffix = ".exe" if os.name == "nt" else ""
    names = ("ffmpeg" + suffix, "ffprobe" + suffix)
    try:
        anchor = Path(exe_path or sys.executable).resolve().parent
    except Exception:
        return ""
    folders = []
    for base in (anchor, *list(anchor.parents)[:2]):
        folders += [base / "ffmpeg", base]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        folders.append(Path(meipass) / "ffmpeg")
    for folder in folders:
        try:
            if all((folder / name).is_file() for name in names):
                return str(folder)
        except OSError:  # unreadable path (permission, offline drive): keep looking
            continue
    return ""


def _bundled_ffmpeg_dir():
    """The folder static-ffmpeg keeps its binaries in, if they are already there.

    Reading the directory directly (instead of calling `add_paths`) matters:
    `add_paths` takes a lock file inside its own package folder, which fails
    with PermissionError in a read-only install like `C:\\Program Files\\IDLR`,
    and used to be reported as 'FFmpeg missing' — or, before that, as a crash.
    """
    shipped = _shipped_ffmpeg_dir()
    if shipped:
        return shipped
    if static_ffmpeg is None:
        return ""
    try:
        from static_ffmpeg.run import get_platform_dir  # type: ignore

        folder = get_platform_dir()
    except Exception:
        return ""
    suffix = ".exe" if sys.platform == "win32" else ""
    if all(os.path.isfile(os.path.join(folder, tool + suffix))
           for tool in ("ffmpeg", "ffprobe")):
        return folder
    return ""


def _have_ffmpeg_on_path():
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _setup_ffmpeg():
    """Make ffmpeg/ffprobe reachable — and NEVER let this kill startup.

    static-ffmpeg downloads a ~30 MB FFmpeg build the first time it runs. That
    network call (a proxy, a certificate-rewriting antivirus, a read-only
    install folder, or a stale lock file left by a previous crash) used to raise
    straight out of `import app`, which took down the whole app and the bundled
    worker with it: IDLR simply never opened. Prefer a system FFmpeg, treat the
    bundled one as an optional extra, and never block startup on a download.
    """
    global _FFMPEG_READY
    if _have_ffmpeg_on_path():
        _FFMPEG_READY = True
        log("SETUP", "  [OK] FFmpeg is available on PATH")
        return
    bundled = _bundled_ffmpeg_dir()
    if bundled:
        os.environ["PATH"] = os.pathsep.join(
            [bundled] + [p for p in os.environ.get("PATH", "").split(os.pathsep)
                         if p and p != bundled]
        )
        _FFMPEG_READY = _have_ffmpeg_on_path()
        log("SETUP", "  [OK] Using the FFmpeg bundled with IDLR" if _FFMPEG_READY
            else "  [WARN] Bundled FFmpeg is incomplete — MP3/merging may fail")
        return
    if static_ffmpeg is None:
        report_issue("ffmpeg", "FFmpeg was not found",
                     "Install FFmpeg — it is needed for MP3 conversion and for merging "
                     "video with audio. Audio-only downloads from a single file still work.",
                     "blocking")
        log("SETUP", "  [WARN] No FFmpeg helper and none on PATH — MP3/merging may fail")
        return
    if IS_FROZEN:
        # An installed app must not create files inside its own (possibly
        # read-only, Program Files) folder — that is also exactly the kind of
        # behaviour antivirus loves to block. The build ships FFmpeg instead.
        report_issue("ffmpeg", "This IDLR build has no FFmpeg and none is on your PATH",
                     "Install FFmpeg, or update IDLR to a build that bundles it — needed "
                     "for MP3 conversion and for merging video with audio.",
                     "blocking")
        log("SETUP", "  [WARN] No FFmpeg in the bundle and none on PATH — not downloading into the install folder")
        return

    # Nothing on disk: fetch it in the background so opening IDLR never waits
    # on a 30 MB download. refresh_ffmpeg_paths() picks it up from the next
    # download onwards.
    if os.environ.get("IDLR_NO_AUTO_INSTALL") == "1":
        # The same switch the dependency probe above honours: "do not fetch
        # anything at startup". Tests and CI run with it, because a 30 MB
        # download started by a background thread while assertions are running
        # is a source of flakes, not of coverage. FFmpeg simply stays unset,
        # which is a supported state (the app says so and keeps working).
        log("SETUP", "  [..] FFmpeg not found — skipping the automatic download (IDLR_NO_AUTO_INSTALL=1)")
        return

    def fetch():
        global _FFMPEG_READY
        try:
            static_ffmpeg.add_paths(weak=True)
            if _have_ffmpeg_on_path():
                _FFMPEG_READY = True
                refresh_ffmpeg_paths()
                log("SETUP", "  [OK] FFmpeg ready (downloaded in the background)")
        except Exception as e:
            log("SETUP", f"  [WARN] Background FFmpeg setup failed ({type(e).__name__}: {str(e)[:110]})")
            report_issue("ffmpeg", "IDLR could not prepare FFmpeg",
                         "Install FFmpeg yourself (offline, proxy, or antivirus blocked "
                         "the automatic copy). MP3 conversion and merging need it.",
                         "blocking", f"{type(e).__name__}: {e}")

    threading.Thread(target=fetch, name="idlr-ffmpeg-setup", daemon=True).start()
    log("SETUP", "  [..] FFmpeg not found — preparing it in the background")


FFMPEG = shutil.which("ffmpeg") or ""


def refresh_ffmpeg_paths(wait=0.0):
    """Re-detect ffmpeg/ffprobe before a job.

    If the background FFmpeg setup from earlier in this run has finished, the
    next download picks it up without needing an app restart.

    `wait` is for the job that starts within moments of launch: FFmpeg is now
    prepared in a background thread, so a download kicked off immediately would
    otherwise conclude "no FFmpeg" and produce an unconverted file. It waits at
    most that long, and only while the checks are still running — once they are
    done the answer is known, so a machine without FFmpeg never waits twice.
    """
    global FFMPEG, _FFMPEG_READY
    if wait and not _FFMPEG_READY and not _STARTUP_CHECKS.is_set():
        _STARTUP_CHECKS.wait(wait)
    found = shutil.which("ffmpeg") or ""
    if found:
        _FFMPEG_READY = True
        if found != FFMPEG:
            FFMPEG = found
            log("SETUP", f"FFmpeg ready: {found}")
    return FFMPEG


def _finish_startup_checks():
    """Import the optional packages and prepare FFmpeg — *after* the app is up.

    Everything here is a cost that used to be paid serially inside `import app`,
    which is exactly what the packaged worker's startup is: ~120 ms for
    static-ffmpeg, ~1.3 s for spotdl, plus a certificate-sensitive download on a
    cold machine. The startup probe now only asks whether each package is
    installed (`find_spec`), and this thread pays the real cost a moment later so
    `BROKEN_PKGS`, the Spotify engine badge, the FFmpeg badge and the health
    banner become accurate instead of merely optimistic.

    A package that is installed but broken (the pydantic-core clash from the
    changelog) is still reported rather than silently mislabelled. Nothing here
    is on the critical path of a download: `_ytdlp_run` calls
    refresh_ffmpeg_paths() per job, so a download started before this finishes
    simply takes the existing fallback path.
    """
    global static_ffmpeg
    try:
        for pkg, module in OPTIONAL_IMPORTS.items():
            if IS_FROZEN and pkg == "spotdl":
                # Nothing to verify in this process: spotdl lives in the IDLR-spotdl
                # helper, whose presence the startup probe already checked. Going
                # deeper here (run the helper, or import the package) is how an
                # earlier revision of this check cost every launch a onefile
                # unpack of ~150 MB into %TEMP% — spent painting a badge. A helper
                # that is present but broken now reports itself from _spotify_run,
                # where there is real evidence and the user is already waiting.
                continue
            try:
                imported = __import__(module)
                if module == "static_ffmpeg":
                    static_ffmpeg = imported
            except Exception as e:
                if pkg in BROKEN_PKGS:
                    continue
                BROKEN_PKGS.add(pkg)
                if pkg == "spotdl":
                    report_issue("spotdl", "Spotify's own downloader is unavailable",
                                 "Spotify links still download — IDLR matches the track "
                                 "on YouTube instead, so quality and metadata may differ.",
                                 "notice", f"{type(e).__name__}: {e}")
                else:
                    report_issue("static-ffmpeg", "FFmpeg helper is unavailable",
                                 "IDLR is using the FFmpeg already on your PATH.",
                                 "notice", f"{type(e).__name__}: {e}")
            if pkg == "static-ffmpeg":
                # Resolve FFmpeg immediately — before spotdl, the slow import of
                # the two — because a job that started during startup is waiting
                # on the PATH this sets up. OPTIONAL_IMPORTS is ordered so the
                # FFmpeg helper is always the first entry.
                _setup_ffmpeg()
        log("SETUP", "Optional components verified.")
    except Exception as e:  # a background check must never be an app crash
        log("SETUP", f"  [WARN] Startup checks did not finish ({type(e).__name__}: {str(e)[:110]})")
    finally:
        _STARTUP_CHECKS.set()


threading.Thread(target=_finish_startup_checks, name="idlr-startup-checks",
                 daemon=True).start()

log("SETUP", "Application modules ready.")

# ── Storage ───────────────────────────────────────────────────────────────────
ACCS_FILE = os.path.join(APP_DIR, "accounts.json")
HIST_FILE = os.path.join(APP_DIR, "history.json")
os.makedirs(APP_DIR, exist_ok=True)
SERVER_URL = "http://localhost:5000"
# History/accounts are written from download threads AND request handlers.
_DATA_LOCK = threading.RLock()
# Upper bound for history.json entries; newest rows win, starred/trashed are kept.
_HISTORY_CAP = 1500

def jload(p, d):
    """Load JSON, tolerating a file that was truncated by a crash/power loss.

    A half-written history.json used to mean `except: return d` — every entry
    silently vanished and the user just saw an empty app. We keep the damaged
    copy next to the file and hand back the default so the app still opens.
    """
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return d
    except Exception as e:
        return _salvage_json(p, e, d)


def _salvage_json(p, error, default):
    """Keep a damaged file instead of pretending it was empty."""
    try:
        if os.path.isfile(p):
            salvage = p + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(p, salvage)
            log("DATA", f"{os.path.basename(p)} was unreadable ({error}); kept a copy at {salvage}")
    except OSError:
        pass
    return default
def _replace_with_retry(src, dst, attempts=6):
    """os.replace that survives Windows file locking.

    Defender holds newly written files for a fraction of a second to scan them;
    a replace that lands in that window raises PermissionError and the save is
    simply lost. A few short retries fix that, and the final failure is logged
    instead of swallowed.
    """
    last = None
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            last = e
            time.sleep(0.05 * (attempt + 1))
    raise last


def jsave(p, d):
    """Atomic save: write a temp file in the same folder, then os.replace it.

    Download threads and request handlers write history at the same time, so
    this is also serialised by a lock — a torn write here used to wipe the
    user's whole history.
    """
    directory = os.path.dirname(p) or "."
    with _DATA_LOCK:
        tmp = None
        try:
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                            prefix="." + os.path.basename(p) + ".",
                                            suffix=".tmp", delete=False) as f:
                tmp = f.name
                json.dump(d, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp, p)
        except OSError as e:
            log("DATA", f"Could not save {os.path.basename(p)}: {e}")
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def hashpw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def local_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        s.connect(("8.8.8.8",80)); ip=s.getsockname()[0]; s.close(); return ip
    except: return "localhost"

def is_spotify(url):
    return "spotify.com" in url or url.startswith("spotify:")

_DIRECT_HLS_EXTS = {".m3u8", ".m3u", ".ts", ".m2ts", ".mts"}
_DIRECT_VIDEO_EXTS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".m4v", ".f4v",
    ".3gp", ".m2v", ".wmv", ".mpeg", ".mpg", ".ogv",
}


def _direct_media_extension(url):
    """Return the media extension before URL query/fragment data, if any.

    Signed HLS URLs commonly look like ``master.m3u8?token=...`` and browser
    links can include ``#t=`` fragments. Comparing the entire URL meant those
    streams missed the direct-media path and went through a video-page format
    selector instead.
    """
    path = str(url or "").strip().split("#", 1)[0].split("?", 1)[0]
    return os.path.splitext(path)[1].lower()


def _direct_media_kind(url):
    extension = _direct_media_extension(url)
    if extension in _DIRECT_HLS_EXTS:
        return "hls"
    if extension in _DIRECT_VIDEO_EXTS:
        return "video"
    return ""


def is_hls_or_direct(url):
    """Whether a URL points directly at an HLS playlist or a video file."""
    return bool(_direct_media_kind(url))


def _direct_hls_format(quality):
    """Select a requested HLS height, falling back safely for single variants."""
    heights = {
        "8K": 4320, "4K": 2160, "1440p": 1440, "1080p": 1080,
        "720p": 720, "480p": 480, "360p": 360, "240p": 240, "144p": 144,
    }
    height = heights.get(str(quality))
    if not height:
        return "bestvideo+bestaudio/best"
    return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"

def default_download_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads")

def expand_save_path(p):
    """Resolve %VAR% / ~ / relative bits. The frontend used to send a literal
    '%USERPROFILE%\\Downloads' which Windows never expands by itself."""
    p = (p or "").strip().strip('"').strip("'")
    if not p:
        return default_download_dir()
    return os.path.normpath(os.path.expandvars(os.path.expanduser(p)))

def _folder_error_reason(exc):
    """One clause that says why a folder could not be used, in the app's words.

    `[Errno 13] Permission denied: 'D:\\Music'` is the accurate sentence and the
    one nobody reads, and it is what a toast looked like when the save folder was
    refused — an error inside an error, at the moment the user is trying to
    understand a button. The plain clause comes first; the exception stays in
    parentheses because it is what a bug report needs.
    """
    name = type(exc).__name__
    if isinstance(exc, PermissionError):
        why = "this PC will not let IDLR write there"
    elif isinstance(exc, FileNotFoundError):
        why = "that path does not exist any more"
    elif isinstance(exc, NotADirectoryError):
        why = "that is a file, not a folder"
    elif isinstance(exc, FileExistsError):
        # The path was not a file a moment ago and is now; `makedirs(exist_ok=True)`
        # says exactly this when the name exists but is not a directory.
        why = "something else is already there under that name"
    elif getattr(exc, "errno", None) in (28, 122):   # ENOSPC / EDQUOT
        why = "that drive is full"
    else:
        why = "this PC refused it"
    return f"{why} ({name}: {exc})"


def ensure_writable_dir(path):
    """Create the folder and PROVE we can write in it (probe file).
    Returns (ok, real_path, error)."""
    if os.path.exists(path) and not os.path.isdir(path):
        # Asked first, because `makedirs(exist_ok=True)` answers this case with
        # FileExistsError — technically true, and it told the user "this PC
        # refused it" when the real news was that they had typed a filename.
        return False, path, (f"'{path}' is a file, not a folder. Choose the folder "
                             f"you want downloads saved inside.")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        return False, path, (f"Cannot create save folder '{path}': "
                             f"{_folder_error_reason(e)}. Choose a folder you can write "
                             f"in — your Documents, or a folder on another drive.")
    probe_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix=".idlr_write_test_", dir=path, delete=False) as probe:
            probe_path = probe.name
            probe.write(b"ok")
        os.remove(probe_path)
        return True, path, ""
    except Exception as e:
        if probe_path and os.path.exists(probe_path):
            try:
                os.remove(probe_path)
            except OSError as cleanup_error:
                return False, path, (f"IDLR could not clean up after testing '{path}': "
                                     f"{cleanup_error}")
        return False, path, (f"'{path}' will not accept a new file: "
                             f"{_folder_error_reason(e)}. Choose a different folder.")

def pick_temp_dir(savepath=""):
    """A temp dir we KNOW is writable. Never blindly trust ambient TEMP —
    on misconfigured machines it points at C:\\Windows\\system32, and every
    yt-dlp temp write then dies with Errno 13 (your screenshot)."""
    cands = [os.path.join(APP_DIR, "tmp")]
    if savepath:
        try:
            if os.path.isdir(savepath):
                cands.append(os.path.join(savepath, ".idlr_tmp"))
        except Exception:
            pass
    import tempfile
    try:
        cands.append(tempfile.gettempdir())
    except Exception:
        pass
    for c in cands:
        try:
            ok, real, _ = ensure_writable_dir(c)
            if ok:
                return real
        except Exception:
            continue
    return APP_DIR

def get_movie_opts(job, url, quality, fmt, savepath):
    """yt-dlp options tuned for movie/HLS sites"""
    q_map = {
        "Best (Max Quality)": "bestvideo+bestaudio/best",
        "4K":   "bestvideo[height<=2160]+bestaudio/best",
        "1440p":"bestvideo[height<=1440]+bestaudio/best",
        "1080p":"bestvideo[height<=1080]+bestaudio/best",
        "720p": "bestvideo[height<=720]+bestaudio/best",
        "480p": "bestvideo[height<=480]+bestaudio/best",
        "360p": "bestvideo[height<=360]+bestaudio/best",
    }
    opts = {
        "outtmpl": os.path.join(savepath, "%(title)s.%(ext)s"),
        "quiet": True, "no_warnings": True,
        "progress_hooks": [lambda d: _hook(job, d)],
        # Temp files go to a VERIFIED-writable dir, never ambient TEMP
        # (which can point at C:\Windows\system32 -> Errno 13).
        "paths": {"home": savepath, "temp": pick_temp_dir(savepath)},
        # HLS specific
        "hls_prefer_native": False,       # use ffmpeg for HLS (more reliable)
        "hls_use_mpegts": True,
        "allow_unplayable_formats": False,
        # Do not claim a movie completed after silently dropping a missing
        # segment. Retry it (fragment_retries below), then report the failure.
        "skip_unavailable_fragments": False,
        # Generic extractor — try hard to find video on any page
        "force_generic_extractor": False,
        "geo_bypass": True,
        # Better compat
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
        # Each fragment is ~10 s of media and costs a full round-trip, so on a
        # high-latency or Wi-Fi connection the speed is limited by how many
        # requests are in flight, not by bandwidth. Eight is the range yt-dlp
        # documents as useful for HLS/DASH without getting us throttled.
        "concurrent_fragment_downloads": 8,
        # Headers to look like a browser
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": url,
        },
    }
    if FFMPEG:
        opts["ffmpeg_location"] = os.path.dirname(FFMPEG)
    out_fmt = fmt if fmt in ("mp4","mkv","webm") else "mp4"
    opts["format"] = q_map.get(quality, "bestvideo+bestaudio/best")
    opts["merge_output_format"] = out_fmt
    return opts


def _configure_direct_media_opts(opts, media_kind, extension, is_audio, fmt, quality):
    """Use stream-safe options for direct HLS playlists and video-file URLs.

    A previous unconditional ``merge_output_format = mp4`` changed audio-only
    direct links back into video downloads, and an optional thumbnail postprocessor
    could turn an otherwise valid HLS stream into a failed job. Direct sources have
    no dependable cover metadata, so this path focuses on the media itself and
    only remuxes when the requested container differs from the source.
    """
    if not media_kind:
        return False

    opts["writethumbnail"] = False
    if is_audio:
        # HLS may be a muxed stream rather than an audio-only rendition. Let
        # FFmpeg extract audio from the selected media instead of asking yt-dlp's
        # video-page selector to find a separate bestaudio track.
        opts["format"] = "best"
        opts.pop("merge_output_format", None)
        opts["postprocessors"] = [
            processor for processor in opts.get("postprocessors", [])
            if isinstance(processor, dict) and processor.get("key") == "FFmpegExtractAudio"
        ]
        return True

    target = fmt if fmt in ("mp4", "mkv", "webm") else "mp4"
    opts["format"] = _direct_hls_format(quality) if media_kind == "hls" else "best"
    opts["merge_output_format"] = target
    opts["postprocessors"] = []
    if extension != "." + target:
        opts["postprocessors"].append({
            "key": "FFmpegVideoRemuxer",
            "preferedformat": target,
        })
    return True

SCRIPT_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, static_folder=SCRIPT_DIR, template_folder=SCRIPT_DIR)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
_jobs = {}
_JOBS_KEEP = 40          # finished jobs kept for the UI before being dropped
_JOBS_TTL = 30 * 60      # seconds a finished job may stay in memory
_JOBS_GRACE = 90         # never drop a job that just finished: the UI may still poll it
_JOBS_HARD_KEEP = 200    # absolute ceiling, grace window notwithstanding
_JOB_LOG_KEEP = 400      # console lines kept per job (a long playlist logs per second)


def _log_to(job, entry):
    """Append one console line to a job, with the list kept bounded.

    `log_total` counts every line ever appended, so the UI's `since` cursor and
    the delta endpoints stay correct after old lines are dropped from the front.
    """
    lines = job.setdefault("log", [])
    lines.append(entry)
    # A job may already carry seed lines (with no counter yet), so fall back to
    # the length *before* this append.
    total = job.get("log_total", len(lines) - 1) + 1
    job["log_total"] = total
    overflow = len(lines) - _JOB_LOG_KEEP
    if overflow > 0:
        del lines[:overflow]
    return total


def _log_window(job, since):
    """(lines, total) for a `since` cursor, given old lines may be gone."""
    lines = job.get("log", [])
    total = job.get("log_total", len(lines))
    start = max(0, min(len(lines), len(lines) - (total - max(0, int(since or 0)))))
    return lines[start:], total


def _prune_jobs():
    """Keep the in-memory job table bounded.

    The desktop worker is a long-lived process: every download used to add a
    job with an unbounded log list and nothing ever removed it, so a day of
    heavy use slowly grew memory until the worker was killed (which the shell
    reports as an unexplained crash).
    """
    try:
        if len(_jobs) <= _JOBS_KEEP:
            return
        now = time.time()

        def age(job):
            """Seconds since the job finished.

            `_ytdlp_run` flips `done` a moment before the worker thread stamps
            `ended_at`, so a job mid-close-out still reads ended_at=0. Dating it
            from 0 makes it look months old and it gets dropped while the UI is
            still polling it (a download "vanishes" from the progress panel) —
            hence the fallback to created_at.
            """
            return now - (job.get("ended_at") or job.get("created_at") or now)

        finished = sorted((jid for jid, job in _jobs.items() if job.get("done")),
                          key=lambda jid: age(_jobs[jid]))
        victims = [jid for jid in finished if age(_jobs[jid]) > _JOBS_TTL]

        def _drop_to(candidates, floor):
            for jid in candidates:
                if len(_jobs) - len(victims) <= floor:
                    return
                if jid not in victims:
                    victims.append(jid)

        # Over the cap, drop the oldest finished jobs — but only ones the UI has
        # had time to stop reading.
        _drop_to([jid for jid in finished if age(_jobs[jid]) > _JOBS_GRACE], _JOBS_KEEP)
        # Hard ceiling, so a huge burst (a whole playlist) cannot park thousands
        # of job dicts in memory just because they are all still inside the grace.
        _drop_to(finished, _JOBS_HARD_KEEP)
        for jid in victims:
            _jobs.pop(jid, None)
    except Exception as e:  # pruning must never break a download
        log("DL", f"job prune skipped: {e}")

_WIN_RESERVED = {"CON","PRN","AUX","NUL","COM1","COM2","COM3","COM4","COM5",
                 "COM6","COM7","COM8","COM9","LPT1","LPT2","LPT3","LPT4",
                 "LPT5","LPT6","LPT7","LPT8","LPT9"}

class _SafeDL(yt_dlp.YoutubeDL):
    """YoutubeDL that always produces Windows-openable filenames.

    yt-dlp leaves titles like 'Thank you and Goodbye.' untouched, giving
    'Thank you and Goodbye..mp4' — which Windows (and ffmpeg) refuse to open:
    'Postprocessing: Error opening output files: Invalid argument'.
    This affects EVERY site, not just YouTube. We strip trailing dots/spaces,
    illegal chars, reserved names and over-long paths AFTER yt-dlp prepares
    the name, so merge + thumbnails + final files all open fine.
    """
    def prepare_filename(self, info_dict, dir_type="", *, outtmpl=None, warn=False):
        fn = super().prepare_filename(info_dict, dir_type, outtmpl=outtmpl, warn=warn)
        d, b = os.path.split(fn)
        name, ext = os.path.splitext(b)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(". ")
        if not name:
            name = str(info_dict.get("id") or "video")
        if name.upper() in _WIN_RESERVED:
            name = "_" + name
        fn = os.path.join(d, name + ext) if d else (name + ext)
        if len(fn) > 240:  # stay under Windows MAX_PATH with room for dirs
            over = len(fn) - 240
            name = name[:max(1, len(name) - over)]
            fn = os.path.join(d, name + ext) if d else (name + ext)
        return fn

# ── Static ────────────────────────────────────────────────────────────────────
@app.route("/")
def index(): return send_from_directory(SCRIPT_DIR, "index.html")

@app.route("/favicon.svg")
def favicon(): return send_from_directory(SCRIPT_DIR, "favicon.svg")

# ── Auth ──────────────────────────────────────────────────────────────────────
def _user_resp(email, u):
    return jsonify(ok=True, name=u["name"], email=email,
                   username=u.get("username",""),
                   bio=u.get("bio",""),
                   avatar=u.get("avatar",""),
                   banner=u.get("banner",""),
                   method=u.get("method","email"),
                   prefs=u.get("prefs",{}),
                   joined=u.get("joined",""))


def _validated_profile_image(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Choose a valid PNG, JPEG, or WebP image.")
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]+={0,2})", value)
    if not match:
        raise ValueError("Images must be PNG, JPEG, or WebP.")
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("That image could not be read. Choose it again.") from exc
    if not data or len(data) > 250 * 1024:
        raise ValueError("Images must be smaller than 250 KB.")
    mime = match.group(1)
    valid_signature = (
        (mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (mime == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
        or (mime == "image/webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )
    if not valid_signature:
        raise ValueError("The selected file does not match its image format.")
    return value


@app.route("/api/register", methods=["POST"])
def register():
    d=request.json or {}
    accs=jload(ACCS_FILE,{})
    email=d.get("email","").strip().lower()
    pw=d.get("password",""); name=d.get("name","").strip()
    if not email or "@" not in email: return jsonify(error="Invalid email"),400
    if not pw or len(pw)<6: return jsonify(error="Password must be 6+ characters"),400
    if not name: return jsonify(error="Name required"),400
    if email in accs: return jsonify(error="Account already exists"),409
    with _DATA_LOCK:
        accs[email]={"name":name,"pw_hash":hashpw(pw),"joined":str(datetime.date.today()),
                     "method":"email","prefs":{},"username":"","bio":"","avatar":"","banner":"",
                     "image_history":[]}
        jsave(ACCS_FILE,accs)
    return _user_resp(email, accs[email])

@app.route("/api/login", methods=["POST"])
def login():
    d=request.json or {}
    accs=jload(ACCS_FILE,{})
    email=d.get("email","").strip().lower(); pw=d.get("password","")
    if email not in accs: return jsonify(error="No account with this email"),404
    if accs[email].get("pw_hash")!=hashpw(pw): return jsonify(error="Wrong password"),401
    return _user_resp(email, accs[email])

@app.route("/api/google-login", methods=["POST"])
def google_login():
    d=request.json or {}
    accs=jload(ACCS_FILE,{})
    email=d.get("email","").strip().lower()
    if not email or "@" not in email: return jsonify(error="Invalid Gmail"),400
    if email not in accs:
        gname=email.split("@")[0].replace("."," ").title()
        accs[email]={"name":gname,"pw_hash":"","joined":str(datetime.date.today()),
                     "method":"google","prefs":{},"username":"","bio":"","avatar":"","banner":"",
                     "image_history":[]}
        jsave(ACCS_FILE,accs)
    return _user_resp(email, accs[email])

# ── Profile update ────────────────────────────────────────────────────────────
@app.route("/api/profile", methods=["POST"])
def update_profile():
    d=request.json or {}
    email=d.get("user","").strip().lower()
    accs=jload(ACCS_FILE,{})
    if email not in accs: return jsonify(error="User not found"),404
    u=accs[email]
    name = d.get("name", "")
    username = d.get("username", "")
    bio = d.get("bio", "")
    if not all(isinstance(value, str) for value in (name, username, bio)):
        return jsonify(error="Profile fields must be text."),400
    if "name" in d and name.strip():
        u["name"] = name.strip()[:40]
    if "username" in d:
        u["username"] = username.strip()[:24]
    if "bio" in d:
        u["bio"] = bio.strip()[:160]
    try:
        for field in ("avatar", "banner"):
            if field in d:
                u[field] = _validated_profile_image(d[field]) if d[field] else ""
        if "image_history" in d:
            images = d["image_history"]
            if not isinstance(images, list) or len(images) > 12:
                return jsonify(error="Image history can contain up to 12 images."),400
            clean_images = []
            seen = set()
            for image in images:
                if not isinstance(image, dict):
                    return jsonify(error="Invalid image history entry."),400
                data = _validated_profile_image(image.get("data"))
                digest = hashlib.sha256(data.encode("ascii")).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                clean_images.append({
                    "id": digest[:16],
                    "data": data,
                    "added": str(image.get("added") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
                })
            u["image_history"] = clean_images
    except ValueError as exc:
        return jsonify(error=str(exc)),400
    jsave(ACCS_FILE,accs)
    return _user_resp(email, u)


@app.route("/api/profile/images")
def get_profile_images():
    email = request.args.get("user", "").strip().lower()
    accs = jload(ACCS_FILE, {})
    if email not in accs:
        return jsonify(error="User not found"),404
    return jsonify(images=accs[email].get("image_history", []))

# ── Prefs ─────────────────────────────────────────────────────────────────────
@app.route("/api/prefs", methods=["POST"])
def save_prefs():
    d=request.json or {}
    email=d.get("user","").strip().lower(); prefs=d.get("prefs",{})
    accs=jload(ACCS_FILE,{})
    if email not in accs: return jsonify(error="User not found"),404
    with _DATA_LOCK:
        accs = jload(ACCS_FILE, {})          # re-read inside the lock
        if email not in accs: return jsonify(error="User not found"),404
        accs[email]["prefs"]=prefs
        jsave(ACCS_FILE,accs)
    return jsonify(ok=True)

# ── History (Files / Favorites / Trash) ───────────────────────────────────────
def _hist_match(h, email, url):
    return h.get("user")==email and h.get("url")==url

@app.route("/api/history")
def get_history():
    email=request.args.get("user","")
    hist=jload(HIST_FILE,[])
    return jsonify([h for h in hist if h.get("user")==email][-120:])

@app.route("/api/history/clear", methods=["DELETE"])
def clear_history():
    """Move ALL of the user's entries to Trash (restorable, not gone)."""
    email=request.args.get("user","")
    with _DATA_LOCK:
        hist=jload(HIST_FILE,[])
        for h in hist:
            if h.get("user")==email:
                h["trashed"]=True
        jsave(HIST_FILE,hist)
    log("HIST", f"Moved all history to trash for {email}")
    return jsonify(ok=True)

@app.route("/api/history/remove", methods=["DELETE"])
def remove_history_item():
    """Move entry to Trash (restorable). Permanent delete lives in Trash tab."""
    d=request.json or {}
    email=d.get("user",""); url=d.get("url","")
    with _DATA_LOCK:
        hist=jload(HIST_FILE,[])
        for h in hist:
            if _hist_match(h,email,url):
                h["trashed"]=True
        jsave(HIST_FILE,hist)
    return jsonify(ok=True)

@app.route("/api/history/delete", methods=["POST"])
def delete_history_forever():
    """Permanently delete an entry (from the Trash tab)."""
    d=request.json or {}
    email=d.get("user",""); url=d.get("url","")
    with _DATA_LOCK:
        jsave(HIST_FILE,[h for h in jload(HIST_FILE,[])
                         if not _hist_match(h,email,url)])
    return jsonify(ok=True)

def _reveal_in_file_manager(path, select):
    """Ask the desktop shell to show `path`, selecting it when `select` is set.

    Every branch is list-argv with no shell: a path can legally contain `&`, `|`
    or a quote, and an unquoted shell command would either fail on them or run
    part of the filename.
    """
    if os.name == "nt":
        cmd = ["explorer", "/select," + path] if select else ["explorer", path]
    elif sys.platform == "darwin":
        cmd = ["open", "-R", path] if select else ["open", path]
    else:
        # No portable "select this file" on XDG; opening the containing folder is
        # the closest thing that works on every desktop environment.
        folder = os.path.dirname(path) if select and os.path.isfile(path) else path
        cmd = ["xdg-open", folder]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "file" if (select and os.path.isfile(path)) else "folder"


def reveal_key(path):
    """The comparable form of a folder path.

    One directory has three spellings in this app: what a Windows user types
    (`%USERPROFILE%\\Music`), what a POSIX user types (`~/Music`), and what
    /api/validate-save-path stored after resolving either. Comparing raw strings
    made them look like three different folders, so the buttons that open a
    folder refused to open the folder the user had just chosen. Expanding through
    the same helper the writers use — `expand_save_path`, which knows about
    `%VAR%` where `expanduser` does not — collapses them back into one key.
    """
    return os.path.normcase(os.path.realpath(expand_save_path(path)))


@app.route("/api/reveal", methods=["POST"])
def reveal_download():
    """Show a finished download in Explorer / Finder.

    A path from the client is only ever opened if it is a folder IDLR has already
    used for that account — the default, the save folder in their preferences, or
    a folder a history row names. Accepting any path would turn a localhost UI
    with no auth into "reveal any file on this machine".
    """
    d = request.json or {}
    email = d.get("user", ""); url = d.get("url", "")
    hist = jload(HIST_FILE, [])
    entry = next((h for h in hist if _hist_match(h, email, url)), None)
    if entry is None:
        # "Open the download folder" from the History header names no download,
        # so fall back to a folder IDLR has already used. The allow-list matters:
        # without it this endpoint would open any folder the caller types out.
        want = str(d.get("folder") or "").strip()
        accs = jload(ACCS_FILE, {})
        acc = accs.get(email) if isinstance(accs.get(email), dict) else {}
        prefs = acc.get("prefs") if isinstance(acc.get("prefs"), dict) else {}
        # `savepath` is what /api/prefs stores today; `path` is the name older
        # accounts were written with, so both count.
        prefs_path = str(prefs.get("savepath") or prefs.get("path") or "").strip()
        allowed = {default_download_dir(), prefs_path}
        allowed |= {h["folder"] for h in hist
                    if h.get("user") == email and isinstance(h.get("folder"), str)}
        allowed = {reveal_key(p) for p in allowed if p}
        if want:
            if reveal_key(want) not in allowed:
                # Naming a folder this account never saved into stays a refusal —
                # that is the line that keeps this endpoint out of the rest of the
                # disk. What changed is the comparison above: a folder chosen in
                # Settings now matches itself, which it did not when the test was
                # a raw string equality between `~/Music` and `/home/me/Music`.
                return jsonify(error="IDLR only opens folders it saves into. Yours "
                                     "are the Downloads folder and the one shown in "
                                     "Settings > Download folder."), 404
            target = expand_save_path(want)
        else:
            target = expand_save_path(prefs_path or default_download_dir())
        if not os.path.isdir(target):
            return jsonify(error="That save folder does not exist any more — choose it "
                                 "again in Settings and IDLR will create it."), 404
        try:
            kind = _reveal_in_file_manager(target, False)
        except Exception as e:
            log("HIST", f"reveal folder failed: {type(e).__name__}: {str(e)[:120]}")
            return jsonify(error="Your file manager refused to open."), 500
        return jsonify(ok=True, kind=kind, path=target)
    target = expand_save_path(entry["file"]) if entry.get("file") else ""
    select = bool(target) and os.path.isfile(target)
    if not select:
        target = expand_save_path(entry.get("folder") or "")
    if not target or not os.path.exists(target):
        return jsonify(error="IDLR can't find that file any more — it may have been "
                             "moved, renamed, or deleted."), 404
    try:
        kind = _reveal_in_file_manager(target, select)
    except Exception as e:
        log("HIST", f"reveal failed: {type(e).__name__}: {str(e)[:120]}")
        # The raw path stays in the log, out of the toast: it is the one string
        # that turns a "your file manager said no" into an unreadable wall of
        # text in a 380px corner of the screen.
        return jsonify(error="Your file manager refused to open that folder."), 500
    log("HIST", f"revealed {kind}: {target}")
    return jsonify(ok=True, kind=kind, path=target)


@app.route("/api/history/restore", methods=["POST"])
def restore_history_item():
    d=request.json or {}
    email=d.get("user",""); url=d.get("url","")
    with _DATA_LOCK:
        hist=jload(HIST_FILE,[])
        for h in hist:
            if _hist_match(h,email,url):
                h["trashed"]=False
        jsave(HIST_FILE,hist)
    return jsonify(ok=True)

@app.route("/api/history/empty-trash", methods=["DELETE"])
def empty_trash():
    email=request.args.get("user","")
    with _DATA_LOCK:
        jsave(HIST_FILE,[h for h in jload(HIST_FILE,[])
                         if not(h.get("user")==email and h.get("trashed"))])
    log("HIST", f"Emptied trash for {email}")
    return jsonify(ok=True)

@app.route("/api/history/star", methods=["POST"])
def star_history_item():
    d=request.json or {}
    email=d.get("user",""); url=d.get("url","")
    starred=False
    with _DATA_LOCK:
        hist=jload(HIST_FILE,[])
        for h in hist:
            if _hist_match(h,email,url):
                h["starred"]=not h.get("starred",False)
                starred=h["starred"]
        jsave(HIST_FILE,hist)
    return jsonify(ok=True,starred=starred)

# ── Spotify download via spotdl ───────────────────────────────────────────────
def _parse_bitrate(quality, default="320k"):
    """Accept '320k' / '256k' / ... or 'Best (Max Quality)' -> default."""
    import re as _re
    m = _re.search(r"(\d+)\s*k", str(quality or ""))
    if m:
        kbps = max(64, min(320, int(m.group(1))))
        return f"{kbps}k", str(kbps)
    return default, default.replace("k", "")

def _spotify_title(url, timeout=15):
    """Best-effort 'Artist - Song' title for a Spotify link via public oEmbed
    (no auth). Returns '' when Spotify blocks the lookup."""
    import urllib.request as _req, urllib.parse as _up, json as _js
    try:
        oembed = "https://open.spotify.com/oembed?url=" + _up.quote(url, safe="")
        UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"}
        for _ in range(2):
            try:
                with _req.urlopen(_req.Request(oembed, headers=UA), timeout=timeout) as r:
                    return (_js.load(r).get("title") or "").strip()[:120]
            except Exception:
                continue
    except Exception:
        pass
    return ""

def _spotify_run(job, url, savepath, user, fmt, quality="320k", job_id="?", want_thumb=True):
    """Download Spotify track/album/playlist.

    Primary engine: spotdl CLI. Spotify keeps changing their private API, so
    when spotdl fails we fall back to a YouTube audio match (title via public
    oEmbed -> ytsearch1: -> yt-dlp audio). One of the two almost always works.
    """
    os.makedirs(savepath, exist_ok=True)
    log("SPOTIFY", f"[{job_id}] {url}")
    log("SPOTIFY", f"[{job_id}] Target folder: {savepath}")
    if "spotdl" in BROKEN_PKGS:
        log("SPOTIFY", f"[{job_id}] spotdl engine flagged broken — going straight to YouTube-match fallback")
        return _spotify_yt_fallback(job, url, savepath, user, fmt, quality, job_id, want_thumb,
                                    "Spotify engine (spotdl) is broken on this PC")
    _log_to(job, {"t":"Detected Spotify link — using spotdl…","c":"dim"})
    job["status"]="downloading"

    title = url
    thumb = ""

    audio_fmt = fmt if fmt in ("mp3","m4a","opus","ogg","flac") else "mp3"
    bitrate, _ = _parse_bitrate(quality, "320k")
    log("SPOTIFY", f"[{job_id}] Format={audio_fmt} bitrate={bitrate}")

    # Light metadata attempt (non-fatal): prettify the title from the URL.
    try:
        if "track" in url or "album" in url or "playlist" in url:
            slug = url.split("?")[0].rstrip("/").split("/")[-1][:60]
            if slug:
                title = "Spotify — " + slug
                job["title"] = title
    except Exception:
        pass

    try:
        refresh_ffmpeg_paths(wait=8)
        if getattr(sys, "frozen", False):
            worker_name = "IDLR-spotdl.exe" if os.name == "nt" else "IDLR-spotdl"
            worker_path = _find_bundled_binary(worker_name)
            if worker_path is None:
                raise FileNotFoundError(
                    f"Bundled Spotify helper is missing next to {sys.executable} — "
                    "Windows Defender may have quarantined it."
                )
            cmd = [worker_path]
        else:
            cmd = [sys.executable, "-m", "spotdl"]
        help_result = subprocess.run(
            cmd + ["--help"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        help_text = (help_result.stdout or "") + "\n" + (help_result.stderr or "")
        if help_result.returncode != 0:
            raise RuntimeError(f"Could not inspect spotdl options: {help_text[-300:]}")
        # Build the real command once, from the options this spotdl version
        # actually advertises. (It used to be built twice, so every flag was
        # passed two or three times and an unsupported one could not be dropped.)
        if "--format" in help_text:
            cmd += ["--format", audio_fmt]
        elif "--output-format" in help_text:
            cmd += ["--output-format", audio_fmt]
        else:
            raise RuntimeError("This spotdl version does not expose a supported output-format option.")
        cmd += ["--output", savepath]
        if "--bitrate" in help_text:
            cmd += ["--bitrate", bitrate]
        else:
            _log_to(job, {
                "t": "This bundled spotdl version uses its default bitrate.",
                "c": "dim",
            })
            log("SPOTIFY", f"[{job_id}] spotdl uses its default bitrate ({bitrate} is unavailable in this version)")
        # Point spotdl at OUR ffmpeg explicitly — its own PATH lookup fails on
        # machines where only static-ffmpeg provides it (FFmpegError).
        if FFMPEG and "--ffmpeg" in help_text:
            cmd += ["--ffmpeg", FFMPEG]
        cmd += ["download", url] if "download" in help_text else [url]
        log("SPOTIFY", f"[{job_id}] Running spotdl for {audio_fmt} ...")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=900,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (result.stdout or "") + "\n" + (result.stderr or "")
        # Mirror meaningful lines to the in-app console + terminal.
        shown = 0
        for line in out.split("\n"):
            s = line.strip()
            if not s:
                continue
            if any(k in s for k in ("Downloaded", "Skipping", "Found", "Searching")):
                _log_to(job, {"t": s[:160], "c":"ok"})
                log("SPOTIFY", f"[{job_id}] {s[:160]}")
                shown += 1
            elif any(k in s for k in ("Error", "Failed", "Traceback", "WARNING")):
                _log_to(job, {"t": s[:160], "c":"err"})
                log("SPOTIFY", f"[{job_id}] !! {s[:200]}")
                shown += 1
            if shown > 30:
                break

        ok = result.returncode == 0 or "Downloaded" in out
        if ok:
            job.update({"status":"done","progress":100,"done":True})
            _log_to(job, {"t":f"✓ Spotify download saved to {savepath}","c":"ok"})
            log("SPOTIFY", f"[{job_id}] DONE -> {savepath}")
            _save_hist(user, url, title, thumb, "Audio Only", quality, audio_fmt, "done",
                       folder=savepath)
            # A download that worked is the only real proof the engine is fine, and
            # it has to be allowed to contradict an earlier failure.
            clear_issue("spotdl")
            BROKEN_PKGS.discard("spotdl")
            return
        tail = "\n".join([l for l in out.split("\n") if l.strip()][-6:])
        if _looks_like_engine_failure(tail):
            # Only an engine that cannot run earns a banner. "This video is private"
            # is not news about IDLR, and a notice about the downloader would be the
            # kind of wrong warning this file exists to remove.
            report_issue("spotdl", "Spotify's own downloader is unavailable",
                         "Spotify links still download — IDLR matched this one on "
                         "YouTube instead, so quality and metadata may differ.",
                         "notice", tail[-300:])
        log("SPOTIFY", f"[{job_id}] spotdl FAILED (exit {result.returncode}) — trying YouTube-match fallback")
        _log_to(job, {"t":"spotdl failed — trying YouTube match instead…","c":"dim"})
        return _spotify_yt_fallback(job, url, savepath, user, fmt, quality, job_id, want_thumb,
                                    _friendly_dl_error(tail or "spotdl failed"))
    except subprocess.TimeoutExpired:
        log("SPOTIFY", f"[{job_id}] spotdl timed out — trying YouTube-match fallback")
        return _spotify_yt_fallback(job, url, savepath, user, fmt, quality, job_id, want_thumb,
                                    "spotdl timed out after 15 minutes")

def _spotify_yt_fallback(job, url, savepath, user, fmt, quality, job_id, want_thumb, why):
    """Fallback: resolve the song title (public oEmbed) and download the top
    YouTube match as audio via yt-dlp. No Spotify API needed."""
    title = _spotify_title(url)
    if not title:
        raise Exception(f"{why}. Spotify is also blocking title lookup right now — paste the YouTube link for this song instead.")
    log("SPOTIFY", f"[{job_id}] Fallback match: {title}")
    _log_to(job, {"t":f"Fallback: matched '{title[:60]}' on YouTube","c":"dim"})
    job["title"] = title + " (YouTube match)"
    # ytsearch1: is a first-class yt-dlp URL; reuse the whole audio pipeline.
    return _ytdlp_run(job, f"ytsearch1:{title}", "Audio Only", quality, fmt,
                      savepath, user, want_thumb, False, job_id)

def _friendly_dl_error(raw):
    """Turn raw yt-dlp/spotdl errors into short human-readable messages."""
    s = (raw or "").strip().replace("\n", " ")
    s = re.sub(r"\x1b\[[0-9;]*m", "", s)  # strip ANSI colors leaking into UI
    low = s.lower()
    if "pydantic" in low and ("incompatible" in low or "core" in low):
        return "Spotify engine broken (pydantic library clash). Repair: python -m pip install -U pydantic pydantic-core spotdl — then restart IDLR."
    if "systemerror" in low and "pydantic" in low:
        return "Spotify engine broken (pydantic library clash). Repair: python -m pip install -U pydantic pydantic-core spotdl — then restart IDLR."
    if "video unavailable" in low or "private video" in low:
        return "This video is private or unavailable."
    if "age" in low and "sign in" in low:
        return "Age-restricted video — sign-in required, cannot download."
    if "403" in low or "forbidden" in low:
        return "Source blocked the download (403). Update yt-dlp in Settings and retry."
    if "404" in low or "not found" in low:
        return "Link not found (404). Check the URL."
    if "unsupported url" in low or "no video formats found" in low:
        return "No downloadable video found at this URL."
    if "timed out" in low or "timeout" in low:
        return "Connection timed out. Check your internet and retry."
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "FFmpeg is missing — install it and restart IDLR."
    if "errno 13" in low or "permission denied" in low:
        return ("Permission denied writing files. Your temp/save folder isn't writable — "
                "restart IDLR and pick a folder inside your user profile (click the path bar). "
                "Don't run from C:\\Windows\\System32.")
    if "copyright" in low or "drm" in low:
        return "This content is DRM-protected and cannot be downloaded."
    if "error opening output" in low:
        return "Couldn't write the finished file (bad filename or locked folder). Update IDLR and retry — filenames are now Windows-safe."
    if not s:
        return "Download failed for an unknown reason."
    return s[:220]

# ── Generic yt-dlp download ───────────────────────────────────────────────────
def _ytdlp_run(job, url, mode, quality, fmt, savepath, user,
               want_thumb=True, playlist_all=False, job_id="?"):
    # Format picks that stay FAST on real machines:
    #  - [protocol!*=m3u8] keeps multi-GB "Untested" HLS variants OUT unless
    #    HLS is all a page has (final /best fallback still allows it).
    #  - [vcodec*=avc1] prefers H.264 for mp4 output: much smaller than VP9,
    #    plays everywhere, merges in seconds. Falls back automatically when a
    #    video has no H.264 version. (>1080p has no H.264 on YouTube, so those
    #    keep plain best.)
    _nohls = "[protocol!*=m3u8]"
    _avc = "[vcodec*=avc1]"
    q_map={
        "Best (Max Quality)":f"bestvideo{_nohls}+bestaudio{_nohls}/bestvideo+bestaudio/best",
        "4K":   f"bestvideo[height<=2160]{_nohls}+bestaudio{_nohls}/bestvideo[height<=2160]+bestaudio/best",
        "8K":   f"bestvideo[height<=4320]{_nohls}+bestaudio{_nohls}/bestvideo[height<=4320]+bestaudio/best",
        "1440p":f"bestvideo[height<=1440]{_nohls}+bestaudio{_nohls}/bestvideo[height<=1440]+bestaudio/best",
        "1080p":f"bestvideo{_avc}[height<=1080]{_nohls}+bestaudio{_nohls}/bestvideo[height<=1080]{_nohls}+bestaudio/bestvideo[height<=1080]+bestaudio/best",
        "720p": f"bestvideo{_avc}[height<=720]{_nohls}+bestaudio{_nohls}/bestvideo[height<=720]{_nohls}+bestaudio/bestvideo[height<=720]+bestaudio/best",
        "480p": f"bestvideo{_avc}[height<=480]{_nohls}+bestaudio{_nohls}/bestvideo[height<=480]{_nohls}+bestaudio/bestvideo[height<=480]+bestaudio/best",
        "360p": f"bestvideo{_avc}[height<=360]{_nohls}+bestaudio{_nohls}/bestvideo[height<=360]{_nohls}+bestaudio/bestvideo[height<=360]+bestaudio/best",
        "240p": f"bestvideo{_avc}[height<=240]{_nohls}+bestaudio{_nohls}/bestvideo[height<=240]{_nohls}+bestaudio/bestvideo[height<=240]+bestaudio/best",
        "144p": f"bestvideo{_avc}[height<=144]{_nohls}+bestaudio{_nohls}/bestvideo[height<=144]{_nohls}+bestaudio/bestvideo[height<=144]+bestaudio/best",
    }
    os.makedirs(savepath, exist_ok=True)
    log("YT-DLP", f"[{job_id}] {url}")
    log("YT-DLP", f"[{job_id}] mode={mode} quality={quality} format={fmt} thumb={'on' if want_thumb else 'off'} playlist_all={'on' if playlist_all else 'off'}")
    log("YT-DLP", f"[{job_id}] Target folder: {savepath}")
    # FFmpeg is prepared off the startup path, so a download started in the
    # first instants gets a moment to inherit it (see refresh_ffmpeg_paths).
    refresh_ffmpeg_paths(wait=8)
    if not FFMPEG:
        _log_to(job, {"t":"⚠ FFmpeg not found — merging/MP3 may fail. Install FFmpeg.","c":"err"})
        log("YT-DLP", f"[{job_id}] WARNING: FFmpeg not found on PATH")

    # Use movie-grade opts for all downloads (HLS support, browser headers)
    opts = get_movie_opts(job, url, quality, fmt, savepath)
    opts["postprocessor_hooks"] = [lambda d: _postprocessor_hook(job, d)]
    is_playlist_mode = "Playlist" in mode
    opts["noplaylist"] = not (is_playlist_mode or playlist_all)

    is_audio = "Audio" in mode or fmt in ("mp3","m4a","flac","wav","opus")
    if is_audio:
        ext = fmt if fmt in ("mp3","m4a","flac","wav","opus") else "mp3"
        _, kbps = _parse_bitrate(quality, "320k")
        opts["format"] = f"bestaudio{_nohls}/bestaudio/best"
        opts.pop("merge_output_format", None)
        opts["postprocessors"] = [
            {"key":"FFmpegExtractAudio","preferredcodec":ext,"preferredquality":kbps},
        ]
        if want_thumb:
            opts["postprocessors"].append({"key":"EmbedThumbnail"})
            opts["writethumbnail"] = True
        else:
            opts["writethumbnail"] = False
    else:
        opts["format"] = q_map.get(quality, "bestvideo+bestaudio/best")
        opts["merge_output_format"] = fmt if fmt in ("mp4","mkv","webm") else "mp4"
        if want_thumb:
            opts["writethumbnail"] = True
            opts["postprocessors"] = [{"key":"EmbedThumbnail"}]
        else:
            opts["writethumbnail"] = False
            opts["postprocessors"] = []

    media_kind = _direct_media_kind(url)
    if media_kind:
        _configure_direct_media_opts(
            opts, media_kind, _direct_media_extension(url), is_audio, fmt, quality)
        label = "Direct HLS stream" if media_kind == "hls" else "Direct video file"
        _log_to(job, {"t": f"{label} — downloading stream segments and finalizing the file", "c": "dim"})

    title = url; thumb = ""
    try:
        # Try to get metadata first
        log("YT-DLP", f"[{job_id}] Fetching title/thumbnail ...")
        meta_opts = {
            "quiet":True,"no_warnings":True,
            "http_headers": opts.get("http_headers",{}),
            "geo_bypass": True,
            "socket_timeout": 20,
            "paths": opts.get("paths", {"temp": pick_temp_dir(savepath)}),
        }
        with _SafeDL(meta_opts) as y:
            try:
                info = y.extract_info(url, download=False)
                # Search results / playlists come back with an entries list —
                # use the first match so title/thumb are real, not the query.
                entries = info.get("entries")
                if (not info.get("title")) and entries:
                    try:
                        first = next(e for e in entries if e)
                    except StopIteration:
                        first = None
                    if first:
                        info = first
                title = (info.get("title") or info.get("webpage_title") or url)[:80]
                thumb = info.get("thumbnail","")
                # For movies/series: try to get better poster
                if not thumb and info.get("thumbnails"):
                    thumb = info["thumbnails"][-1].get("url","")
                job["title"] = title
                job["thumb"] = thumb
                _log_to(job, {"t": title, "c":"dim"})
                log("YT-DLP", f"[{job_id}] Found: {title[:80]}")
            except Exception as me:
                _log_to(job, {"t": f"Metadata: {_friendly_dl_error(str(me))[:90]}","c":"dim"})
                log("YT-DLP", f"[{job_id}] Metadata lookup failed: {str(me)[:120]}")

        job["status"] = "downloading"
        log("YT-DLP", f"[{job_id}] Downloading ...")
        with _SafeDL(opts) as y:
            y.download([url])

        job.update({"status":"done","progress":100,"done":True})
        _log_to(job, {"t": f"✓ Saved to {savepath}","c":"ok"})
        log("YT-DLP", f"[{job_id}] DONE -> {savepath}")
        _save_hist(user, url, title, thumb, mode, quality, fmt, "done",
                   folder=savepath, path_hint=_job_output_file(job, savepath))

    except Exception as e:
        err_msg = _friendly_dl_error(str(e))
        job.update({"status":"error","error":err_msg,"done":True})
        _log_to(job, {"t": f"✗ {err_msg}","c":"err"})
        log("YT-DLP", f"[{job_id}] FAILED: {err_msg}")
        _save_hist(user, url, title, thumb, mode, quality, fmt, "error", folder=savepath)

def _trim_history(hist):
    """Bound history.json, keeping the rows the user cares about.

    The UI only ever reads the newest 120 rows per user, so a history file in
    the tens of MBs just makes every save slower. Starred and trashed rows are
    protected: they are things the user asked for explicitly, and silently
    deleting them would look like data loss.
    """
    overflow = len(hist) - _HISTORY_CAP
    if overflow <= 0:
        return hist
    kept = []
    for item in hist:
        if overflow > 0 and not item.get("starred") and not item.get("trashed"):
            overflow -= 1
            continue
        kept.append(item)
    return kept


_MEDIA_EXTS = _DIRECT_VIDEO_EXTS | {".ts", ".m2ts", ".mts", ".mp3", ".m4a",
                                     ".opus", ".ogg", ".wav", ".flac", ".aac"}


def _title_key(name):
    """Fold a filename/title down to letters+digits for a forgiving compare.

    yt-dlp sanitises `%(title)s` its own way (punctuation becomes a dash, emoji
    get dropped), so rebuilding the filename from the title would be wrong for
    most real titles. Comparing folded alphanumerics gets the same answer
    without having to duplicate yt-dlp's rules.
    """
    return re.sub(r"[^0-9a-z]+", "", str(name).lower())


def _file_size(path):
    try:
        return int(os.path.getsize(path))
    except OSError:
        return 0


def _resolve_output_file(folder, title):
    """The file a finished download produced, or ("", 0) when unsure.

    Only used to put "Show in folder" on a history row, so guessing wrong costs a
    missing button and never a wrong deletion. A prefix match on the folded title
    is enough — the folder belongs to this job and was just written to — and the
    newest candidate wins so a re-download points at the new file.
    """
    best, best_mtime = "", -1.0
    try:
        names = os.listdir(folder)
    except Exception:
        return "", 0
    want = _title_key(title)
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext.lower() not in _MEDIA_EXTS:
            continue
        key = _title_key(stem)
        if want and key:
            probe = want[:12] if len(want) >= 12 else want
            alt = key[:12] if len(key) >= 12 else key
            if not (key.startswith(probe) or want.startswith(alt)):
                continue
        path = os.path.join(folder, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best, (_file_size(best) if best else 0)


def _save_hist(user, url, title, thumb, mode, quality, fmt, status,
               folder=None, path_hint=None):
    # Locked end to end: jload/jsave are atomic on their own, but two downloads
    # finishing together each started from the same snapshot and the later save
    # silently dropped the earlier row (a download vanishing from History).
    with _DATA_LOCK:
        hist = jload(HIST_FILE, [])
        entry={"user":user,"url":url,"title":title,"thumb":thumb,"mode":mode,
               "quality":quality,"format":fmt,"status":status,
               "starred":False,"trashed":False,
               "date":datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
        if folder:
            # History could always say *what* was downloaded but never *where it
            # went*, which is why a row had no "open file" action and its only
            # detail was its quality. Recorded at write time, not derived on read:
            # a download folder can hold hundreds of files and scanning it on
            # every /api/history call is a waste on the app's hot path.
            entry["folder"] = folder
            if path_hint and os.path.exists(path_hint):
                entry["file"], entry["bytes"] = path_hint, _file_size(path_hint)
            else:
                path, size = _resolve_output_file(folder, title)
                if path:
                    entry["file"], entry["bytes"] = path, size
        for item in hist:
            if (item.get("user")==user and item.get("url")==url
                    and item.get("mode")==mode):
                # keep a star/trash state the user set while this download ran
                entry["starred"] = bool(item.get("starred"))
                entry["trashed"] = bool(item.get("trashed"))
        # Re-downloading moves the row to "newest" rather than leaving a stale
        # duplicate behind it, which is what the History list assumes.
        hist = [h for h in hist
                if not(h.get("user")==user and h.get("url")==url
                       and h.get("mode")==mode)]
        hist.append(entry)
        jsave(HIST_FILE, _trim_history(hist))

def _job_sidecars(savepath, title):
    """Leftover fragment/part files belonging to one download (same title).
    Old buggy runs left `Title..f616` (double dot); interrupted runs leave
    `.part` / `.ytdl` / `.temp`. The final media file itself never matches."""
    out = []
    try:
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title or "").rstrip(". ")
        if not safe or len(safe) < 3:
            return []
        for f in os.listdir(savepath):
            if f == safe or f.startswith(safe + "."):
                if (f.endswith((".part", ".ytdl", ".temp", ".tmp"))
                        or "..f" in f
                        or re.search(r"\.f\d+([-_].*)?(\.|$)", f)):
                    p = os.path.join(savepath, f)
                    if os.path.isfile(p):
                        out.append(p)
    except Exception:
        pass
    return out

def _sweep_stale_sidecars(savepath):
    """One download = one file. Sweep residue from older broken runs:
    the `..f616` double-dot signature, and `.part` files stale 24h+."""
    try:
        now = time.time()
        n = 0
        for f in os.listdir(savepath):
            p = os.path.join(savepath, f)
            if not os.path.isfile(p):
                continue
            if "..f" in f and re.search(r"\.\.f\d+", f):
                try: os.remove(p); n += 1
                except Exception: pass
            elif f.endswith(".part"):
                try:
                    if now - os.path.getmtime(p) > 24 * 3600:
                        os.remove(p); n += 1
                except Exception:
                    pass
        if n:
            log("DL", f"Cleaned {n} leftover temp file(s) — one download = one file")
    except Exception:
        pass

# ── Download endpoint ─────────────────────────────────────────────────────────
@app.route("/api/download", methods=["POST"])
def start_download():
    d=request.json or {}
    url=d.get("url","").strip(); mode=d.get("mode","Video")
    quality=d.get("quality","Best (Max Quality)"); fmt=d.get("format","mp4")
    savepath=expand_save_path(d.get("path",""))
    user=d.get("user","")
    # Settings toggles forwarded by the frontend (default = old behaviour)
    want_thumb = d.get("thumb", True)
    if isinstance(want_thumb, str):
        want_thumb = want_thumb.lower() not in ("0","false","no","off")
    playlist_all = d.get("playlist_all", False)
    if isinstance(playlist_all, str):
        playlist_all = playlist_all.lower() in ("1","true","yes","on")
    if not url: return jsonify(error="No URL"),400
    ok, savepath, err = ensure_writable_dir(savepath)
    if not ok: return jsonify(error=err),400
    _sweep_stale_sidecars(savepath)

    jid=str(uuid.uuid4())[:8]
    _jobs[jid]={"status":"starting","progress":0,"speed":"","eta":"",
                "log":[],"title":"","thumb":"","error":None,"done":False,
                "created_at":time.time(),"ended_at":0}
    _prune_jobs()
    log("DL", f"[{jid}] NEW user={user or 'guest'} mode={mode} quality={quality} format={fmt}")
    log("DL", f"[{jid}] URL: {url[:140]}")

    def run():
        job=_jobs[jid]
        try:
            if is_spotify(url):
                _spotify_run(job, url, savepath, user, fmt, quality, jid, want_thumb)
            else:
                _ytdlp_run(job, url, mode, quality, fmt, savepath, user,
                           want_thumb, playlist_all, jid)
        except Exception as e:
            msg = _friendly_dl_error(str(e))
            job.update({"status":"error","error":msg,"done":True})
            _log_to(job, {"t":f"✗ {msg}","c":"err"})
            log("DL", f"[{jid}] FAILED: {msg}")
            try: _save_hist(user, url, job.get("title") or url, job.get("thumb",""),
                            mode, quality, fmt, "error", folder=savepath)
            except Exception: pass
        finally:
            # A job that never reports back leaves the UI stuck on
            # "Downloading…" forever, so always close it out here.
            if not job.get("done"):
                job.update({"status":"error","done":True,
                            "error":job.get("error") or "The download stopped before it finished. Try again."})
            job["ended_at"] = time.time()
            # One download = one file: drop this job's sidecars (fragments /
            # .part residue). yt-dlp already cleans on success; this covers
            # failures and interrupted runs.
            try:
                t = job.get("title") or ""
                if t and t != url:
                    for p in _job_sidecars(savepath, t):
                        try: os.remove(p)
                        except Exception: pass
            except Exception:
                pass
            _prune_jobs()

    threading.Thread(target=run, daemon=True).start()
    return jsonify(job_id=jid)

def _capture_job_output(job, candidate):
    """Remember an actual output path reported by yt-dlp, never a guess."""
    if not isinstance(candidate, str) or not candidate:
        return
    try:
        path = os.path.abspath(os.path.expanduser(candidate))
        if os.path.isfile(path) and not path.lower().endswith((".part", ".ytdl", ".temp")):
            job["file_path"] = path
    except (OSError, ValueError):
        pass


def _postprocessor_hook(job, d):
    """Capture yt-dlp's final filename after remuxing/conversion has finished."""
    try:
        info = d.get("info_dict") if isinstance(d, dict) else None
        if not isinstance(info, dict):
            info = {}
        _capture_job_output(job, info.get("filepath") or info.get("_filename")
                            or d.get("filename"))
    except Exception:
        # A history-path hint is optional; it must never abort the media job.
        pass


def _job_output_file(job, savepath):
    """Return the captured file only when it really landed in this job's folder."""
    candidate = job.get("file_path") if isinstance(job, dict) else None
    if not isinstance(candidate, str) or not candidate:
        return ""
    try:
        path = os.path.realpath(os.path.abspath(candidate))
        folder = os.path.realpath(os.path.abspath(savepath))
        if os.path.normcase(os.path.commonpath((path, folder))) != os.path.normcase(folder):
            return ""
        return path if os.path.isfile(path) else ""
    except (OSError, ValueError):
        return ""


def _hook(job, d):
    # Never let a weird progress value kill the download: hook exceptions
    # propagate up through yt-dlp and abort everything.
    try:
        info = d.get("info_dict") if isinstance(d, dict) else None
        if not isinstance(info, dict):
            info = {}
        _capture_job_output(job, info.get("filepath") or info.get("_filename")
                            or d.get("filename"))
        if d["status"]=="downloading":
            raw=d.get("_percent_str","0%").strip()
            try: pct=float(re.sub(r"[^\d.]","",raw) or 0)
            except ValueError: pct=0
            job.update({"progress":pct,"speed":d.get("_speed_str","").strip(),
                        "eta":d.get("_eta_str","").strip()})
        elif d["status"]=="finished":
            job["status"]="merging"
            _log_to(job, {"t":"Merging video + audio…","c":"dim"})
    except Exception:
        pass

@app.route("/api/progress/<jid>")
def progress(jid):
    def stream():
        ll=0
        # This endpoint holds an open connection; without a deadline a job that
        # never reports back keeps its handler thread alive forever.
        deadline = time.time() + 6 * 3600
        while time.time() < deadline:
            job=_jobs.get(jid)
            if not job: yield f"data:{json.dumps({'error':'not found'})}\n\n"; break
            new,ll=_log_window(job, ll)
            yield f"data:{json.dumps({'status':job['status'],'progress':job['progress'],'speed':job['speed'],'eta':job['eta'],'title':job['title'],'thumb':job.get('thumb',''),'logs':new,'done':job['done'],'error':job['error']})}\n\n"
            if job["done"]: break
            time.sleep(0.3)
    return Response(stream(),mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.route("/api/progress-state/<jid>")
def progress_state(jid):
    job = _jobs.get(jid)
    if not job:
        return jsonify(error="Download job not found."),404
    try:
        since = max(0, int(request.args.get("since", "0")))
    except ValueError:
        return jsonify(error="Invalid progress cursor."),400
    # `since` counts every line ever logged, not the retained window, so the
    # cursor still works after _log_to() trims old lines (and the frontend's
    # Math.max() on log_count never sees the number go backwards).
    lines, total = _log_window(job, since)
    return jsonify(
        status=job["status"],
        progress=job["progress"],
        speed=job["speed"],
        eta=job["eta"],
        title=job["title"],
        thumb=job.get("thumb", ""),
        logs=lines,
        log_count=total,
        done=job["done"],
        error=job["error"],
    )

@app.route("/api/preview", methods=["POST"])
def preview_url():
    """Fetch title + thumbnail from any URL using yt-dlp (no download)"""
    d = request.json or {}
    url = d.get("url","").strip()
    if not url: return jsonify(error="No URL"),400
    try:
        meta_opts = {
            "quiet":True,"no_warnings":True,"skip_download":True,
            "socket_timeout":15,"retries":2,
            "paths":{"temp":pick_temp_dir()},
            "http_headers":{
                "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
                "Referer": url,
            },
            "geo_bypass":True,
        }
        with yt_dlp.YoutubeDL(meta_opts) as y:
            info = y.extract_info(url, download=False)
            title = (info.get("title") or info.get("webpage_title") or "")[:120]
            thumb = info.get("thumbnail","")
            if not thumb and info.get("thumbnails"):
                # pick highest resolution thumbnail
                thumbs = sorted(info["thumbnails"], key=lambda t: t.get("width",0) or t.get("preference",0), reverse=True)
                thumb = thumbs[0].get("url","") if thumbs else ""
            duration = info.get("duration")
            uploader = info.get("uploader") or info.get("channel") or ""
            return jsonify(ok=True, title=title, thumb=thumb,
                           duration=duration, uploader=uploader)
    except Exception as e:
        # Still return partial info
        return jsonify(ok=False, title="", thumb="", error=str(e)[:200])

@app.route("/api/thumbnail", methods=["POST"])
def download_thumbnail():
    """Download ONLY the cover art for any URL (no video/audio).

    YouTube etc: thumbnail via yt-dlp metadata. Spotify: cover via the
    public open.spotify.com oEmbed endpoint (no auth). Saves the image
    file into the save folder and logs it to history.
    """
    import re as _re, urllib.request as _req, urllib.parse as _up
    d = request.json or {}
    url = d.get("url","").strip()
    savepath = expand_save_path(d.get("path",""))
    user = d.get("user","")
    if not url: return jsonify(error="Paste a URL first"),400
    ok, savepath, err = ensure_writable_dir(savepath)
    if not ok: return jsonify(error=err),400
    log("THUMB", f"Thumbnail-only: {url[:140]}")

    UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"}
    thumb_url, title = "", url
    try:
        if is_spotify(url):
            oembed = "https://open.spotify.com/oembed?url=" + _up.quote(url, safe="")
            with _req.urlopen(_req.Request(oembed, headers=UA), timeout=20) as r:
                meta = json.load(r)
            thumb_url = meta.get("thumbnail_url","")
            title = (meta.get("title") or url)[:100]
            log("THUMB", f"Spotify oEmbed: {title[:60]}")
        else:
            meta_opts = {"quiet":True,"no_warnings":True,"skip_download":True,
                         "socket_timeout":20,"retries":2,"geo_bypass":True,
                         "paths":{"home":savepath,"temp":pick_temp_dir(savepath)},
                         "http_headers":{"User-Agent":UA["User-Agent"],"Referer":url}}
            with yt_dlp.YoutubeDL(meta_opts) as y:
                info = y.extract_info(url, download=False)
            title = ((info.get("title") or info.get("webpage_title") or url)[:100])
            thumb_url = info.get("thumbnail","") or ""
            if not thumb_url and info.get("thumbnails"):
                thumbs = sorted(info["thumbnails"],
                                key=lambda t: t.get("width",0) or t.get("preference",0),
                                reverse=True)
                thumb_url = (thumbs[0].get("url","") if thumbs else "")
            log("THUMB", f"Found: {title[:60]}")
        if not thumb_url:
            return jsonify(error="No cover art found for this link"),404

        with _req.urlopen(_req.Request(thumb_url, headers={**UA,"Referer":url}), timeout=30) as r:
            data = r.read(15*1024*1024 + 1)
            ctype = (r.headers.get_content_type() or "").lower()
        if len(data) > 15*1024*1024:
            return jsonify(error="Cover art is too large (>15 MB)"),413
        ext = {"image/jpeg":".jpg","image/png":".png","image/webp":".webp",
               "image/gif":".gif"}.get(ctype, "")
        if not ext:
            m = _re.search(r"\.(jpe?g|png|webp|gif)(?:[?#]|$)", thumb_url.lower())
            ext = ("." + m.group(1).replace("jpeg","jpg")) if m else ".jpg"

        base = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip().rstrip(".")[:80] or "cover"
        dest = os.path.join(savepath, base + ext)
        i = 1
        while os.path.exists(dest):
            i += 1
            dest = os.path.join(savepath, f"{base} ({i}){ext}")
        with open(dest, "wb") as f:
            f.write(data)
        log("THUMB", f"DONE -> {dest}")
        _save_hist(user, url, title, thumb_url, "Thumbnail", "-", ext.lstrip("."), "done",
                   folder=savepath, path_hint=dest)
        return jsonify(ok=True, file=os.path.basename(dest), title=title, thumb=thumb_url)
    except Exception as e:
        msg = _friendly_dl_error(str(e))
        log("THUMB", f"FAILED: {msg}")
        try: _save_hist(user, url, title, "", "Thumbnail", "-", "img", "error",
                        folder=savepath)
        except Exception: pass
        return jsonify(error=msg),400

@app.route("/api/info")
def info():
    return jsonify(ytdlp=yt_dlp.version.__version__,
                   ffmpeg=bool(FFMPEG),
                   ffmpeg_path=FFMPEG,
                   version=APP_VERSION,
                   platform=sys.platform,
                   python=".".join(str(x) for x in sys.version_info[:3]),
                   default_path=default_download_dir(),
                   network=SERVER_URL,
                   frozen=bool(getattr(sys, "frozen", False)),
                   active_ytdlp_update=_ACTIVE_UPDATED_YTDLP,
                   pending_ytdlp_update=_PENDING_UPDATED_YTDLP,
                   spotify_engine=("youtube-fallback" if "spotdl" in BROKEN_PKGS else "spotdl"),
                   problems=list(SETUP_PROBLEMS),
                   data_dir=APP_DIR,
                   log_file=LOG_FILE)

@app.route("/api/validate-save-path", methods=["POST"])
def validate_save_path():
    data = request.json or {}
    requested_path = data.get("path", "")
    if not isinstance(requested_path, str) or not requested_path.strip():
        return jsonify(error="Enter a folder path."),400
    path = expand_save_path(requested_path)
    ok, resolved, error = ensure_writable_dir(path)
    if not ok:
        return jsonify(error=error),400
    return jsonify(path=resolved)

@app.route("/api/history/rename", methods=["POST"])
def rename_history_item():
    """Rename a history entry — called by the ✏️ button in History."""
    d = request.json or {}
    email = d.get("user",""); url = d.get("url","")
    new_title = (d.get("newTitle") or d.get("new_title") or "").strip()[:120]
    if not email or not url: return jsonify(error="Missing user or url"),400
    if not new_title: return jsonify(error="Title cannot be empty"),400
    hist = jload(HIST_FILE, [])
    found = False
    for h in hist:
        if h.get("user")==email and h.get("url")==url:
            h["title"] = new_title
            found = True
    if not found: return jsonify(error="Entry not found"),404
    jsave(HIST_FILE, hist)
    log("HIST", f"Renamed entry for {email}: {new_title[:60]}")
    return jsonify(ok=True)

@app.route("/api/delete-account", methods=["POST"])
def delete_account():
    """Permanently delete an account + its history — called from Settings."""
    d = request.json or {}
    email = (d.get("user") or "").strip().lower()
    if not email: return jsonify(error="Missing user"),400
    with _DATA_LOCK:
        accs = jload(ACCS_FILE, {})
        if email not in accs: return jsonify(error="Account not found"),404
        del accs[email]
        jsave(ACCS_FILE, accs)
        hist = [h for h in jload(HIST_FILE,[]) if h.get("user")!=email]
        jsave(HIST_FILE, hist)
    log("AUTH", f"Deleted account: {email}")
    return jsonify(ok=True)

def _install_frozen_ytdlp_update():
    global _PENDING_UPDATED_YTDLP
    with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=30) as response:
        metadata_bytes = response.read(5 * 1024 * 1024 + 1)
    if len(metadata_bytes) > 5 * 1024 * 1024:
        raise ValueError("The PyPI update response was unexpectedly large.")
    release = json.loads(metadata_bytes.decode("utf-8"))
    latest = str(release["info"]["version"])
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+_-]{0,79}", latest):
        raise ValueError("PyPI returned an invalid yt-dlp version.")
    wheels = [
        item for item in release.get("urls", [])
        if item.get("filename", "").endswith("-py3-none-any.whl")
        and item.get("filename", "").startswith("yt_dlp-")
    ]
    if not wheels:
        raise RuntimeError("PyPI did not provide a universal yt-dlp wheel.")
    wheel = wheels[0]
    wheel_url = wheel.get("url", "")
    if not wheel_url.startswith("https://files.pythonhosted.org/"):
        raise ValueError("PyPI returned an unexpected wheel download host.")
    with urllib.request.urlopen(wheel_url, timeout=60) as response:
        wheel_bytes = response.read(40 * 1024 * 1024 + 1)
    if len(wheel_bytes) > 40 * 1024 * 1024:
        raise ValueError("The yt-dlp update package exceeded the size limit.")
    expected_digest = str(wheel.get("digests", {}).get("sha256", ""))
    actual_digest = hashlib.sha256(wheel_bytes).hexdigest()
    if not expected_digest or actual_digest != expected_digest:
        raise ValueError("The yt-dlp package checksum did not match PyPI.")

    current = str(yt_dlp.version.__version__)
    current_parts = tuple(int(part) for part in re.findall(r"\d+", current))
    latest_parts = tuple(int(part) for part in re.findall(r"\d+", latest))
    if latest_parts <= current_parts:
        return latest, True

    os.makedirs(_YTDLP_UPDATE_PACKAGES, exist_ok=True)
    package_dir = os.path.join(_YTDLP_UPDATE_PACKAGES, "yt-dlp-" + latest)
    if not os.path.isfile(os.path.join(package_dir, "yt_dlp", "__init__.py")):
        staging = tempfile.mkdtemp(prefix=".yt-dlp-update-", dir=_YTDLP_UPDATE_PACKAGES)
        try:
            with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
                for member in archive.infolist():
                    parts = member.filename.replace("\\", "/").split("/")
                    if (not parts or ".." in parts or parts[0] != "yt_dlp"
                            and not parts[0].startswith("yt_dlp-")):
                        continue
                    mode = member.external_attr >> 16
                    if mode & 0o170000 == 0o120000:
                        raise ValueError("The yt-dlp package contains an unsafe symbolic link.")
                    archive.extract(member, staging)
            if not os.path.isfile(os.path.join(staging, "yt_dlp", "__init__.py")):
                raise ValueError("The downloaded yt-dlp package is incomplete.")
            os.replace(staging, package_dir)
        finally:
            if os.path.isdir(staging):
                shutil.rmtree(staging)

    state_tmp = _YTDLP_UPDATE_STATE + ".tmp"
    with open(state_tmp, "w", encoding="utf-8") as stream:
        json.dump({"version": latest}, stream)
    os.replace(state_tmp, _YTDLP_UPDATE_STATE)
    _PENDING_UPDATED_YTDLP = latest
    return latest, False


@app.route("/api/update-ytdlp", methods=["POST"])
def update_ytdlp():
    if getattr(sys, "frozen", False):
        log("UPDATE", "Checking PyPI for a signed yt-dlp wheel update ...")
        try:
            latest, already_current = _install_frozen_ytdlp_update()
            current = str(yt_dlp.version.__version__)
            if already_current:
                message = f"yt-dlp {latest} is already the latest version."
                log("UPDATE", message)
                return jsonify(ok=True, msg=message, version=latest,
                               requires_restart=False, already_current=True)
            message = f"yt-dlp {latest} is installed. Restart IDLR to finish the update."
            log("UPDATE", message)
            return jsonify(ok=True, msg=message, version=latest,
                           previous_version=current, requires_restart=True)
        except Exception as exc:
            message = f"yt-dlp update failed: {exc}"
            log("UPDATE", message)
            return jsonify(ok=False, msg=message),502

    log("UPDATE", "Updating yt-dlp + spotdl to latest ...")
    try:
        # NOTE: pydantic + pydantic-core are upgraded TOGETHER with spotdl.
        # Upgrading spotdl alone is what causes the pydantic-core version clash
        # that used to kill the whole app on next startup (now also guarded).
        subprocess.check_output([sys.executable,"-m","pip","install","--upgrade",
                                 "yt-dlp","spotdl","pydantic","pydantic-core"],
                           stderr=subprocess.STDOUT, text=True)
        from importlib.metadata import version
        BROKEN_PKGS.discard("spotdl")
        msg = f"Updated yt-dlp v{version('yt-dlp')} and spotdl. Restart IDLR to finish."
        log("UPDATE", msg)
        return jsonify(ok=True,msg=msg,version=version("yt-dlp"),requires_restart=True)
    except subprocess.CalledProcessError as e:
        tail = (e.output or "")[-400:]
        log("UPDATE", f"FAILED: {tail[:200]}")
        return jsonify(ok=False,msg=_friendly_dl_error(tail))
    except Exception as e:
        log("UPDATE", f"FAILED: {e}")
        return jsonify(ok=False,msg=str(e))

def _print_startup(port, desktop=False):
    if sys.stdout is None:
        return
    ip = local_ip()
    import platform as _pf
    def _p(s):
        if sys.stdout is None:
            return
        try: print(s, flush=True)
        except (UnicodeEncodeError, OSError): print(s.encode("ascii","replace").decode(), flush=True)
    _p("")
    _p("==========================================================")
    _p("  IDLR -- Internet Downloader")
    _p(f"  v{APP_VERSION} | Python {_pf.python_version()} | yt-dlp v{yt_dlp.version.__version__}")
    _p("  YouTube | Spotify | SoundCloud | TikTok | 1000+ sites")
    _p("==========================================================")
    _p(f"  > Local      http://localhost:{port}")
    if not desktop:
        _p(f"  > Network    http://{ip}:{port}   (share on your Wi-Fi)")
    _p(f"  > FFmpeg     {'FOUND ' + FFMPEG if FFMPEG else 'NOT FOUND -- install FFmpeg for MP3/merging'}")
    _p(f"  > Data       {APP_DIR}  (accounts + history stay on this PC)")
    _p(f"  > Temp       {pick_temp_dir()}  (verified writable)")
    _p("==========================================================")
    _p("  Downloads:")
    _p("    - Paste a link in the app -> watch progress HERE + in the app console")
    _p("    - Statuses: [YT-DLP] video / [SPOTIFY] audio / [UPDATE] updater")
    _p("    - DONE = saved to your Save Location | FAILED = reason shown here")
    _p("==========================================================")
    if desktop:
        _p("  Close the IDLR window to stop the app.\n")
    else:
        _p("  Keep this window OPEN while using the app. Press Ctrl+C to stop.\n")


def _serve_in_browser(host, port, auto_open=True):
    global SERVER_URL
    from werkzeug.serving import make_server

    server = make_server(host, port, app, threaded=True)
    actual_port = server.server_port
    SERVER_URL = f"http://{local_ip() if host == '0.0.0.0' else 'localhost'}:{actual_port}"
    _print_startup(actual_port)
    url = f"http://localhost:{actual_port}"
    if auto_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    else:
        log("DESKTOP", f"Browser auto-open is off; open {url} manually or start with --browser.")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _run_desktop():
    global SERVER_URL
    try:
        import webview
    except ImportError as exc:
        if getattr(sys, "frozen", False):
            raise RuntimeError("The desktop window component is missing from this IDLR build.") from exc
        log("DESKTOP", "pywebview is not installed; serving IDLR without auto-opening a browser. Use --browser to open it on purpose.")
        return _serve_in_browser("127.0.0.1", 5000, auto_open=False)

    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    SERVER_URL = f"http://localhost:{port}"
    _print_startup(port, desktop=True)
    log("DESKTOP", f"Serving local app at {SERVER_URL}")
    threading.Thread(target=server.serve_forever, name="idlr-web-server", daemon=True).start()
    api = _DesktopApi()
    try:
        window = webview.create_window(
            "IDLR App — Internet Downloader",
            SERVER_URL,
            min_size=(800, 600),
            js_api=api,
        )
        api.window = window
        webview.start()
    finally:
        server.shutdown()
        server.server_close()


class _DesktopApi:
    def __init__(self):
        self.window = None

    def select_folder(self, current_path=""):
        if self.window is None:
            raise RuntimeError("The desktop folder picker is not ready.")
        import webview

        directory = expand_save_path(current_path) if current_path else default_download_dir()
        selected = self.window.create_file_dialog(
            webview.FileDialog.FOLDER,
            directory=directory,
            allow_multiple=False,
        )
        if not selected:
            return {"path": ""}
        return {"path": os.path.normpath(selected[0])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IDLR Internet Downloader")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Open IDLR in the default browser instead of a desktop window.",
    )
    parser.add_argument(
        "--network",
        action="store_true",
        help="Open in a browser and allow other devices on your Wi-Fi to connect.",
    )
    args = parser.parse_args()
    try:
        if args.browser or args.network:
            _serve_in_browser("0.0.0.0" if args.network else "127.0.0.1", 5000)
        else:
            _run_desktop()
    except Exception as exc:
        log("FATAL", f"{type(exc).__name__}: {exc}")
        raise