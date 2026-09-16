"""Execution timing survives pauses, delayed delivery and node recovery."""
import copy
from datetime import datetime, timezone
import re
import unittest
from unittest.mock import patch

from expman.agent import Agent, _docker_timestamp, _new_timing
from tests import test_agent


class TimingAgentTests(unittest.TestCase):
    setUp = test_agent.AgentTests.setUp
    tearDown = test_agent.AgentTests.tearDown
    record = test_agent.AgentTests.record
    docker_spec = test_agent.AgentTests.docker_spec

    def timed_record(self, backend="demo"):
        with patch("expman.agent.common.now", return_value=10):
            record = self.record(spec=self.docker_spec() if backend == "docker" else None)
            if backend == "docker":
                record["container_name"] = "owned-container"
            record["_timing"] = _new_timing()
            self.agent._save(record)
        return record

    def save_at(self, record, timestamp, **changes):
        with patch("expman.agent.common.now", return_value=timestamp):
            self.agent._save(record, **changes)

    def report_at(self, record, timestamp):
        with patch("expman.agent.common.now", return_value=timestamp):
            return self.agent._report(record)["timing"]

    def docker_time(self, timestamp):
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")

    def test_new_assignment_records_zero_time_before_execution(self):
        response = {"jobs": [{"id": self.job_id, "spec": self.spec}]}
        with patch("expman.agent.common.api_request", return_value=response), \
                patch("expman.agent.common.now", return_value=10):
            self.agent._sync({})
        record = self.agent.records()[0]
        self.save_at(record, 20, state="preparing")
        self.save_at(record, 30, state="starting")
        self.assertEqual(self.report_at(record, 100), {
            "started_at": None, "finished_at": None, "elapsed_seconds": 0,
            "observed_at": 100, "complete": True})

    def test_stop_counts_until_exit_and_resume_excludes_pause_and_preparation(self):
        record = self.timed_record()
        self.save_at(record, 100, state="running", ever_started=True)
        with patch("expman.agent.common.now", return_value=110):
            self.agent._command(record, "stop", 1)
        self.assertEqual(self.report_at(record, 120)["elapsed_seconds"], 20)
        with patch("expman.agent.common.now", return_value=125), \
                patch.object(self.agent, "_checkpoint", return_value=True):
            self.agent._finish(record, 0)
        self.assertEqual(record["state"], "paused")
        self.assertEqual(self.report_at(record, 200)["elapsed_seconds"], 25)
        self.assertEqual(self.report_at(record, 200)["finished_at"], 125)
        with patch("expman.agent.common.now", return_value=220), \
                patch.object(self.agent, "_checkpoint", return_value=True):
            self.agent._command(record, "resume", 2)
        self.assertIsNone(self.report_at(record, 250)["finished_at"])
        self.assertEqual(self.report_at(record, 250)["elapsed_seconds"], 25)
        self.save_at(record, 300, state="starting")
        self.save_at(record, 310, state="running")
        with patch("expman.agent.common.now", return_value=350):
            self.agent._finish(record, 0)
        timing = self.report_at(record, 900)
        self.assertEqual(timing["elapsed_seconds"], 65)
        self.assertEqual(timing["started_at"], 100)
        self.assertEqual(timing["finished_at"], 350)
        self.assertTrue(timing["complete"])

    def test_delayed_terminal_report_does_not_count_upload_or_offline_time(self):
        record = self.timed_record()
        self.save_at(record, 100, state="running")
        self.save_at(record, 150, state="failed")
        with patch("expman.agent.common.api_request", side_effect=OSError("offline")), \
                patch("expman.agent.common.now", return_value=900):
            self.agent._sync({})
        timing = self.report_at(self.agent.records()[0], 1000)
        self.assertEqual(timing["elapsed_seconds"], 50)
        self.assertEqual(timing["finished_at"], 150)
        self.assertEqual(timing["observed_at"], 1000)

    def test_sync_sends_new_duration_without_new_metrics(self):
        record = self.timed_record()
        self.save_at(record, 100, state="running")
        reports = []

        def request(url, token, payload, **kwargs):
            reports.extend(copy.deepcopy(payload["reports"]))
            return {"ack": {self.job_id: payload["reports"][0]["seq"]}, "jobs": []}

        with patch("expman.agent.common.api_request", side_effect=request):
            for timestamp in (110, 125):
                with patch("expman.agent.common.now", return_value=timestamp):
                    self.agent._sync({})
        self.assertEqual([item["timing"]["elapsed_seconds"] for item in reports], [10, 25])
        self.assertLess(reports[0]["seq"], reports[1]["seq"])

    def test_demo_restart_keeps_only_confirmed_time_and_marks_partial(self):
        record = self.timed_record()
        self.save_at(record, 100, state="running")
        self.save_at(record, 130, detail="last observation")
        self.agent.close()
        with patch("expman.agent.common.now", return_value=500):
            self.agent = Agent(self.config)
        record = self.agent.records()[0]
        timing = self.report_at(record, 900)
        self.assertEqual(record["state"], "interrupted")
        self.assertEqual(timing["elapsed_seconds"], 30)
        self.assertIsNone(timing["finished_at"])
        self.assertFalse(timing["complete"])

    def test_docker_restart_recovers_actual_start_and_finish(self):
        record = self.timed_record("docker")
        self.save_at(record, 90, state="starting", container_name="owned-container")
        self.agent.close()
        with patch("expman.agent.common.now", return_value=500):
            self.agent = Agent(self.config)
        record = self.agent.records()[0]
        container = {"State": {"Status": "exited", "Running": False, "ExitCode": 0,
                               "StartedAt": self.docker_time(100), "FinishedAt": self.docker_time(160)}}
        with patch.object(self.agent, "_inspect", return_value=container), \
                patch("expman.agent.subprocess.run"), patch("expman.agent.common.now", return_value=500):
            self.agent._reconcile(record)
        timing = self.report_at(record, 800)
        self.assertEqual(timing["started_at"], 100)
        self.assertEqual(timing["finished_at"], 160)
        self.assertEqual(timing["elapsed_seconds"], 60)
        self.assertTrue(timing["complete"])

    def test_docker_reattachment_corrects_start_and_never_resets_previous_attempt(self):
        record = self.timed_record("docker")
        self.save_at(record, 100, state="running")
        self.save_at(record, 120, state="paused")
        self.save_at(record, 200, state="ready", attempt=2)
        self.save_at(record, 220, state="running")
        container = {"State": {"Status": "running", "Running": True,
                               "StartedAt": self.docker_time(210)}}
        with patch.object(self.agent, "_inspect", return_value=container):
            for timestamp in (250, 270):
                with patch("expman.agent.common.now", return_value=timestamp):
                    self.agent._reconcile(record)
        timing = self.report_at(self.agent.records()[0], 280)
        self.assertEqual(timing["elapsed_seconds"], 90)
        self.assertEqual(timing["started_at"], 100)
        self.assertTrue(timing["complete"])

    def test_missing_container_marks_duration_partial(self):
        record = self.timed_record("docker")
        self.save_at(record, 100, state="running")
        self.save_at(record, 130)
        with patch.object(self.agent, "_inspect", return_value=None), \
                patch("expman.agent.common.now", return_value=300):
            self.agent._reconcile(record)
        timing = self.report_at(record, 500)
        self.assertEqual(timing["elapsed_seconds"], 30)
        self.assertFalse(timing["complete"])
        self.assertIsNone(timing["finished_at"])

    def test_legacy_history_is_not_fabricated(self):
        record = self.record("succeeded")
        self.assertNotIn("timing", self.agent._report(record))
        record["state"] = "running"
        record["spec"] = self.docker_spec()
        container = {"State": {"Status": "running", "Running": True,
                               "StartedAt": self.docker_time(100)}}
        with patch.object(self.agent, "_inspect", return_value=container), \
                patch("expman.agent.common.now", return_value=150):
            self.agent._reconcile(record)
        timing = self.report_at(record, 160)
        self.assertEqual(timing["elapsed_seconds"], 60)
        self.assertFalse(timing["complete"])

    def test_clock_rollback_never_produces_negative_duration(self):
        record = self.timed_record()
        self.save_at(record, 100, state="running")
        self.save_at(record, 90, state="failed")
        timing = self.report_at(record, 95)
        self.assertEqual(timing["elapsed_seconds"], 0)
        self.assertFalse(timing["complete"])

    def test_docker_clock_rollback_marks_history_partial(self):
        record = self.timed_record("docker")
        self.save_at(record, 100, state="running")
        container = {"State": {"Status": "exited", "Running": False, "ExitCode": 0,
                               "StartedAt": self.docker_time(100), "FinishedAt": self.docker_time(90)}}
        with patch.object(self.agent, "_inspect", return_value=container), \
                patch("expman.agent.subprocess.run"), patch("expman.agent.common.now", return_value=500):
            self.agent._reconcile(record)
        timing = self.report_at(record, 800)
        self.assertEqual(timing["elapsed_seconds"], 0)
        self.assertFalse(timing["complete"])

    def test_docker_fractional_timestamps_support_python_310_and_timezone_offsets(self):
        def parse_like_python310(value):
            fraction = re.search(r"\.(\d+)", value)
            if fraction and len(fraction.group(1)) not in (3, 6):
                raise ValueError("Python 3.10 requires three or six fractional digits")
            return datetime.fromisoformat(value)

        with patch("expman.agent.datetime") as parser:
            parser.fromisoformat.side_effect = parse_like_python310
            for offset in ("Z", "+08:00", "-03:30"):
                for digits in range(1, 10):
                    fraction = "123456789"[:digits]
                    with self.subTest(offset=offset, digits=digits):
                        raw = "2026-09-16T12:34:56." + fraction + offset
                        expected = datetime.fromisoformat("2026-09-16T12:34:56." +
                            fraction.ljust(6, "0")[:6] + offset.replace("Z", "+00:00")).timestamp()
                        self.assertEqual(_docker_timestamp(raw), expected)

    def test_docker_nanosecond_times_recover_elapsed_execution(self):
        record = self.timed_record("docker")
        self.save_at(record, 90, state="starting")
        container = {"State": {"Status": "exited", "Running": False, "ExitCode": 0,
            "StartedAt": "1970-01-01T00:01:40.123456789Z",
            "FinishedAt": "1970-01-01T00:02:40.623456789Z"}}
        with patch.object(self.agent, "_inspect", return_value=container), \
                patch("expman.agent.subprocess.run"), patch("expman.agent.common.now", return_value=500):
            self.agent._reconcile(record)
        timing = self.report_at(record, 800)
        self.assertAlmostEqual(timing["elapsed_seconds"], 60.5)
        self.assertTrue(timing["complete"])

    def test_overdue_stop_after_lost_start_ack_counts_execution_once(self):
        record = self.timed_record("docker")
        self.save_at(record, 90, state="starting", stop_at=101, stop_action="stop")
        container = {"State": {"Status": "running", "Running": True,
                               "StartedAt": self.docker_time(100)}}
        with patch.object(self.agent, "_inspect", return_value=container), \
                patch.object(self.agent, "_exec") as execute, \
                patch("expman.agent.common.now", return_value=200):
            self.agent._reconcile(record)
            self.assertEqual(record["state"], "running")
            execute.assert_called_once_with(["docker", "stop", "--time", "10", "owned-container"], timeout=30)
            self.agent._save(record, metrics={"epoch": 1})
        self.assertEqual(self.report_at(record, 200)["elapsed_seconds"], 100)
        container["State"].update(Status="exited", Running=False, ExitCode=0,
                                  FinishedAt=self.docker_time(200))
        with patch.object(self.agent, "_inspect", return_value=container), \
                patch("expman.agent.subprocess.run"), patch("expman.agent.common.now", return_value=300):
            self.agent._reconcile(record)
        self.assertEqual(self.report_at(record, 500)["elapsed_seconds"], 100)


if __name__ == "__main__":
    unittest.main()
