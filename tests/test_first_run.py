import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

from scripts import sasrec_first_run as helper
from tests.support import temporary_directory


GPU1 = "GPU-11111111-1111-1111-1111-111111111111"
GPU2 = "GPU-22222222-2222-2222-2222-222222222222"
IMAGE = "local/sasrec@sha256:" + "a" * 64
INVENTORY = f"{GPU1}, NVIDIA GeForce RTX 2080 Ti, 11264, 10000\n{GPU2}, NVIDIA GeForce RTX 2080 Ti, 11264, 9000"


class FirstRunTests(unittest.TestCase):
    def test_pack_keeps_current_sources_and_excludes_data_results_git_and_cache(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            project = root / "SASRec_Original"
            (project / "src" / "nested").mkdir(parents=True)
            (project / "src" / "experiment.py").write_bytes(b"# current uncommitted bytes\n")
            (project / "src" / "nested" / "model.py").write_bytes(b"# model\n")
            for relative in ("data/private.py", "results/train.py", ".git/token", "src/__pycache__/bad.py", "src/token.json"):
                path = project / relative
                path.parent.mkdir(exist_ok=True, parents=True)
                path.write_text("do not pack", encoding="utf-8")
            output = root / "snapshot.zip"
            with patch.object(helper.subprocess, "run", side_effect=AssertionError("pack must not inspect Git")):
                helper.pack(project, output)
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertIn("experiment_manager/examples/sasrec-task.template.json", names)
                self.assertIn("experiment_manager/scripts/sasrec_first_run.py", names)
                self.assertIn("experiment_manager/expman/adapters/sasrec.py", names)
                self.assertIn("experiment_manager/deploy/Dockerfile.sasrec", names)
                self.assertFalse(any(any(part in name for part in ("private.py", "train.py", "/.git/", "bad.py", "token.json")) for name in names))
                manifest = json.loads(archive.read("source-manifest.json"))
                self.assertFalse(manifest["git_inspected_or_modified"])
                for name, info in manifest["files"].items():
                    self.assertEqual(info["sha256"], hashlib.sha256(archive.read(name)).hexdigest())
            before = output.read_bytes()
            with self.assertRaisesRegex(ValueError, "already exists"):
                helper.pack(project, output)
            self.assertEqual(before, output.read_bytes())

    def _fixture(self, root):
        repo = root / "source"
        (repo / "SASRec_Original" / "src").mkdir(parents=True)
        (repo / "SASRec_Original" / "src" / "experiment.py").write_text("# snapshot\n", encoding="utf-8")
        for args in (["init", "-q"], ["add", "SASRec_Original"],
                     ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "snapshot"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        # Windows Git creates read-only object files; make only our disposable
        # test repo removable by the existing temporary_directory helper.
        if os.name == "nt":
            for path in (repo / ".git").rglob("*"):
                if path.is_file():
                    path.chmod(0o600)
        config = root / "node.json"
        config.write_text(json.dumps({"node_id": "win2080", "hub_url": "http://127.0.0.1:8765",
                                      "token": "private-test-token", "root": "old", "poll_seconds": 5}), encoding="utf-8")
        report = root / "smoke-report.json"
        report.write_text(json.dumps({"status": "passed", "device": "cuda", "gpu_container_verified": False}), encoding="utf-8")
        return repo, config, report

    def _configure(self, repo, config, reports, output, gpus=None, image=IMAGE, inventory=INVENTORY):
        real_run = helper._run
        def execute(argv):
            return inventory if argv[0] == "nvidia-smi" else real_run(argv)
        # Production configure is Linux-only. Python 3.13's Windows 0700 ACL
        # removes the sandbox account's access, so keep inherited ACLs here.
        real_mkdir = os.mkdir
        def mkdir(path, mode=0o777, **kwargs):
            return real_mkdir(path, 0o777 if os.name == "nt" else mode, **kwargs)
        with patch.object(helper.sys, "platform", "linux"), patch.object(helper, "_run", side_effect=execute), patch.object(os, "mkdir", side_effect=mkdir):
            return helper.configure(config, repo, image, gpus or [GPU1], reports, output)

    def test_pack_rejects_source_symlink_before_creating_archive(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            project = root / "SASRec_Original"
            (project / "src" / "linked").mkdir(parents=True)
            (project / "src" / "experiment.py").write_text("# source", encoding="utf-8")
            original = Path.is_symlink
            def is_symlink(path):
                return path.name == "linked" or original(path)
            with patch.object(Path, "is_symlink", is_symlink), self.assertRaisesRegex(ValueError, "symlinks"):
                helper.pack(project, root / "snapshot.zip")
            self.assertFalse((root / "snapshot.zip").exists())

    def test_configure_creates_usable_task_and_preserves_credentials_only_in_private_config(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            repo, config, report = self._fixture(root)
            original = config.read_bytes()
            output = root / "ready"
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                self._configure(repo, config, [report], output)
            node = json.loads((output / "win2080.ready.json").read_text())
            task = json.loads((output / "sasrec-toy.task.json").read_text())
            record = (output / "deployment-record.json").read_text()
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(node["token"], "private-test-token")
            self.assertEqual(node["hub_url"], "http://127.0.0.1:8765")
            self.assertNotIn("private-test-token", record + captured.getvalue() + json.dumps(task))
            self.assertEqual(node["gpu_policy"][GPU2]["max_jobs"], 0)
            self.assertEqual(node["gpu_policy"][GPU1], {"reserve_mb": 2048, "max_jobs": 1})
            self.assertEqual(node["policy"]["max_running"], 1)
            self.assertEqual(node["stop_grace_seconds"], 600)
            self.assertEqual(task["source"]["repo"], str(repo.resolve()))
            self.assertEqual(len(task["source"]["commit"]), 40)
            self.assertEqual(task["resources"], {"gpu_memory_mb": 2048, "cpu": 2, "ram_mb": 4096, "exclusive": True})
            self.assertEqual(task["params"]["epochs"], 3)
            self.assertEqual(task["params"]["gpu_id"], 0)
            self.assertEqual(task["tags"], ["win2080"])
            self.assertIn("synthetic_toy", task["metric_protocol"])
            self.assertEqual((Path(node["assets"][helper.ASSET]) / "Toy" / "Toy.train.txt").read_text(), "1 1 2 3 4\n2 2 3 5\n")
            with self.assertRaisesRegex(ValueError, "already exists"):
                self._configure(repo, config, [report], output)

    def test_rejects_failed_cpu_or_missing_smoke_report_before_creating_outputs(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            repo, config, report = self._fixture(root)
            for value in ({"status": "failed", "device": "cuda"}, {"status": "passed", "device": "cpu"}, {}):
                report.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(report=value), self.assertRaisesRegex(ValueError, "status=passed"):
                    self._configure(repo, config, [report], root / "ready")
                self.assertFalse((root / "ready").exists())

    def test_rejects_bad_digest_repo_gpu_memory_and_pairing(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            repo, config, report = self._fixture(root)
            cases = [{"image": "sasrec:latest"}, {"gpus": [GPU1, GPU2]},
                     {"gpus": ["GPU-not-real"]}, {"inventory": INVENTORY.replace("10000", "N/A")},
                     {"inventory": INVENTORY.replace("10000", "99999")},
                     {"inventory": INVENTORY.replace("2080 Ti", "RTX 3090")}]
            for case in cases:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    self._configure(repo, config, [report], root / "ready", **case)
                self.assertFalse((root / "ready").exists())
            for bad_repo in (Path("relative-source"), root, repo / "SASRec_Original"):
                with self.subTest(repo=bad_repo), self.assertRaises(ValueError):
                    self._configure(bad_repo, config, [report], root / "ready")
            with self.assertRaisesRegex(ValueError, "outside"):
                self._configure(repo, config, [report], repo / "ready")
            (repo / "SASRec_Original" / "src" / "uncommitted.py").write_text("# changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs from HEAD"):
                self._configure(repo, config, [report], root / "ready")

    def test_two_gpus_require_two_reports_and_keep_initial_single_task_limit(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            repo, config, report = self._fixture(root)
            second_report = root / "second-smoke-report.json"
            second_report.write_bytes(report.read_bytes())
            output = root / "ready"
            self._configure(repo, config, [report, second_report], output, gpus=[GPU1, GPU2])
            node = json.loads((output / "win2080.ready.json").read_text())
            self.assertEqual([node["gpu_policy"][gpu]["max_jobs"] for gpu in (GPU1, GPU2)], [1, 1])
            self.assertEqual(node["policy"]["max_running"], 1)
            self.assertEqual(len(json.loads((output / "deployment-record.json").read_text())["gpus"]), 2)

    def test_script_help_works_outside_repository(self):
        with temporary_directory() as temporary:
            result = subprocess.run([sys.executable, str(helper.SOFTWARE_ROOT / "scripts" / "sasrec_first_run.py"), "configure", "--help"],
                                    cwd=temporary, check=True, capture_output=True, text=True)
            self.assertIn("not hardware proof", result.stdout)


if __name__ == "__main__":
    unittest.main()
