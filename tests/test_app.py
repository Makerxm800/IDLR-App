import base64
import hashlib
import io
import json
import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
