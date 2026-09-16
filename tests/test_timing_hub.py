import csv
import io
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch
import urllib.request

from expman.hub import APIError, Hub, make_server
from tests.support import temporary_directory


SPEC = {"name": "timed experiment", "backend": "demo", "params": {"steps": 2, "delay": 0}}
SNAPSHOT = {"allow_demo": True, "policy": {"max_prefetch": 4, "cpu_budget": 4, "ram_budget_mb": 8192}}


class HubTimingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.hub = Hub(self.root)
        self.hub.add_node("timing-node")

    def tearDown(self):
        self.hub.close()
        self.temporary.__exit__(None, None, None)

    def sync(self, reports=()):
        return self.hub.sync("timing-node", {
            "node_id": "timing-node", "snapshot": SNAPSHOT, "reports": list(reports),
        })

    def assigned(self, request_id="timed-job"):
        identity = self.hub.submit({"spec": SPEC, "request_id": request_id})["ids"][0]
        self.sync()
        self.assertEqual(self.hub.job(identity)["state"], "assigned")
        return identity

    @staticmethod
    def timing(**changes):
        return {"started_at": 10, "finished_at": None, "elapsed_seconds": 4.5,
                "observed_at": 14.5, "complete": True, **changes}

    @staticmethod
    def report(identity, seq, state="running", attempt=1, **fields):
        return {"id": identity, "seq": seq, "state": state, "attempt": attempt, **fields}

    def test_offline_completion_is_complete_without_running_report(self):
        identity = self.assigned()
        sample = self.timing(finished_at=42, elapsed_seconds=32, observed_at=45,
                             received_at=99999)
        with patch("expman.hub.now", return_value=1000):
            self.sync([self.report(identity, 1, "succeeded", timing=sample)])
            job = self.hub.job(identity)
            self.assertEqual(job["time"], 1000)
            self.assertEqual(self.hub.state()["jobs"][0]["timing"], job["timing"])
        self.assertEqual(job["timing"], {**sample, "received_at": 1000})
        self.assertTrue(job["timing"]["complete"])

    def test_timing_survives_restart_and_clock_offsets(self):
        identity = self.assigned()
        # Node wall-clock adjustments do not invalidate measured runtime.
        sample = self.timing(started_at=5000, finished_at=4000, observed_at=3000,
                             elapsed_seconds=50, complete=False)
        with patch("expman.hub.now", return_value=1000):
            self.sync([self.report(identity, 1, "interrupted", timing=sample)])
        self.hub.close()
        self.hub = Hub(self.root)
        self.assertEqual(self.hub.job(identity)["timing"], {**sample, "received_at": 1000})

    def test_duplicate_old_and_rejected_reports_cannot_change_timing(self):
        identity = self.assigned()
        with patch("expman.hub.now", return_value=1000):
            self.sync([self.report(identity, 3, timing=self.timing())])
        expected = self.hub.job(identity)["timing"]
        for seq, state, attempt in [(3, "running", 1), (2, "succeeded", 1),
                                    (4, "ready", 1), (5, "running", 2)]:
            with self.subTest(seq=seq, state=state, attempt=attempt):
                self.sync([self.report(identity, seq, state, attempt,
                                       timing=self.timing(elapsed_seconds=999))])
                self.assertEqual(self.hub.job(identity)["timing"], expected)
        self.sync([self.report(identity, 6, "succeeded", timing=self.timing(finished_at=20))])
        terminal = self.hub.job(identity)["timing"]
        self.sync([self.report(identity, 7, "running", timing=self.timing(elapsed_seconds=999))])
        self.assertEqual(self.hub.job(identity)["timing"], terminal)

    def test_resume_preserves_total_and_ignores_older_attempt(self):
        identity = self.assigned()
        stopped = self.timing(finished_at=20, elapsed_seconds=10, observed_at=20)
        self.sync([self.report(identity, 1, "paused", timing=stopped)])
        command = self.hub.action({"job_id": identity, "action": "resume"})
        preparing = self.timing(elapsed_seconds=10, observed_at=200)
        self.sync([self.report(identity, 2, "preparing", attempt=2, timing=preparing,
                               command_ack=command["command_id"])])
        ready_timing = self.hub.job(identity)["timing"]
        self.assertIsNone(ready_timing["finished_at"])
        self.assertEqual(ready_timing["elapsed_seconds"], 10)
        self.sync([self.report(identity, 3, "succeeded", attempt=1, timing=stopped)])
        self.assertEqual(self.hub.job(identity)["timing"], ready_timing)
        resumed = self.timing(elapsed_seconds=15, observed_at=205)
        self.sync([self.report(identity, 4, "running", attempt=2, timing=resumed)])
        self.assertEqual(self.hub.job(identity)["timing"]["elapsed_seconds"], 15)
        self.assertEqual(self.hub.job(identity)["timing"]["started_at"], 10)

    def test_legacy_reports_clear_unknown_timing(self):
        identity = self.assigned()
        self.assertIsNone(self.hub.job(identity)["timing"])
        self.sync([self.report(identity, 1)])
        self.assertIsNone(self.hub.job(identity)["timing"])
        self.sync([self.report(identity, 2, timing=self.timing())])
        self.sync([self.report(identity, 3)])
        self.assertIsNone(self.hub.job(identity)["timing"])
        self.sync([self.report(identity, 4, timing=self.timing())])
        self.sync([self.report(identity, 5, "paused")])
        self.assertIsNone(self.hub.job(identity)["timing"])

    def test_migration_keeps_existing_jobs_with_unknown_timing(self):
        identity = self.assigned()
        self.hub.close()
        connection = sqlite3.connect(self.root / "hub.sqlite3")
        try:
            connection.execute("ALTER TABLE jobs DROP COLUMN timing")
            connection.commit()
        finally:
            connection.close()
        self.hub = Hub(self.root)
        self.assertIsNone(self.hub.job(identity)["timing"])
        self.assertEqual(self.hub.job(identity)["state"], "assigned")
        self.sync([self.report(identity, 1, timing=self.timing())])
        self.assertEqual(self.hub.job(identity)["timing"]["elapsed_seconds"], 4.5)

    def test_invalid_timing_rejects_entire_batch_and_heartbeat(self):
        identity = self.assigned()
        last_seen = self.hub.state()["nodes"][0]["last_seen"]
        invalid = [None, [], {}, self.timing(complete=1), self.timing(complete="true")]
        for key in ("started_at", "finished_at", "elapsed_seconds", "observed_at"):
            for value in (-1, True, "10", float("nan"), float("inf"), 10**500):
                invalid.append(self.timing(**{key: value}))
        invalid.extend([self.timing(elapsed_seconds=None), self.timing(observed_at=None)])
        for sample in invalid:
            with self.subTest(sample=sample), self.assertRaises(APIError) as raised:
                self.sync([self.report(identity, 1, timing=self.timing()),
                           self.report(identity, 2, timing=sample)])
            self.assertEqual(raised.exception.status, 400)
            job = self.hub.job(identity)
            self.assertEqual(job["seq"], 0)
            self.assertIsNone(job["timing"])
            self.assertEqual(self.hub.state()["nodes"][0]["last_seen"], last_seen)

    def test_unstarted_sample_has_no_dates_and_zero_elapsed(self):
        identity = self.assigned()
        sample = self.timing(started_at=None, elapsed_seconds=0)
        self.sync([self.report(identity, 1, "preparing", attempt=0, timing=sample)])
        self.assertIsNone(self.hub.job(identity)["timing"]["started_at"])
        self.assertEqual(self.hub.job(identity)["timing"]["elapsed_seconds"], 0)

    def test_csv_exports_recorded_samples_and_leaves_legacy_cells_blank(self):
        identity = self.assigned()
        legacy = self.assigned("legacy")
        self.sync([self.report(identity, 1, timing=self.timing()),
                   self.report(legacy, 1, "succeeded")])
        with patch("expman.hub.now", return_value=9999999999):
            exported = self.hub.results_csv().decode("utf-8-sig")
        rows = {row["id"]: row for row in csv.DictReader(io.StringIO(exported))}
        self.assertEqual(rows[identity]["timing.elapsed_seconds"], "4.5")
        self.assertEqual(rows[identity]["timing.started_at"], "10")
        self.assertEqual(rows[identity]["timing.finished_at"], "")
        self.assertEqual(rows[identity]["timing.observed_at"], "14.5")
        self.assertEqual(rows[identity]["timing.complete"], "True")
        for key in self.timing():
            self.assertEqual(rows[legacy]["timing." + key], "")

    def test_timing_script_is_served_without_authentication(self):
        server = make_server(self.hub, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/timing.js") as response:
                self.assertEqual(response.headers.get_content_type(), "text/javascript")
                self.assertGreater(len(response.read()), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
