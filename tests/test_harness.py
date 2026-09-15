"""External harness tests execute tiny real entry points, never algorithm imports."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from expman.harness import _matches, _Metrics, execute, validate_manifest
from expman.sdk import Run
from tests.support import temporary_directory


class HarnessTests(unittest.TestCase):
    def setUp(self):
        # Linux needs its native temporary filesystem for FIFO/symlink tests;
        # WSL's /mnt/<drive> may not support those even when mkfifo is present.
        self.temporary = tempfile.TemporaryDirectory() if os.name == "posix" else temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.addCleanup(self.temporary.__exit__, None, None, None)
        self.source = self.root / "original source"
        self.source.mkdir()
        self.output = self.root / "run output"

    def run_manifest(self, manifest, params=None, attempt=1, resume=False):
        with patch.dict(os.environ, {"EXPERIMENT_ATTEMPT": str(attempt), "EXPERIMENT_RESUME": "1" if resume else "0"}):
            run = Run(output=self.output, params=params or {})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = execute(manifest, self.source, run)
        return result

    def test_existing_cli_keeps_original_and_captures_outputs_without_shell(self):
        code = '''import argparse, json, pathlib, sys
p = argparse.ArgumentParser()
p.add_argument('--label')
p.add_argument('--count', type=int)
p.add_argument('--enabled', action='store_true')
a = p.parse_args()
pathlib.Path('result.json').write_text(json.dumps(vars(a)))
pathlib.Path('main.py').write_text('changed only in disposable working copy')
print('epoch=3 loss=0.25')
print('original warning', file=sys.stderr)
'''
        original = self.source / "main.py"
        original.write_text(code, encoding="utf-8")
        manifest = {"schema_version": 1, "command": ["{python}", "main.py"],
                    "parameters": {"label": {"type": "string"}, "count": {"type": "integer", "default": 2},
                                   "enabled": {"type": "boolean", "default": True}},
                    "bindings": [{"param": "label", "flag": "--label"}, {"param": "count", "flag": "--count"},
                                 {"param": "enabled", "flag": "--enabled", "mode": "store_true"}],
                    "metrics": [{"pattern": r"epoch=(?P<step>\d+) loss=(?P<loss>[\d.]+)", "values": {"train_loss": "loss"}}],
                    "artifacts": ["result.json"]}
        malicious_shell_text = "hello; echo SHOULD_NOT_RUN & $(echo BAD)"
        self.assertEqual(self.run_manifest(manifest, {"label": malicious_shell_text}), 0)
        self.assertEqual(original.read_text(encoding="utf-8"), code)
        result = json.loads((self.output / "work-attempt-1/result.json").read_text())
        self.assertEqual(result, {"label": malicious_shell_text, "count": 2, "enabled": True})
        metric = json.loads((self.output / "metrics.jsonl").read_text())
        self.assertEqual((metric["step"], metric["train_loss"]), (3, .25))
        self.assertIn("epoch=3", (self.output / "stdout.log").read_text())
        self.assertIn("original warning", (self.output / "stderr.log").read_text())
        self.assertEqual(json.loads((self.output / "result.json").read_text())["artifacts"], ["work-attempt-1/result.json"])

    def test_config_json_and_text_templates_preserve_types_and_file_metrics(self):
        (self.source / "main.py").write_text('''import json, pathlib
c=json.loads(pathlib.Path('config/run.json').read_text())
assert c['epochs'] == 4 and type(c['epochs']) is int
assert c['selected_gpu'] == 'GPU-test-uuid'
assert pathlib.Path('config/run.yaml').read_text() == 'lr: 0.125\\n'
p=pathlib.Path(c['output'])
p.mkdir()
(p/'train.log').write_text('epoch 1 loss .75\\nepoch 2 loss .25')
''', encoding="utf-8")
        manifest = {"schema_version": 1, "command": ["{python}", "main.py"],
                    "parameters": {"epochs": {"type": "integer", "default": 4}, "lr": {"type": "number", "default": .125}},
                    "fixed_params": {"gpu": "{env.CUDA_VISIBLE_DEVICES}"},
                    "config_files": [{"path": "config/run.json", "values": {"epochs": "{params.epochs}",
                                      "selected_gpu": "{params.gpu}", "output": "{output}/native-output"}},
                                     {"path": "config/run.yaml", "format": "text", "template": "lr: {params.lr}\n"}],
                    "metrics": [{"stream": "file", "path": "native-output/*.log",
                                 "pattern": r"epoch (?P<step>\d+) loss (?P<loss>[\d.]+)", "values": {"loss": "loss"}}]}
        with patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "GPU-test-uuid"}):
            self.assertEqual(self.run_manifest(manifest), 0)
        lines = [json.loads(line) for line in (self.output / "metrics.jsonl").read_text().splitlines()]
        self.assertEqual([(line["step"], line["loss"]) for line in lines], [(1, .75), (2, .25)])
        self.assertFalse((self.source / "config").exists())

    def test_invalid_inputs_rejected_before_any_source_execution(self):
        manifest = {"schema_version": 1, "command": ["{python}", "missing.py"],
                    "parameters": {"epochs": {"type": "integer", "default": 3}},
                    "fixed_params": {"output": "{output}"}}
        for supplied in ({"typo": 1}, {"epochs": True}, {"output": "unapproved"}):
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                self.run_manifest(manifest, supplied)
        self.assertFalse((self.output / "work-attempt-1").exists())
        for bad in ({**manifest, "command": "python main.py"},
                    {**manifest, "cwd": "../original source"},
                    {**manifest, "config_files": [{"path": "../main.py", "values": {}}]},
                    {**manifest, "metrics": [{"stream": "file", "path": "../*.txt", "pattern": "", "values": {}}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_manifest(bad)

    def test_no_native_resume_never_silently_restarts(self):
        (self.source / "main.py").write_text("raise AssertionError('must not launch')", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no declared native resume"):
            self.run_manifest({"schema_version": 1, "command": ["{python}", "main.py"]}, resume=True)
        self.assertFalse((self.output / "work-attempt-1").exists())

    def test_native_stop_file_and_resume_restore_exact_original_state(self):
        # The algorithm itself implements stop and resume; the harness supplies
        # flags, writes its documented stop file, and transports the saved state.
        (self.source / "main.py").write_text('''import pathlib, sys, time
p=pathlib.Path('state.txt')
if '--resume' in sys.argv:
    assert p.read_text() == '17'
    pathlib.Path('completed.txt').write_text('18')
else:
    pathlib.Path('ready').touch()
    while not pathlib.Path('request-stop').exists(): time.sleep(.02)
    p.write_text('17')
''', encoding="utf-8")
        manifest = {"schema_version": 1, "command": ["{python}", "main.py"],
                    "resume": {"supported": True, "command": ["{python}", "main.py", "--resume"],
                               "checkpoint_files": ["state.txt"], "stop": {"file": "request-stop"}}}
        def stop_when_ready():
            until = time.monotonic() + 10
            while not (self.output / "work-attempt-1/ready").exists() and time.monotonic() < until:
                time.sleep(.02)
            (self.output / "STOP").touch()
        stop = threading.Thread(target=stop_when_ready, daemon=True)
        stop.start()
        self.assertEqual(self.run_manifest(manifest), 0)
        stop.join(timeout=2)
        self.assertTrue((self.output / "checkpoint.json").exists())
        (self.output / "STOP").unlink()
        self.assertEqual(self.run_manifest(manifest, attempt=2, resume=True), 0)
        self.assertEqual((self.output / "work-attempt-2/completed.txt").read_text(), "18")
        self.assertFalse((self.source / "state.txt").exists())
        self.assertFalse((self.output / "checkpoint.json").exists())

    def test_original_failure_remains_failure_and_keeps_console(self):
        (self.source / "main.py").write_text("import sys\nprint('expected diagnostic', file=sys.stderr)\nsys.exit(7)", encoding="utf-8")
        self.assertEqual(self.run_manifest({"schema_version": 1, "command": ["{python}", "main.py"]}), 7)
        self.assertEqual(json.loads((self.output / "result.json").read_text())["status"], "failed")
        self.assertIn("expected diagnostic", (self.output / "stderr.log").read_text())

    def test_large_file_metrics_final_drain_reads_through_eof_once(self):
        run = Run(output=self.output, params={})
        (self.output / "large.log").write_bytes(b"ignored" * (512 * 1024) + b"\nepoch=7 loss=0.5")
        metrics = _Metrics({"metrics": [{"stream": "file", "path": "large.log",
                            "pattern": r"epoch=(?P<step>\d+) loss=(?P<loss>[\d.]+)", "values": {"loss": "loss"}}]}, run)
        metrics.poll_files(final=True)
        metrics.poll_files(final=True)
        records = (self.output / "metrics.jsonl").read_text().splitlines()
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0])["step"], 7)

    def test_literal_and_nested_link_detection_does_not_depend_on_glob_version(self):
        linked = self.root / "dangling"
        original_is_symlink = Path.is_symlink

        def is_symlink(path):
            return path == linked or original_is_symlink(path)

        # Simulate a dangling link and Python 3.10's empty literal-glob result.
        # This also runs on Windows without requiring link-creation privileges.
        with patch.object(Path, "glob", return_value=iter(())), patch.object(Path, "is_symlink", is_symlink):
            for pattern in ("dangling", "dangling/absent.log", "dangling/*.log"):
                with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, "Symbolic links"):
                    list(_matches(self.root, pattern))

    def test_verbose_console_backpressure_preserves_all_raw_output(self):
        (self.source / "main.py").write_text("for i in range(2000): print('line-' + str(i))", encoding="utf-8")
        self.assertEqual(self.run_manifest({"schema_version": 1, "command": ["{python}", "main.py"]}), 0)
        lines = (self.output / "stdout.log").read_text().splitlines()
        self.assertEqual(lines, ["line-" + str(i) for i in range(2000)])

    def test_launch_failure_preserves_previous_checkpoint(self):
        self.output.mkdir()
        checkpoint = self.output / "checkpoint.json"
        checkpoint.write_text('{"previous": "valid recovery remains available"}', encoding="utf-8")
        original = checkpoint.read_bytes()
        with self.assertRaises(OSError):
            self.run_manifest({"schema_version": 1, "command": [str(self.root / "missing-executable")]})
        self.assertEqual(checkpoint.read_bytes(), original)

    def test_fixed_integer_does_not_accept_boolean_and_stop_exit_codes_are_explicit(self):
        with self.assertRaisesRegex(ValueError, "Fixed parameter"):
            self.run_manifest({"schema_version": 1, "command": ["{python}", "missing.py"],
                               "fixed_params": {"epochs": 1}}, {"epochs": True})
        for invalid in ([True], [], ["0"]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_manifest({"schema_version": 1, "command": ["{python}", "main.py"],
                    "resume": {"supported": True, "command": ["{python}", "main.py", "--resume"],
                               "checkpoint_files": ["state"], "stop": {"file": "request", "accepted_exit_codes": invalid}}})

    def test_native_stop_failure_does_not_publish_stale_saved_weights(self):
        self.output.mkdir()
        (self.output / "checkpoint.json").write_text('{"old": true}', encoding="utf-8")
        (self.source / "main.py").write_text('''import pathlib, sys, time
pathlib.Path('weights.pt').write_text('old partial weights')
pathlib.Path('ready').touch()
while not pathlib.Path('request-stop').exists(): time.sleep(.02)
sys.exit(5)
''', encoding="utf-8")
        manifest = {"schema_version": 1, "command": ["{python}", "main.py"],
                    "resume": {"supported": True, "command": ["{python}", "main.py", "--resume"],
                               "checkpoint_files": ["weights.pt"], "stop": {"file": "request-stop", "accepted_exit_codes": [0]}}}
        def stop_when_ready():
            until = time.monotonic() + 10
            while not (self.output / "work-attempt-1/ready").exists() and time.monotonic() < until:
                time.sleep(.02)
            (self.output / "STOP").touch()
        stop = threading.Thread(target=stop_when_ready, daemon=True)
        stop.start()
        self.assertEqual(self.run_manifest(manifest), 5)
        stop.join(timeout=2)
        self.assertFalse((self.output / "checkpoint.json").exists())
        self.assertFalse(json.loads((self.output / "result.json").read_text())["checkpoint_published"])

    @unittest.skipUnless(os.name == "posix", "POSIX subprocess groups and filesystem links")
    def test_exited_entry_with_inherited_pipes_fails_promptly(self):
        (self.source / "main.py").write_text('''import subprocess, sys
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
print('entry returned before its child')
''', encoding="utf-8")
        started = time.monotonic()
        with patch("expman.harness._PIPE_DRAIN_SECONDS", .15):
            result = self.run_manifest({"schema_version": 1, "command": ["{python}", "main.py"]})
        self.assertNotEqual(result, 0)
        self.assertLess(time.monotonic() - started, 4)
        report = json.loads((self.output / "result.json").read_text())
        self.assertEqual(report["status"], "failed")
        self.assertIn("background process", report["harness_error"])

    @unittest.skipUnless(os.name == "posix", "POSIX FIFO and symlink creation")
    def test_generated_artifact_fifo_and_broken_symlink_are_rejected(self):
        for name, code in (("fifo", "import os; os.mkfifo('artifact')"),
                           ("link", "import os; os.symlink('missing', 'artifact')")):
            with self.subTest(name=name):
                self.output = self.root / name
                (self.source / "main.py").write_text(code, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.run_manifest({"schema_version": 1, "command": ["{python}", "main.py"], "artifacts": ["artifact"]})
                self.assertFalse((self.output / "checkpoint.json").exists())


if __name__ == "__main__":
    unittest.main()
