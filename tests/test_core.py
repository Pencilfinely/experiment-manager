import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from tests.support import temporary_directory

from expman.common import validate_task, expand_grid, safe_child, now, atomic_json, read_json
from expman.scheduler import select_device, choose_node, eligible
from expman.sdk import Run, _atomic


IMAGE = "example/torch@sha256:" + "a" * 64


def gpu_task(memory=4000, exclusive=False):
    return validate_task({"source": {"repo": "https://example.org/repo.git", "commit": "a" * 40},
        "command": ["python", "train.py"], "environments": [{"profile": "torch", "image": IMAGE}],
        "resources": {"gpu_memory_mb": memory, "exclusive": exclusive}})


def snapshot():
    return {"policy": {"max_running": 4}, "tags": [], "assets": [], "profiles": {"torch": IMAGE},
            "allowed_repos": ["https://example.org/repo.git"],
            "free_ram_mb": 32000, "disk_free_mb": 100000,
            "gpus": [{"uuid": "GPU-1", "name": "Test", "total_mb": 16000, "free_mb": 16000,
                      "reserve_mb": 2000, "max_jobs": 3, "profiles": ["torch"]}]}


class CoreTests(unittest.TestCase):
    def test_unknown_telemetry_is_not_idle(self):
        spec, snap = gpu_task(), snapshot()
        self.assertIsNotNone(select_device(spec, snap, []))
        snap["gpus"][0]["free_mb"] = None
        self.assertIsNone(select_device(spec, snap, []))

    def test_reserved_growth_and_exclusive(self):
        spec, snap = gpu_task(5000), snapshot()
        active = [{"spec": spec, "gpu_uuid": "GPU-1"}]
        self.assertIsNotNone(select_device(spec, snap, active))
        snap["gpus"][0]["free_mb"] = 11000
        self.assertIsNone(select_device(spec, snap, active))
        self.assertIsNone(select_device(gpu_task(1000, True), snap, active))

    def test_environment_exact_match_and_stale_nodes(self):
        spec, snap = gpu_task(), snapshot()
        nodes = [{"id": "slow", "last_seen": now(), "snapshot": snap},
                 {"id": "stale", "last_seen": now() - 100, "snapshot": snap}]
        self.assertEqual(choose_node(spec, nodes, {}), "slow")
        snap["profiles"]["torch"] = "wrong"
        self.assertFalse(eligible(spec, snap))

    def test_matrix_and_validation(self):
        spec = {"backend": "demo", "params": {}, "resources": {"gpu_memory_mb": 0}}
        self.assertEqual(len(expand_grid(spec, {"seed": [1, 2], "steps": [3, 4]})), 4)
        with self.assertRaises(ValueError):
            expand_grid(spec, {"seed": list(range(257))})
        invalid = gpu_task()
        invalid["source"]["commit"] = "main"
        with self.assertRaises(ValueError):
            validate_task(invalid)

    def test_paths_and_checkpoint_integrity(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            for path in ("../bad", "C:\\bad", "/etc/passwd", "a/../../x"):
                with self.assertRaises(ValueError):
                    safe_child(root, path)
            run = Run(root, {})
            _atomic(root / "state.json", {"step": 1})
            run.publish_checkpoint("state.json", 1)
            self.assertEqual(run.checkpoint(), root / "state.json")
            _atomic(root / "state.json", {"step": 2})
            with self.assertRaises(ValueError):
                run.checkpoint()

    def test_work_hours_and_capacity(self):
        spec, snap = gpu_task(), snapshot()
        snap["policy"]["windows"] = ["22:00-07:00"]
        snap["local_time"] = "03:00"
        self.assertTrue(eligible(spec, snap))
        snap["local_time"] = "12:00"
        self.assertFalse(eligible(spec, snap))

    def test_invalid_execution_fields_are_rejected_before_assignment(self):
        for field in ("repo", "command", "gpu_count"):
            spec = gpu_task()
            if field == "repo":
                spec["source"]["repo"] = ""
            elif field == "command":
                spec["command"][0] = ""
            else:
                spec["resources"]["gpu_count"] = True
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_task(spec)

    def test_sdk_missing_resume_checkpoint_and_metadata(self):
        with temporary_directory() as temporary:
            with patch.dict(os.environ, {"EXPERIMENT_RESUME": "1", "EXPERIMENT_ATTEMPT": "2"}):
                run = Run(temporary, {})
            with self.assertRaises(ValueError):
                run.checkpoint()
            run.metric(3, loss=0.5)
            self.assertEqual(read_json(Path(temporary) / "latest_metrics.json")["attempt"], 2)
            for values in ({"time": 12}, {"attempt": 4}, {"loss": True}):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    run.metric(4, **values)
            with self.assertRaises(ValueError):
                run.metric(True, loss=1)

    def test_checkpoint_path_and_step_validation(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            run = Run(root, {})
            (root / "sub").mkdir()
            _atomic(root / "state.json", {"step": 1})
            with self.assertRaises(ValueError):
                run.publish_checkpoint("sub/../state.json", 1)
            with self.assertRaises(ValueError):
                run.publish_checkpoint("state.json", -1)
            _atomic(root / "checkpoint.json", {"path": "state.json", "sha256": "bad"})
            with self.assertRaises(ValueError):
                run.checkpoint()

    @unittest.skipUnless(os.name == "nt", "Windows-only sharing-lock retry")
    def test_atomic_json_retries_temporary_windows_reader_lock(self):
        real_replace = os.replace
        with temporary_directory() as temporary:
            for function, module in ((atomic_json, "expman.common"), (_atomic, "expman.sdk")):
                attempts = []
                def replace(source, target):
                    attempts.append(1)
                    if len(attempts) < 3:
                        raise PermissionError("simulated reader handle")
                    return real_replace(source, target)
                target = Path(temporary) / (module + ".json")
                with patch(module + ".os.replace", side_effect=replace), patch(module + ".time.sleep"):
                    function(target, {"ok": True})
                self.assertEqual(read_json(target), {"ok": True})
                self.assertEqual(len(attempts), 3)


if __name__ == "__main__":
    unittest.main()
