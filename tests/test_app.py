import base64
import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import app as idlr

ROOT = Path(__file__).resolve().parents[1]


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
        # This assertion used to pin "Cannot create save folder", which is what the
        # endpoint said when the name existed but was a *file* — `makedirs
        # (exist_ok=True)` raises FileExistsError there, so the message blamed the
        # PC for a typo. The case now has its own sentence; the folder-creation
        # failure keeps the one below it in FolderErrorWordingTests.
        self.assertIn("is a file, not a folder", invalid.json["error"])

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

    def test_desktop_fallback_does_not_automatically_open_a_browser(self):
        with (
            mock.patch.dict(idlr.sys.modules, {"webview": None}),
            mock.patch.object(idlr, "_serve_in_browser") as serve,
            mock.patch.object(idlr, "log"),
        ):
            idlr._run_desktop()
        serve.assert_called_once_with("127.0.0.1", 5000, auto_open=False)

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


class DirectMediaDownloadTests(unittest.TestCase):
    def test_signed_hls_and_direct_video_urls_keep_their_media_extension(self):
        self.assertEqual(
            idlr._direct_media_extension("https://cdn.example/master.M3U8?token=x#t=15"),
            ".m3u8",
        )
        self.assertEqual(
            idlr._direct_media_kind("https://cdn.example/master.m3u8?token=x#t=15"),
            "hls",
        )
        self.assertEqual(
            idlr._direct_media_kind("https://cdn.example/movie.MKV?sig=x"),
            "video",
        )
        self.assertFalse(idlr.is_hls_or_direct("https://example.com/watch/movie"))

    def test_hls_download_honors_quality_and_remuxes_to_the_requested_container(self):
        options = {
            "format": "old selector",
            "merge_output_format": "mp4",
            "writethumbnail": True,
            "postprocessors": [{"key": "EmbedThumbnail"}],
        }
        configured = idlr._configure_direct_media_opts(
            options, "hls", ".m3u8", False, "mkv", "720p")

        self.assertTrue(configured)
        self.assertEqual(
            options["format"],
            "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        )
        self.assertEqual(options["merge_output_format"], "mkv")
        self.assertEqual(options["postprocessors"], [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"},
        ])
        self.assertFalse(options["writethumbnail"])

    def test_direct_hls_audio_keeps_audio_extraction_instead_of_becoming_video(self):
        options = {
            "format": "bestaudio",
            "merge_output_format": "mp4",
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3"},
                {"key": "EmbedThumbnail"},
            ],
        }
        idlr._configure_direct_media_opts(
            options, "hls", ".m3u8", True, "mp3", "320k")

        self.assertEqual(options["format"], "best")
        self.assertNotIn("merge_output_format", options)
        self.assertEqual(options["postprocessors"], [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3"},
        ])
        self.assertFalse(options["writethumbnail"])

    def test_direct_file_avoids_a_needless_remux_and_thumbnail_postprocessor(self):
        options = {
            "format": "old selector",
            "writethumbnail": True,
            "postprocessors": [{"key": "EmbedThumbnail"}],
        }
        idlr._configure_direct_media_opts(
            options, "video", ".mp4", False, "mp4", "1080p")

        self.assertEqual(options["format"], "best")
        self.assertEqual(options["merge_output_format"], "mp4")
        self.assertEqual(options["postprocessors"], [])
        self.assertFalse(options["writethumbnail"])

    def test_ytdlp_hooks_record_the_final_file_for_history_without_guessing(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as elsewhere:
            source = os.path.join(folder, "movie.ts")
            final = os.path.join(folder, "movie.mp4")
            outside = os.path.join(elsewhere, "unrelated.mp4")
            for path in (source, final, outside):
                Path(path).write_bytes(b"video")

            # _job_output_file answers with the *resolved* path (realpath), so
            # compare against that form: on macOS tempfile lives under /var,
            # which is a symlink to /private/var, and on Windows CI the temp
            # root is an 8.3 short name — both spellings name the same file,
            # but only the resolved one can come back.
            job = {}
            idlr._hook(job, {"status": "finished", "filename": source})
            self.assertEqual(idlr._job_output_file(job, folder), os.path.realpath(source))
            idlr._postprocessor_hook(job, {
                "status": "finished", "info_dict": {"filepath": final},
            })
            self.assertEqual(idlr._job_output_file(job, folder), os.path.realpath(final))
            job["file_path"] = outside
            self.assertEqual(idlr._job_output_file(job, folder), "",
                             "a history row must never point outside its download folder")


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
        self.accounts_file = os.path.join(self.temp.name, "accounts.json")
        self.client = idlr.app.test_client()
        patcher = mock.patch.object(idlr, "HIST_FILE", self.history_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        accounts = mock.patch.object(idlr, "ACCS_FILE", self.accounts_file)
        accounts.start()
        self.addCleanup(accounts.stop)
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

    def save_folder_of(self, email, path):
        """Write the account the way /api/prefs leaves it: `savepath`, verbatim."""
        with open(self.accounts_file, "w", encoding="utf-8") as stream:
            json.dump({email: {"email": email, "name": "T",
                               "prefs": {"mode": "Video", "savepath": path}}}, stream)

    def test_a_folder_chosen_in_settings_can_be_opened(self):
        # The bug this guard holds shut: the allow-list compared the path the
        # screen was showing with the path history had stored as raw strings, so
        # `%USERPROFILE%\Music`, `~/Music` and `/home/me/Music` were three
        # different folders. The button in front of the user's own save path then
        # answered "that download is not in your history" — a command that looked
        # like it opened a folder and opened nothing.
        music = os.path.join(self.temp.name, "Music")
        os.makedirs(music)
        self.save_folder_of("u@e.com", music)
        indirect = os.path.join(self.temp.name, "Downloads", "..", "Music")
        self.assertNotEqual(indirect, music)
        response = self.client.post("/api/reveal",
                                    json={"user": "u@e.com", "url": "", "folder": indirect})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.revealed, [(music, False)])

    def test_a_refusal_from_the_shell_does_not_dump_the_path(self):
        # This is the message the toast prints verbatim, in a corner of the screen
        # about 380px wide. The path belongs in the log line beside it; pasted into
        # the toast it buries the one sentence that explains what happened.
        music = os.path.join(self.temp.name, "Music")
        os.makedirs(music)
        saved = self.write("Song.mp3", 20)
        self.save_folder_of("u@e.com", music)
        idlr._save_hist("u@e.com", "https://a/11", "Song", "", "Audio Only",
                        "320k", "mp3", "done", folder=music)
        # Both branches: the named history entry, and the folder-only fallback.
        # They are separate returns, and only one of them ever gets edited.
        for body in ({"user": "u@e.com", "url": "https://a/11"},
                     {"user": "u@e.com", "url": "", "folder": music}):
            with self.subTest(body=body), mock.patch.object(
                    idlr, "_reveal_in_file_manager",
                    side_effect=OSError(2, "No such file or directory")):
                response = self.client.post("/api/reveal", json=body)
            self.assertEqual(response.status_code, 500)
            self.assertIn("file manager", response.json["error"])
            for leaked in (self.temp.name, saved, music):
                self.assertNotIn(leaked, response.json["error"],
                                 "the raw path leaked into a user-facing message")

    def test_a_folder_spelled_with_a_home_prefix_is_the_same_folder(self):
        # The screen can hold `~/Music` while history and preferences hold the
        # absolute `/home/me/Music`. `realpath` alone does not close that gap, and
        # on Windows the same trap catches `%USERPROFILE%\Music`, which
        # `expanduser` never expands at all — so the comparison has to go through
        # the same expansion the writers used, on both sides.
        music = os.path.join(self.temp.name, "Music")
        os.makedirs(music)
        idlr._save_hist("u@e.com", "https://a/9", "Song", "", "Audio Only",
                        "320k", "mp3", "done", folder=music)
        home_var = "USERPROFILE" if os.name == "nt" else "HOME"
        with mock.patch.dict(os.environ, {home_var: self.temp.name}):
            response = self.client.post("/api/reveal",
                                        json={"user": "u@e.com", "url": "", "folder": "~/Music"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.revealed, [(music, False)])

    def test_the_history_header_opens_the_accounts_own_folder(self):
        # "Open the download folder" names no download, so what it should open is
        # the folder this account saves into — not whatever ~/Downloads means.
        music = os.path.join(self.temp.name, "Music")
        os.makedirs(music)
        self.save_folder_of("u@e.com", music)
        response = self.client.post("/api/reveal", json={"user": "u@e.com", "url": ""})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.revealed, [(music, False)])
        self.assertEqual(response.json["path"], music)

    def test_a_refused_folder_says_what_the_app_will_open(self):
        secret = os.path.join(self.temp.name, "secret")
        os.makedirs(secret)
        response = self.client.post("/api/reveal",
                                    json={"user": "u@e.com", "url": "", "folder": secret})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.revealed, [], "the refusal has to happen before the shell")
        self.assertIn("folders it saves into", response.json["error"],
                      "a bare denial reads as a broken button; the message has to name "
                      "the two places the app will open")

    def test_a_missing_save_folder_is_reported_not_opened(self):
        self.save_folder_of("u@e.com", os.path.join(self.temp.name, "gone"))
        response = self.client.post("/api/reveal", json={"user": "u@e.com", "url": ""})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.revealed, [])
        self.assertIn("does not exist any more", response.json["error"])

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


