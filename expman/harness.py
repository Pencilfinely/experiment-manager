"""Run an existing algorithm entry point using external, declarative configuration.

This module never imports the algorithm. It can be shipped alongside sdk.py and
executed by older worker images. The Docker worker supplies process isolation;
the disposable working copy prevents ordinary relative writes to source files.
"""
from __future__ import annotations

import argparse
import codecs
import copy
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import zipfile

try:
    from .sdk import Run
except ImportError:  # Standalone runtime included in portable project bundles.
    from sdk import Run


_PLACEHOLDER = re.compile(r"\{(python|workspace|output|params\.[A-Za-z0-9_-]+|assets\.[A-Za-z0-9_.-]+|env\.[A-Za-z0-9_]+)\}")
_IGNORED = {".git", "__pycache__", ".venv", "venv"}
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
_PIPE_DRAIN_SECONDS = 2.0


def _relative(value, label, glob=False):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"{label} must be a relative POSIX path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or (not glob and any(c in value for c in "*?[]")):
        raise ValueError(f"{label} must stay inside its working directory")
    return value


def _command(value, label):
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or "\0" in v for v in value):
        raise ValueError(f"{label} must be a nonempty argv array of strings; shell text is not supported")


def validate_manifest(value):
    """Validate a schema 1 manifest without importing or executing its project."""
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("Harness manifest requires schema_version: 1")
    value = copy.deepcopy(value)
    _command(value.get("command"), "command")
    _relative(value.setdefault("cwd", "."), "cwd")
    for key in ("fixed_params", "parameters", "environment"):
        if not isinstance(value.setdefault(key, {}), dict):
            raise ValueError(f"{key} must be an object")
    for key, spec in value["parameters"].items():
        if not isinstance(spec, dict) or spec.get("type", "string") not in _TYPES:
            raise ValueError(f"Invalid parameter definition: {key}")
    for binding in value.setdefault("bindings", []):
        if not isinstance(binding, dict) or not isinstance(binding.get("param"), str):
            raise ValueError("Each binding needs a param")
        if not isinstance(binding.get("flag"), str) or not binding["flag"].startswith("-"):
            raise ValueError("Each binding needs an explicit CLI flag")
        if binding.get("mode", "value") not in ("value", "store_true", "store_false"):
            raise ValueError("Unsupported binding mode")
    for config in value.setdefault("config_files", []):
        _relative(config.get("path"), "config file path")
        if config.get("format", "json") == "json":
            if not isinstance(config.get("values"), dict):
                raise ValueError("JSON config_files require a values object")
        elif config["format"] == "text":
            if not isinstance(config.get("template"), str):
                raise ValueError("Text config_files require a template string")
        else:
            raise ValueError("Config file format must be json or text")
    for metric in value.setdefault("metrics", []):
        if metric.get("stream", "stdout") not in ("stdout", "stderr", "file"):
            raise ValueError("Metric stream must be stdout, stderr, or file")
        if metric.get("stream") == "file":
            _relative(metric.get("path"), "metric file glob (relative to output)", glob=True)
        pattern = re.compile(metric["pattern"])
        groups = set(pattern.groupindex)
        if metric.get("step", "step") not in groups:
            raise ValueError("Metric step must name a regex capture group")
        if not isinstance(metric.get("values"), dict) or not metric["values"]:
            raise ValueError("Metric values must map names to regex capture groups")
        if any(group not in groups for group in metric["values"].values()):
            raise ValueError("Metric value refers to an absent regex capture group")
    for path in value.setdefault("artifacts", []):
        _relative(path, "artifact glob", glob=True)
    resume = value.setdefault("resume", {"supported": False})
    if not isinstance(resume, dict) or not isinstance(resume.get("supported", False), bool):
        raise ValueError("resume.supported must be a boolean")
    if resume.get("supported"):
        _command(resume.get("command"), "resume.command")
        if not isinstance(resume.get("checkpoint_files"), list) or not resume["checkpoint_files"]:
            raise ValueError("Native resume requires explicit checkpoint_files")
        for path in resume["checkpoint_files"]:
            _relative(path, "native checkpoint path")
        if len(set(resume["checkpoint_files"])) != len(resume["checkpoint_files"]):
            raise ValueError("Native checkpoint paths must be unique")
        stop = resume.get("stop")
        if stop:
            if not isinstance(stop, dict):
                raise ValueError("Native stop must be an object")
            if bool(stop.get("file")) == bool(stop.get("signal")):
                raise ValueError("Native stop requires exactly one of file or signal")
            if stop.get("file"):
                _relative(stop["file"], "native stop file")
            elif stop["signal"] not in ("SIGINT", "SIGTERM"):
                raise ValueError("Only explicitly declared SIGINT or SIGTERM native stop is supported")
            grace = stop.get("grace_seconds", 30)
            if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not 1 <= grace <= 50:
                raise ValueError("Native stop grace_seconds must be between 1 and 50")
            exits = stop.get("accepted_exit_codes", [0])
            if not isinstance(exits, list) or not exits or any(type(code) is not int for code in exits):
                raise ValueError("Native stop accepted_exit_codes must be a nonempty array of integer exit codes")
    return value


