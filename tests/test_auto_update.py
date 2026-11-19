"""Manifest check, package validation and Windows handoff. No window."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from echo3d_bridge import auto_update as installer

MANIFEST = {"schema_version": 1, "id": "echo3d-bridge", "version": "0.3.0", "channel": "intl",
            "feature_release": {"version": "0.3.0", "notes": "上传更稳\n日志更清楚",
                                "release_url": "https://tripo-public.tripo3d.ai/plugins/echo3d-bridge/"},
            "packages": {"default": {"url": "https://tripo-public.tripo3d.ai/plugins/echo3d-bridge/Tripo3d_echo3d_Bridge-latest.zip",
                                    "sha256": "ab" * 32}}}


def package_bytes(version="0.3.0", exe=b"MZ-echo", extra=None, omit=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        if installer.EXE_NAME not in omit:
            archive.writestr(installer.EXE_NAME, exe)
        if installer.VERSION_NAME not in omit:
            archive.writestr(installer.VERSION_NAME, "__version__ = %r\n" % version)
        if extra:
            archive.writestr(*extra)
    return output.getvalue()


class Server:
    def __init__(self):
        self.routes = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                code, body = owner.routes.get(self.path, (404, b""))
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%s" % self.httpd.server_port

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ManifestTests(unittest.TestCase):
    def test_version_and_notes(self):
        self.assertEqual(installer.version("v0.2.2"), (0, 2, 2))
        with self.assertRaises(ValueError):
            installer.version("0.2")
        result = installer.resolve(MANIFEST, "0.2.2")
        self.assertTrue(result["available"])
        self.assertEqual(result["latest"], "0.3.0")
        self.assertEqual(result["url"], installer.latest_package_url())
        self.assertEqual(result["notes"], "上传更稳\n日志更清楚")
        self.assertTrue(result["auto_install"])
        same = installer.resolve(dict(MANIFEST, version="0.2.2"), "0.2.2")
        self.assertFalse(same["available"])
        self.assertFalse(same["notes"])

    def test_latest_zip_when_manifest_is_missing(self):
        self.assertEqual(installer.latest_package_url(),
                         "https://tripo-public.tripo3d.ai/plugins/echo3d-bridge/Tripo3d_echo3d_Bridge-latest.zip")

        def fake_fetch(url, local=False, method="GET"):
            if method == "HEAD":
                self.assertEqual(url, installer.latest_package_url())
                return "v0.3.0", "ab" * 32
            raise OSError("missing manifest")

        with tempfile.TemporaryDirectory() as folder, patch.object(installer, "fetch", side_effect=fake_fetch), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRIPO_UPDATE_URL", None)
            found = installer.check("0.2.2", folder)
            self.assertEqual(found["url"], installer.latest_package_url())
            self.assertEqual(found["latest"], "0.3.0")
            self.assertEqual(found["sha256"], "ab" * 32)
            self.assertTrue(found["auto_install"])
            cached = installer.check("0.2.2", folder)
            self.assertTrue(cached["cached"])
            self.assertEqual(cached["url"], installer.latest_package_url())

    def test_identity_digest_and_host(self):
        broken = dict(MANIFEST, id="other")
        with self.assertRaises(ValueError):
            installer.resolve(broken, "0.2.2")
        package = dict(MANIFEST["packages"]["default"], sha256="abcd")
        with self.assertRaises(ValueError):
            installer.resolve(dict(MANIFEST, packages={"default": package}), "0.2.2")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRIPO_UPDATE_URL", None)
            remote = dict(MANIFEST["packages"]["default"], url="http://127.0.0.1/app.zip")
            with self.assertRaises(ValueError):
                installer.resolve(dict(MANIFEST, packages={"default": remote}), "0.2.2")

    def test_local_check_cache_and_disable(self):
        server = Server()
        self.addCleanup(server.close)
        body = package_bytes()
        digest = hashlib.sha256(body).hexdigest()
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["packages"]["default"]["url"] = server.base + "/app.zip"
        manifest["packages"]["default"]["sha256"] = digest
        server.routes["/manifest.json"] = (200, json.dumps(manifest).encode())
        server.routes["/app.zip"] = (200, body)
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {"TRIPO_UPDATE_URL": server.base + "/manifest.json"}):
            os.environ.pop("TRIPO_UPDATE_CHECK", None)
            found = installer.check("0.2.2", folder)
            self.assertTrue(found["available"])
            self.assertFalse(found.get("cached"))
            cached = installer.check("0.2.2", folder)
            self.assertTrue(cached["cached"])
            server.routes["/manifest.json"] = (500, b"")
            self.assertTrue(installer.check("0.2.2", folder, force=False)["cached"])
            with patch.dict(os.environ, {"TRIPO_UPDATE_CHECK": "0"}):
                self.assertEqual(installer.check("0.2.2", folder), {"disabled": True})
            stage = installer.prepare(found, folder, threading.Event())
            self.addCleanup(lambda: installer.cleanup(stage) if Path(stage["root"]).exists() else None)
            self.assertEqual(Path(stage["exe"]).read_bytes(), b"MZ-echo")
            self.assertEqual((Path(stage["root"]) / "version.txt").read_text(encoding="utf-8"),
                             "__version__ = '0.3.0'\n")

    def test_bad_package_is_not_extracted(self):
        server = Server()
        self.addCleanup(server.close)
        cases = [
            package_bytes(extra=("../Tripo-Echo3D-Bridge.exe", b"outside")),
            package_bytes(version="0.2.1"),
            package_bytes(extra=("note.txt", b"hi")),
            package_bytes(exe=b""),
        ]
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {"TRIPO_UPDATE_URL": server.base + "/manifest.json"}):
            os.environ.pop("TRIPO_UPDATE_CHECK", None)
            for index, body in enumerate(cases):
                server.routes["/pkg-%s.zip" % index] = (200, body)
                update = dict(installer.resolve(MANIFEST, "0.2.2"),
                              url=server.base + "/pkg-%s.zip" % index,
                              sha256=hashlib.sha256(body).hexdigest())
                with self.assertRaises(ValueError):
                    installer.prepare(update, folder, threading.Event())
            outside = Path(folder).parent / "Tripo-Echo3D-Bridge.exe"
            self.assertFalse(outside.exists())
            body = package_bytes()
            server.routes["/bad-digest.zip"] = (200, body)
            update = dict(installer.resolve(MANIFEST, "0.2.2"),
                          url=server.base + "/bad-digest.zip", sha256="cd" * 32)
            with self.assertRaises(ValueError):
                installer.prepare(update, folder, threading.Event())


class LifecycleTests(unittest.TestCase):
    def _updater(self, folder, busy=None, restarted=None):
        callback = (lambda: restarted.append(None)) if restarted is not None else (lambda: None)
        return installer.Updater("0.2.2", folder, is_busy=busy or (lambda: False), on_restart=callback)

    def _wait(self, updater, predicate):
        deadline = time.time() + 2
        while time.time() < deadline and not predicate():
            updater.poll()
            time.sleep(0.01)
        updater.poll()

    def test_manual_check_arms_install(self):
        notice = {"available": True, "latest": "0.3.0", "auto_install": True, "current": "0.2.2"}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(installer, "check", return_value=dict(notice)):
            updater = self._updater(folder)
            updater.start_check(manual=False)
            self._wait(updater, lambda: updater.status["checked"])
            self.assertFalse(updater.update_ready)
            self.assertTrue(updater.request_check())
            self._wait(updater, lambda: updater.status["checked"] and updater.status["manual"])
            self.assertTrue(updater.update_ready)
            display = installer.update_display(status=updater.status, state=updater.state, notice=updater.notice,
                                              update_ready=True, can_install=False)
            self.assertEqual(display.label, "Update available: v0.3.0")
            self.assertEqual(display.hint, "New version")
            self.assertEqual(display.icon, "refresh")
            ready = installer.update_display(status=updater.status, state=updater.state, notice=updater.notice,
                                            update_ready=True, can_install=True)
            self.assertEqual(ready.label, "Install v0.3.0")
            self.assertEqual(ready.icon, "download")

    def test_source_checkout_is_not_installed(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(installer, "packaged_executable", return_value=None):
            updater = self._updater(folder)
            self.assertFalse(updater.start_install())
            self.assertEqual(updater.state["phase"], "failed")
            self.assertIn("packaged Windows app", updater.state["error"])

    def test_busy_bridge_blocks_install(self):
        with tempfile.TemporaryDirectory() as folder:
            updater = self._updater(folder, busy=lambda: True)
            with patch.object(installer, "packaged_executable", return_value=Path(folder) / "app.exe"):
                self.assertFalse(updater.start_install())
            self.assertIn("transfer", updater.state["error"])

    def test_install_replaces_packaged_exe_and_restarts(self):
        restarted = []
        with tempfile.TemporaryDirectory() as folder:
            current = Path(folder) / "Tripo-Echo3D-Bridge.exe"
            current.write_bytes(b"old")
            root = Path(folder) / ".tripo-stage-test"
            root.mkdir()
            new = root / installer.EXE_NAME
            new.write_bytes(b"new")
            stage = {"root": str(root), "parent": folder, "exe": str(new), "version": "0.3.0"}
            notice = {"available": True, "latest": "0.3.0", "auto_install": True,
                      "current": "0.2.2", "channel": "intl"}
            updater = self._updater(folder, restarted=restarted)
            with patch.object(installer, "check", return_value=dict(notice)), \
                    patch.object(installer, "prepare", return_value=stage), \
                    patch.object(installer, "packaged_executable", return_value=current), \
                    patch.object(installer, "schedule_restart") as schedule:
                self.assertTrue(updater.start_install())
                self._wait(updater, lambda: updater.installed)
            self.assertTrue(updater.installed)
            schedule.assert_called_once_with(new, current)
            self.assertEqual(restarted, [None])
            self.assertTrue(root.exists())

    def test_connection_during_download_keeps_the_old_app(self):
        with tempfile.TemporaryDirectory() as folder:
            current = Path(folder) / "Tripo-Echo3D-Bridge.exe"
            current.write_bytes(b"old")
            root = Path(folder) / ".tripo-stage-busy"
            root.mkdir()
            (root / installer.EXE_NAME).write_bytes(b"new")
            stage = {"root": str(root), "parent": folder, "exe": str(root / installer.EXE_NAME), "version": "0.3.0"}
            busy = {"value": False}

            def prepare(*_args, **_kwargs):
                busy["value"] = True
                return stage

            updater = self._updater(folder, busy=lambda: busy["value"])
            notice = {"available": True, "latest": "0.3.0", "auto_install": True, "current": "0.2.2", "channel": "intl"}
            with patch.object(installer, "check", return_value=dict(notice)), \
                    patch.object(installer, "prepare", side_effect=prepare), \
                    patch.object(installer, "packaged_executable", return_value=current), \
                    patch.object(installer, "schedule_restart") as schedule:
                self.assertTrue(updater.start_install())
                self._wait(updater, lambda: updater.state.get("phase") == "failed")
            schedule.assert_not_called()
            self.assertFalse(root.exists())
            self.assertEqual(current.read_bytes(), b"old")

    def test_shutdown_discards_a_staged_package(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / ".tripo-stage-drop"
            root.mkdir()
            (root / installer.EXE_NAME).write_bytes(b"new")
            stage = {"root": str(root), "parent": folder, "exe": str(root / installer.EXE_NAME)}
            updater = self._updater(folder)
            updater._install_token = threading.Event()
            updater._messages.put(("ready", updater._install_token, (stage, {})))
            updater.shutdown()
            self.assertFalse(root.exists())

    def test_restart_script_quotes_paths_and_waits(self):
        script = installer.restart_script(41, r"C:\O'Brien\Tripo-Echo3D-Bridge.exe",
                                         r"C:\Program Files\Tripo-Echo3D-Bridge.exe")
        self.assertIn("$processId = 41", script)
        self.assertIn("Wait-Process -Id $processId", script)
        self.assertIn(r"'C:\O''Brien\Tripo-Echo3D-Bridge.exe'", script)
        self.assertIn(r"'C:\Program Files\Tripo-Echo3D-Bridge.exe'", script)
        self.assertIn("Start-Process -FilePath $dest", script)
        with tempfile.TemporaryDirectory() as folder:
            new = Path(folder) / installer.EXE_NAME
            dest = Path(folder) / "installed.exe"
            new.write_bytes(b"new")
            dest.write_bytes(b"old")
            with patch("echo3d_bridge.auto_update.subprocess.Popen") as popen:
                installer.schedule_restart(new, dest)
            popen.assert_called_once()
            written = (new.with_name("replace.ps1")).read_text(encoding="utf-8-sig")
            self.assertIn("Wait-Process", written)
            self.assertIn(str(new), written)


class DisplayTests(unittest.TestCase):
    def test_footer_states(self):
        checking = installer.update_display(status={"checking": True}, state={}, notice={}, update_ready=False)
        self.assertEqual(checking.label, "Checking for updates…")
        self.assertTrue(checking.busy)
        failed = installer.update_display(status={"checked": True}, state={"phase": "failed"},
                                          notice={"error": "offline"}, update_ready=False)
        self.assertEqual(failed.label, "Update failed. Click to retry")
        blocked = installer.update_display(status={}, state={}, notice={"available": True, "latest": "0.3.0"},
                                           update_ready=True, transferring=True, can_install=True)
        self.assertEqual(blocked.label, "Finish the current transfer before updating")
        self.assertTrue(blocked.busy)
        self.assertEqual(blocked.hint, "New version")