class EngineFailureClassificationTests(unittest.TestCase):
    """Which spotdl failures are news about IDLR, and which are news about the song.

    Every failure already falls back to a YouTube match, so a banner has to be
    earned: `ERROR: This video is private` must not become "Spotify's own
    downloader is unavailable" — that is the wrong-warning mistake all over again.
    """

    def test_only_an_engine_problem_reads_as_an_engine_problem(self):
        for text in ("Traceback (most recent call last):\n  File \"spotdl/core.py\"",
                     "pydantic_core._pydantic_core.ValidationError: 1 validation error",
                     "ModuleNotFoundError: No module named 'spotdl'",
                     "Fatal Python error: init_import_site: Failed to import the site module"):
            self.assertTrue(idlr._looks_like_engine_failure(text), text)

    def test_a_per_track_error_stays_a_per_track_error(self):
        for text in ("ERROR: This video is private",
                     "ERROR: [youtube] HTTP Error 403: Forbidden",
                     "ERROR: Unable to download JSON metadata: 404",
                     "Spotify is not available in your country",
                     "", None):
            self.assertFalse(idlr._looks_like_engine_failure(text), str(text))


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
        """It runs at import time, on a cold start, inside files AV is scanning."""
        idlr.IS_FROZEN = True
        idlr._find_bundled_binary = lambda name: None      # helper quarantined, say
        with mock.patch.object(idlr.subprocess, "run") as run:
            ok, reason = idlr._dependency_present("spotdl", "spotdl")
        self.assertFalse(ok)
        self.assertTrue(reason)
        run.assert_not_called(), "even a missing helper must cost no subprocess"

    def test_the_background_check_neither_imports_nor_spawns_for_spotdl(self):
        """Presence is all the packaged check may ask for.

        A deeper check — importing the package here, or running the onefile helper
        to print --help — either reports a false answer or unpacks ~150 MB into
        %TEMP% on every launch. That cost bought a badge; a real download pays it
        anyway, and reports the truth.
        """
        idlr.IS_FROZEN = True
        idlr._find_bundled_binary = lambda name: "/opt/idlr/IDLR-spotdl"
        real_import = builtins.__import__
        attempted = []

        def guarded_import(name, *args, **kwargs):
            if name == "spotdl":
                attempted.append(name)
                raise ModuleNotFoundError("No module named 'spotdl'")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import), \
                mock.patch.object(subprocess, "run") as run:
            idlr._finish_startup_checks()
        self.assertEqual(attempted, [], "the packaged check must not import spotdl here")
        run.assert_not_called()
        self.assertNotIn("spotdl", idlr.BROKEN_PKGS)
        self.assertEqual([problem for problem in idlr.SETUP_PROBLEMS
                          if isinstance(problem, dict) and problem.get("package") == "spotdl"],
                         [], "a healthy helper must not leave a notice behind")

    def test_a_present_helper_counts_as_healthy_however_it_is_laid_out(self):
        idlr.IS_FROZEN = True
        for found in ("/opt/idlr/binaries/IDLR-spotdl", "/opt/idlr/IDLR-spotdl"):
            idlr._find_bundled_binary = lambda name, path=found: path
            self.assertEqual(idlr._dependency_present("spotdl", "spotdl"), (True, ""))

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


