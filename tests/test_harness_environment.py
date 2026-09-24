import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from expman.harness_environment import _Docker, ensure_environment, validate_runtime
from tests.support import temporary_directory


GPU = "GPU-11111111-1111-1111-1111-111111111111"
DISPLAY = "GPU-22222222-2222-2222-2222-222222222222"
BASE = "localhost:5001/base@sha256:" + "a" * 64
DERIVED = "localhost:5001/expman-harness@sha256:" + "b" * 64


class EnvironmentTests(unittest.TestCase):
    def config(self):
        return {"node_id": "worker", "token": "never-in-build", "root": "/worker/runtime",
                "profiles": {"base": {"image": BASE, "verified": True, "gpu_name_patterns": ["RTX 2080 Ti"]}},
                "gpu_policy": {GPU: {"max_jobs": 1, "reserve_mb": 2048}, DISPLAY: {"max_jobs": 0}},
                "policy": {"max_running": 1, "cpu_budget": 4}, "assets": {"private-data": "/private/data"}}

    def fake(self, calls, missing=False, gpu_failure=False, pulled=True, existing_registry=None):
        def command(instance, argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[:3] == ["docker", "context", "inspect"]:
                return 0, "unix:///var/run/docker.sock\n"
            if argv[0] == "nvidia-smi":
                return 0, GPU + ", NVIDIA GeForce RTX 2080 Ti\n" + DISPLAY + ", NVIDIA GeForce RTX 2080 Ti\n"
            if "--entrypoint" in argv:
                image = argv[argv.index("--entrypoint") + 2]
                identity = argv[-1]
                if missing and image == BASE or gpu_failure and identity:
                    return 1, "ImportError: missing scipy" if not identity else "CUDA mismatch"
                return 0, "EXPMAN_ENVIRONMENT=" + json.dumps({"status": "passed", "gpu_uuid": identity,
                    "imports": json.loads(argv[-3]), "versions": {"scipy": "1.15.3"}})
            if argv[:3] == ["docker", "pull", BASE] and not pulled:
                return 1, "registry offline, cached base still exists"
            if argv[:3] == ["docker", "container", "inspect"]:
                return (0, json.dumps([existing_registry])) if existing_registry else (1, "not found")
            if argv[:2] == ["docker", "build"]:
                root = Path(kwargs["cwd"])
                self.assertEqual({p.name for p in root.iterdir()}, {"Dockerfile", "requirements.txt"})
                text = (root / "Dockerfile").read_text()
                self.assertIn("FROM ${BASE_IMAGE}", text)
                self.assertNotIn("never-in-build", text)
                self.assertNotIn("private-data", text)
                self.assertEqual((root / "requirements.txt").read_text(), "scipy==1.15.3\ntqdm==4.67.1\n")
            if argv[:2] == ["docker", "push"]:
                return 0, "tag: digest: sha256:" + "b" * 64 + " size: 856"
            return 0, ""
        return command

    def test_existing_imports_reuse_profile_and_recheck_only_enabled_gpu(self):
        calls = []
        config = self.config()
        old = copy.deepcopy(config)
        with temporary_directory() as temporary, patch.object(_Docker, "command", autospec=True, side_effect=self.fake(calls)):
            result = ensure_environment({"imports": ["scipy"]}, config, Path(temporary))
        self.assertEqual(result["config"], old)
        self.assertEqual(config, old)
        self.assertEqual(result["environments"], [{"profile": "base", "image": BASE}])
        self.assertFalse(any(argv[:2] == ["docker", "build"] for argv, _ in calls))
        checks = [argv for argv, _ in calls if "--gpus" in argv]
        self.assertEqual(len(checks), 1)
        self.assertIn("device=" + GPU, checks[0])
        self.assertIn("CUDA_VISIBLE_DEVICES=" + GPU, checks[0])
        self.assertIn("--read-only", checks[0])

    def test_missing_dependencies_build_pin_verify_and_add_profile(self):
        calls = []
        config = self.config()
        original = copy.deepcopy(config)
        runtime = {"imports": ["scipy", "tqdm"], "requirements": ["scipy==1.15.3", "tqdm==4.67.1"]}
        with temporary_directory() as temporary, patch.object(_Docker, "command", autospec=True, side_effect=self.fake(calls, missing=True)):
            result = ensure_environment(runtime, config, Path(temporary))
            repeat_calls = []
            with patch.object(_Docker, "command", new=self.fake(repeat_calls, missing=True)):
                repeated = ensure_environment(runtime, result["config"], Path(temporary))
            self.assertEqual(repeated["config"], result["config"])
            self.assertEqual(repeated["environments"], result["environments"])
            self.assertFalse(any(argv[:2] == ["docker", "build"] for argv, _ in repeat_calls))
        self.assertEqual(config, original)
        self.assertEqual(result["config"]["profiles"]["base"], original["profiles"]["base"])
        self.assertEqual(result["config"]["gpu_policy"], original["gpu_policy"])
        choice = result["environments"][0]
        self.assertTrue(choice["profile"].startswith("harness-"))
        self.assertEqual(choice["image"], DERIVED)
        self.assertTrue(result["config"]["profiles"][choice["profile"]]["verified"])
        self.assertTrue(any(argv[:2] == ["docker", "push"] for argv, _ in calls))
        self.assertIn((["docker", "pull", DERIVED], {"timeout": 7200}), calls)
        self.assertFalse(any(argv[:3] == ["docker", "container", "inspect"] for argv, _ in calls))
        builds = [argv for argv, _ in calls if argv[:2] == ["docker", "build"]]
        self.assertEqual(len(builds), 1)
        self.assertNotIn("--network", builds[0])

    def test_host_setup_builds_on_host_but_verifies_without_network(self):
        config = self.config()
        config["setup_network"] = "host"
        original = copy.deepcopy(config)
        calls = []
        runtime = {"imports": ["scipy"], "requirements": ["scipy==1.15.3", "tqdm==4.67.1"]}
        with temporary_directory() as temporary, patch.object(_Docker, "command", autospec=True,
                side_effect=self.fake(calls, missing=True, pulled=False)):
            result = ensure_environment(runtime, config, Path(temporary))
        self.assertEqual(config, original)
        self.assertEqual(result["config"]["setup_network"], "host")
        self.assertTrue(result["environments"][0]["image"].startswith("localhost:5002/"))
        starts = [argv for argv, _ in calls if argv[:3] == ["docker", "run", "-d"]]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][starts[0].index("--network") + 1], "host")
        self.assertIn("REGISTRY_HTTP_ADDR=127.0.0.1:5002", starts[0])
        self.assertIn("REGISTRY_HTTP_DEBUG_ADDR=", starts[0])
        self.assertIn("expman.component=harness-registry", starts[0])
        self.assertNotIn("-p", starts[0])
        builds = [argv for argv, _ in calls if argv[:2] == ["docker", "build"]]
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0][builds[0].index("--network") + 1], "host")
        checks = [argv for argv, _ in calls if "--entrypoint" in argv]
        self.assertTrue(any("--gpus" in argv for argv in checks))
        self.assertTrue(all(argv[argv.index("--network") + 1] == "none" for argv in checks))

    def test_unknown_setup_network_is_rejected_before_docker_commands(self):
        for mode in (None, "", "none", "bridge; echo unsafe", ["host"]):
            with self.subTest(mode=mode), temporary_directory() as temporary, patch.object(_Docker, "command") as command:
                config = self.config()
                config["setup_network"] = mode
                with self.assertRaisesRegex(ValueError, "setup_network must be bridge or host"):
                    ensure_environment({}, config, temporary)
                command.assert_not_called()

    def test_existing_host_registry_is_reused_or_started(self):
        for running in (True, False):
            existing = {"Config": {"Labels": {"expman.component": "harness-registry"},
                                   "Env": ["REGISTRY_HTTP_ADDR=127.0.0.1:5002", "REGISTRY_HTTP_DEBUG_ADDR=", "OTEL_TRACES_EXPORTER=none"]},
                        "HostConfig": {"NetworkMode": "host", "PortBindings": None},
                        "State": {"Running": running}}
            calls = []
            with self.subTest(running=running), temporary_directory() as temporary, \
                    patch.object(_Docker, "command", autospec=True, side_effect=self.fake(calls, existing_registry=existing)):
                docker = _Docker(temporary, setup_network="host")
                self.assertEqual(docker.registry(BASE, base_pulled=False), "localhost:5002")
            self.assertEqual([argv for argv, _ in calls],
                             [["docker", "container", "inspect", "expman-harness-registry"]] +
                             ([] if running else [["docker", "start", "expman-harness-registry"]]))

    def test_bridge_registry_accepts_default_and_legacy_network_metadata(self):
        for mode in (None, "default", "bridge"):
            existing = {"Config": {"Labels": {"expman.component": "harness-registry"}},
                        "HostConfig": {"PortBindings": {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5002"}]}},
                        "State": {"Running": True}}
            if mode is not None:
                existing["HostConfig"]["NetworkMode"] = mode
            calls = []
            with self.subTest(mode=mode), temporary_directory() as temporary, \
                    patch.object(_Docker, "command", autospec=True, side_effect=self.fake(calls, existing_registry=existing)):
                self.assertEqual(_Docker(temporary).registry(BASE, base_pulled=False), "localhost:5002")
            self.assertEqual([argv for argv, _ in calls], [["docker", "container", "inspect", "expman-harness-registry"]])

    def test_registry_network_conflicts_are_rejected_without_mutation(self):
        expected = {"Config": {"Labels": {"expman.component": "harness-registry"},
                               "Env": ["REGISTRY_HTTP_ADDR=127.0.0.1:5002", "REGISTRY_HTTP_DEBUG_ADDR="]},
                    "HostConfig": {"NetworkMode": "host"}, "State": {"Running": False}}
        conflicts = []
        for debug in ([], ["REGISTRY_HTTP_DEBUG_ADDR=:5001"],
                      ["REGISTRY_HTTP_DEBUG_ADDR=127.0.0.1:5003"],
                      ["REGISTRY_HTTP_DEBUG_ADDR=", "REGISTRY_HTTP_DEBUG_ADDR=:5001"]):
            item = copy.deepcopy(expected)
            item["Config"]["Env"] = ["REGISTRY_HTTP_ADDR=127.0.0.1:5002", *debug]
            conflicts.append(("host", item))
        for addresses in (None, [], ["REGISTRY_HTTP_ADDR=0.0.0.0:5002"],
                          ["REGISTRY_HTTP_ADDR=127.0.0.1:5001"],
                          ["REGISTRY_HTTP_ADDR=127.0.0.1:5002", "REGISTRY_HTTP_ADDR=0.0.0.0:5002"]):
            item = copy.deepcopy(expected)
            item["Config"]["Env"] = addresses
            conflicts.append(("host", item))
        for mode in ("bridge", "default", "none"):
            item = copy.deepcopy(expected)
            item["HostConfig"]["NetworkMode"] = mode
            conflicts.append(("host", item))
        item = copy.deepcopy(expected)
        item["HostConfig"]["PortBindings"] = {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5002"}]}
        conflicts.extend([("host", item), ("bridge", item)])
        item = copy.deepcopy(expected)
        item["Config"]["Labels"] = {}
        conflicts.append(("host", item))
        for mode, existing in conflicts:
            calls = []
            with self.subTest(mode=mode, existing=existing), temporary_directory() as temporary, \
                    patch.object(_Docker, "command", autospec=True, side_effect=self.fake(calls, existing_registry=existing)):
                with self.assertRaisesRegex(ValueError, "left unchanged"):
                    _Docker(temporary, setup_network=mode).registry(BASE, base_pulled=False)
            self.assertEqual([argv for argv, _ in calls], [["docker", "container", "inspect", "expman-harness-registry"]])

    def test_restarting_registry_is_rejected_before_building(self):
        existing = {"Config": {"Labels": {"expman.component": "harness-registry"},
                               "Env": ["REGISTRY_HTTP_ADDR=127.0.0.1:5002", "REGISTRY_HTTP_DEBUG_ADDR="]},
                    "HostConfig": {"NetworkMode": "host"}, "State": {"Running": True, "Restarting": True}}
        calls = []
        with temporary_directory() as temporary, \
                patch.object(_Docker, "command", autospec=True, side_effect=self.fake(calls, existing_registry=existing)):
            with self.assertRaisesRegex(RuntimeError, "restarting"):
                _Docker(temporary, setup_network="host").registry(BASE, base_pulled=False)
        self.assertEqual([argv for argv, _ in calls], [["docker", "container", "inspect", "expman-harness-registry"]])

    def test_failed_cuda_check_never_changes_input_config_or_returns_environment(self):
        config = self.config()
        original = copy.deepcopy(config)
        calls = []
        with temporary_directory() as temporary, patch.object(_Docker, "command", autospec=True, side_effect=self.fake(calls, gpu_failure=True)):
            with self.assertRaisesRegex(RuntimeError, "No runtime passed.*"):
                ensure_environment({"imports": []}, config, Path(temporary))
        self.assertEqual(config, original)

    def test_dependency_declarations_cannot_inject_pip_flags_urls_or_shell(self):
        invalid = [{"requirements": ["scipy"]}, {"requirements": ["--index-url=https://elsewhere/"]},
                   {"requirements": ["scipy==1.15.3; echo x"]}, {"imports": ["os;print('bad')"]},
                   {"requirements": ["torch==2.7", "torch==2.8"]}]
        for runtime in invalid:
            with self.subTest(runtime=runtime), self.assertRaises(ValueError):
                validate_runtime(runtime)

    def test_fallback_registry_is_local_owned_and_unrelated_container_is_preserved(self):
        calls = []
        config = self.config()
        runtime = {"imports": ["scipy"], "requirements": ["scipy==1.15.3", "tqdm==4.67.1"]}
        with temporary_directory() as temporary, patch.object(_Docker, "command", autospec=True, side_effect=self.fake(calls, missing=True, pulled=False)):
            result = ensure_environment(runtime, config, Path(temporary))
        self.assertTrue(result["environments"][0]["image"].startswith("localhost:5002/"))
        starts = [argv for argv, _ in calls if argv[:3] == ["docker", "run", "-d"]]
        self.assertEqual(len(starts), 1)
        self.assertIn("127.0.0.1:5002:5000", starts[0])
        self.assertIn("expman.component=harness-registry", starts[0])
        calls = []
        with temporary_directory() as temporary, patch.object(_Docker, "command", autospec=True,
                side_effect=self.fake(calls, missing=True, pulled=False, existing_registry={"Config": {"Labels": {}}})):
            with self.assertRaisesRegex(RuntimeError, "unrelated container"):
                ensure_environment(runtime, config, Path(temporary))
        self.assertFalse(any(argv[:2] in (["docker", "stop"], ["docker", "rm"], ["docker", "start"]) for argv, _ in calls))


if __name__ == "__main__":
    unittest.main()
