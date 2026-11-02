#!/usr/bin/env python3
"""
IDLR App — Internet Downloader
YouTube · Spotify · SoundCloud · Twitter · Instagram · TikTok and more
Fast · Private · Unlimited
"""
import argparse, base64, hashlib, json, os, re, shutil, subprocess, sys, tempfile, urllib.request, zipfile
import io

APP_VERSION = "2.0.3"
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

def log(tag, msg):
    """Pretty timestamped terminal log, e.g. [12:01:44] [IDLR:DL] message."""
    import datetime as _dt
    ts = _dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{tag}] {msg}"
    if sys.stdout is None:
        log_path = os.path.join(os.path.expanduser("~"), ".idlr_app", "idlr.log")
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            pass
        return
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        print(line.encode("ascii", "replace").decode(), flush=True)


_activate_updated_ytdlp()


def pip(pkg):
    log("SETUP", f"Installing {pkg} ... (first run only)")
    subprocess.check_call([sys.executable,"-m","pip","install","-U",pkg,"-q"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("SETUP", f"Installed {pkg} OK")

# spotdl is OPTIONAL: the app must start even if the Spotify engine is broken
# (e.g. a pydantic/pydantic-core version clash after an upgrade). In that case
# YouTube etc. keep working and Spotify downloads show the exact repair command.
OPTIONAL_PKGS = {"spotdl"}
BROKEN_PKGS = set()

log("SETUP", "Checking dependencies (flask, yt-dlp, static-ffmpeg, spotdl) ...")
for p,i in [("flask","flask"),("yt-dlp","yt_dlp"),("static-ffmpeg","static_ffmpeg"),("spotdl","spotdl")]:
    try:
        __import__(i)
        log("SETUP", f"  [OK] {p} already installed")
    except Exception as e:
        if getattr(sys, "frozen", False):
            if p in OPTIONAL_PKGS:
                BROKEN_PKGS.add(p)
                log("SETUP", f"  [WARN] Bundled {p} is unavailable; Spotify downloads will use the YouTube fallback.")
                continue
            log("SETUP", f"  [FATAL] Bundled {p} is unavailable: {type(e).__name__}: {str(e)[:160]}")
            sys.exit(1)
        # NOTE: except Exception (not just ImportError) — a broken install can
        # raise SystemError and friends at import time; that must never kill startup.
        log("SETUP", f"  [!!] {p} import failed ({type(e).__name__}: {str(e)[:110]}) — repairing ...")
        try:
            pip(p)
            __import__(i)
            log("SETUP", f"  [OK] {p} repaired")
        except Exception as e2:
            if p in OPTIONAL_PKGS:
                BROKEN_PKGS.add(p)
                log("SETUP", f"  [WARN] {p} still broken — starting anyway. Spotify downloads will explain the fix.")
                log("SETUP", f"  [WARN] Manual repair: python -m pip install -U pydantic pydantic-core spotdl")
            else:
                log("SETUP", f"  [FATAL] {p} is required but broken: {str(e2)[:160]}")
                log("SETUP", f"  [FATAL] Repair with: python -m pip install -U --force-reinstall {p}")
                sys.exit(1)

log("SETUP", "Loading application modules ...")
from flask import Flask, request, jsonify, Response, send_from_directory
import yt_dlp, static_ffmpeg, shutil, json, hashlib, datetime
import threading, uuid, re, time, socket, webbrowser

log("SETUP", "Preparing FFmpeg ...")
static_ffmpeg.add_paths()
FFMPEG = shutil.which("ffmpeg") or ""
log("SETUP", "Application modules ready.")

# ── Storage ───────────────────────────────────────────────────────────────────
ACCS_FILE = os.path.join(APP_DIR, "accounts.json")
HIST_FILE = os.path.join(APP_DIR, "history.json")
os.makedirs(APP_DIR, exist_ok=True)
SERVER_URL = "http://localhost:5000"

def jload(p, d):
    try:
        with open(p) as f: return json.load(f)
    except: return d

def jsave(p, d):
    with open(p,"w") as f: json.dump(d, f, indent=2)

def hashpw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def local_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        s.connect(("8.8.8.8",80)); ip=s.getsockname()[0]; s.close(); return ip
    except: return "localhost"

def is_spotify(url):
    return "spotify.com" in url or url.startswith("spotify:")

def is_hls_or_direct(url):
    """Direct HLS/m3u8 stream or direct video file URL"""
    u = url.lower().split("?")[0]
    return any(u.endswith(ext) for ext in (".m3u8",".m3u",".ts",".mp4",".mkv",".webm",".avi",".mov",".flv"))

def default_download_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads")

def expand_save_path(p):
    """Resolve %VAR% / ~ / relative bits. The frontend used to send a literal
    '%USERPROFILE%\\Downloads' which Windows never expands by itself."""
    p = (p or "").strip().strip('"').strip("'")
    if not p:
        return default_download_dir()
    return os.path.normpath(os.path.expandvars(os.path.expanduser(p)))

def ensure_writable_dir(path):
    """Create the folder and PROVE we can write in it (probe file).
    Returns (ok, real_path, error)."""
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        return False, path, f"Cannot create save folder '{path}': {e}"
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
                return False, path, f"Save folder check failed: {e}; could not remove probe file: {cleanup_error}"
        return False, path, f"Save folder not writable '{path}': {e}. Pick another folder (click the path bar)."

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
        # Generic extractor — try hard to find video on any page
        "force_generic_extractor": False,
        "geo_bypass": True,
        # Better compat
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 5,
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

SCRIPT_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, static_folder=SCRIPT_DIR, template_folder=SCRIPT_DIR)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
_jobs = {}

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
    accs[email]["prefs"]=prefs; jsave(ACCS_FILE,accs)
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
    jsave(HIST_FILE,[h for h in jload(HIST_FILE,[])
                     if not _hist_match(h,email,url)])
    return jsonify(ok=True)

@app.route("/api/history/restore", methods=["POST"])
def restore_history_item():
    d=request.json or {}
    email=d.get("user",""); url=d.get("url","")
    hist=jload(HIST_FILE,[])
    for h in hist:
        if _hist_match(h,email,url):
            h["trashed"]=False
    jsave(HIST_FILE,hist)
    return jsonify(ok=True)

@app.route("/api/history/empty-trash", methods=["DELETE"])
def empty_trash():
    email=request.args.get("user","")
    jsave(HIST_FILE,[h for h in jload(HIST_FILE,[])
                     if not(h.get("user")==email and h.get("trashed"))])
    log("HIST", f"Emptied trash for {email}")
    return jsonify(ok=True)

@app.route("/api/history/star", methods=["POST"])
def star_history_item():
    d=request.json or {}
    email=d.get("user",""); url=d.get("url","")
    hist=jload(HIST_FILE,[])
    starred=False
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
    job["log"].append({"t":"Detected Spotify link — using spotdl…","c":"dim"})
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
        if getattr(sys, "frozen", False):
            worker_name = "IDLR-spotdl.exe" if os.name == "nt" else "IDLR-spotdl"
            worker_path = os.path.join(os.path.dirname(sys.executable), worker_name)
            if not os.path.isfile(worker_path):
                raise FileNotFoundError(f"Bundled Spotify helper is missing: {worker_path}")
            cmd = [worker_path]
        else:
            cmd = [sys.executable, "-m", "spotdl"]
        cmd += ["--output", savepath,
                "--format", audio_fmt,
                "--bitrate", bitrate]
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
            job["log"].append({
                "t": "This bundled spotdl version uses its default bitrate.",
                "c": "dim",
            })
            log("SPOTIFY", f"[{job_id}] spotdl uses its default bitrate ({bitrate} is unavailable in this version)")
        # Point spotdl at OUR ffmpeg explicitly — its own PATH lookup fails on
        # machines where only static-ffmpeg provides it (FFmpegError).
        if FFMPEG and "--ffmpeg" in help_text:
            cmd += ["--ffmpeg", FFMPEG]
        cmd += [url]
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
                job["log"].append({"t": s[:160], "c":"ok"})
                log("SPOTIFY", f"[{job_id}] {s[:160]}")
                shown += 1
            elif any(k in s for k in ("Error", "Failed", "Traceback", "WARNING")):
                job["log"].append({"t": s[:160], "c":"err"})
                log("SPOTIFY", f"[{job_id}] !! {s[:200]}")
                shown += 1
            if shown > 30:
                break

        ok = result.returncode == 0 or "Downloaded" in out
        if ok:
            job.update({"status":"done","progress":100,"done":True})
            job["log"].append({"t":f"✓ Spotify download saved to {savepath}","c":"ok"})
            log("SPOTIFY", f"[{job_id}] DONE -> {savepath}")
            _save_hist(user, url, title, thumb, "Audio Only", quality, audio_fmt, "done")
            return
        tail = "\n".join([l for l in out.split("\n") if l.strip()][-6:])
        log("SPOTIFY", f"[{job_id}] spotdl FAILED (exit {result.returncode}) — trying YouTube-match fallback")
        job["log"].append({"t":"spotdl failed — trying YouTube match instead…","c":"dim"})
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
    job["log"].append({"t":f"Fallback: matched '{title[:60]}' on YouTube","c":"dim"})
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
    if not FFMPEG:
        job["log"].append({"t":"⚠ FFmpeg not found — merging/MP3 may fail. Install FFmpeg.","c":"err"})
        log("YT-DLP", f"[{job_id}] WARNING: FFmpeg not found on PATH")

    # Use movie-grade opts for all downloads (HLS support, browser headers)
    opts = get_movie_opts(job, url, quality, fmt, savepath)
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

    # If it's a direct HLS/m3u8 URL, force ffmpeg concat
    if is_hls_or_direct(url):
        opts["format"] = "best"
        opts["merge_output_format"] = "mp4"
        job["log"].append({"t":"🎬 Direct stream detected — using HLS downloader","c":"dim"})

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
                job["log"].append({"t": title, "c":"dim"})
                log("YT-DLP", f"[{job_id}] Found: {title[:80]}")
            except Exception as me:
                job["log"].append({"t": f"Metadata: {_friendly_dl_error(str(me))[:90]}","c":"dim"})
                log("YT-DLP", f"[{job_id}] Metadata lookup failed: {str(me)[:120]}")

        job["status"] = "downloading"
        log("YT-DLP", f"[{job_id}] Downloading ...")
        with _SafeDL(opts) as y:
            y.download([url])

        job.update({"status":"done","progress":100,"done":True})
        job["log"].append({"t": f"✓ Saved to {savepath}","c":"ok"})
        log("YT-DLP", f"[{job_id}] DONE -> {savepath}")
        _save_hist(user, url, title, thumb, mode, quality, fmt, "done")

    except Exception as e:
        err_msg = _friendly_dl_error(str(e))
        job.update({"status":"error","error":err_msg,"done":True})
        job["log"].append({"t": f"✗ {err_msg}","c":"err"})
        log("YT-DLP", f"[{job_id}] FAILED: {err_msg}")
        _save_hist(user, url, title, thumb, mode, quality, fmt, "error")

def _save_hist(user, url, title, thumb, mode, quality, fmt, status):
    hist=jload(HIST_FILE,[])
    entry={"user":user,"url":url,"title":title,"thumb":thumb,"mode":mode,
           "quality":quality,"format":fmt,"status":status,
           "starred":False,"trashed":False,
           "date":datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    hist=[h for h in hist if not(h.get("user")==user and h.get("url")==url and h.get("mode")==mode)]
    hist.append(entry)
    jsave(HIST_FILE,hist)

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
                "log":[],"title":"","thumb":"","error":None,"done":False}
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
            job["log"].append({"t":f"✗ {msg}","c":"err"})
            log("DL", f"[{jid}] FAILED: {msg}")
            try: _save_hist(user, url, job.get("title") or url, job.get("thumb",""),
                            mode, quality, fmt, "error")
            except Exception: pass
        finally:
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

    threading.Thread(target=run, daemon=True).start()
    return jsonify(job_id=jid)

def _hook(job, d):
    # Never let a weird progress value kill the download: hook exceptions
    # propagate up through yt-dlp and abort everything.
    try:
        if d["status"]=="downloading":
            raw=d.get("_percent_str","0%").strip()
            try: pct=float(re.sub(r"[^\d.]","",raw) or 0)
            except ValueError: pct=0
            job.update({"progress":pct,"speed":d.get("_speed_str","").strip(),
                        "eta":d.get("_eta_str","").strip()})
        elif d["status"]=="finished":
            job["status"]="merging"
            job["log"].append({"t":"Merging video + audio…","c":"dim"})
    except Exception:
        pass

@app.route("/api/progress/<jid>")
def progress(jid):
    def stream():
        ll=0
        while True:
            job=_jobs.get(jid)
            if not job: yield f"data:{json.dumps({'error':'not found'})}\n\n"; break
            new=job["log"][ll:]; ll=len(job["log"])
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
    return jsonify(
        status=job["status"],
        progress=job["progress"],
        speed=job["speed"],
        eta=job["eta"],
        title=job["title"],
        thumb=job.get("thumb", ""),
        logs=job["log"][since:],
        log_count=len(job["log"]),
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
        _save_hist(user, url, title, thumb_url, "Thumbnail", "-", ext.lstrip("."), "done")
        return jsonify(ok=True, file=os.path.basename(dest), title=title, thumb=thumb_url)
    except Exception as e:
        msg = _friendly_dl_error(str(e))
        log("THUMB", f"FAILED: {msg}")
        try: _save_hist(user, url, title, "", "Thumbnail", "-", "img", "error")
        except Exception: pass
        return jsonify(error=msg),400

@app.route("/api/info")
def info():
    return jsonify(ytdlp=yt_dlp.version.__version__,
                   ffmpeg=bool(FFMPEG),
                   ffmpeg_path=FFMPEG,
                   version=APP_VERSION,
                   default_path=default_download_dir(),
                   network=SERVER_URL,
                   frozen=bool(getattr(sys, "frozen", False)),
                   active_ytdlp_update=_ACTIVE_UPDATED_YTDLP,
                   pending_ytdlp_update=_PENDING_UPDATED_YTDLP)

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


def _serve_in_browser(host, port):
    global SERVER_URL
    from werkzeug.serving import make_server

    server = make_server(host, port, app, threaded=True)
    actual_port = server.server_port
    SERVER_URL = f"http://{local_ip() if host == '0.0.0.0' else 'localhost'}:{actual_port}"
    _print_startup(actual_port)
    url = f"http://localhost:{actual_port}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
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
        log("DESKTOP", "pywebview is not installed; opening IDLR in your browser instead.")
        return _serve_in_browser("127.0.0.1", 5000)

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