def _render(value, context):
    def lookup(match):
        key = match.group(1)
        if "." in key:
            group, name = key.split(".", 1)
            if name not in context[group]:
                raise ValueError(f"Missing harness placeholder: {key}")
            return context[group][name]
        return context[key]
    if isinstance(value, dict):
        return {k: _render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, context) for v in value]
    if not isinstance(value, str):
        return value
    whole = _PLACEHOLDER.fullmatch(value)
    if whole:
        return lookup(whole)
    return _PLACEHOLDER.sub(lambda match: str(lookup(match)), value)


def _safe_path(root, relative):
    path = root / relative
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError(f"Symbolic links are not allowed in run paths: {relative}")
        cursor = cursor.parent
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Run path escaped working directory")
    return path


def _copy_source(source, target):
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Source must be a regular project directory")
    if target.resolve().is_relative_to(source.resolve()):
        raise ValueError("Run output cannot be inside the algorithm source")
    for directory, directories, files in os.walk(source, followlinks=False):
        directories[:] = [p for p in directories if p not in _IGNORED]
        for name in directories + files:
            path = Path(directory) / name
            if path.is_symlink() or not (stat.S_ISDIR(path.stat().st_mode) or stat.S_ISREG(path.stat().st_mode)):
                raise ValueError(f"Source contains an unsupported link or special file: {path}")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*_IGNORED))
    # Read-only source mounts and bundled source permissions must not make the
    # disposable copy read-only. The original files are never chmod'ed.
    for directory, _, files in os.walk(target):
        Path(directory).chmod(Path(directory).stat().st_mode | 0o700)
        for name in files:
            path = Path(directory) / name
            path.chmod(path.stat().st_mode | 0o600)


def _parameters(manifest, supplied, context):
    if not isinstance(supplied, dict):
        raise ValueError("Experiment parameters must be an object")
    definitions = manifest["parameters"]
    values = {name: spec["default"] for name, spec in definitions.items() if "default" in spec}
    allowed = set(definitions) | set(manifest["fixed_params"])
    if set(supplied) - allowed:
        raise ValueError(f"Unknown experiment parameters: {sorted(set(supplied) - allowed)}")
    values.update(supplied)
    context["params"] = values
    for name, value in manifest["fixed_params"].items():
        value = _render(value, context)
        if name in supplied and (supplied[name] != value or type(supplied[name]) is not type(value)):
            raise ValueError(f"Fixed parameter cannot be overridden: {name}")
        values[name] = value
    for name, spec in definitions.items():
        if name not in values:
            if spec.get("required"):
                raise ValueError(f"Required parameter missing: {name}")
            continue
        value = values[name]
        kind = spec.get("type", "string")
        if not isinstance(value, _TYPES[kind]) or (kind in ("integer", "number") and isinstance(value, bool)):
            raise ValueError(f"Parameter {name} must be {kind}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Parameter {name} must be finite")
        if "choices" in spec and value not in spec["choices"]:
            raise ValueError(f"Parameter {name} is not one of its choices")
    json.dumps(values, allow_nan=False)  # Reject non-finite fixed values too.
    return values