class IssueLifetimeTests(unittest.TestCase):
    """`report_issue` only ever grows, so a one-off failure had to be out-voted.

    The Spotify notice is raised from a *single* failed download (a helper that
    prints a traceback), which is right for that moment and wrong forever after:
    a session that then finished twenty Spotify links would still claim "Spotify's
    own downloader is unavailable" — the stale-alarm mistake this UI was rebuilt
    to stop making.
    """

    def setUp(self):
        self.problems = list(idlr.SETUP_PROBLEMS)
        self.broken = set(idlr.BROKEN_PKGS)

        def restore():
            while idlr.SETUP_PROBLEMS:
                idlr.SETUP_PROBLEMS.pop()
            idlr.SETUP_PROBLEMS.extend(self.problems)
            idlr.BROKEN_PKGS.clear()
            idlr.BROKEN_PKGS.update(self.broken)
        self.addCleanup(restore)

    def raise_one(self):
        idlr.SETUP_PROBLEMS[:] = []
        idlr.BROKEN_PKGS.add("spotdl")
        idlr.report_issue("spotdl", "Spotify's own downloader is unavailable",
                          "matched on YouTube instead", "notice",
                          "RuntimeError: Traceback (most recent call last)")

    def test_a_healthy_download_clears_the_notice(self):
        self.raise_one()
        idlr.clear_issue("spotdl")
        self.assertEqual(idlr.SETUP_PROBLEMS, [])

    def test_clearing_only_touches_the_package_that_recovered(self):
        self.raise_one()
        idlr.report_issue("flask", "flask is required but broken", "cannot start", "blocking")
        idlr.clear_issue("spotdl")
        self.assertEqual([problem["package"] for problem in idlr.SETUP_PROBLEMS], ["flask"],
                         "a Spotify success says nothing about a missing required package")

    def test_clearing_nothing_is_quiet(self):
        idlr.SETUP_PROBLEMS[:] = []
        idlr.clear_issue("spotdl")
        self.assertEqual(idlr.SETUP_PROBLEMS, [])

    def test_the_title_can_be_targeted(self):
        self.raise_one()
        idlr.clear_issue("spotdl", "some other complaint")
        self.assertEqual(len(idlr.SETUP_PROBLEMS), 1, "an unrelated title must not clear this")
        idlr.clear_issue("spotdl", "Spotify's own downloader is unavailable")
        self.assertEqual(idlr.SETUP_PROBLEMS, [])

    def test_it_is_the_successful_run_that_clears_it(self):
        """Placement matters: clearing from anywhere makes the notice meaningless."""
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        body = source.split("def _spotify_run(")[1].split("\ndef _spotify_yt_fallback")[0]
        self.assertIn('clear_issue("spotdl")', body)
        before = body.split('clear_issue("spotdl")')[0]
        self.assertIn("_save_hist(", before,
                      "the clear belongs after the download is confirmed and recorded")
        self.assertEqual(body.count('clear_issue("spotdl")'), 1,
                         "exactly one clear, and only on the path that saved a file")
        success = body.split("if ok:")[1].split("tail =")[0]
        self.assertIn('clear_issue("spotdl")', success,
                      "clearing it anywhere else makes the notice meaningless")

    def test_a_per_track_error_never_reaches_the_notice_in_the_first_place(self):
        self.assertFalse(idlr._looks_like_engine_failure("ERROR: This video is private"))
        self.assertTrue(idlr._looks_like_engine_failure(
            "Traceback (most recent call last):\n  ImportError: No module named 'ytmusicapi'"))


