# ⚡ IDLR App — Internet Downloader

**Your personal, self‑hosted media download hub.**
Paste any link, and IDLR grabs the video or audio you want — no ads, no redirects, no third‑party uploads.
Everything runs locally on your machine.

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-brightgreen)
![yt‑dlp](https://img.shields.io/badge/yt--dlp-latest-orange)
![spotdl](https://img.shields.io/badge/spotdl-latest-brightgreen)

> **Current version: v2.0.3** — a native Tauri desktop app with a bundled media worker, plus a local browser option. Both use the same features and local data, with layouts designed for their respective screens.

[Download the latest Windows setup](https://github.com/Makerxm800/IDLR-App/releases/latest/download/IDLR-App-Setup.exe) · [All releases](https://github.com/Makerxm800/IDLR-App/releases)

The release page keeps the main downloads easy to find: Windows, Linux, and macOS. The Windows installer is 64-bit for Intel/AMD PCs and can run on Windows ARM laptops using Windows x64 emulation. **32-bit Windows is not supported by this build.**

---

## ✨ Features

- 🌐 **1000+ supported sites** via `yt‑dlp` – YouTube, TikTok, Instagram, Twitch, HLS / movie streams, and more
- 🎵 **Spotify audio** – tracks, albums & playlists via `spotdl`, with automatic YouTube-match fallback when Spotify's API misbehaves (format + bitrate respected)
- 🔍 **Live URL detection** – paste a link and the UI instantly recognises the platform (+ optional auto‑set of Mode/Format)
- 🖼️ **Thumbnail preview + Thumbnail button** – see title, uploader, duration & cover before you download, or save just the cover art with one click
- 🎛️ **Settings toggles that actually work** – every switch changes real behaviour (see table below)
- 🎨 **6 themes + Liquid Glass** – Midnight, Light, Ocean, Sunset, Forest, and Rosegold, with reduced-motion support
- ℹ️ **Premium Info panel (ⓘ)** – live app / yt‑dlp / FFmpeg versions, browser URL, save folder, supported sites
- 📋 **Bulk history management** – search, select, rename ✏️, re-download, star ★ favorites, or move to 🗑 Trash (restore / empty)
- 👤 **Continue as guest** – use downloads and history without registering; guest history and preferences stay on this device
- 🖥️ **Download diagnostics** – the legacy Python development server prints setup and per‑download progress; the installed native app does not require Python
- 🔒 **100% private** – accounts, history & settings live in `~/.idlr_app/` on your PC only

---

## 🧩 Supported Platforms

| Platform | Video | Audio | Notes |
|----------|:-----:|:-----:|-------|
| YouTube  | ✅    | ✅    | Playlists supported |
| Spotify  | —     | ✅    | Via `spotdl`, MP3/M4A/OPUS/FLAC |
| SoundCloud | —  | ✅    | Auto‑switches to Audio |
| TikTok   | ✅    | ✅    |  |
| Twitter/X| ✅    | ✅    |  |
| Instagram| ✅    | ✅    |  |
| Twitch   | ✅    | ✅    | Clips & VODs |
| Vimeo    | ✅    | ✅    |  |
| Facebook | ✅    | ✅    |  |
| Reddit   | ✅    | ✅    |  |
| Dailymotion | ✅ | ✅    |  |
| HLS / `.m3u8` streams | ✅ | ✅ | Merged via FFmpeg |
| Direct `.mp4/.mkv/.webm` files | ✅ | ✅ | Downloaded directly |
| + 1000 more via `yt‑dlp` | ✅ | ✅ | Generic extractor fallback |

---

## 🚀 IDLR App for Desktop

The installed app is a native Tauri window with a desktop sidebar and workspace layout. On startup it can also open a browser view at a private, randomly assigned `127.0.0.1` address; that view uses a responsive web layout. Both views use the same local account, history, settings, and download services, so you can keep either or both open. The Settings panel's **Browser URL** button copies the address. The bridge only listens on this PC and checks the request origin. A bundled background worker runs media downloads; the installed app does not require a separate Python installation.

- **Windows:** build the `IDLR_*_x64-setup.exe` NSIS installer for 64-bit Intel/AMD Windows. Windows ARM laptops can use this installer through Windows x64 emulation. This release does not provide a 32-bit Windows installer. It registers an uninstaller and upgrades an existing installation when rerun. Find the installer under `src-tauri/target/release/bundle/nsis/`.
- **Linux:** build `.deb` and `.AppImage` bundles on Linux.
- **macOS:** build a `.dmg` installer and signed `.app.tar.gz` updater bundle on macOS. Builds are not Apple-notarized; macOS may require you to right-click the app and choose **Open** the first time.

Build on the operating system you want to run on (Tauri and PyInstaller do not cross-compile). On Windows, install Node.js LTS, Python 3.10+, the stable Rust toolchain, and Visual Studio C++ Build Tools with the Windows SDK first. Signed installer builds need the local updater key configured as described below. Then run these commands from the project folder in PowerShell:

```powershell
npm ci
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt -r requirements.txt
$env:TAURI_SIGNING_PRIVATE_KEY = "$env:USERPROFILE\.tauri\idlr-updater.key"
$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = ""
.\.venv\Scripts\python.exe build.py
```

Building also requires the target OS's Tauri prerequisites. On Linux, install the GTK 3 and WebKit2GTK 4.1 development packages listed in the workflow; on macOS, install Xcode Command Line Tools. The setup's uninstaller removes IDLR's program files and shortcuts while preserving account data, image history, settings, and downloads.

The media worker and Spotify helper are bundled as private app resources. The installed app does not depend on a separately installed Python interpreter.

Manual runs of **Build desktop apps** in GitHub Actions produce native bundles for Windows, Linux and macOS. A version tag builds signed update packages and publishes the installers plus `latest.json` to GitHub Releases. A system FFmpeg is recommended for audio conversion and video merging.

### Signed desktop app updates

The Settings panel keeps **Update download engine** for yt-dlp/spotdl and has a separate **Check for IDLR updates** action for the desktop application. App packages are signature-verified by Tauri before installation. Browser-only installs cannot self-update.

The release workflow targets `Makerxm800/IDLR-App`. Generate the signing key once with `npm run tauri -- signer generate -w "$env:USERPROFILE\.tauri\idlr-updater.key"`; the public key is already configured in `src-tauri/tauri.conf.json`. Keep a secure backup of the private key: losing it prevents future updates. Do not commit it or share it in chat. Before publishing, add the private key contents as the repository Actions secret `TAURI_SIGNING_PRIVATE_KEY`. If you protect the key with a password, also add `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` as an Actions secret. The local build commands above use the key file path; CI uses the secret contents.

Push a matching version tag (for example `v2.0.3`). GitHub Actions builds signed Windows, Linux, and macOS packages, publishes the Windows setup, Linux AppImage, macOS DMG and macOS updater archive, and removes stale build files from that release. The updater manifest includes its package signatures, so standalone signature files are not needed in the release downloads.

### Run from source

Install the JavaScript and Python build/runtime dependencies once. These commands start the actual Tauri desktop app in development mode:

```powershell
npm ci
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt -r requirements.txt
npm run tauri -- dev
```

On macOS/Linux, activate `.venv` and use `python -m pip` for the matching setup commands. To work only on the browser interface and Flask API, start `python app.py --browser` instead. This development server is not the installed desktop runtime; it uses the browser layout and its own local API server.

### Editing IDLR as a developer

The app is intentionally straightforward to edit without generating frontend code:

| What you want to change | Edit this |
|---|---|
| Layout, styling, text, settings controls, and browser/desktop presentation | `index.html` |
| Download behavior, API routes, and the development web server | `app.py` |
| Native desktop commands, local data, and the bundled-worker bridge | `src-tauri/src/lib.rs` |
| Worker entry points packaged into the app | `download_worker.py`, `spotdl_worker.py` |
| Native window, bundle metadata, and icon list | `src-tauri/tauri.conf.json` |
| Signed update manifest for GitHub releases | `scripts/create_updater_manifest.py`, `.github/workflows/desktop-build.yml` |
| App icon shown in the browser | `favicon.svg` |
| Python tests | `tests/test_app.py` |

For frontend edits, change `index.html` and refresh the browser or restart `npm run tauri -- dev`. `scripts/prepare_frontend.py` copies the HTML and favicon into `frontend-dist/` before a Tauri run or build; edit the source files, not that generated folder. For backend edits, run `python -m unittest discover -s tests` from the project root. To produce a new installer after testing, run `python build.py` on the target OS. User accounts and settings are kept in `~/.idlr_app/`, outside the source tree.

### Build prerequisites

- Python 3.10 or newer is needed on the build machine to bundle the hidden download worker; it is not needed to run the installed desktop app.
- `ffmpeg` recommended (required for MP3 extraction, video merging & thumbnails).
  The app ships `static-ffmpeg` as a fallback, but a system FFmpeg is more reliable.

### Legacy Python server (development only)

The commands below run the Flask/browser development backend, not the native desktop application. Use the Windows installer or build the Tauri app above for the native app.

```bash
# Create a virtual environment and install development dependencies
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows
venv\Scripts\activate

python -m pip install -r requirements.txt
```

```bash
python app.py --browser
```

Use `python app.py --network` to allow other devices on your Wi-Fi to connect to the legacy server.

---

## 🖥️ Usage

1. **Paste a URL** into the main input – the platform badge appears automatically.
2. **Select Mode / Quality / Format** from the dropdowns (or let Auto‑detect set them).
3. **Click “Download Now”** – watch live progress in the app console. Browser/terminal mode also shows backend logs. Or click **“🖼 Thumbnail”** to save just the cover art.
4. **Track everything in History** – tabs for Files / ★ Favorites / 🕘 Recent / 🗑 Trash. Search, rename ✏️, re‑download ↺, star ★, or delete (goes to Trash, restorable).
5. **Customise via Settings** (gear icon) – every toggle takes effect immediately.
6. **Need help?** Click the **ⓘ Info button** for versions, system status & supported sites.

---

## ⚙️ Configuration

All settings live in the in‑app Settings panel (gear icon). They save on this device.

| Setting | Default | What it actually does |
|---------|---------|----------------------|
| Theme | Midnight | 6 themes: Midnight, Light, Ocean, Sunset, Forest, Rosegold. Saved per device (`idlr_theme`), applied before first paint (no flash) |
| Liquid Glass | ON | Frosted blur on cards, modals & topbar (Nebula-style). OFF = solid surfaces, faster paint |
| Auto‑detect URL | ON | Auto‑switches Mode/Format on paste (e.g. Spotify → Audio/MP3). OFF = badge only, your options stay untouched |
| Download Thumbnail | ON | Embeds & saves cover art. OFF = no `writethumbnail`/`EmbedThumbnail`, faster + fewer FFmpeg failures |
| Link Preview | ON | Fetches title, uploader, duration & cover on paste. OFF = no auto preview calls |
| Full Playlist | OFF | ON = downloads the whole playlist even in single Video/Audio mode (`noplaylist=false`) |
| Download Alerts | ON | Bell notification when a download finishes or fails. OFF = silent |
| Confirm Before Delete | ON | Asks before bulk/clear history deletes. OFF = delete immediately |
| Hero Animations | ON | Animated hero title + chips. OFF = static, instant (adds `no-anim` to page) |
| Calm Motion | OFF | ON = minimises all animations site-wide (accessibility, `reduce-motion`) |
| Console Log | ON | Live download output under the progress bar. OFF = console hidden (`hide-console`) |
| Save Login Session | ON | Keeps you signed in across restarts (`localStorage`). OFF = session cleared on login |
| Save folder | `~/Downloads` | Where finished files go. Click the path bar to change |
| Update yt‑dlp + spotdl | — | One‑click updater in Python/browser mode; packaged desktop builds use a newer IDLR release for bundled updates |

> The panel is responsive: bottom-sheet on phones (≤560px), 2-column grid on tablets/desktops.
> Every row shows its live state (e.g. “ON — cover art embedded”), toggles are keyboard-friendly (Enter/Space), and switches animate with a spring knob + glow.

> All data (accounts, history) is stored locally in `~/.idlr_app/` (`accounts.json`, `history.json`).
> Nothing is ever sent anywhere except the download request to the source site.

---

## 🗂️ Project Structure

```text
idlr/
├── app.py                  # Flask development server and download API
├── index.html              # Shared UI source; web and native layouts
├── favicon.svg             # Source brand mark
├── download_worker.py      # Bundled media worker and API
├── spotdl_worker.py        # Spotify helper entry point
├── build.py                # PyInstaller + Tauri release build
├── requirements*.txt       # Runtime and build dependencies
├── scripts/                # Frontend asset preparation
├── src-tauri/              # Native Rust commands and Tauri config
├── tests/test_app.py       # Python API and download regression tests
└── README.md               # Setup, usage, and developer guide
```

`frontend-dist/`, `dist/`, `build/`, `node_modules/`, and `src-tauri/target/` are generated or installed build outputs; make source changes in the files above. The app keeps its accounts and history in `~/.idlr_app/`. Packaged builds include the frontend, backend dependencies, and Spotify helper.

### Key API endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/register` · `/api/login` · `/api/google-login` | Auth |
| POST | `/api/profile` · `/api/prefs` | Profile + download prefs |
| POST | `/api/validate-save-path` | Check/create a writable download folder |
| POST | `/api/delete-account` | Permanently delete account + history |
| POST | `/api/download` | Start a download (`thumb`, `playlist_all` respected) |
| GET  | `/api/progress/<job_id>` (SSE) | Live progress stream |
| POST | `/api/preview` | Title / thumbnail / duration lookup |
| POST | `/api/thumbnail` | Download cover art only (YouTube via yt-dlp, Spotify via oEmbed), saved to save folder + history |
| GET  | `/api/history` | History (with `starred` / `trashed` flags) |
| DELETE | `/api/history/clear` | Move ALL entries to Trash (restorable) |
| DELETE | `/api/history/remove` | Move entry to Trash (restorable) |
| POST | `/api/history/restore` · POST `/api/history/delete` · DELETE `/api/history/empty-trash` | Restore / delete forever / empty trash |
| POST | `/api/history/star` | Toggle favorite ★ |
| POST | `/api/history/rename` | Rename a history entry ✏️ |
| GET  | `/api/info` | Versions (`version`, `ytdlp`, `ffmpeg`), local browser URL |
| POST | `/api/update-ytdlp` | Upgrade yt‑dlp (verified PyPI wheel in desktop builds) and yt‑dlp + spotdl in Python mode |

---

## 🐛 Troubleshooting

| Issue | Solution |
|-------|----------|
| `ffmpeg not found` / merge fails | Install FFmpeg and restart the terminal. Turning **Download Thumbnail OFF** can avoid thumbnail-related post-processing |
| YouTube `403` / `Sign in to confirm` | Settings → **Update yt‑dlp + spotdl**, then retry. Age‑restricted / DRM content cannot be downloaded |
| Spotify download fails with `KeyError: 'uri'` / traceback | Spotify changed their private API and broke spotdl's lookup. IDLR now auto-falls back to a YouTube audio match — just retry the download. If title lookup is also blocked, paste the YouTube link for the song directly |
| Spotify download fails | Make sure the link is public. Check bitrate/format choice. Terminal shows the exact reason and which engine was used |
| `app.py` crashes on startup with a `pydantic`/`SystemError` wall of text | A library update clashed versions (spotdl's `pydantic-core` mismatch). The app starts in degraded mode if Spotify cannot load. In Python mode, Settings can update the dependency pair together; packaged desktop builds need a newer IDLR release. Manual repair: `python -m pip install -U pydantic pydantic-core spotdl` |
| `No downloadable video found` | The page has no direct media (login wall, JS‑only player, or DRM like Netflix) |
| Save folder not writable | Pick an existing folder via the path bar; the backend creates it and probe-writes before starting, so you get the reason up front instead of mid-download |
| `Postprocessing: Error opening output files: Invalid argument` | The video title ends with a dot or has characters Windows forbids (`Thank you and Goodbye.` → `..mp4`). Fixed: all filenames are now Windows-sanitised (trailing dots/spaces, illegal chars, reserved names, over-long paths) on every site |
| `Permission denied: 'C:\WINDOWS\system32\tmp...tmp'` | Your process temp folder points somewhere Windows blocks (often from launching inside System32 or a broken TEMP var). Fixed: yt-dlp temp is now pinned to a verified-writable dir (`~/.idlr_app/tmp`), preview/metadata calls too. Restart `python app.py` from your normal folder (not System32) and you're set |
| Toggles “do nothing” | Fixed in v1.1.0 — every toggle now applies instantly + persists. Hard‑refresh (`Ctrl+Shift+R`) if you still see the old UI |
| Update button seems dead | In Python/browser mode it shows update progress and needs `app.py` running. Packaged desktop builds use newer IDLR releases for bundled updates |
| Desktop app does not open | Check `~/.idlr_app/idlr.log`; Windows needs the Microsoft Edge WebView2 Runtime, and Linux needs GTK 3 + WebKit2GTK 4.1 |
| Rename / Delete Account 404 | Fixed in v1.1.0 — backend now implements both endpoints |

---

## 📜 Changelog

### v2.0.3 — guest access and interface fixes

- Added guest access without registration, with device-local guest history and preferences.
- Improved history selection controls and updated profile/account screens for desktop and web.

### v2.0.1 — signed app updates and interface polish

- Added the separate signed IDLR desktop updater while preserving the existing yt-dlp/spotdl updater.
- Added a tag-triggered release pipeline that publishes setup packages and a platform-specific updater manifest.
- Refined desktop/web interaction feedback, history tabs, notification controls, and light-theme contrast.

### v2.0.2 — IDLR App release

- Consolidated the full desktop and browser app into the IDLR App repository and aligned product, window, and installer names.
- Added a direct latest Windows setup download and moved signed app updates to the IDLR App releases.

### v2.0.0 — native Tauri desktop app

- Replaced the packaged Python/Flask desktop window with a native Tauri app and a hidden JSON-line download worker.
- Moved account, profile, preference, history, and save-folder operations into native app commands; the Tauri window no longer starts a local HTTP server.
- Added native folder selection, an NSIS installer/uninstaller, and Linux/macOS bundle targets.
- Kept existing accounts and history under `~/.idlr_app` so the app can reuse them.

### v1.9.0 — Windows setup & desktop reliability

### v1.8.0 — native folder picker & account switching

- The desktop Save Location control opens a native folder picker. Selected folders are checked for write access before they become the download target; browser mode uses an in-app path dialog because browsers cannot expose native folder paths.
- Account switching explicitly returns email accounts to sign-in mode, while Add Account always starts a clean registration flow without dropping saved accounts.
- Added regression coverage for native folder selection, save-path validation, and Google account sign-in persistence.

### v1.7.0 — updater, profile image library & resilient downloads

- Settings updater now installs and checksum-verifies the official latest yt-dlp wheel in packaged builds; Python mode updates yt-dlp and spotdl. Both modes clearly request an app restart when needed.
- Profile picture and cover image can be changed from the profile editor. Compressed, validated uploads are saved in a chronological image library, where saved pictures can be reused or removed.
- Download UI now catches failed requests, recovers from interrupted progress streams by polling the job state, and reports useful server-side errors instead of getting stuck.
- History tabs now use a responsive full-width layout with clearer counts and improved row hover styling.

### v1.6.0 — desktop app + multi-platform builds

- IDLR now opens in its own desktop window, using a loopback-only local server on port 5000 so saved browser preferences persist between launches; browser and Wi-Fi sharing remain available with `--browser` and `--network`.
- Added a PyInstaller build for Windows, Linux and macOS, plus a GitHub Actions workflow that builds each target on its native OS.
- Packaged a separate Spotify CLI helper so Spotify track, album and playlist downloads remain available in frozen desktop builds.
- Packaged builds update through newer IDLR releases instead of trying to modify bundled Python libraries in place.

### v1.5.0 — one file per download + Favorites & Trash

- **One download = one file:** fragment/`.part` sidecars are auto-removed when a job finishes or fails, and every new download sweeps legacy junk — the old `..f616` double-dot leftovers and 24h-stale `.part` files (your screenshot). Delete the big leftovers once by just starting any download.
- **Favorites ★ + Trash 🗑:** History now has Files / Favorites / Recent (7 days) / Trash tabs with counts. ★ pins favorites, 🗑 moves to Trash (restorable, Restore All), Empty Trash deletes forever. Clear All now moves to Trash instead of wiping.
- **Quiet refresh:** “Welcome back” greetings + notifications fire only on fresh sign-in, never on reload. Notifications capped at 50 so the list stays meaningful.
- **BETA pill** next to the IDLR logo (topbar + login), subtle hover sheen on settings rows and theme cards. Toggles untouched — all still work.

### v1.4.1 — merge-failure fix (“Error opening output files”)

- **What you saw:** download ran, “Merging video + audio…”, then `X ERROR: Postprocessing: Error opening output files: Invalid argument` — on YouTube and potentially any site.
- **Root cause:** titles ending with a dot (`Thank you and Goodbye.` → `Thank you and Goodbye..mp4`) are illegal on Windows, so ffmpeg couldn't open the output. Proven by printing the generated filename; `--windows-filenames` does NOT strip this in current yt-dlp.
- **Fix:** new `_SafeDL` layer sanitises every output name after yt-dlp prepares it — trailing dots/spaces, illegal chars, reserved names (`CON`, `NUL`…), over-long paths. Applies to downloads, merges and thumbnails on all sites.
- **Proven live:** your exact video downloaded + merged → `Thank you and Goodbye.mp4` on disk.
- Also: ANSI color codes (`[0;31m…`) no longer leak into the app's error lines.

### v1.4.0 — Spotify fallback + Shorts (tested with real downloads)

- **Spotify fixed for real:** Spotify's private API now returns `GenericError`, breaking spotdl's track lookup (`KeyError: 'uri'`) — plus spotdl couldn't find FFmpeg on its own. Fixed both: spotdl gets our FFmpeg path explicitly, and when it still fails IDLR auto-falls back to a YouTube audio match (title via public oEmbed → `ytsearch1:` → same MP3 pipeline with your format/bitrate). Proven live: `spotdl FAILED → Fallback match → DONE → .mp3 on disk`.
- **Shorts verified:** `/shorts/` links download end-to-end (metadata → fragments → merge → `.mp4`). Proven live: `Me at the zoo.mp4` saved. Paste badge now says “YouTube Short detected — vertical video”.
- YouTube watch/music extraction re-verified (incl. 4K), search-result titles/thumbs now resolve to the actual match instead of the query string.

### v1.3.2 — “failed fetch / Permission denied system32” fix

- **What you saw:** every download + preview failed with `Permission denied: 'C:\WINDOWS\system32\tmp....tmp'`, and the save bar showed a literal `%USERPROFILE%\Downloads`.
- **Root causes:** (1) yt-dlp trusted the machine's ambient temp folder, which on your setup resolves into `System32` where writes are blocked; (2) the frontend displayed a `%USERPROFILE%` placeholder that Windows never expands, and old copies of it could even be saved as the real path.
- **Fixes:** yt-dlp `temp` is now pinned to a verified-writable dir (`~/.idlr_app/tmp`, probe-written at use) for downloads, metadata, preview and thumbnail fetches; save folder is `%VAR%`/`~`-expanded, created, and probe-written *before* a job starts (clear 400 error up front, no more mid-download `✗`); `/api/info` now serves the real `default_path` which the UI displays; legacy `%…%` values in saved settings are discarded; `Errno 13` maps to a human message telling you to relaunch outside System32.
- Launch `python app.py` from your normal folder (not `C:\Windows\System32`) and paste the link again.

### v1.3.1 — startup crash guard (“doesn't work anymore” fix)

- **What happened:** an update pulled mismatched helper libraries (`pydantic` 2.13.5 vs `pydantic-core` 2.49.0), so `import spotdl` exploded and `app.py` died on startup with a scary traceback. Nothing was hacked — just a version clash.
- **Repaired your PC:** matched pair reinstalled (`pydantic-core` 2.46.5), `import spotdl` verified working.
- **So it can never kill the app again:** startup check now catches *any* import failure (was `ImportError` only), self-repairs, and if the optional Spotify engine is still broken it starts anyway with a `[WARN]` — YouTube etc. keep working, Spotify shows the exact repair command instead of a traceback.
- **Root cause fixed:** Settings updater now upgrades `pydantic` + `pydantic-core` together with `spotdl`, which is the combination that caused the clash.
- Friendly-error map + degraded-mode path tested live (broken-engine simulation → clean `error` job with repair instructions).

### v1.3.0 — thumbnail button + notification & dropdown fixes

- **New — 🖼 Thumbnail button:** next to Download Now. Saves only the cover art (YouTube etc. via yt-dlp metadata, Spotify via public oEmbed — no auth). Sanitised filename, no-overwrite numbering, 15 MB cap, history entry (`Thumbnail` mode gets its own row per link), terminal `[THUMB]` logs, respects Download Alerts.
- **Fixed — notifications in Light mode:** popover + items were stuck dark. Remade premium: icon bubbles per type (success/error/info/update), count pill in header, staggered entrance, hover slide, friendlier empty state — all theme-aware.
- **Fixed — dropdown sliding under next card (your screenshot):** Liquid Glass blurred `.card`, and blur creates a stacking context that trapped the open Mode/Quality/Format menu underneath the Save Location card. Cards are now never blurred (their `var(--card)` is near-opaque anyway) — blur stays on modals, popovers, topbar and the dropdown itself. Also removed a mobile `overflow-x` on the topbar that could clip the notification popover.
- History dedupe now keys on `(user, url, mode)` so a thumbnail save no longer overwrites the video row for the same link.

### v1.2.0 — themes + settings glow-up

**New — themes (Nebula-style, small set)**

- 6 themes: **Midnight** (default), **Light**, **Ocean**, **Sunset**, **Forest**, **Rosegold** — `body[data-theme]` variable overrides, saved as `idlr_theme`, applied before first paint (no white flash).
- **Light theme** done properly: dark text + tinted surfaces, not just inverted vars.
- Accent color (`--red`) follows the theme, so toggles, buttons, badges and glows all match.

**New — 5 more toggles (all functional)**

| Toggle | Default | Effect |
|--------|---------|--------|
| Link Preview | ON | OFF = no auto title/cover fetch on paste |
| Download Alerts | ON | OFF = no bell notification on done/failed |
| Confirm Before Delete | ON | OFF = bulk/clear history deletes skip `confirm()` |
| Liquid Glass | ON | OFF = solid surfaces, faster paint (`body.liquid-glass`) |
| Calm Motion | OFF | ON = site-wide animation kill (`body.reduce-motion`, accessibility) |

**Redesigned — Settings GUI**

- Fits every device: bottom-sheet on phones (≤560px, safe-area padding), auto-fit grid on tablets/desktops, `86–92dvh` scrolling, `clamp()` padding.
- Better info: each row has a live status dot + sub-text describing its CURRENT state (updates on every flip).
- Animation: spring knob (`cubic-bezier(.34,1.8,.64,1)`), glow pop on enable, hover grow, press squash, keyboard support (Tab + Enter/Space, `role="switch"`, focus rings).
- Theme picker: swatch cards with active ring + check, same interaction language as Nebula Cloud.
- Small global mobile fit: wrapping topbar, full-width Download button, single-column options.

### v1.1.0 — reliability + honest UI update

**Fixed — downloads**

- **Spotify fixed:** removed the fragile `Song.from_url` Python‑API import that crashed on newer `spotdl` versions; now uses the stable `python -m spotdl` CLI only, respects your **format + bitrate** (96–320k), mirrors progress to the app console, and saves history on success *and* failure.
- **Video fixed:** thumbnails are now conditional (`Download Thumbnail` toggle) instead of forced `EmbedThumbnail` (which used to fail whole downloads when FFmpeg/thumb was missing); **audio bitrate choice is respected** (`FFmpegExtractAudio` quality from your `320k/256k/…` pick); direct HLS/`.mp4` handling kept; `noplaylist` now honours the **Full Playlist** toggle.
- **Clear errors:** raw `yt‑dlp`/`spotdl` tracebacks are translated (`403`, private video, age‑restricted, DRM, timeout, missing FFmpeg…) in both the app console and terminal.

**Fixed — missing backend (frontend called them, server 404'd)**

- Added `POST /api/history/rename` (History ✏️ button works again).
- Added `POST /api/delete-account` (Settings → Delete Account works again).
- Fixed Settings updater row (it looked for `#sp-update-title`/`#sp-update-sub` that didn't exist — ids added, Updating…/Updated ✓ states visible).
- `/api/info` now also returns `version` + `ffmpeg_path`.

**Fixed — premium toggles (“didn't change anything”)**

| Toggle | Before | After |
|--------|--------|-------|
| Auto‑detect URL | Always auto‑switched options | OFF = badge only, your Mode/Format untouched |
| Download Thumbnail | Ignored | Sent as `thumb` → backend skips thumbnail steps |
| Full Playlist | Ignored (`"Playlist" in mode` only) | Sent as `playlist_all` → forces full‑playlist download |
| Hero Animations | Ignored | Toggles `body.no-anim`, kills/re‑enables animations + toast feedback |
| Console Log | Ignored | Toggles `body.hide-console`, hides/shows console + toast |
| Save Login Session | Partly worked (DOM race) | Uses settings state directly; session set/cleared reliably |

**Better Info (ⓘ)**

- Rebuilt from a 2‑line stub into a premium card: hero + version badge, live **App / yt‑dlp / FFmpeg** stats, *What it does* guide, supported‑source chips, **System status** rows (network URL, save folder), then Legal & Support.
- **What’s New** popup rewritten to describe these real changes and re‑shown once for existing users.

**Better terminal (`python app.py`)**

- Old: 6 plain lines, silent installs, zero download output.
- New: ASCII‑safe startup banner (version, Python, yt‑dlp, local + network URLs, FFmpeg status, data dir, how to read downloads), `[SETUP]` dependency lines, and per‑download logs: `[DL] NEW …`, `[YT-DLP]/[SPOTIFY] … Found / Downloading / DONE -> folder / FAILED: reason`, `[UPDATE]`, `[HIST]`, `[AUTH]` lines. Safe on Windows `cp1252` consoles.

**Docs**

- README rewritten: correct install (no phantom `requirements.txt`/`static/`/`templates/`), accurate settings table, endpoint list, expanded troubleshooting, and this changelog.

### v1.0.0 — initial release

- Flask + single‑file UI, auth, history, notifications, YouTube/Spotify/SoundCloud/TikTok/Instagram/Twitch support, HLS handling, settings panel shell, About/Terms/Privacy/Cookies modals.

---

## 🤝 Contributing

This is a personal self‑hosted project, but contributions are welcome!
If you find a bug or have a feature request, please open an issue or submit a pull request.

## 📄 License

Distributed under the MIT License. See the `LICENSE` file for more information.

## 🙌 Acknowledgements

- `yt‑dlp` – the powerhouse behind media extraction
- `spotdl` – for seamless Spotify audio
- `Flask` – the lightweight web framework
- `FFmpeg` – for audio/video processing

Made with ❤️ for a faster, cleaner download experience.
