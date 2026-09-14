import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request

from expman.hub import APIError, Hub, make_server
from tests.support import temporary_directory


SPEC = {"name": "test", "backend": "demo", "params": {"steps": 2, "delay": 0}}
SNAPSHOT = {"allow_demo": True, "policy": {"max_prefetch": 4, "cpu_budget": 4, "ram_budget_mb": 8192}, "tags": [], "assets": []}


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.hub = Hub(self.root)
        self.token = self.hub.add_node("node-a")
        self.other_token = self.hub.add_node("node-b")

    def tearDown(self):
        self.hub.close()
        self.temporary.__exit__(None, None, None)

    def sync(self, reports=None, node="node-a", snapshot=None):
        return self.hub.sync(node, {"node_id": node, "snapshot": snapshot or SNAPSHOT, "reports": reports or []})

    def submit(self, request_id="submission", spec=None):
        return self.hub.submit({"spec": spec or SPEC, "request_id": request_id})["ids"][0]

    def assigned(self):
        identity = self.submit()
        self.sync()
        return identity

    def report(self, identity, seq, state, attempt=1, **fields):
        return {"id": identity, "seq": seq, "state": state, "attempt": attempt, **fields}

    def upload(self, identity, data, offset=0, block=None, name="outputs/result.txt", digest=None):
        return self.hub.upload("node-a", {"job_id": identity, "name": name,
            "sha256": digest or hashlib.sha256(data).hexdigest(), "size": len(data), "offset": offset,
            "data": base64.b64encode(data if block is None else block).decode()})

    def test_duplicate_submission_is_durable_and_conflicts_rejected(self):
        first = self.submit()
        self.assertEqual(first, self.submit())
        with self.assertRaises(APIError) as error:
            self.submit(spec={**SPEC, "name": "different"})
        self.assertEqual(error.exception.status, 409)
        self.hub.close()
        self.hub = Hub(self.root)
        self.assertEqual(first, self.submit())
        self.assertEqual(len(self.hub.state()["jobs"]), 1)

    def test_grid_is_one_idempotent_submission(self):
        payload = {"spec": SPEC, "request_id": "grid", "grid": {"seed": [1, 2], "steps": [2, 3]}}
        ids = self.hub.submit(payload)["ids"]
        self.assertEqual(len(ids), 4)
        self.assertEqual(ids, self.hub.submit(payload)["ids"])

    def test_lost_ack_duplicate_reports_and_terminal_regression(self):
        identity = self.assigned()
        report = self.report(identity, 1, "running")
        self.assertEqual(self.sync([report])["ack"][identity], 1)
        self.assertEqual(self.sync([report])["ack"][identity], 1)
        self.sync([self.report(identity, 3, "succeeded", metrics={"accuracy": 0.9})])
        self.sync([self.report(identity, 2, "running")])
        self.sync([self.report(identity, 4, "running")])
        job = self.hub.job(identity)
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["metrics"], {"accuracy": 0.9})
        self.assertEqual(job["seq"], 4)

    def test_ownership_survives_offline_and_restart(self):
        identity = self.assigned()
        self.hub.db.execute("UPDATE nodes SET last_seen=0 WHERE id='node-a'")
        self.sync(node="node-b")
        self.assertEqual(self.hub.job(identity)["node_id"], "node-a")
        self.hub.close()
        self.hub = Hub(self.root)
        self.assertEqual(self.hub.job(identity)["node_id"], "node-a")
        with self.assertRaises(APIError) as error:
            self.sync([self.report(identity, 1, "running")], node="node-b")
        self.assertEqual(error.exception.status, 403)

    def test_command_ack_and_resume_new_attempt(self):
        identity = self.assigned()
        self.sync([self.report(identity, 1, "running")])
        command = self.hub.action({"job_id": identity, "action": "stop"})
        self.assertEqual(command, self.hub.action({"job_id": identity, "action": "stop"}))
        self.assertEqual(self.sync()["jobs"][0]["action"], "stop")
        self.sync([self.report(identity, 2, "paused", command_ack=command["command_id"])])
        self.assertEqual(self.sync()["jobs"], [])
        resume = self.hub.action({"job_id": identity, "action": "resume"})
        self.assertGreater(resume["command_id"], command["command_id"])
        self.assertEqual(self.sync()["jobs"][0]["action"], "resume")
        # ACK can precede the transition; permission must survive this report.
        self.sync([self.report(identity, 3, "paused", command_ack=resume["command_id"])])
        self.sync([self.report(identity, 4, "ready", attempt=2, command_ack=resume["command_id"])])
        self.assertEqual(self.hub.job(identity)["state"], "ready")
        self.assertEqual(self.hub.job(identity)["attempt"], 2)
        self.sync([self.report(identity, 5, "succeeded", attempt=1)])
        self.assertEqual(self.hub.job(identity)["state"], "ready")

    def test_drain_blocks_assignment_but_keeps_existing_job(self):
        identity = self.assigned()
        self.hub.set_mode({"node_id": "node-a", "mode": "drain"})
        waiting = self.submit("second")
        response = self.sync()
        self.assertEqual(response["mode"], "drain")
        self.assertEqual(response["jobs"][0]["id"], identity)
        self.assertIsNone(self.hub.job(waiting)["node_id"])

    def test_cancel_paused_and_prevent_same_attempt_regression(self):
        identity = self.assigned()
        self.sync([self.report(identity, 1, "running")])
        self.sync([self.report(identity, 2, "ready")])
        self.assertEqual(self.hub.job(identity)["state"], "running")
        self.sync([self.report(identity, 3, "paused")])
        command = self.hub.action({"job_id": identity, "action": "cancel"})
        self.sync([self.report(identity, 4, "canceled", command_ack=command["command_id"])])
        self.assertEqual(self.hub.job(identity)["state"], "canceled")

    def test_add_node_from_separate_cli_instance(self):
        separate = Hub(self.root)
        try:
            token = separate.add_node("node-c")
            self.assertEqual(self.hub.authenticate("Bearer " + token), ("node", "node-c"))
        finally:
            separate.close()

    def test_metrics_history_and_bounded_persistent_log_tail(self):
        identity = self.assigned()
        report = self.report(identity, 1, "running", metrics={"step": 1, "loss": 2}, log_tail="first line\n")
        self.sync([report])
        self.sync([report])
        self.sync([self.report(identity, 2, "running", metrics={"step": 2, "loss": 1}, log_tail="second line\n")])
        self.sync([self.report(identity, 3, "running", metrics={"step": 2, "loss": 1})])
        job = self.hub.job(identity)
        metrics = [event for event in job["events"] if event["kind"] == "metrics"]
        self.assertEqual([event["data"]["metrics"]["loss"] for event in metrics], [2, 1])
        self.assertEqual(job["log_tail"], "second line\n")
        self.assertNotIn("log_tail", self.hub.state()["jobs"][0])
        with self.assertRaises(APIError):
            self.sync([self.report(identity, 4, "running", log_tail="中" * 6000)])
        self.hub.close()
        self.hub = Hub(self.root)
        self.assertEqual(self.hub.job(identity)["log_tail"], "second line\n")

    def test_resume_superseded_by_cancel_before_new_attempt_report(self):
        identity = self.assigned()
        self.sync([self.report(identity, 1, "paused")])
        self.hub.action({"job_id": identity, "action": "resume"})
        cancel = self.hub.action({"job_id": identity, "action": "cancel"})
        self.sync([self.report(identity, 2, "canceled", attempt=2, command_ack=cancel["command_id"])])
        self.assertEqual(self.hub.job(identity)["state"], "canceled")
        self.assertEqual(self.hub.job(identity)["attempt"], 2)

    def test_simultaneous_duplicate_submissions_create_one_job(self):
        results, errors = [], []
        barrier = threading.Barrier(6)
        def submit():
            try:
                barrier.wait(timeout=3)
                results.append(self.submit())
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=submit) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(self.hub.state()["jobs"]), 1)

    def test_legacy_jobs_table_gets_log_column(self):
        schema = self.hub.db.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
        self.hub.close()
        connection = sqlite3.connect(self.root / "hub.sqlite3")
        try:
            connection.execute("DROP TABLE jobs")
            connection.execute(schema.replace(", log_tail TEXT NOT NULL DEFAULT ''", ""))
            connection.commit()
        finally:
            connection.close()
        self.hub = Hub(self.root)
        self.assertEqual(self.hub.job(self.submit())["log_tail"], "")

    def test_bad_report_batch_does_not_partially_commit(self):
        identity = self.assigned()
        with self.assertRaises(APIError):
            self.sync([self.report(identity, 1, "running"), self.report("0" * 32, 1, "running")])
        self.assertEqual(self.hub.job(identity)["state"], "assigned")
        self.assertEqual(self.hub.job(identity)["seq"], 0)

    def test_upload_disconnect_resume_repeated_chunk_and_versions(self):
        identity = self.assigned()
        data = b"abcdef" * 100
        self.assertEqual(self.upload(identity, data, block=data[:100]), {"offset": 100, "complete": False})
        # A retry of an already persisted chunk reports the authoritative offset.
        self.assertEqual(self.upload(identity, data, block=data[:100]), {"offset": 100, "complete": False})
        self.hub.close()
        self.hub = Hub(self.root)
        self.assertEqual(self.upload(identity, data, block=b""), {"offset": 100, "complete": False})
        self.assertEqual(self.upload(identity, data, offset=100, block=data[100:]), {"offset": len(data), "complete": True})
        self.assertEqual(self.upload(identity, data, block=b"")["complete"], True)
        self.assertEqual(self.hub.artifact(identity, hashlib.sha256(data).hexdigest()).read_bytes(), data)
        self.upload(identity, b"new version")
        self.assertEqual(len(self.hub.job(identity)["artifacts"]), 2)

    def test_hash_failure_never_completes_and_can_retry(self):
        identity = self.assigned()
        data = b"right"
        with self.assertRaises(APIError) as error:
            self.upload(identity, data, block=b"wrong")
        self.assertEqual(error.exception.status, 422)
        self.assertEqual(self.hub.job(identity)["artifacts"], [])
        self.assertEqual(self.upload(identity, data, block=b"")["offset"], 0)
        self.assertTrue(self.upload(identity, data)["complete"])

    def test_empty_file_and_unsafe_paths(self):
        identity = self.assigned()
        self.assertTrue(self.upload(identity, b"")["complete"])
        for name in ("../escape", "/tmp/a", "C:\\secret", "a/../../x", "a\\..\\x", "a//b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.upload(identity, b"x", name=name)
        with self.assertRaises(APIError) as error:
            self.hub.upload("node-b", {"job_id": identity, "name": "a", "sha256": hashlib.sha256(b"").hexdigest(), "size": 0, "offset": 0, "data": ""})
        self.assertEqual(error.exception.status, 403)

    def test_csv_has_params_metrics_and_formula_protection(self):
        identity = self.submit(spec={**SPEC, "name": "=1+1", "params": {"seed": 42, "note": " @SUM(1)"}})
        self.sync()
        self.sync([self.report(identity, 1, "succeeded", metrics={"nested": {"score": 0.5}})])
        text = self.hub.results_csv().decode("utf-8-sig")
        self.assertIn("params.seed", text)
        self.assertIn("metrics.nested.score", text)
        self.assertIn("'=1+1", text)
        self.assertIn("' @SUM(1)", text)

    def test_http_auth_roles_invalid_json_and_download(self):
        server = make_server(self.hub, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        def request(path, token=None, data=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            return urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers), timeout=3)
        try:
            for token, status in ((None, 401), (self.token, 403), ("fake", 401)):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request("/api/state", token)
                self.assertEqual(error.exception.code, status)
            with request("/api/state", self.hub.config["admin_token"]) as response:
                self.assertNotIn("token", response.read().decode())
            with self.assertRaises(urllib.error.HTTPError) as error:
                request("/api/sync", self.token, b'{"node_id":"node-b","snapshot":{}}')
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as error:
                request("/api/jobs", self.hub.config["admin_token"], b'{"x":NaN}')
            self.assertEqual(error.exception.code, 400)
            identity = self.assigned()
            self.upload(identity, b"download")
            digest = hashlib.sha256(b"download").hexdigest()
            with request(f"/api/artifact?job_id={identity}&sha256={digest}", self.hub.config["admin_token"]) as response:
                self.assertEqual(response.read(), b"download")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
