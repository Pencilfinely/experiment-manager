import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from expman.diagnostics import inspect_node
from tests.support import temporary_directory


TOKEN = "diagnostic-secret-never-display"
IMAGE = "local/test@sha256:" + "a" * 64


def configuration(root, demo=False):
    return {"node_id": "worker", "hub_url": "http://127.0.0.1:8765", "token": TOKEN,
            "root": str(root), "allow_demo": demo,
            "assets": {}, "profiles": {} if demo else {
                "test": {"image": IMAGE, "verified": True, "gpu_name_patterns": ["2080"]}},
            "gpu_policy": {} if demo else {"GPU-test": {"reserve_mb": 2048, "max_jobs": 2}},
            "allowed_repos": [] if demo else ["https://example.invalid/code.git"],
            "policy": {"min_disk_free_mb": 1}}


class DiagnosticsTests(unittest.TestCase):
    def check(self, result, key):
        return next(item for item in result["checks"] if item["id"] == key)

    def write_config(self, temporary, demo=False, **changes):
        path = Path(temporary) / "node.json"
        config = configuration(Path(temporary) / "unused" / "node", demo)
        config.update(changes)
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def fake_run(self, argv, **kwargs):
        self.commands.append((argv, kwargs))
        if "--version" in argv:
            output = "git version 2.47" if argv[0] == "git" else "Docker version 28.5.1"
        elif "context" in argv:
            output = "unix:///var/run/docker.sock"
        elif "info" in argv:
            output = "28.5.1"
        elif argv[0] == "nvidia-smi":
            output = "GPU-test, NVIDIA GeForce RTX 2080 Ti, 11264, 10000\n"
        else:
            raise AssertionError("Unexpected command " + repr(argv))
        return subprocess.CompletedProcess(argv, 0, output, "")

    def linux_inspection(self, path, command_side_effect=None, environ=None):
        self.commands = []
        with patch("expman.diagnostics.sys.platform", "linux"), \
                patch("expman.diagnostics.shutil.which", side_effect=lambda name: name), \
                patch("expman.diagnostics.os.environ", environ or {}), \
                patch("expman.diagnostics.subprocess.run", side_effect=command_side_effect or self.fake_run), \
                patch("expman.agent.common.api_request", side_effect=AssertionError("No network")), \
                patch("expman.agent.sqlite3.connect", side_effect=AssertionError("No database")):
            return inspect_node(path)

    def test_valid_linux_snapshot_uses_only_read_commands_and_no_state(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary)
            before = list(Path(temporary).rglob("*"))
            result = self.linux_inspection(path)
            self.assertTrue(result["ready"])
            self.assertEqual(result["readiness_scope"], "local_preflight")
            self.assertFalse(result["gpu_runtime_validated"])
            self.assertEqual(self.check(result, "gpus")["usable_count"], 1)
            self.assertEqual(self.check(result, "gpu_runtime")["status"], "warn")
            self.assertEqual(before, list(Path(temporary).rglob("*")))
            self.assertIsNone(result["snapshot"]["pending_uploads"])
            for argv, kwargs in self.commands:
                self.assertFalse(kwargs["shell"])
                self.assertLessEqual(kwargs["timeout"], 8)
                self.assertNotIn("pull", argv)
                self.assertNotIn("run", argv)
                if "info" in argv:
                    self.assertEqual(argv[:3], ["docker", "--host", "unix:///var/run/docker.sock"])
            self.assertNotIn(TOKEN, json.dumps(result))

    def test_native_windows_demo_can_pass_without_git_or_docker(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary, demo=True)
            with patch("expman.diagnostics.sys.platform", "win32"), \
                    patch("expman.diagnostics.shutil.which", return_value=None), \
                    patch("expman.diagnostics.subprocess.run", side_effect=AssertionError("No commands")):
                result = inspect_node(path)
            self.assertTrue(result["ready"])
            self.assertEqual(result["mode"], "demo")
            self.assertEqual(self.check(result, "platform")["status"], "warn")
            self.assertFalse((Path(temporary) / "unused").exists())

    def test_native_windows_gpu_config_fails_with_wsl_instruction(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary)
            with patch("expman.diagnostics.sys.platform", "win32"), patch("expman.diagnostics.shutil.which", return_value=None):
                result = inspect_node(path)
            self.assertFalse(result["ready"])
            self.assertIn("WSL2", self.check(result, "platform")["message"])

    def test_invalid_json_returns_structured_failure_without_commands(self):
        with temporary_directory() as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text("{ token: " + TOKEN, encoding="utf-8")
            with patch("expman.diagnostics.subprocess.run", side_effect=AssertionError("No commands")):
                result = inspect_node(path)
            self.assertFalse(result["ready"])
            self.assertIsNone(result["snapshot"])
            self.assertNotIn(TOKEN, json.dumps(result))

    def test_invalid_shapes_report_failure_instead_of_crashing(self):
        changes = [{"assets": []}, {"profiles": {"broken": []}}, {"gpu_policy": {"GPU-test": None}},
                   {"policy": {"max_running": "two"}}, {"policy": {"windows": ["99:00-12:00"]}},
                   {"hub_url": "http://user:pass@localhost"}, {"allow_demo": "true"}]
        with temporary_directory() as temporary:
            for change in changes:
                with self.subTest(change=change):
                    path = self.write_config(temporary, **change)
                    result = inspect_node(path)
                    self.assertFalse(result["ready"])
                    self.assertEqual(self.check(result, "config")["status"], "fail")

    def test_assets_reject_relative_and_missing_but_accept_shared_directory(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary, assets={"relative": "data", "missing": str(Path(temporary) / "missing"), "shared": temporary})
            result = self.linux_inspection(path)
            self.assertFalse(result["ready"])
            self.assertEqual(self.check(result, "assets")["unavailable_ids"], ["relative", "missing"])
            self.assertEqual(result["snapshot"]["assets"], ["shared"])

    def test_unverified_image_never_makes_gpu_available(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary, profiles={"test": {"image": IMAGE, "gpu_name_patterns": ["2080"], "verified": False}})
            result = self.linux_inspection(path)
            self.assertFalse(result["ready"])
            self.assertEqual(self.check(result, "profiles")["status"], "fail")
            self.assertEqual(self.check(result, "gpus")["usable_count"], 0)

    def test_mutable_image_tag_is_a_failure_even_when_marked_verified(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary, profiles={"test": {"image": "pytorch:latest", "verified": True, "gpu_name_patterns": ["2080"]}})
            result = self.linux_inspection(path)
            self.assertFalse(result["ready"])
            self.assertEqual(self.check(result, "profiles")["invalid_digest_names"], ["test"])

    def test_remote_context_never_contacts_daemon(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary)
            for endpoint in ("tcp://127.0.0.1:2375", "ssh://other-node"):
                with self.subTest(endpoint=endpoint):
                    result = self.linux_inspection(path, environ={"DOCKER_HOST": endpoint})
                    self.assertFalse(result["ready"])
                    self.assertEqual(self.check(result, "docker_endpoint")["status"], "fail")
                    self.assertFalse(any("info" in argv for argv, _ in self.commands))
                    self.assertFalse(any(argv[0] == "nvidia-smi" for argv, _ in self.commands))

    def test_explicit_context_takes_precedence_over_host(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary)
            result = self.linux_inspection(path, environ={"DOCKER_CONTEXT": "local-context", "DOCKER_HOST": "ssh://remote"})
            self.assertTrue(result["ready"])
            self.assertTrue(any("local-context" in argv for argv, _ in self.commands))
            for argv, kwargs in self.commands:
                if "--host" in argv:
                    self.assertNotIn("DOCKER_CONTEXT", kwargs["env"])
                    self.assertNotIn("DOCKER_HOST", kwargs["env"])

    def test_command_timeout_and_os_error_are_structured(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary)
            for error in (subprocess.TimeoutExpired("docker", 8), OSError("secret " + TOKEN)):
                with self.subTest(error=type(error).__name__):
                    result = self.linux_inspection(path, command_side_effect=error)
                    self.assertFalse(result["ready"])
                    self.assertNotIn(TOKEN, json.dumps(result))
                    self.assertEqual(self.check(result, "docker_cli")["status"], "fail")

    def test_node_token_redacted_even_from_unexpected_command_output(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary)
            def with_secret(argv, **kwargs):
                result = self.fake_run(argv, **kwargs)
                if "--version" in argv:
                    result.stdout += TOKEN
                return result
            result = self.linux_inspection(path, command_side_effect=with_secret)
            self.assertNotIn(TOKEN, json.dumps(result))

    def test_token_in_snapshot_keys_is_redacted_and_json_is_finite(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary, profiles={TOKEN: {"image": IMAGE, "verified": True, "gpu_name_patterns": ["2080"]}},
                                     policy={"extra": float("nan")})
            result = self.linux_inspection(path)
            encoded = json.dumps(result, allow_nan=False)
            self.assertNotIn(TOKEN, encoded)
            self.assertIn("[REDACTED]", encoded)

    def test_low_disk_and_file_root_do_not_create_anything(self):
        with temporary_directory() as temporary:
            path = self.write_config(temporary, policy={"min_disk_free_mb": 10**20})
            result = self.linux_inspection(path)
            self.assertFalse(result["ready"])
            self.assertEqual(self.check(result, "disk")["status"], "fail")
            path = self.write_config(temporary, root=str(path))
            result = self.linux_inspection(path)
            self.assertFalse(result["ready"])
            self.assertEqual(self.check(result, "disk")["status"], "fail")


if __name__ == "__main__":
    unittest.main()
