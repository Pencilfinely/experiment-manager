"""Real HTTP + SQLite + real built-in subprocesses; no Docker simulation claim."""
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from expman.agent import Agent
from expman.common import atomic_json, api_request, sha256_file
from expman.hub import Hub, make_server
from expman.__main__ import node_config
from tests.support import temporary_directory


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.hub = Hub(self.root / "hub")
        self.node_token = self.hub.add_node("worker")
        self.server = make_server(self.hub, port=0)
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:" + str(self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        config = node_config("worker", self.url, self.node_token, self.root / "node", True)
        config["sync_timeout"] = 0.2
        config["policy"]["max_running"] = 1
        config["policy"]["max_prefetch"] = 4
        self.config_path = self.root / "node.json"
        atomic_json(self.config_path, config)
        self.agent = Agent(self.config_path)

    def tearDown(self):
        for record in self.agent.records():
            if record["state"] in ("running", "starting"):
                atomic_json(self.agent.root / "runs" / record["id"] / "STOP", {"reason": "test cleanup"})
        for process in self.agent.processes.values():
            try:
                process.wait(timeout=3)
            except Exception:
                process.terminate()
                process.wait(timeout=3)
        self.agent.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.hub.close()
        self.temporary.__exit__(None, None, None)

    def submit(self, steps=20, grid=None):
        spec = {"backend": "demo", "params": {"steps": steps, "delay": 0.025, "seed": 42},
                "resources": {"gpu_memory_mb": 0, "cpu": 1, "ram_mb": 256}}
        body = {"spec": spec, "grid": grid or {}, "request_id": "request-" + str(time.time_ns())}
        result = api_request(self.url + "/api/jobs", self.hub.config["admin_token"], body)
        duplicate = api_request(self.url + "/api/jobs", self.hub.config["admin_token"], body)
        self.assertEqual(result, duplicate)
        return result["ids"]

    def pump(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.agent.tick()
            if predicate():
                return
            time.sleep(0.035)
        self.fail("Timed out: " + repr(self.agent.records()) + " error=" + str(self.agent.last_error))

    def test_center_offline_then_return_archives_all_results(self):
        ids = self.submit(30, {"seed": [42, 43, 44]})
        self.pump(lambda: len(self.agent.records()) == 3 and
                  any(r["state"] == "running" for r in self.agent.records()) and
                  all(r.get("prepared") for r in self.agent.records()))
        self.server.shutdown()
        self.server.server_close()
        self.server = None
        self.hub.close()
        self.pump(lambda: all(r["state"] == "succeeded" for r in self.agent.records()))
        self.assertFalse(self.agent.online)
        # Real coordinator database reload, not an in-memory stub.
        self.hub = Hub(self.root / "hub")
        self.server = make_server(self.hub, port=self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.pump(lambda: all(self.hub.job(i)["state"] == "succeeded" and
                 any(a["name"] == "result.json" for a in self.hub.job(i)["artifacts"]) for i in ids))
        self.pump(lambda: self.agent.db.execute("SELECT COUNT(*) FROM uploads WHERE complete=0").fetchone()[0] == 0)
        for identity in ids:
            job = self.hub.job(identity)
            self.assertEqual(job["node_id"], "worker")
            self.assertEqual(job["attempt"], 1)
            self.assertEqual(job["metrics"]["step"], 30)
            for item in job["artifacts"]:
                path = self.hub.artifact(identity, item["sha256"])
                self.assertEqual(sha256_file(path), item["sha256"])

    def test_lost_assignment_response_does_not_duplicate_execution(self):
        identity = self.submit(8)[0]
        original = api_request
        dropped = False
        def request(url, token, payload=None, timeout=10):
            nonlocal dropped
            response = original(url, token, payload, timeout)
            if url.endswith("/api/sync") and response["jobs"] and not dropped:
                dropped = True
                raise TimeoutError("simulated lost response after committed assignment")
            return response
        with patch("expman.agent.common.api_request", side_effect=request):
            self.agent.tick()
            self.assertEqual(self.hub.job(identity)["node_id"], "worker")
            self.pump(lambda: self.hub.job(identity)["state"] == "succeeded")
        record = self.agent.records()[0]
        self.assertEqual(record["attempt"], 1)
        logs = list((self.agent.root / "runs" / identity).glob("attempt-*.log"))
        self.assertEqual(len(logs), 1)

    def test_real_cooperative_stop_resume_preserves_steps(self):
        identity = self.submit(70)[0]
        self.pump(lambda: self.agent.records() and self.agent.records()[0].get("metrics", {}).get("step", 0) >= 3)
        self.hub.action({"job_id": identity, "action": "stop"})
        self.pump(lambda: self.hub.job(identity)["state"] == "paused")
        self.assertTrue((self.agent.root / "runs" / identity / "checkpoint.json").exists())
        self.hub.action({"job_id": identity, "action": "resume"})
        self.pump(lambda: self.hub.job(identity)["state"] == "succeeded")
        self.assertEqual(self.hub.job(identity)["attempt"], 2)
        self.assertEqual(self.hub.job(identity)["metrics"]["step"], 70)


if __name__ == "__main__":
    unittest.main()
