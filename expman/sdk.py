"""Algorithm-side adapter: parameters, metrics, cooperative stop, checkpoints.

This module is standard-library only and can be copied into an algorithm repo.
The algorithm itself must save all state needed to resume (model, optimizer,
sampler, RNG, scheduler and AMP scaler as applicable).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time


def _atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 7:
                    raise
                time.sleep(min(0.025 * 2**attempt, 0.2))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(data)
    return value.hexdigest()


class Run:
    def __init__(self, output=None, params=None):
        self.output = Path(output or os.environ["EXPERIMENT_OUTPUT"]).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        if params is None:
            with open(os.environ["EXPERIMENT_PARAMS"], encoding="utf-8-sig") as stream:
                params = json.load(stream)
        self.params = params
        self.assets = {key: Path(value) for key, value in json.loads(
            os.environ.get("EXPERIMENT_ASSETS", "{}")).items()}
        self.resuming = os.environ.get("EXPERIMENT_RESUME", "0") == "1"
        self.attempt = int(os.environ.get("EXPERIMENT_ATTEMPT", "1"))
        if self.attempt < 1:
            raise ValueError("EXPERIMENT_ATTEMPT must be a positive integer")

    def should_stop(self):
        return (self.output / "STOP").exists()

    def metric(self, step, **values):
        if isinstance(step, bool) or not isinstance(step, int) or step < 0:
            raise ValueError("metric step must be a nonnegative integer")
        if "time" in values or "attempt" in values:
            raise ValueError("time and attempt are reserved metric metadata")
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Metric {key} must be a finite number")
        record = {"step": step, "time": time.time(), "attempt": self.attempt, **values}
        with (self.output / "metrics.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
        _atomic(self.output / "latest_metrics.json", record)

    def publish_checkpoint(self, file, step):
        """Publish only after the algorithm has closed/atomically replaced file."""
        if isinstance(step, bool) or not isinstance(step, int) or step < 0:
            raise ValueError("checkpoint step must be a nonnegative integer")
        file = self._regular_output_file(file)
        manifest = {"path": file.relative_to(self.output).as_posix(),
                    "sha256": _digest(file), "step": step, "time": time.time()}
        _atomic(self.output / "checkpoint.json", manifest)
        return manifest

    def _regular_output_file(self, file):
        file = Path(file)
        if not file.is_absolute():
            file = self.output / file
        try:
            parts = file.relative_to(self.output).parts
        except ValueError as error:
            raise ValueError("Checkpoint must stay inside the run output") from error
        candidate = self.output
        for part in parts:
            if part == "..":
                raise ValueError("Checkpoint paths cannot contain parent traversal")
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError("Checkpoint paths cannot contain symbolic links")
        if not file.resolve().is_relative_to(self.output) or not file.is_file():
            raise ValueError("Checkpoint must be a regular file inside the run output")
        return file.resolve()

    def checkpoint(self):
        path = self.output / "checkpoint.json"
        if not path.exists():
            if self.resuming:
                raise ValueError("Resume requested but checkpoint manifest is missing; refusing silent restart")
            return None
        with path.open(encoding="utf-8") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("path"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256", ""))):
            raise ValueError("Invalid checkpoint manifest")
        file = self._regular_output_file(manifest["path"])
        if _digest(file) != manifest["sha256"]:
            raise ValueError("Checkpoint missing or checksum mismatch; refusing silent restart")
        return file

    def finish(self, result):
        _atomic(self.output / "result.json", result)


class SharedCacheLock:
    """Cross-process advisory lock for a shared derived-cache directory.

    Producers must all use this lock and publish files by atomic rename.
    Immutable source datasets/models should remain read-only instead.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0)
        if self.stream.read(1) == b"":
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)
        except BaseException:
            self.stream.close()
            self.stream = None
            raise
        return self

    def __exit__(self, *_):
        if self.stream:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.stream.seek(0)
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None