def _argv(command, manifest, context):
    argv = [str(_render(value, context)) for value in command]
    for binding in manifest["bindings"]:
        name = binding["param"]
        if name not in context["params"]:
            continue
        value = context["params"][name]
        mode = binding.get("mode", "value")
        if mode == "value":
            if isinstance(value, (dict, list, bool)):
                raise ValueError(f"CLI value binding {name} must be a scalar, use a flag mode for booleans")
            argv.extend([binding["flag"], str(value)])
        else:
            if not isinstance(value, bool):
                raise ValueError(f"CLI boolean binding {name} requires a boolean")
            if value == (mode == "store_true"):
                argv.append(binding["flag"])
    return argv


class _Metrics:
    def __init__(self, manifest, run):
        self.run = run
        self.rules = [(rule, re.compile(rule["pattern"])) for rule in manifest["metrics"]]
        self.files = {}
        self.warnings = []
        self.last_step = 0

    def line(self, stream, line, rule_index=None):
        for index, (rule, pattern) in enumerate(self.rules):
            if rule.get("stream", "stdout") != stream or (rule_index is not None and index != rule_index):
                continue
            match = pattern.search(line)
            if not match:
                continue
            try:
                step = int(match.group(rule.get("step", "step")))
                values = {key: float(match.group(group)) for key, group in rule["values"].items()}
                self.run.metric(step, **values)
                self.last_step = max(self.last_step, step)
            except (ValueError, TypeError) as error:
                # A malformed log line must not silently kill a healthy training
                # process. Raw logs and this warning are retained for diagnosis.
                message = f"Metric rule {index}: {error}"
                if message not in self.warnings:
                    self.warnings.append(message)

    def poll_files(self, final=False):
        for index, (rule, _) in enumerate(self.rules):
            if rule.get("stream") != "file":
                continue
            for path in self.run.output.glob(rule["path"]):
                _safe_path(self.run.output, path.relative_to(self.run.output))
                if path.is_dir():
                    continue
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError("Metric log must be a regular file: " + str(path))
                key = (index, str(path))
                offset, pending = self.files.get(key, (0, b""))
                size = path.stat().st_size
                if size < offset:
                    offset, pending = 0, b""
                with path.open("rb") as stream:
                    stream.seek(offset)
                    while True:
                        data = stream.read(min(1024 * 1024, max(0, size - offset)))
                        offset = stream.tell()
                        lines = (pending + data).split(b"\n")
                        pending = lines.pop()[-1024 * 1024:]
                        for line in lines:
                            self.line("file", line.decode("utf-8", "replace"), index)
                        if not final or not data or offset >= size:
                            break
                if final and pending:
                    self.line("file", pending.decode("utf-8", "replace"), index)
                    pending = b""
                self.files[key] = (offset, pending)


def _pipe_reader(pipe, name, path, events, canceled):
    def emit(kind, value):
        while not canceled.is_set():
            try:
                events.put((name, kind, value), timeout=0.1)
                return
            except queue.Full:
                pass
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    try:
        with path.open("ab") as log:
            while not canceled.is_set():
                data = pipe.read1(65536)
                if not data:
                    break
                log.write(data)
                log.flush()
                emit("console", data.decode("utf-8", "replace"))
                pending += decoder.decode(data)
                lines = pending.split("\n")
                pending = lines.pop()
                for line in lines:
                    emit("line", line)
                if len(pending) > 1024 * 1024:
                    pending = pending[-1024 * 1024:]
            pending += decoder.decode(b"", final=True)
            if pending:
                emit("line", pending)
    except OSError as error:
        emit("error", str(error))
    finally:
        pipe.close()


