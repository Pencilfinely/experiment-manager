import base64
import copy
import json
from pathlib import Path
import subprocess
import time
import threading
import unittest
from unittest.mock import patch

from expman import common
from expman.agent import Agent, run
from tests.support import temporary_directory


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.__enter__())
        self.config = self.folder / "node.json"
        common.atomic_json(self.config, {"node_id": "test", "hub_url": "http://127.0.0.1:8765",
            "token": "test-token", "root": str(self.folder / "node"), "allow_demo": True,
            "policy": {"max_running": 2, "cpu_budget": 4, "ram_budget_mb": 2048}})
        self.agent = Agent(self.config)
        self.spec = common.validate_task({"backend": "demo", "params": {"steps": 2, "delay": 0}})
        self.job_id = "a" * 32

    def tearDown(self):
        for process in self.agent.processes.values():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        self.agent.close()
        self.temporary.__exit__(None, None, None)

    def record(self, state="assigned", spec=None):
        record = {"id": self.job_id, "state": state, "spec": spec or self.spec,
                  "attempt": 1, "seq": 0, "command_ack": 0, "prepared": state != "assigned",
                  "metrics": {}}
        self.agent._save(record)
        self.agent._output(record).mkdir(parents=True, exist_ok=True)
        return record

    def test_single_agent_lock_released_on_close(self):
        with self.assertRaises(RuntimeError):
            Agent(self.config)
        self.agent.close()
        self.agent = Agent(self.config)
        self.assertEqual(self.agent.records(), [])

    def test_diagnostic_does_not_create_state_or_recover_jobs(self):
        record = self.record("running")
        snapshot = Agent.inspect_config(self.config)
        self.assertTrue(snapshot["allow_demo"])
        self.assertEqual(self.agent.records()[0]["state"], "running")
        self.assertFalse((self.agent._output(record) / "STOP").exists())

    def test_replayed_assignment_never_resets_running_task(self):
        self.record("running")
        response = {"jobs": [{"id": self.job_id, "spec": self.spec, "command_id": 0}], "mode": "run"}
        with patch("expman.agent.common.api_request", return_value=response):
            self.agent._sync({})
            first = self.agent.records()[0]
            self.agent._sync({})
        self.assertEqual(self.agent.records()[0], first)
        self.assertEqual(first["state"], "running")

    def test_offline_cached_task_starts_and_reports_replay_after_lost_ack(self):
        self.record("ready")
        starts = []

        def start(record, choice):
            starts.append(record["id"])
            self.agent._save(record, state="running")

        with patch("expman.agent.common.api_request", side_effect=OSError("Hub offline")), \
                patch.object(self.agent, "_start", side_effect=start), \
                patch.object(self.agent, "_reconcile"):
            self.agent.tick()
            self.agent.tick()
        self.assertEqual(starts, [self.job_id])
        self.assertFalse(self.agent.online)
        sent = []

        def request(url, token, payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            if len(sent) == 1:
                raise OSError("Server committed, response was lost")
            return {"jobs": [], "ack": {self.job_id: payload["reports"][0]["seq"]}}

        with patch("expman.agent.common.api_request", side_effect=request):
            self.agent._sync({})
            self.agent._sync({})
        self.assertEqual(sent[0]["reports"], sent[1]["reports"])

    def test_more_than_one_thousand_reports_are_batched_then_not_replayed(self):
        rows = []
        for index in range(1201):
            job_id = f"{index:032x}"
            record = {"id": job_id, "spec": self.spec, "seq": 1, "state": "succeeded",
                      "attempt": 1, "command_ack": 0, "updated": index, "metrics": {}}
            rows.append((job_id, json.dumps(record)))
        with self.agent.db:
            self.agent.db.executemany("INSERT INTO tasks VALUES (?,?)", rows)
        batches = []

        def request(url, token, payload, **kwargs):
            batches.append(copy.deepcopy(payload["reports"]))
            return {"jobs": [], "ack": {record["id"]: record["seq"] for record in payload["reports"]}}

        with patch("expman.agent.common.api_request", side_effect=request):
            for _ in range(4):
                self.agent._sync({})
        self.assertEqual([len(batch) for batch in batches], [500, 500, 201, 0])
        self.assertEqual(len({record["id"] for batch in batches for record in batch}), 1201)
        self.assertTrue(all(record["seq"] == 1 for record in self.agent.records()))

    def test_ack_persists_after_restart_and_terminal_commands_still_arrive(self):
        record = self.record("paused")
        original_seq = record["seq"]
        with patch("expman.agent.common.api_request", return_value={"jobs": [], "ack": {self.job_id: original_seq}}):
            self.agent._sync({})
        self.agent.close()
        self.agent = Agent(self.config)
        sent = []

        def request(url, token, payload, **kwargs):
            sent.append(copy.deepcopy(payload["reports"]))
            return {"jobs": [{"id": self.job_id, "spec": self.spec, "command_id": 1, "action": "resume"}],
                    "ack": {item["id"]: item["seq"] for item in payload["reports"]}}

        with patch("expman.agent.common.api_request", side_effect=request):
            self.agent._sync({})
            self.agent._sync({})
        self.assertEqual(sent[0], [])
        self.assertEqual(len(sent[1]), 1)
        self.assertEqual(sent[1][0]["attempt"], 2)
        self.assertEqual(sent[1][0]["command_ack"], 1)

    def test_lost_ack_is_replayed_even_after_restart(self):
        record = self.record("succeeded")
        seen = []

        def lost_response(url, token, payload, **kwargs):
            seen.append(copy.deepcopy(payload["reports"]))
            raise OSError("Server committed but ACK was lost")

        with patch("expman.agent.common.api_request", side_effect=lost_response):
            self.agent._sync({})
        self.agent.close()
        self.agent = Agent(self.config)
        with patch("expman.agent.common.api_request", side_effect=lost_response):
            self.agent._sync({})
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(self.agent.records()[0]["seq"], record["seq"])

    def test_report_bytes_and_unicode_log_tail_are_bounded(self):
        rows = []
        for index in range(100):
            job_id = f"{index:032x}"
            record = {"id": job_id, "spec": self.spec, "seq": 1, "state": "succeeded", "attempt": 1,
                      "command_ack": 0, "updated": index, "metrics": {}, "log_tail": "实验输出" * 4000}
            rows.append((job_id, json.dumps(record)))
        with self.agent.db:
            self.agent.db.executemany("INSERT INTO tasks VALUES (?,?)", rows)
        with patch("expman.agent.common.api_request", return_value={"jobs": [], "ack": {}}) as request:
            self.agent._sync({})
        payload = request.call_args.args[2]
        self.assertLess(len(json.dumps(payload).encode("utf-8")), 2 * 1024 * 1024)
        self.assertGreater(len(payload["reports"]), 1)
        self.assertLess(len(payload["reports"]), 100)
        self.assertTrue(all(len(record["log_tail"].encode("utf-8")) <= 16000 for record in payload["reports"]))

    def test_pending_upload_count_diagnostic_and_runtime(self):
        self.assertIsNone(Agent.inspect_config(self.config)["pending_uploads"])
        self.assertEqual(self.agent.snapshot()["pending_uploads"], 0)
        record = self.record("succeeded")
        (self.agent._output(record) / "result.txt").write_text("result")
        self.agent._snapshot_files(record)
        self.assertEqual(self.agent.snapshot()["pending_uploads"], 1)

    def test_foreground_reports_only_connectivity_changes_without_token(self):
        offline = {"online": False, "error": "Connection refused test-token"}
        online = {"online": True, "error": None}
        with patch("expman.agent.Agent", return_value=self.agent), \
                patch.object(self.agent, "tick", side_effect=[offline, offline, online, KeyboardInterrupt]), \
                patch("expman.agent.time.sleep"), patch("builtins.print") as printer:
            run(self.config)
        self.assertEqual(printer.call_count, 3)
        output = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("connecting to http://127.0.0.1:8765", output)
        self.assertIn("Hub offline", output)
        self.assertIn("Hub online", output)
        self.assertNotIn("test-token", output)

    def test_demo_restart_marks_interrupted_never_launches_again(self):
        self.record("running")
        self.agent.close()
        with patch.object(Agent, "_exec", side_effect=OSError("No test GPU")):
            snapshot = Agent.inspect_config(self.config)
        with patch("expman.agent.subprocess.Popen") as spawn, \
                patch.object(Agent, "snapshot", return_value=snapshot):
            self.agent = Agent(self.config)
            with patch("expman.agent.common.api_request", side_effect=OSError("offline")):
                self.agent.tick()
        spawn.assert_not_called()
        record = self.agent.records()[0]
        self.assertEqual(record["state"], "interrupted")
        self.assertTrue((self.agent._output(record) / "STOP").exists())

    def test_drain_persists_when_hub_is_offline(self):
        self.record("ready")
        with patch("expman.agent.common.api_request", return_value={"jobs": [], "mode": "drain"}):
            self.agent._sync({})
        self.agent.close()
        self.agent = Agent(self.config)
        with patch("expman.agent.common.api_request", side_effect=OSError("offline")), \
                patch.object(self.agent, "_start") as start:
            self.agent.tick()
        start.assert_not_called()
        self.assertEqual(self.agent.mode, "drain")

    def test_cooperative_stop_checkpoint_resume_and_duplicate_command(self):
        record = self.record("running")
        output = self.agent._output(record)
        checkpoint = output / "weights.bin"
        checkpoint.write_bytes(b"complete-checkpoint")
        common.atomic_json(output / "checkpoint.json", {
            "path": "weights.bin", "sha256": common.sha256_file(checkpoint), "step": 5})
        self.agent._command(record, "stop", 1)
        self.assertTrue((output / "STOP").exists())
        self.agent._finish(record, 0)
        self.assertEqual(record["state"], "paused")
        self.agent._command(record, "resume", 2)
        self.assertEqual(record["state"], "ready")
        self.assertEqual(record["attempt"], 2)
        self.assertFalse((output / "STOP").exists())
        response = {"jobs": [{"id": self.job_id, "spec": self.spec, "command_id": 2, "action": "resume"}]}
        with patch("expman.agent.common.api_request", return_value=response):
            self.agent._sync({})
        self.assertEqual(self.agent.records()[0]["attempt"], 2)

    def test_invalid_checkpoint_never_claims_paused(self):
        record = self.record("running")
        common.atomic_json(self.agent._output(record) / "checkpoint.json", {
            "path": "../escape.bin", "sha256": "0" * 64, "step": 1})
        self.agent._command(record, "stop", 1)
        self.agent._finish(record, 143)
        self.assertEqual(record["state"], "interrupted")

    def test_resume_without_checkpoint_is_refused_after_execution(self):
        record = self.record("interrupted")
        self.agent._save(record, ever_started=True)
        self.agent._command(record, "resume", 3)
        self.assertEqual(record["state"], "interrupted")
        self.assertEqual(record["attempt"], 1)
        self.assertIn("no verified checkpoint", record["detail"])

    def test_resume_before_first_execution_remains_fresh_start(self):
        record = self.record("ready")
        self.agent._command(record, "stop", 1)
        self.agent._command(record, "resume", 2)
        self.assertEqual(record["state"], "ready")
        self.assertEqual(self.agent._environment(record)["EXPERIMENT_RESUME"], "0")

    def test_real_demo_cooperatively_stops_and_resumes_offline(self):
        spec = common.validate_task({"backend": "demo", "params": {"steps": 8, "delay": 0.03}})
        self.record(spec=spec)
        with patch("expman.agent.common.api_request", side_effect=OSError("Hub offline")):
            self.agent.tick()
            deadline = time.time() + 10
            while time.time() < deadline:
                self.agent.tick()
                record = self.agent.records()[0]
                if record.get("metrics", {}).get("step", 0) >= 2:
                    break
                time.sleep(0.01)
            self.assertEqual(record["state"], "running", record.get("log_tail"))
            self.agent._command(record, "stop", 1)
            while time.time() < deadline:
                self.agent.tick()
                record = self.agent.records()[0]
                if record["state"] == "paused":
                    break
                time.sleep(0.01)
            self.assertEqual(record["state"], "paused")
            self.assertTrue(self.agent._checkpoint(record))
            self.agent._command(record, "resume", 2)
            while time.time() < deadline:
                self.agent.tick()
                record = self.agent.records()[0]
                if record["state"] == "succeeded":
                    break
                time.sleep(0.01)
        self.assertEqual(record["state"], "succeeded", record.get("log_tail"))
        self.assertEqual(record["attempt"], 2)
        self.assertEqual(record["metrics"]["step"], 8)
        output = self.agent._output(record)
        metrics = [json.loads(line) for line in (output / "metrics.jsonl").read_text().splitlines()]
        self.assertEqual([item["step"] for item in metrics], list(range(1, 9)),
            (output / "attempt-1.log").read_text() + "\nRESUME:\n" + (output / "attempt-2.log").read_text())
        self.assertTrue((output / "execution-attempt-2.json").exists())

    def docker_spec(self):
        return common.validate_task({"backend": "docker", "source": {
            "repo": "https://example.test/research.git", "commit": "b" * 40},
            "environments": [{"profile": "test", "image": "example/test@sha256:" + "c" * 64}],
            "command": ["python", "train.py"], "resources": {"gpu_memory_mb": 2000}})

    def test_repository_allowlist_checked_before_any_execution(self):
        record = self.record(spec=self.docker_spec())
        self.agent.online = True
        with patch("expman.agent.sys.platform", "linux"), patch.object(self.agent, "_exec") as execute:
            with self.assertRaisesRegex(ValueError, "allowed_repos"):
                self.agent._prepare(record, {"docker_available": True})
        execute.assert_not_called()

    def test_docker_recovery_inspects_existing_container_without_duplicate_create(self):
        record = self.record("starting", self.docker_spec())
        record["container_name"] = f"expman-{self.job_id}-1"
        self.agent._save(record)
        container = {"State": {"Running": True, "Status": "running"},
                     "Config": {"Labels": {"expman.job": self.job_id, "expman.node": "test"}}}
        calls = []

        def execute(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps([container]), "")

        with patch.object(self.agent, "_exec", side_effect=execute):
            self.agent._reconcile(record)
        self.assertEqual(record["state"], "running")
        self.assertEqual(calls, [["docker", "inspect", record["container_name"]]])

    def test_docker_unknown_container_owner_is_never_stopped(self):
        record = self.record("running", self.docker_spec())
        record.update(container_name="expman-wrong", stop_at=0)
        container = {"State": {"Running": True}, "Config": {"Labels": {"expman.node": "somebody-else"}}}
        with patch.object(self.agent, "_exec", return_value=subprocess.CompletedProcess([], 0, json.dumps([container]), "")) as execute:
            with self.assertRaisesRegex(ValueError, "ownership"):
                self.agent._reconcile(record)
        self.assertEqual(execute.call_count, 1)

    def test_resume_does_not_start_a_second_container_while_old_one_runs(self):
        record = self.record("interrupted", self.docker_spec())
        record.update(container_name="expman-test", ever_started=True)
        with patch.object(self.agent, "_inspect", return_value={"State": {"Running": True}}):
            self.agent._command(record, "resume", 1)
        self.assertEqual(record["attempt"], 1)
        self.assertEqual(record["command_ack"], 0)
        self.assertEqual(record["state"], "interrupted")

    def test_docker_create_uses_fixed_inputs_and_never_implicitly_pulls(self):
        record = self.record("ready", self.docker_spec())
        environment = record["spec"]["environments"][0]
        record["cached_environments"] = [{**environment, "image_id": "sha256:cached"}]
        calls = []

        def execute(argv, **kwargs):
            calls.append(argv)
            data = json.dumps([{"Id": "sha256:cached"}]) if argv[:3] == ["docker", "image", "inspect"] else ""
            return subprocess.CompletedProcess(argv, 0, data, "")

        with patch("expman.agent.sys.platform", "linux"), \
                patch("expman.agent.os.getuid", return_value=1234, create=True), \
                patch("expman.agent.os.getgid", return_value=5678, create=True), \
                patch.object(self.agent, "_exec", side_effect=execute), patch.object(self.agent, "_verify_checkout"):
            self.agent._start(record, {"gpu_uuid": "GPU-allowed", "environment": environment})
        command = next(argv for argv in calls if argv[:2] == ["docker", "create"])
        self.assertEqual(command[command.index("--user") + 1], "1234:5678")
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--read-only", command)
        self.assertEqual(command[command.index("--security-opt") + 1], "no-new-privileges")
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertNotIn("--privileged", command)
        self.assertIn("--pull=never", command)
        self.assertIn("--restart=no", command)
        self.assertIn("device=GPU-allowed", command)
        self.assertIn("EXPERIMENT_ATTEMPT=1", command)
        self.assertTrue(any("dst=/workspace/code,readonly" in item for item in command))
        self.assertNotIn("/var/run/docker.sock", " ".join(command))
        self.assertEqual(command[-3:], [environment["image"], "python", "train.py"])
        metadata = common.read_json(self.agent._output(record) / "execution-attempt-1.json")
        self.assertEqual(metadata["container_user"], "1234:5678")
        self.assertEqual(metadata["image_id"], "sha256:cached")
        self.assertEqual(metadata["gpu_uuid"], "GPU-allowed")

    def test_cuda_selection_follows_assigned_uuid_on_each_attempt_not_host_environment(self):
        record = self.record("ready", self.docker_spec())
        environment = record["spec"]["environments"][0]
        record["cached_environments"] = [{**environment, "image_id": "sha256:cached"}]
        gpus = ["GPU-11111111-1111-1111-1111-111111111111",
                "GPU-22222222-2222-2222-2222-222222222222"]
        calls = []

        def execute(argv, **kwargs):
            calls.append(argv)
            data = json.dumps([{"Id": "sha256:cached"}]) if argv[:3] == ["docker", "image", "inspect"] else ""
            return subprocess.CompletedProcess(argv, 0, data, "")

        with patch("expman.agent.sys.platform", "linux"), \
                patch("expman.agent.os.getuid", return_value=1234, create=True), \
                patch("expman.agent.os.getgid", return_value=5678, create=True), \
                patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": "wrong-host-GPU"}), \
                patch.object(self.agent, "_exec", side_effect=execute), patch.object(self.agent, "_verify_checkout"):
            for attempt, gpu in enumerate(gpus, 1):
                with self.subTest(attempt=attempt, gpu=gpu):
                    # A resumed attempt may be assigned a different verified GPU.
                    record.update(attempt=attempt, resume_from_checkpoint=attempt > 1)
                    self.agent._start(record, {"gpu_uuid": gpu, "environment": environment})
                    command = [argv for argv in calls if argv[:2] == ["docker", "create"]][-1]
                    env = [command[i + 1] for i, value in enumerate(command) if value == "--env"]
                    self.assertEqual([value for value in env if value.startswith("CUDA_VISIBLE_DEVICES=")],
                                     ["CUDA_VISIBLE_DEVICES=" + gpu])
                    self.assertEqual(command[command.index("--gpus") + 1], "device=" + gpu)
                    self.assertIn(f"EXPERIMENT_RESUME={int(attempt > 1)}", env)
                    self.assertNotIn("wrong-host-GPU", " ".join(command))
        for attempt, gpu in enumerate(gpus, 1):
            metadata = common.read_json(self.agent._output(record) / f"execution-attempt-{attempt}.json")
            self.assertEqual(metadata["gpu_uuid"], gpu)
            self.assertEqual(metadata["cuda_visible_devices"], gpu)
        self.assertEqual(record["state"], "running")

    def test_docker_start_rejects_native_windows_before_mutating_intent_or_invoking_docker(self):
        record = self.record("ready", self.docker_spec())
        before = copy.deepcopy(record)
        with patch("expman.agent.sys.platform", "win32"), patch.object(self.agent, "_exec") as execute:
            with self.assertRaisesRegex(ValueError, "native Windows"):
                self.agent._start(record, {"gpu_uuid": "GPU-allowed", "environment": record["spec"]["environments"][0]})
        execute.assert_not_called()
        self.assertEqual(record, before)

    def test_cancel_paused_task_becomes_canceled(self):
        record = self.record("paused")
        self.agent._command(record, "cancel", 2)
        self.assertEqual(record["state"], "canceled")
        self.assertEqual(record["command_ack"], 2)

    def test_created_container_obeys_drain_after_restart(self):
        record = self.record("starting", self.docker_spec())
        record.update(container_name="expman-test", gpu_uuid="GPU-test", environment=record["spec"]["environments"][0])
        self.agent.mode = "drain"
        container = {"State": {"Running": False, "Status": "created"}}
        with patch.object(self.agent, "_exec", side_effect=OSError("No test GPU")):
            snapshot = self.agent.snapshot()
        self.assertFalse(snapshot["policy"]["run_enabled"])
        with patch.object(self.agent, "_inspect", return_value=container), patch.object(self.agent, "_exec") as execute, \
                patch.object(self.agent, "snapshot", return_value=snapshot):
            self.agent._reconcile(record)
        execute.assert_not_called()
        self.assertEqual(record["state"], "starting")

    def test_preparation_runs_in_one_background_thread_without_sqlite_access(self):
        record = self.record(spec=self.docker_spec())
        self.agent.config["allowed_repos"] = [record["spec"]["source"]["repo"]]
        self.agent.online = True
        started, release = threading.Event(), threading.Event()

        def prepare_inputs(item, snapshot):
            started.set()
            release.wait(timeout=3)
            return [{"profile": "test", "image": "cached"}]

        with patch("expman.agent.sys.platform", "linux"), patch.object(self.agent, "_prepare_inputs", side_effect=prepare_inputs) as prepare:
            self.agent._prepare(record, {"docker_available": True})
            self.assertTrue(started.wait(timeout=1))
            self.assertEqual(self.agent.records()[0]["state"], "preparing")
            self.agent._prepare(record, {"docker_available": True})
            self.assertEqual(prepare.call_count, 1)
            release.set()
            self.agent.preparation["thread"].join(timeout=3)
            self.agent._poll_preparation()
        self.assertEqual(self.agent.records()[0]["state"], "ready")

    def test_missing_telemetry_provides_no_gpu(self):
        self.agent.config["gpu_policy"] = {"GPU-test": {"max_jobs": 2}}
        with patch("expman.agent.sys.platform", "linux"), patch.object(self.agent, "_exec", side_effect=OSError("missing")):
            snapshot = self.agent.snapshot()
        self.assertEqual(snapshot["gpus"], [])
        self.assertIsInstance(snapshot["disk_free_mb"], int)

    def test_archive_upload_recovers_server_offset_after_lost_ack(self):
        record = self.record("succeeded")
        data = b"experiment-result\n" * 80000
        (self.agent._output(record) / "result.bin").write_bytes(data)
        self.agent._snapshot_files(record)
        received = bytearray()
        disconnected = False

        def request(url, token, payload, **kwargs):
            nonlocal disconnected
            block = base64.b64decode(payload["data"])
            if not block:
                return {"offset": len(received), "complete": len(received) == len(data)}
            self.assertEqual(payload["offset"], len(received))
            received.extend(block)
            if not disconnected:
                disconnected = True
                raise OSError("ACK lost after server appended chunk")
            return {"offset": len(received), "complete": len(received) == len(data)}

        self.agent.online = True
        with patch("expman.agent.common.api_request", side_effect=request):
            self.agent._uploads(chunks=8)
            self.agent._uploads(chunks=8)
        self.assertEqual(bytes(received), data)
        self.assertEqual(self.agent.db.execute("SELECT complete FROM uploads").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
