"""Desktop lifecycle preserves controller identity and only stops its own service."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from expman import common, desktop
from expman.hub import Hub
from tests.support import temporary_directory


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__()) / "controller"
        self.thread = None
        self.nonce = None
        self.errors = []

    def tearDown(self):
        if self.thread and self.thread.is_alive():
            process = common.read_json(self.root / "desktop-process.json", {})
            common.atomic_json(self.root / "desktop-stop.json", {"nonce": self.nonce or process.get("nonce")})
            self.thread.join(5)
        self.temporary.__exit__(None, None, None)

    def serve(self):
        def run():
            try:
                desktop.controller_serve(self.root, 0, "127.0.0.1")
            except Exception as error:
                self.errors.append(error)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = desktop.controller_status(self.root)
            if status["running"]:
                self.nonce = common.read_json(self.root / "desktop-process.json")["nonce"]
                return status
            if self.errors:
                raise self.errors[0]
            time.sleep(0.01)
        self.fail("Desktop controller did not start")

    def test_ephemeral_port_authenticated_status_and_owned_stop(self):
        status = self.serve()
        self.assertGreater(status["port"], 0)
        self.assertTrue(status["managed"])
        settings = common.read_json(self.root / "launcher.json")
        self.assertEqual(status["port"], settings["port"])
        url = "http://127.0.0.1:" + str(status["port"]) + "/api/state"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(urllib.error.HTTPError) as denied:
            opener.open(url, timeout=2)
        self.assertEqual(denied.exception.code, 401)
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1"}):
            self.assertTrue(desktop.controller_status(self.root)["running"])
        common.atomic_json(self.root / "desktop-stop.json", {"nonce": "other-service"})
        time.sleep(0.35)
        self.assertTrue(self.thread.is_alive())
        # A malformed request must not destroy the stop monitor.
        (self.root / "desktop-stop.json").write_text("{incomplete", encoding="utf-8")
        time.sleep(0.35)
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(desktop.controller_stop(self.root)["status"], "stopping")
        self.thread.join(4)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])
        self.assertFalse((self.root / "desktop-process.json").exists())
        self.assertFalse((self.root / "desktop-stop.json").exists())
        self.assertFalse(desktop.controller_status(self.root)["running"])
        self.assertTrue((self.root / "hub.sqlite3").is_file())

    def test_open_existing_controller_does_not_spawn_duplicate_or_change_identity(self):
        state = self.serve()
        before = common.read_json(self.root / "hub.json")
        with patch("expman.desktop.subprocess.Popen") as start:
            reopened = desktop.controller_start(self.root, port=state["port"])
        start.assert_not_called()
        self.assertTrue(reopened["running"])
        self.assertEqual(common.read_json(self.root / "hub.json"), before)

    def test_update_status_and_cooperative_stop_leave_controller_data_intact(self):
        self.serve()
        identity = common.read_json(self.root / 'hub.json')
        self.assertTrue(desktop.controller_update_status(self.root)['ready_for_update'])
        result = desktop.controller_stop_for_update(self.root)
        self.assertTrue(result['ready_for_update'])
        self.assertEqual(result['status'], 'stopping')
        self.thread.join(4)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])
        self.assertFalse(desktop.controller_status(self.root)['running'])
        self.assertTrue(desktop.controller_update_status(self.root)['ready_for_update'])
        self.assertEqual(common.read_json(self.root / 'hub.json'), identity)

    def test_status_refreshes_owner_published_while_health_probe_waits(self):
        common.atomic_json(self.root / "hub.json", {"admin_token": "test-only-token"})
        common.atomic_json(self.root / "launcher.json", {"port": 8765})
        def became_ready(*args, **kwargs):
            common.atomic_json(self.root / "desktop-process.json", {"nonce": "newly-ready"})
            return io.BytesIO(b'{"nodes":[],"jobs":[]}')
        with patch("expman.desktop.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = became_ready
            status = desktop.controller_status(self.root)
        self.assertTrue(status["running"])
        self.assertTrue(status["managed"])

    def test_stop_refuses_controller_without_desktop_ownership(self):
        self.root.mkdir()
        with patch("expman.desktop.controller_status", return_value={"running": True, "status": "running"}):
            with self.assertRaisesRegex(ValueError, "旧启动入口"):
                desktop.controller_stop(self.root)
        self.assertFalse((self.root / "desktop-stop.json").exists())

    def test_cleanup_leaves_replacement_owner_record_untouched(self):
        self.serve()
        replacement = {"pid": 123, "nonce": "replacement-owner"}
        common.atomic_json(self.root / "desktop-process.json", replacement)
        common.atomic_json(self.root / "desktop-stop.json", {"nonce": self.nonce})
        self.thread.join(4)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(common.read_json(self.root / "desktop-process.json"), replacement)

    def test_worker_release_cannot_launch_or_control_controller(self):
        actual = desktop.read_json
        def read(path, default=None):
            if Path(path).name == "release-role.json":
                return {"role": "windows-worker-x64"}
            return actual(path, default)
        for action in ("controller-start", "controller-open", "controller-stop", "controller-serve", "controller-status"):
            with self.subTest(action=action), patch("expman.desktop.read_json", side_effect=read), \
                    patch("sys.argv", ["desktop", action, "--root", str(self.root)]), \
                    patch("expman.desktop.controller_start") as start, contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as error:
                    desktop.main()
                self.assertEqual(error.exception.code, 1)
                self.assertIn("worker edition", json.loads(output.getvalue())["detail"])
                start.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_native_picker_uses_constant_script_and_preserves_unicode_path(self):
        selected = self.root.parent / "算法 source"
        result = subprocess.CompletedProcess([], 0, stdout="\ufeff" + str(selected), stderr="")
        with patch("expman.desktop.sys.platform", "win32"), \
                patch("expman.desktop.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch("expman.desktop.subprocess.run", return_value=result) as run:
            self.assertEqual(desktop.choose_directory(), str(selected.resolve()))
        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[:4], ["powershell.exe", "-NoProfile", "-STA", "-EncodedCommand"])
        script = base64.b64decode(command[4]).decode("utf-16-le")
        self.assertIn("FolderBrowserDialog", script)
        self.assertNotIn(str(selected), script)
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["creationflags"], 0x08000000)
        result.stdout = ""
        with patch("expman.desktop.sys.platform", "win32"), \
                patch("expman.desktop.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch("expman.desktop.subprocess.run", return_value=result):
            self.assertIsNone(desktop.choose_directory())


if __name__ == "__main__":
    unittest.main()
