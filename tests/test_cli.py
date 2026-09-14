import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import types
import unittest
from unittest.mock import patch

from expman.__main__ import main, node_config, _stop_demo
from expman.common import atomic_json, read_json
from expman.hub import Hub, make_server
from tests.support import temporary_directory


class CLITests(unittest.TestCase):
    def invoke(self, args):
        output = io.StringIO()
        with patch.object(sys, "argv", ["expman", *args]), contextlib.redirect_stdout(output):
            main()
        return output.getvalue()

    def test_submit_retries_same_request_through_real_http(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            hub = Hub(root / "hub")
            server = make_server(hub, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                spec = root / "task.json"
                atomic_json(spec, {"backend": "demo", "params": {"steps": 1}})
                args = ["submit", "--root", str(hub.root), "--url", "http://127.0.0.1:" + str(server.server_address[1]), "--spec", str(spec), "--request-id", "cli-retry"]
                first = self.invoke(args)
                self.assertEqual(first, self.invoke(args))
                self.assertEqual(len(hub.state()["jobs"]), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
                hub.close()

    def test_add_node_preserves_existing_config_and_enrolls_online(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            hub = Hub(root / "hub")
            try:
                target = root / "node.json"
                args = ["add-node", "--root", str(hub.root), "--id", "worker", "--url", "http://127.0.0.1:8765", "--output", str(target)]
                self.invoke(args)
                config = read_json(target)
                self.assertEqual(hub.authenticate("Bearer " + config["token"]), ("node", "worker"))
                with self.assertRaises(ValueError):
                    self.invoke(args)
                self.assertEqual(read_json(target), config)
            finally:
                hub.close()

    def test_doctor_uses_read_only_inspection(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "node.json"
            data = node_config("demo", "http://127.0.0.1:1", "token", Path(temporary) / "unused", True)
            atomic_json(target, data)
            with patch("expman.diagnostics.inspect_node", return_value={"ready": True, "snapshot": {"allow_demo": True}}) as inspect:
                result = self.invoke(["doctor", "--config", str(target)])
            inspect.assert_called_once_with(str(target))
            self.assertTrue(json.loads(result)["snapshot"]["allow_demo"])
            self.assertFalse((Path(temporary) / "unused").exists())

    def test_demo_random_port_is_written_to_agent_config(self):
        with temporary_directory() as temporary:
            class DemoAgent:
                def __init__(self, path):
                    self.config = read_json(path)
                    self.root = Path(self.config["root"])
                    self.processes = {}
                    self.mode = "run"
                def tick(self):
                    raise KeyboardInterrupt()
                def records(self):
                    return []
                def close(self):
                    pass
            with patch("expman.agent.Agent", DemoAgent):
                self.invoke(["demo", "--root", temporary, "--port", "0"])
            config = read_json(Path(temporary) / "demo-node.json")
            self.assertNotEqual(config["hub_url"], "http://127.0.0.1:0")

    def test_demo_shutdown_reaps_only_its_child_handles(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            instance = types.SimpleNamespace(root=root, mode="run", processes={"owned": child}, records=lambda: [], _reconcile=lambda record: None)
            try:
                with patch("expman.__main__.time.sleep"):
                    _stop_demo(instance)
                self.assertIsNotNone(child.poll())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
