import copy
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from expman import common
from expman.launcher import InstanceLock
from expman.worker_upgrade import agent_is_running, choose_config, discover_configs, process_config, run_existing
from tests.support import temporary_directory


class WorkerUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.home = self.root / "home"
        self.home.mkdir()
        self.path = self.home / "expman-first-run/video-games-a0450b0222fa3f34/node.ready.json"
        self.config = {"node_id": "win2080", "token": "private-node-token", "hub_url": "http://127.0.0.1:8765",
            "root": "runtime", "policy": {"run_enabled": False, "max_running": 2},
            "profiles": {"old": {"verified": True, "image": "local/image@sha256:" + "a" * 64}},
            "assets": {"Video_Games": "/existing/data"}, "allowed_repos": ["/existing/research"],
            "tags": ["win2080"], "task_templates": [{"name": "existing"}], "gpu_policy": {"GPU-old": {"max_jobs": 2}}}
        common.atomic_json(self.path, self.config)

    def tearDown(self):
        self.temporary.__exit__(None, None, None)

    def test_recognizes_legacy_and_launcher_argv_resolving_relative_paths(self):
        cwd = self.home / "expman-first-run/experiment_manager"
        args = ["python3", "-m", "expman", "agent", "--config", "../video-games-a0450b0222fa3f34/node.ready.json"]
        self.assertEqual(process_config(args, cwd, self.home), self.path.resolve())
        self.assertEqual(process_config(["python3", "-m", "expman.launcher", "worker"], cwd, self.home),
                         self.home / ".local/share/experiment-manager/worker/node.ready.json")
        self.assertEqual(process_config(["python3", "-m", "expman.launcher", "worker", "--root=../custom"], cwd, self.home),
                         (cwd / "../custom/node.ready.json").resolve())
        self.assertIsNone(process_config(["python3", "-m", "expman", "doctor", "--config", str(self.path)], cwd, self.home))
        self.assertIsNone(process_config(["python3", "unrelated.py", "agent", "--config", str(self.path)], cwd, self.home))

    def test_discovery_of_stopped_legacy_node_is_read_only_and_redacts_credentials(self):
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        candidates = discover_configs(self.home, self.root / "nonexistent-proc")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["path"], str(self.path))
        self.assertEqual(candidates[0]["root"], str(self.path.parent / "runtime"))
        self.assertNotIn("private-node-token", json.dumps(candidates))
        self.assertNotIn("token", json.dumps(candidates))
        after = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.path.parent / "runtime").exists())
        self.assertEqual(choose_config(candidates)["path"], str(self.path))

    def test_ambiguous_configs_require_selection_and_one_live_config_is_preferred(self):
        first = discover_configs(self.home, self.root / "no-proc")[0]
        second_path = self.home / ".local/share/experiment-manager/worker/node.ready.json"
        common.atomic_json(second_path, {**self.config, "node_id": "different-node"})
        candidates = discover_configs(self.home, self.root / "no-proc")
        prompts = []
        selected = choose_config(candidates, input_fn=lambda prompt: "2", print_fn=prompts.append)
        self.assertEqual(selected, candidates[1])
        self.assertTrue(any(str(self.path) in line for line in prompts))
        live = copy.deepcopy(first)
        live["pids"] = [1234]
        self.assertEqual(choose_config([candidates[0], live], input_fn=lambda _: self.fail("Unexpected selection prompt")), live)
        self.assertEqual(choose_config(candidates, supplied=str(self.path)), first)

    def test_existing_lock_is_observed_without_mutating_or_creating_state(self):
        runtime = self.path.parent / "runtime"
        self.assertFalse(agent_is_running(runtime))
        self.assertFalse(runtime.exists())
        with InstanceLock(runtime / "agent.lock"):
            self.assertTrue(agent_is_running(runtime))
        before = (runtime / "agent.lock").read_bytes()
        self.assertFalse(agent_is_running(runtime))
        self.assertEqual((runtime / "agent.lock").read_bytes(), before)

    def test_switch_waits_for_old_owner_and_reuses_exact_configuration_without_setup(self):
        candidate = discover_configs(self.home, self.root / "no-proc")[0]
        before = self.path.read_bytes()
        messages, prompts, started = [], [], []
        with patch("expman.worker_upgrade.sys.platform", "linux"), \
                patch("expman.worker_upgrade.os.geteuid", return_value=1000, create=True), \
                patch("expman.worker_upgrade.shutil.which", return_value="/usr/bin/fake"), \
                patch("expman.worker_upgrade.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "29.7", "")), \
                patch("expman.worker_upgrade.agent_is_running", side_effect=[True, False]), \
                patch("expman.worker_setup.WorkerSetup", side_effect=AssertionError("Must not reconfigure the worker")), \
                patch.dict(os.environ, {}, clear=False):
            run_existing(candidate, input_fn=lambda prompt: prompts.append(prompt) or "", print_fn=messages.append,
                         runner=lambda path: started.append(path))
        self.assertEqual(started, [str(self.path)])
        self.assertEqual(len(prompts), 1)
        self.assertTrue(any("Ctrl+C" in message for message in messages))
        self.assertNotIn("private-node-token", "\n".join(messages + prompts))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(common.read_json(self.path), self.config)


if __name__ == "__main__":
    unittest.main()
