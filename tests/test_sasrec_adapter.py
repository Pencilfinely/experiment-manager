"""Managed checkpoint invariants and optional real SASRec CPU integration.

Set EXPMAN_SASREC_PROJECT to a local SASRec_Original directory to run real
training. Tests only read that source tree; all data and outputs are isolated.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

from expman.adapters.sasrec import publish_bundle, restore_bundle
from expman.common import atomic_json, read_json, sha256_file
from expman.sdk import Run
from tests.support import temporary_directory


class SasrecBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.run = Run(self.root / "run", {})
        self.directory = self.run.output / "sasrec" / "Toy" / "managed"
        self.directory.mkdir(parents=True)
        self.config = {"dataset": "Toy", "seed": 42}
        atomic_json(self.directory / "config.json", self.config)
        (self.directory / "sasrec_last.pt").write_bytes(b"last epoch one")

    def tearDown(self):
        self.temporary.__exit__(None, None, None)

    def replacement_bundle(self, entries, metadata=None):
        bundle = self.run.output / "replacement.zip"
        if metadata is None:
            metadata = {"schema": 1, "epoch": 1, "files": {
                name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}}
        with zipfile.ZipFile(bundle, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
            archive.writestr("bundle.json", json.dumps(metadata))
        self.run.publish_checkpoint(bundle, 1)
        return bundle

    def test_bundle_restores_exact_pair_after_working_files_are_torn(self):
        (self.directory / "sasrec_best.pt").write_bytes(b"best epoch one")
        bundle = publish_bundle(self.run, self.directory, 1)
        self.assertEqual(self.run.checkpoint(), bundle)
        (self.directory / "sasrec_last.pt").write_bytes(b"partial next epoch")
        (self.directory / "sasrec_best.pt").unlink()
        (self.directory / "metrics.json").write_text("stale evaluation", encoding="utf-8")
        for _ in range(2):
            self.assertEqual(restore_bundle(self.run, self.directory, self.config), 1)
            self.assertEqual((self.directory / "sasrec_last.pt").read_bytes(), b"last epoch one")
            self.assertEqual((self.directory / "sasrec_best.pt").read_bytes(), b"best epoch one")
        self.assertFalse((self.directory / "metrics.json").exists())

    def test_prevalidation_bundle_removes_uncommitted_best(self):
        publish_bundle(self.run, self.directory, 1)
        (self.directory / "sasrec_best.pt").write_bytes(b"newer but uncommitted")
        restore_bundle(self.run, self.directory, self.config)
        self.assertFalse((self.directory / "sasrec_best.pt").exists())

    def test_failed_publication_keeps_previous_resume_authority(self):
        original = publish_bundle(self.run, self.directory, 1)
        manifest = read_json(self.run.output / "checkpoint.json")
        (self.directory / "sasrec_last.pt").write_bytes(b"epoch two")
        with patch.object(self.run, "publish_checkpoint", side_effect=OSError("interrupted manifest")):
            with self.assertRaises(OSError):
                publish_bundle(self.run, self.directory, 2)
        self.assertTrue(original.is_file())
        self.assertEqual(read_json(self.run.output / "checkpoint.json"), manifest)
        self.assertEqual(self.run.checkpoint(), original)
        latest = publish_bundle(self.run, self.directory, 2)
        self.assertEqual(list((self.run.output / "checkpoints").glob("*.zip")), [latest])

    def test_bad_member_digest_changes_no_working_file(self):
        entries = {"config.json": json.dumps(self.config).encode(), "sasrec_last.pt": b"bad payload"}
        metadata = {"schema": 1, "epoch": 1, "files": {
            name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}}
        metadata["files"]["sasrec_last.pt"] = "0" * 64
        self.replacement_bundle(entries, metadata)
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with self.assertRaisesRegex(ValueError, "checksum"):
            restore_bundle(self.run, self.directory, self.config)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_unexpected_zip_member_cannot_escape_output(self):
        entries = {"config.json": json.dumps(self.config).encode(), "sasrec_last.pt": b"valid",
                   "../../escape.txt": b"must not be extracted"}
        self.replacement_bundle(entries)
        with self.assertRaisesRegex(ValueError, "members"):
            restore_bundle(self.run, self.directory, self.config)
        self.assertFalse((self.root / "escape.txt").exists())
        self.assertEqual((self.directory / "sasrec_last.pt").read_bytes(), b"last epoch one")

    def test_changed_config_is_rejected_before_restore(self):
        publish_bundle(self.run, self.directory, 1)
        (self.directory / "sasrec_last.pt").write_bytes(b"working bytes")
        with self.assertRaisesRegex(ValueError, "config"):
            restore_bundle(self.run, self.directory, {**self.config, "seed": 99})
        self.assertEqual((self.directory / "sasrec_last.pt").read_bytes(), b"working bytes")


REAL_PROJECT = os.environ.get("EXPMAN_SASREC_PROJECT")
REAL_AVAILABLE = bool(REAL_PROJECT and importlib.util.find_spec("torch") and importlib.util.find_spec("numpy"))


@unittest.skipUnless(REAL_AVAILABLE, "Set EXPMAN_SASREC_PROJECT and provide torch/numpy for real CPU integration")
class SasrecRealCpuTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.original = Path(REAL_PROJECT).resolve()
        self.original_hashes = self.source_hashes(self.original)
        self.project = self.root / "algorithm"
        shutil.copytree(self.original / "src", self.project / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.data = self.root / "data"
        dataset = self.data / "Toy"
        dataset.mkdir(parents=True)
        for split, text in {
            "train": "1 1 2 3 4\n2 2 3 5\n",
            "valid": "1 1 2 3 4 5\n2 2 3 5 6\n",
            "test": "1 1 2 3 4 5 6\n2 2 3 5 6 7\n",
        }.items():
            (dataset / ("Toy." + split + ".txt")).write_text(text, encoding="utf-8")
        self.params = {"dataset": "Toy", "data_asset": "toy-v1", "device": "cpu", "torch_threads": 1,
            "epochs": 3, "star_test": -1, "validation_interval": 1, "patience": 10,
            "seed": 42, "max_seq_length": 5, "batch_size": 2, "hidden_size": 8,
            "num_hidden_layers": 1, "num_attention_heads": 1, "candidate_chunk_size": 4,
            "attention_probs_dropout_prob": 0.2, "hidden_dropout_prob": 0.2}

    @staticmethod
    def source_hashes(project):
        return {path.relative_to(project).as_posix(): sha256_file(path)
                for path in (project / "src").rglob("*.py")}

    def tearDown(self):
        try:
            self.assertEqual(self.source_hashes(self.original), self.original_hashes,
                             "The original algorithm sources must remain unchanged")
        finally:
            self.temporary.__exit__(None, None, None)

    def invoke(self, output, *, resume=False, params=None, success=True):
        output.mkdir(parents=True, exist_ok=True)
        param_path = output / "params.json"
        atomic_json(param_path, self.params if params is None else params)
        environment = dict(os.environ)
        environment.update(EXPERIMENT_OUTPUT=str(output), EXPERIMENT_PARAMS=str(param_path),
            EXPERIMENT_ASSETS=json.dumps({"toy-v1": str(self.data)}), EXPERIMENT_RESUME="1" if resume else "0",
            EXPERIMENT_ATTEMPT="2" if resume else "1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        process = subprocess.run([sys.executable, "-B", "-m", "expman.adapters.sasrec", "--project", str(self.project)],
            cwd=Path(__file__).resolve().parents[1], env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=90)
        if success:
            self.assertEqual(process.returncode, 0, process.stdout + "\n" + process.stderr)
        else:
            self.assertNotEqual(process.returncode, 0, process.stdout)
        return process

    @staticmethod
    def checkpoint_path(output):
        return output / "sasrec" / "Toy" / "managed" / "sasrec_last.pt"

    def assert_nested_equal(self, actual, expected, path="state"):
        import numpy
        import torch
        if isinstance(expected, torch.Tensor):
            self.assertTrue(torch.equal(actual, expected), path)
        elif isinstance(expected, numpy.ndarray):
            self.assertTrue(numpy.array_equal(actual, expected), path)
        elif isinstance(expected, dict):
            self.assertEqual(set(actual), set(expected), path)
            for key, value in expected.items():
                self.assert_nested_equal(actual[key], value, path + "." + str(key))
        elif isinstance(expected, (tuple, list)):
            self.assertEqual(type(actual), type(expected), path)
            self.assertEqual(len(actual), len(expected), path)
            for index, value in enumerate(expected):
                self.assert_nested_equal(actual[index], value, path + "[" + str(index) + "]")
        else:
            self.assertEqual(actual, expected, path)

    def test_real_training_stop_restore_and_resume_are_exact(self):
        import torch
        full, resumed = self.root / "full", self.root / "resumed"
        self.invoke(full)
        resumed.mkdir()
        (resumed / "STOP").write_text("cooperative test", encoding="utf-8")
        paused = self.invoke(resumed)
        self.assertIn('"status": "paused"', paused.stdout)
        self.assertFalse((resumed / "result.json").exists())
        checkpoint_manifest = read_json(resumed / "checkpoint.json")
        self.assertEqual(checkpoint_manifest["step"], 1)
        self.assertEqual(torch.load(self.checkpoint_path(resumed), weights_only=False, map_location="cpu")["epoch"], 1)
        # A killed later epoch may leave torn working files; immutable bundle wins.
        self.checkpoint_path(resumed).write_bytes(b"interrupted working checkpoint")
        (resumed / "sasrec" / "Toy" / "managed" / "sasrec_best.pt").unlink()
        (resumed / "STOP").unlink()
        self.invoke(resumed, resume=True)
        expected = torch.load(self.checkpoint_path(full), weights_only=False, map_location="cpu")
        actual = torch.load(self.checkpoint_path(resumed), weights_only=False, map_location="cpu")
        for key in ("model", "optimizer", "early_stopping", "rng_state", "epoch", "global_step"):
            self.assert_nested_equal(actual[key], expected[key], key)
        result = read_json(resumed / "result.json")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["epochs_completed"], 3)
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(result["test_metrics"], read_json(full / "result.json")["test_metrics"])
        rows = [json.loads(line) for line in (resumed / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({row["attempt"] for row in rows}, {1, 2})
        self.assertTrue(any("valid/NDCG@10" in row for row in rows))
        self.assertTrue(any("test/NDCG@10" in row for row in rows))

    def test_real_resume_refuses_data_source_and_parameter_changes(self):
        output = self.root / "guarded"
        output.mkdir()
        (output / "STOP").write_text("stop after first epoch", encoding="utf-8")
        self.invoke(output)
        (output / "STOP").unlink()
        checkpoint_digest = sha256_file(self.checkpoint_path(output))
        changed = self.invoke(output, resume=True, params={**self.params, "seed": 99}, success=False)
        self.assertIn("refusing resume", changed.stderr)
        for path in (self.data / "Toy" / "Toy.train.txt", self.project / "src" / "models.py"):
            previous = path.read_bytes()
            try:
                path.write_bytes(previous + b"\n")
                changed = self.invoke(output, resume=True, success=False)
                self.assertIn("refusing resume", changed.stderr)
                self.assertEqual(sha256_file(self.checkpoint_path(output)), checkpoint_digest)
            finally:
                path.write_bytes(previous)
        self.invoke(output, resume=True)
        self.assertEqual(read_json(output / "result.json")["epochs_completed"], 3)

    def test_unknown_parameters_and_missing_assets_do_not_start_training(self):
        for name, params, message in (
            ("unknown", {**self.params, "learnig_rate": 0.01}, "Unknown SASRec parameters"),
            ("asset", {**self.params, "data_asset": "unregistered"}, "registered shared resource"),
        ):
            with self.subTest(case=name):
                output = self.root / name
                process = self.invoke(output, params=params, success=False)
                self.assertIn(message, process.stderr)
                self.assertFalse((output / "sasrec").exists())


if __name__ == "__main__":
    unittest.main()