def _restore(run, workspace, resume):
    checkpoint = run.checkpoint()
    expected = set(resume["checkpoint_files"])
    with zipfile.ZipFile(checkpoint) as archive:
        if set(archive.namelist()) != expected or len(archive.namelist()) != len(expected):
            raise ValueError("Native checkpoint archive does not match declared files")
        for name in archive.namelist():
            path = _safe_path(workspace, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, path.open("wb") as target:
                shutil.copyfileobj(source, target)


def _publish(run, workspace, resume, step):
    paths = [_safe_path(workspace, name) for name in resume["checkpoint_files"]]
    if not all(path.is_file() for path in paths):
        raise ValueError("Native process exited without every declared checkpoint file")
    checkpoint = _safe_path(run.output, f"native-checkpoint-{run.attempt}.zip")
    if checkpoint.exists() and not checkpoint.is_file():
        raise ValueError("Native checkpoint archive must be a regular file")
    with zipfile.ZipFile(checkpoint, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, name in zip(paths, resume["checkpoint_files"]):
            archive.write(path, name)
    run.publish_checkpoint(checkpoint, step)


def _signal_process(process, requested):
    try:
        if os.name == "posix":
            os.killpg(process.pid, requested)
        else:
            process.send_signal(requested)
    except ProcessLookupError:
        pass  # Child exited between poll() and signal delivery.


def execute(manifest, source, run=None):
    """Launch the original command and return its exit code, preserving raw logs."""
    manifest = validate_manifest(manifest)
    run = run or Run()
    resume = manifest["resume"]
    if run.resuming and not resume.get("supported"):
        raise ValueError("This algorithm has no declared native resume; refusing a fake restart")
    source = Path(source).resolve()
    workspace = run.output / f"work-attempt-{run.attempt}"
    if workspace.exists():
        raise ValueError(f"Working copy already exists for this attempt: {workspace}")
    context = {"python": sys.executable, "workspace": str(workspace), "output": str(run.output),
               "assets": {key: str(path) for key, path in run.assets.items()}, "env": dict(os.environ), "params": {}}
    params = _parameters(manifest, run.params, context)
    command = _argv(resume["command"] if run.resuming else manifest["command"], manifest, context)
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    for key, value in manifest["environment"].items():
        if not isinstance(key, str) or not key or "=" in key or "\0" in key:
            raise ValueError("Invalid environment variable name")
        environment[key] = str(_render(value, context))
    _copy_source(source, workspace)
    if run.resuming:
        _restore(run, workspace, resume)
    for config in manifest["config_files"]:
        path = _safe_path(workspace, config["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if config.get("format", "json") == "json":
            text = json.dumps(_render(config["values"], context), ensure_ascii=False, allow_nan=False, indent=2)
        else:
            text = str(_render(config["template"], context))
        path.write_text(text, encoding="utf-8")
    cwd = _safe_path(workspace, manifest["cwd"])
    if not cwd.is_dir():
        raise ValueError(f"Algorithm cwd does not exist in working copy: {manifest['cwd']}")
    (run.output / f"harness-invocation-{run.attempt}.json").write_text(json.dumps(
        {"command": command, "cwd": str(cwd), "parameters": params, "resuming": run.resuming,
         "resume_supported": resume.get("supported", False)}, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = _Metrics(manifest, run)
    events = queue.Queue(maxsize=64)
    canceled = threading.Event()
    for name in ("stdout.log", "stderr.log"):
        path = _safe_path(run.output, name)
        if path.exists() and not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Captured console log must be a regular file: " + str(path))
    process = subprocess.Popen(command, cwd=cwd, env=environment, shell=False,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=os.name == "posix")
    # Preserve the previous recovery point if the executable cannot even start.
    # Once started, a forced stop must not advertise the previous attempt's
    # checkpoint as newly completed cooperative state.
    try:
        (run.output / "checkpoint.json").unlink(missing_ok=True)
    except OSError:
        if os.name == "posix":
            _signal_process(process, signal.SIGKILL)
        else:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()
        raise
    threads = []
    for name in ("stdout", "stderr"):
        thread = threading.Thread(target=_pipe_reader, args=(getattr(process, name), name,
                                   run.output / f"{name}.log", events, canceled), daemon=True)
        thread.start()
        threads.append(thread)
    stopped = forced = False
    stop_deadline = None
    child_exited_at = orphan_deadline = None
    last_output = last_file_poll = time.monotonic()
    harness_error = None
    native_stop = resume.get("stop") if resume.get("supported") else None
    try:
        while process.poll() is None or any(thread.is_alive() for thread in threads) or not events.empty():
            try:
                stream, kind, text = events.get(timeout=0.05)
                last_output = time.monotonic()
                if kind == "line":
                    metrics.line(stream, text)
                elif kind == "error":
                    raise RuntimeError("Cannot capture original process " + stream + ": " + text)
                else:
                    target = sys.stdout if stream == "stdout" else sys.stderr
                    target.write(text)
                    target.flush()
            except queue.Empty:
                pass
            now = time.monotonic()
            if now - last_file_poll >= 0.25:
                metrics.poll_files()
                last_file_poll = now
            if process.poll() is not None and child_exited_at is None:
                child_exited_at = now
            if (child_exited_at is not None and any(thread.is_alive() for thread in threads)
                    and events.empty() and now - max(child_exited_at, last_output) >= _PIPE_DRAIN_SECONDS):
                if orphan_deadline is None:
                    forced = True
                    harness_error = "Algorithm entry exited while a background process kept its output pipes open; run a foreground entry that waits for training to finish"
                    if os.name == "posix":
                        _signal_process(process, signal.SIGKILL)
                    orphan_deadline = now + _PIPE_DRAIN_SECONDS
                elif now >= orphan_deadline:
                    # A process which deliberately escaped its POSIX group can
                    # still hold these handles. Do not wait forever: ending the
                    # worker container also terminates remaining descendants.
                    break
            if run.should_stop() and not stopped and process.poll() is None:
                stopped = True
                if native_stop and native_stop.get("file"):
                    path = _safe_path(workspace, native_stop["file"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(str(native_stop.get("content", "stop\n")), encoding="utf-8")
                elif native_stop:
                    _signal_process(process, getattr(signal, native_stop["signal"]))
                else:
                    forced = True
                    _signal_process(process, signal.SIGTERM)
                stop_deadline = time.monotonic() + (native_stop.get("grace_seconds", 30) if native_stop else 5)
            if stop_deadline and time.monotonic() >= stop_deadline and process.poll() is None:
                forced = True
                if os.name == "posix":
                    _signal_process(process, signal.SIGKILL)
                else:
                    process.kill()
                stop_deadline = None
        returncode = process.wait()
    finally:
        canceled.set()
        if process.poll() is None:
            if os.name == "posix":
                _signal_process(process, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
        elif os.name == "posix":
            # Never leave descendants behind when their declared entry exits.
            _signal_process(process, signal.SIGKILL)
        for thread in threads:
            thread.join(timeout=2)
    metrics.poll_files(final=True)
    artifacts = []
    for pattern in manifest["artifacts"]:
        for path in workspace.glob(pattern):
            _safe_path(workspace, path.relative_to(workspace))
            if path.is_dir():
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("Artifact must be a regular file: " + str(path))
            artifacts.append(path.relative_to(run.output).as_posix())
    if stopped and native_stop and not forced and returncode in native_stop.get("accepted_exit_codes", [0]):
        _publish(run, workspace, resume, metrics.last_step)
    run.finish({"status": "stopped" if stopped else "succeeded" if returncode == 0 and not forced else "failed",
                "exit_code": returncode, "resume_supported": resume.get("supported", False),
                "checkpoint_published": (run.output / "checkpoint.json").is_file(),
                "workspace": workspace.relative_to(run.output).as_posix(),
                "artifacts": sorted(set(artifacts)), "metric_warnings": metrics.warnings,
                "harness_error": harness_error})
    if harness_error:
        print("Harness error: " + harness_error, file=sys.stderr, flush=True)
    return returncode if not forced else (returncode or 130)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    run_parser = commands.add_parser("run", help="Run an original algorithm through an external manifest")
    run_parser.add_argument("--manifest", required=True, type=Path)
    run_parser.add_argument("--source", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
        return execute(manifest, args.source or args.manifest.parent / "source")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(f"Harness error: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
