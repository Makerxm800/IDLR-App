import base64
import builtins
import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import app as idlr


class IDLRApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.accounts_file = os.path.join(self.temp.name, "accounts.json")
        self.history_file = os.path.join(self.temp.name, "history.json")
        self.client = idlr.app.test_client()
        self.account_path_patch = mock.patch.object(idlr, "ACCS_FILE", self.accounts_file)
        self.history_path_patch = mock.patch.object(idlr, "HIST_FILE", self.history_file)
        self.account_path_patch.start()
        self.history_path_patch.start()
        self.addCleanup(self.account_path_patch.stop)
        self.addCleanup(self.history_path_patch.stop)

    @staticmethod
    def image_data(color):
        return "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + color).decode("ascii")

    def create_account(self):
        response = self.client.post("/api/register", json={
            "email": "idlr-test@example.com",
            "password": "password123",
            "name": "IDLR Test",
        })
        self.assertEqual(response.status_code, 200)

    def test_google_login_loads_existing_accounts_and_persists_new_accounts(self):
        self.create_account()
        existing = self.client.post("/api/google-login", json={
            "email": "IDLR-TEST@example.com",
        })
        self.assertEqual(existing.status_code, 200)
        self.assertEqual(existing.json["email"], "idlr-test@example.com")
        self.assertEqual(existing.json["name"], "IDLR Test")

        new = self.client.post("/api/google-login", json={
            "email": "new-google-user@example.com",
        })
        self.assertEqual(new.status_code, 200)
        self.assertEqual(new.json["method"], "google")
        self.assertEqual(new.json["name"], "New-Google-User")

        with open(self.accounts_file, encoding="utf-8") as stream:
            accounts = json.load(stream)
        self.assertEqual(accounts["new-google-user@example.com"]["method"], "google")

    def test_save_path_validation_returns_a_writable_normalized_folder(self):
        folder = os.path.join(self.temp.name, "new downloads")
        response = self.client.post("/api/validate-save-path", json={"path": folder})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["path"], os.path.normpath(folder))
        self.assertTrue(os.path.isdir(folder))

        blocked_path = os.path.join(self.temp.name, "not-a-folder")
        with open(blocked_path, "w", encoding="utf-8") as stream:
            stream.write("file")
        invalid = self.client.post("/api/validate-save-path", json={"path": blocked_path})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("Cannot create save folder", invalid.json["error"])

    def test_history_survives_a_truncated_file_and_keeps_a_copy(self):
        """A half-written history.json used to mean 'no history' with no trace."""
        self.create_account()
        idlr._save_hist("idlr-test@example.com", "https://a/1", "One", "", "Video", "1080p", "mp4", "done")
        with open(self.history_file, "w", encoding="utf-8") as stream:
            stream.write('[{"user": "idlr-test@example.com", "tit')

        self.assertEqual(self.client.get("/api/history?user=idlr-test@example.com").json, [])

        salvaged = [name for name in os.listdir(self.temp.name) if "history.json.corrupt-" in name]
        self.assertEqual(len(salvaged), 1, "the unreadable file should be kept for recovery")
        with open(os.path.join(self.temp.name, salvaged[0]), encoding="utf-8") as stream:
            self.assertIn('"tit', stream.read())

    def test_concurrent_history_saves_do_not_eat_each_other(self):
        """Two downloads finishing together used to write the same snapshot twice."""
        self.create_account()
        user = "idlr-test@example.com"
        threads = [threading.Thread(
            target=idlr._save_hist,
            args=(user, f"https://race/{index}", f"Race {index}", "", "Video",
                  "1080p", "mp4", "done")) for index in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with open(self.history_file, encoding="utf-8") as stream:
            stored = json.load(stream)
        self.assertEqual(len(stored), 40,
                         f"lost {40 - len(stored)} history rows to a write race")

    def test_history_saves_leave_no_temp_files_behind(self):
        self.create_account()
        for index in range(12):
            idlr._save_hist("idlr-test@example.com", f"https://a/{index}", f"T{index}",
                            "", "Video", "1080p", "mp4", "done")
        strays = [name for name in os.listdir(self.temp.name) if name.endswith(".tmp")]
        self.assertEqual(strays, [])
        with open(self.history_file, encoding="utf-8") as stream:
            self.assertEqual(len(json.load(stream)), 12)

    def test_history_is_bounded_but_protected_entries_survive(self):
        self.create_account()
        user = "idlr-test@example.com"
        cap = 60  # a small cap exercises the same trim logic without the wait
        with mock.patch.object(idlr, "_HISTORY_CAP", cap):
            for index in range(cap + 40):
                idlr._save_hist(user, f"https://bulk/{index}", f"Bulk {index}", "",
                                "Video", "Best (Max Quality)", "mp4", "done")
            with open(self.history_file, encoding="utf-8") as stream:
                stored = json.load(stream)
            self.assertLessEqual(len(stored), cap)
            self.assertNotIn("https://bulk/0", [entry["url"] for entry in stored])

            # Starred and trashed rows are what the user asked for: keep them,
            # even though they are the oldest entries in the file.
            self.client.post("/api/history/star", json={"user": user, "url": stored[0]["url"]})
            self.client.delete("/api/history/remove", json={"user": user, "url": stored[1]["url"]})
            for index in range(cap):
                idlr._save_hist(user, f"https://more/{index}", f"More {index}", "",
                                "Video", "1080p", "mp4", "done")
            with open(self.history_file, encoding="utf-8") as stream:
                after = json.load(stream)
        urls = {entry["url"] for entry in after}
        self.assertIn(stored[0]["url"], urls, "a starred download must not be trimmed away")
        self.assertIn(stored[1]["url"], urls, "a trashed download must not be trimmed away")
        self.assertLessEqual(len(after), cap + 2)

    def test_job_logs_stay_bounded_without_breaking_the_ui_cursor(self):
        """Progress lines are capped, but `log_count` must keep counting up —
        the console polls with a cursor and would otherwise stall or replay."""
        job = {}
        with mock.patch.object(idlr, "_JOB_LOG_KEEP", 25):
            for index in range(60):
                idlr._log_to(job, {"t": f"line {index}", "c": "dim"})
        self.assertEqual(len(job["log"]), 25)
        self.assertEqual(job["log_total"], 60)
        self.assertEqual(job["log"][-1]["t"], "line 59")

        lines, total = idlr._log_window(job, 58)
        self.assertEqual([line["t"] for line in lines], ["line 58", "line 59"])
        self.assertEqual(total, 60)
        self.assertEqual(idlr._log_window(job, 60)[0], [])
        # A client that never polled gets everything still retained.
        self.assertEqual(len(idlr._log_window(job, 0)[0]), 25)

        with mock.patch.dict(idlr._jobs, {"logjob": {
                "status": "downloading", "progress": 40, "speed": "1 MB/s", "eta": 3,
                "title": "T", "thumb": "", "done": False, "error": None,
                "log": job["log"], "log_total": job["log_total"]}}):
            state = self.client.get("/api/progress-state/logjob?since=58").json
        self.assertEqual([line["t"] for line in state["logs"]], ["line 58", "line 59"])
        self.assertEqual(state["log_count"], 60)

    def test_download_jobs_are_pruned_instead_of_piling_up(self):
        self.create_account()
        user = "idlr-test@example.com"
        with (
            mock.patch.dict(idlr._jobs, {}, clear=True),
            mock.patch.object(idlr, "_ytdlp_run",
                              side_effect=lambda job, *a, **k: job.update(
                                  status="done", progress=100, done=True)),
        ):
            for index in range(idlr._JOBS_KEEP + 25):
                response = self.client.post("/api/download", json={
                    "url": f"https://example.com/{index}.mp4", "path": self.temp.name,
                    "user": user, "thumb": False,
                })
                self.assertEqual(response.status_code, 200)
            time.sleep(0.3)
            self.assertGreater(len(idlr._jobs), idlr._JOBS_KEEP)
            # Age every finished job past the TTL, then prune as a new job would.
            long_ago = time.time() - 3600
            idlr._jobs.update({jid: dict(job, done=True, ended_at=long_ago,
                                         created_at=long_ago)
                               for jid, job in idlr._jobs.items()})
            idlr._prune_jobs()
            self.assertLessEqual(len(idlr._jobs), idlr._JOBS_KEEP)

    def test_freshly_finished_jobs_are_not_pruned_while_the_ui_reads_them(self):
        """A job is marked done a moment before its thread stamps `ended_at`.

        Dating that window from 0 made the job look months old and it was dropped
        while the progress panel was still polling it — the download vanished
        mid-flight, which reads as "the app is broken".
        """
        with mock.patch.dict(idlr._jobs, {}, clear=True):
            for index in range(idlr._JOBS_KEEP + 30):
                idlr._jobs[f"recent{index}"] = {"done": True, "ended_at": 0,
                                                "created_at": time.time(), "log": []}
            idlr._prune_jobs()
            self.assertEqual(len(idlr._jobs), idlr._JOBS_KEEP + 30,
                             "jobs that just finished stay readable, over the cap or not")

            # A burst bigger than the hard ceiling is still bounded.
            for index in range(400):
                idlr._jobs[f"burst{index}"] = {"done": True, "ended_at": time.time(),
                                               "created_at": time.time(), "log": []}
            idlr._prune_jobs()
            self.assertLessEqual(len(idlr._jobs), idlr._JOBS_HARD_KEEP + idlr._JOBS_KEEP)

    def test_desktop_folder_picker_returns_selection_or_empty_on_cancel(self):
        api = idlr._DesktopApi()
        api.window = mock.Mock()
        fake_webview = SimpleNamespace(FileDialog=SimpleNamespace(FOLDER=20))
        with mock.patch.dict(idlr.sys.modules, {"webview": fake_webview}):
            api.window.create_file_dialog.return_value = [os.path.join(self.temp.name, "picked")]
            selected = api.select_folder(self.temp.name)
            self.assertEqual(selected["path"], os.path.normpath(os.path.join(self.temp.name, "picked")))
            api.window.create_file_dialog.assert_called_once_with(
                20,
                directory=self.temp.name,
                allow_multiple=False,
            )

            api.window.create_file_dialog.return_value = None
            self.assertEqual(api.select_folder(self.temp.name), {"path": ""})

    def test_desktop_server_uses_available_local_port(self):
        fake_webview = SimpleNamespace(create_window=mock.Mock(return_value=mock.Mock()), start=mock.Mock())
        fake_server = mock.Mock(server_port=43210)
        fake_thread = mock.Mock()
        with (
            mock.patch.dict(idlr.sys.modules, {"webview": fake_webview}),
            mock.patch("werkzeug.serving.make_server", return_value=fake_server) as make_server,
            mock.patch.object(idlr.threading, "Thread", return_value=fake_thread),
            mock.patch.object(idlr, "_print_startup"),
            mock.patch.object(idlr, "log"),
        ):
            idlr._run_desktop()

        make_server.assert_called_once_with("127.0.0.1", 0, idlr.app, threaded=True)
        self.assertEqual(idlr.SERVER_URL, "http://localhost:43210")
        fake_thread.start.assert_called_once_with()
        fake_server.shutdown.assert_called_once_with()
        fake_server.server_close.assert_called_once_with()

    def test_windowed_desktop_skips_console_startup_work(self):
        with (
            mock.patch.object(idlr.sys, "stdout", None),
            mock.patch.object(idlr, "local_ip", side_effect=AssertionError("console output is disabled")),
            mock.patch.object(idlr, "pick_temp_dir", side_effect=AssertionError("console output is disabled")),
        ):
            idlr._print_startup(43210, desktop=True)

    def test_profile_images_are_validated_and_keep_add_order(self):
        self.create_account()
        first = self.image_data(b"first")
        second = self.image_data(b"second")
        response = self.client.post("/api/profile", json={
            "user": "idlr-test@example.com",
            "avatar": first,
            "banner": second,
            "image_history": [
                {"data": first, "added": "2026-01-01 10:00"},
                {"data": second, "added": "2026-01-02 10:00"},
            ],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["avatar"], first)
        self.assertEqual(response.json["banner"], second)

        images = self.client.get("/api/profile/images?user=idlr-test%40example.com")
        self.assertEqual(images.status_code, 200)
        self.assertEqual([image["data"] for image in images.json["images"]], [first, second])

        invalid = self.client.post("/api/profile", json={
            "user": "idlr-test@example.com",
            "avatar": "data:text/html;base64,PHNjcmlwdD4=",
        })
        self.assertEqual(invalid.status_code, 400)

    def test_update_installs_only_digest_verified_wheel_and_marks_restart(self):
        wheel_buffer = io.BytesIO()
        version = "9999.0.0"
        with zipfile.ZipFile(wheel_buffer, "w") as wheel:
            wheel.writestr("yt_dlp/__init__.py", "__version__ = '9999.0.0'\n")
            wheel.writestr(
                f"yt_dlp-{version}.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: yt-dlp\nVersion: 9999.0.0\n",
            )
        wheel_data = wheel_buffer.getvalue()
        metadata = {
            "info": {"version": version},
            "urls": [{
                "filename": f"yt_dlp-{version}-py3-none-any.whl",
                "url": "https://files.pythonhosted.org/packages/test.whl",
                "digests": {"sha256": hashlib.sha256(wheel_data).hexdigest()},
            }],
        }

        update_root = os.path.join(self.temp.name, "packages")
        state_path = os.path.join(self.temp.name, "update.json")
        with (
            mock.patch.object(idlr.sys, "frozen", True, create=True),
            mock.patch.object(idlr, "_YTDLP_UPDATE_PACKAGES", update_root),
            mock.patch.object(idlr, "_YTDLP_UPDATE_STATE", state_path),
            mock.patch.object(idlr, "_PENDING_UPDATED_YTDLP", ""),
            mock.patch.object(idlr.urllib.request, "urlopen", side_effect=[
                io.BytesIO(json.dumps(metadata).encode("utf-8")),
                io.BytesIO(wheel_data),
            ]),
        ):
            response = self.client.post("/api/update-ytdlp")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["requires_restart"])
        self.assertEqual(response.json["version"], version)
        package_root = os.path.join(update_root, "yt-dlp-" + version)
        self.assertTrue(os.path.isfile(os.path.join(package_root, "yt_dlp", "__init__.py")))
        with open(state_path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["version"], version)

    def test_download_request_routes_spotify_and_generic_jobs_and_reports_progress(self):
        save_dir = os.path.join(self.temp.name, "downloads")

        def finish_job(job, *args, **kwargs):
            self.assertTrue(args)
            self.assertFalse(kwargs)
            job.update(status="done", progress=100, done=True)

        for url, runner in (
            ("https://example.com/video.mp4", "_ytdlp_run"),
            ("https://open.spotify.com/track/test", "_spotify_run"),
        ):
            with mock.patch.object(idlr, runner, side_effect=finish_job) as mocked:
                response = self.client.post("/api/download", json={
                    "url": url,
                    "path": save_dir,
                    "user": "idlr-test@example.com",
                    "thumb": False,
                })
                self.assertEqual(response.status_code, 200)
                job_id = response.json["job_id"]
                for _ in range(40):
                    if idlr._jobs[job_id]["done"]:
                        break
                    time.sleep(0.025)
                self.assertTrue(idlr._jobs[job_id]["done"])
                mocked.assert_called_once()
                progress = self.client.get(f"/api/progress-state/{job_id}")
                self.assertEqual(progress.status_code, 200)
                self.assertTrue(progress.json["done"])
                self.assertEqual(progress.json["status"], "done")


class DownloadProvenanceTests(unittest.TestCase):
    """History used to be able to say *what* was downloaded but never *where it
    went*, so the row had no "open it" action and no size — and any endpoint that
    took a path from the browser would be a way to open anything on the machine.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.downloads = os.path.join(self.temp.name, "Downloads")
        os.makedirs(self.downloads)
        self.history_file = os.path.join(self.temp.name, "history.json")
        self.client = idlr.app.test_client()
        patcher = mock.patch.object(idlr, "HIST_FILE", self.history_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.revealed = []
        fake = mock.patch.object(
            idlr, "_reveal_in_file_manager",
            side_effect=lambda path, select: self.revealed.append((path, select)) or (
                "file" if select and os.path.isfile(path) else "folder"))
        fake.start()
        self.addCleanup(fake.stop)
        self.dir_patch = mock.patch.object(idlr, "default_download_dir", lambda: self.downloads)
        self.dir_patch.start()
        self.addCleanup(self.dir_patch.stop)

    def write(self, name, size=1234):
        path = os.path.join(self.downloads, name)
        with open(path, "wb") as stream:
            stream.write(b"x" * size)
        return path

    def test_a_finished_download_records_its_file_and_size(self):
        # A real yt-dlp filename: punctuation folded to dashes, so nothing about
        # the title can be rebuilt by string math — the match has to be forgiving.
        self.write("One Kiss - Calvin Harris And Dua Lipa (JAYDEN OVERDRIVE Edit).mp4", 3_456_789)
        idlr._save_hist("u@e.com", "https://youtu.be/x",
                        "One Kiss - Calvin Harris And Dua Lipa (JAYDEN OVERDRIVE Edit)",
                        "", "Video", "1080p", "mp4", "done", folder=self.downloads)
        entry = json.load(open(self.history_file, encoding="utf-8"))[0]
        self.assertEqual(entry["folder"], self.downloads)
        self.assertTrue(entry["file"].endswith("(JAYDEN OVERDRIVE Edit).mp4"))
        self.assertEqual(entry["bytes"], 3_456_789)

    def test_no_file_is_claimed_when_the_folder_does_not_match(self):
        self.write("something else entirely.mp4")
        idlr._save_hist("u@e.com", "https://youtu.be/y", "A totally different title",
                        "", "Video", "1080p", "mp4", "done", folder=self.downloads)
        entry = json.load(open(self.history_file, encoding="utf-8"))[0]
        self.assertEqual(entry["folder"], self.downloads)
        self.assertNotIn("file", entry, "a wrong file is worse than no file: the row "
                                        "would open something the user never asked for")
        self.assertNotIn("bytes", entry)

    def test_reveal_uses_the_recorded_path(self):
        path = self.write("Track.mp3", 10)
        idlr._save_hist("u@e.com", "https://a/1", "Track", "", "Audio Only",
                        "320k", "mp3", "done", folder=self.downloads)
        response = self.client.post("/api/reveal", json={"user": "u@e.com", "url": "https://a/1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["kind"], "file")
        self.assertEqual(self.revealed, [(path, True)])

    def test_reveal_is_not_a_way_to_open_any_path(self):
        secret = os.path.join(self.temp.name, "secret")
        os.makedirs(secret)
        response = self.client.post("/api/reveal",
                                    json={"user": "u@e.com", "url": "https://nope", "folder": secret})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.revealed, [], "an unknown download must not reach the shell")

    def test_reveal_of_a_folder_idlr_actually_used_is_allowed(self):
        other = os.path.join(self.temp.name, "Music")
        os.makedirs(other)
        idlr._save_hist("u@e.com", "https://a/2", "Song", "", "Audio Only",
                        "320k", "mp3", "done", folder=other)
        response = self.client.post("/api/reveal", json={"user": "u@e.com", "url": "", "folder": other})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.revealed, [(other, False)])

    def test_reveal_of_a_deleted_file_reports_instead_of_lies(self):
        path = self.write("Gone.mp4", 5)
        idlr._save_hist("u@e.com", "https://a/3", "Gone", "", "Video",
                        "1080p", "mp4", "done", folder=self.downloads)
        os.remove(path)
        os.rmdir(self.downloads)
        response = self.client.post("/api/reveal", json={"user": "u@e.com", "url": "https://a/3"})
        self.assertEqual(response.status_code, 404)
        self.assertIn("can't find that file", response.json["error"])


class SetupIssueShapeTests(unittest.TestCase):
    def setUp(self):
        self.client = idlr.app.test_client()

    def test_report_issue_carries_the_reason_and_deduplicates(self):
        before = list(idlr.SETUP_PROBLEMS)
        self.addCleanup(idlr.SETUP_PROBLEMS.__setitem__, slice(None), before)
        idlr.SETUP_PROBLEMS[:] = []
        idlr.report_issue("spotdl", "Spotify's own downloader is unavailable",
                          "falls back to matching", "notice", "RuntimeError: bad pydantic")
        idlr.report_issue("spotdl", "Spotify's own downloader is unavailable",
                          "falls back to matching", "notice", "RuntimeError: bad pydantic")
        self.assertEqual(len(idlr.SETUP_PROBLEMS), 1,
                         "one broken package must not fill the banner with repeats")
        entry = idlr.SETUP_PROBLEMS[0]
        self.assertEqual(entry["severity"], "notice")
        self.assertIn("pydantic", entry["reason"],
                      "the exception text is the only thing that makes this fixable")

    def test_api_info_splits_notices_from_blocking_problems(self):
        before = list(idlr.SETUP_PROBLEMS)
        self.addCleanup(idlr.SETUP_PROBLEMS.__setitem__, slice(None), before)
        idlr.SETUP_PROBLEMS[:] = [
            {"package": "spotdl", "title": "t", "detail": "d", "severity": "notice", "reason": "r"},
        ]
        payload = self.client.get("/api/info").json
        self.assertEqual(payload["problems"][0]["severity"], "notice")
        # The UI decides banner vs dismissible notice from these keys alone.
        for key in ("version", "platform", "python", "data_dir"):
            self.assertIn(key, payload)


class PackagedSpotifyProbeTests(unittest.TestCase):
    """In the desktop build, `spotdl` is frozen into its own helper process.

    The app used to answer "is Spotify available?" by asking its *own* import
    machinery — `find_spec("spotdl")` at startup and `import spotdl` in the
    background check — and in a packaged build that is always false, so every
    installed user got the orange "Spotify's own downloader is unavailable" strip
    and the YouTube-match badge while Spotify downloads worked fine. Health has
    to be measured on the thing that does the work.
    """

    def setUp(self):
        # These tests patch subprocess.run, which is a module-level object shared
        # with the background startup thread: let that thread finish first, or it
        # can trip the "spawned nothing" assertion with work of its own.
        if not idlr._STARTUP_CHECKS.wait(15):
            self.skipTest("the startup check thread never finished")
        self.frozen = idlr.IS_FROZEN
        self.finder = idlr._find_bundled_binary
        self.broken = set(idlr.BROKEN_PKGS)
        self.problems = list(idlr.SETUP_PROBLEMS)
        idlr.BROKEN_PKGS.discard("spotdl")

        def restore():
            idlr.IS_FROZEN = self.frozen
            idlr._find_bundled_binary = self.finder
            idlr.BROKEN_PKGS.clear()
            idlr.BROKEN_PKGS.update(self.broken)
            while idlr.SETUP_PROBLEMS:
                idlr.SETUP_PROBLEMS.pop()
            idlr.SETUP_PROBLEMS.extend(self.problems)
        self.addCleanup(restore)

    def test_a_missing_helper_is_the_only_thing_that_can_call_spotdl_broken(self):
        idlr.IS_FROZEN = True
        idlr._find_bundled_binary = lambda name: None
        ok, reason = idlr._dependency_present("spotdl", "spotdl")
        self.assertFalse(ok)
        self.assertIn("IDLR-spotdl", reason)

    def test_the_startup_probe_imports_and_spawns_nothing(self):
        """It runs at import time, on a cold start, inside scanned files."""
        idlr.IS_FROZEN = True
        idlr._find_bundled_binary = lambda name: None      # helper quarantined, say
        with mock.patch.object(idlr.subprocess, "run") as run:
            ok, reason = idlr._dependency_present("spotdl", "spotdl")
        self.assertFalse(ok)
        self.assertTrue(reason)
        run.assert_not_called(), "even a missing helper must cost no subprocess"

    def test_a_working_helper_outranks_an_unimportable_package(self):
        idlr.IS_FROZEN = True
        idlr._find_bundled_binary = lambda name: "/opt/idlr/IDLR-spotdl"
        real_import = builtins.__import__

        def exploding_import(name, *args, **kwargs):
            if name == "spotdl":
                raise ModuleNotFoundError("No module named 'spotdl'")
            return real_import(name, *args, **kwargs)

        done = subprocess.CompletedProcess([], 0, "usage: spotdl [options]", "")
        with mock.patch("builtins.__import__", side_effect=exploding_import), \
                mock.patch.object(idlr.subprocess, "run", return_value=done) as run:
            self.assertEqual(idlr._dependency_present("spotdl", "spotdl"), (True, ""))
            self.assertEqual(idlr._bundled_spotdl_answers(timeout=20), (True, ""))
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["/opt/idlr/IDLR-spotdl", "--help"])
        self.assertIs(run.call_args[1].get("shell", False) if run.call_args else False, False,
                      "argv must stay a list: a helper name is not a shell command")

    def test_a_helper_that_cannot_run_reports_its_own_error(self):
        idlr.IS_FROZEN = True
        idlr._find_bundled_binary = lambda name: "/opt/idlr/IDLR-spotdl"
        done = subprocess.CompletedProcess([], 3, "", "ImportError: pydantic-core version mismatch")
        with mock.patch.object(idlr.subprocess, "run", return_value=done):
            ok, reason = idlr._bundled_spotdl_answers(timeout=20)
        self.assertFalse(ok)
        self.assertIn("pydantic-core", reason)     # the reason the user pastes into a bug report
        self.assertIn("exited 3", reason)

    def test_a_source_install_still_asks_the_import_path(self):
        idlr.IS_FROZEN = False
        idlr._find_bundled_binary = lambda name: None   # irrelevant when not bundled
        self.assertEqual(idlr._dependency_present("spotdl", "spotdl"), (True, ""))
        self.assertFalse(idlr._dependency_present("not-a-package", "not_a_package")[0])

    def test_the_engine_label_follows_the_probe_not_the_import(self):
        """`/api/info` is what paints the badge, so pin the whole chain."""
        idlr.BROKEN_PKGS.discard("spotdl")
        self.assertEqual(idlr.app.test_client().get("/api/info").get_json()["spotify_engine"],
                         "spotdl")
        idlr.BROKEN_PKGS.add("spotdl")
        self.assertEqual(idlr.app.test_client().get("/api/info").get_json()["spotify_engine"],
                         "youtube-fallback")
        idlr.BROKEN_PKGS.discard("spotdl")


if __name__ == "__main__":
    unittest.main()