if __name__ == "__main__":
    unittest.main()


class FolderErrorWordingTests(unittest.TestCase):
    """What the app says when a folder cannot be used.

    These strings are the whole explanation at the moment someone is trying to
    pick a save folder, so they have to read as a sentence from the app — the
    errno belongs after the reason, not in place of it. `validate-save-path` is
    what the folder dialog shows them in, so the promise is checked through the
    endpoint rather than against the helper.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = idlr.app.test_client()

    def post(self, path):
        return self.client.post("/api/validate-save-path", json={"path": path})

    def test_a_file_is_refused_as_a_file(self):
        a_file = os.path.join(self.temp.name, "not-a-folder.txt")
        with open(a_file, "w", encoding="utf-8") as stream:
            stream.write("x")
        response = self.post(a_file)
        self.assertEqual(response.status_code, 400)
        self.assertIn("is a file, not a folder", response.json["error"])

    def test_a_missing_parent_folder_is_created_rather_than_mourned(self):
        wanted = os.path.join(self.temp.name, "Music", "IDLR")
        response = self.post(wanted)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(os.path.isdir(wanted), "the probe-write is the promise")

    def test_the_technical_reason_survives_the_plain_one(self):
        # The friendly clause leads, but the exception text is what a bug report
        # needs, so neither half may be dropped.
        with mock.patch.object(idlr.os, "makedirs",
                               side_effect=PermissionError(13, "Permission denied")):
            response = self.post(os.path.join(self.temp.name, "nope"))
        self.assertEqual(response.status_code, 400)
        error = response.json["error"]
        self.assertIn("will not let IDLR write there", error)
        self.assertIn("PermissionError", error)
        self.assertLess(error.index("will not let"), error.index("PermissionError"),
                        "the reason has to come before the traceback-shaped tail")
