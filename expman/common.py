"""Shared validation, durable files and a deliberately small HTTP client."""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.request


def now():
    return time.time()


def atomic_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers may briefly hold a sharing lock on the old file.
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


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_child(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError("Invalid relative file name")
    # Reject Windows paths too, even when the receiving host is Linux.
    pieces = relative.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") or ":" in p for p in pieces):
        raise ValueError("File path must stay inside the run directory")
    target = root
    for part in pieces:
        target = target / part
        if target.is_symlink():
            raise ValueError("Symbolic links cannot be archived")
    if not target.resolve().is_relative_to(root):
        raise ValueError("File path escapes run directory")
    return target


def _number(value, name, minimum, maximum, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    if integer and int(value) != value:
        raise ValueError(f"{name} must be an integer")
    return int(value) if integer else value


def validate_task(spec):
    if not isinstance(spec, dict):
        raise ValueError("spec must be a JSON object")
    result = copy.deepcopy(spec)
    for key, default in (("name", "experiment"), ("algorithm", "custom"),
                         ("group", "default"), ("metric_protocol", "unspecified")):
        value = result.setdefault(key, default)
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f"{key} must be a nonempty string of at most 200 characters")
    backend = result.setdefault("backend", "docker")
    if "resume_supported" in result and not isinstance(result["resume_supported"], bool):
        raise ValueError("resume_supported must be true or false")
    if backend not in ("demo", "docker"):
        raise ValueError("backend must be demo or docker")
    if not isinstance(result.setdefault("params", {}), dict):
        raise ValueError("params must be a JSON object")
    if len(json.dumps(result["params"], allow_nan=False)) > 128 * 1024:
        raise ValueError("params are too large; reference files through assets")
    for field in ("assets", "tags"):
        values = result.setdefault(field, [])
        if not isinstance(values, list) or len(values) > 100 or any(
                not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", v)
                or v in (".", "..") for v in values):
            raise ValueError(f"{field} must be a list of versioned names (letters, digits, _, ., -)")
        result[field] = list(dict.fromkeys(values))
    result["priority"] = _number(result.get("priority", 0), "priority", -100, 100, True)
    resources = result.setdefault("resources", {})
    if not isinstance(resources, dict):
        raise ValueError("resources must be an object")
    for key, default, low, high, integer in (
            ("gpu_memory_mb", 0 if backend == "demo" else 4096, 0, 1024 * 1024, True),
            ("cpu", 1, 0.1, 4096, False), ("ram_mb", 1024, 64, 16 * 1024 * 1024, True)):
        resources[key] = _number(resources.get(key, default), key, low, high, integer)
    if not isinstance(resources.setdefault("exclusive", False), bool):
        raise ValueError("exclusive must be true or false")
    if isinstance(resources.get("gpu_count", 1), bool) or resources.get("gpu_count", 1) != 1:
        raise ValueError("v0.1 supports one GPU per task; parallelize independent tasks")
    environments = result.setdefault("environments", [])
    command = result.setdefault("command", [])
    result.setdefault("source", None)
    if backend == "docker":
        source = result["source"]
        if not isinstance(source, dict) or not isinstance(source.get("repo"), str):
            raise ValueError("Docker task requires source.repo and source.commit")
        if not source["repo"].strip() or source["repo"].startswith("-") or any(c in source["repo"] for c in "\x00\r\n"):
            raise ValueError("Invalid Git repository")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(source.get("commit", ""))):
            raise ValueError("source.commit must be a full 40-character Git commit, not a branch")
        source["commit"] = source["commit"].lower()
        if not isinstance(command, list) or not command or any(
                not isinstance(x, str) or "\x00" in x for x in command):
            raise ValueError("command must be a nonempty argv array")
        if len(command) > 128 or not command[0].strip() or command[0].startswith("-"):
            raise ValueError("Invalid command")
        if not isinstance(environments, list) or not environments:
            raise ValueError("At least one validated environment is required")
        for environment in environments:
            if not isinstance(environment, dict) or not re.fullmatch(
                    r"[A-Za-z0-9_.-]{1,100}", str(environment.get("profile", ""))):
                raise ValueError("Invalid environment profile")
            if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", str(environment.get("image", ""))):
                raise ValueError("Pin each Docker image to its sha256 digest")
        if resources["gpu_memory_mb"] <= 0:
            raise ValueError("Declare measured GPU memory budget for Docker GPU tasks")
    else:
        # Demonstration is our built-in CPU workload, never an arbitrary host command.
        if result["source"] is not None or command or environments or result["assets"]:
            raise ValueError("Demo backend only runs the bundled demonstration")
        if resources["gpu_memory_mb"] != 0:
            raise ValueError("Demo does not execute on a GPU")
        params = result["params"]
        _number(params.get("steps", 10), "steps", 1, 10000, True)
        _number(params.get("delay", 0.2), "delay", 0, 60)
        _number(params.get("seed", 42), "seed", 0, 2**32 - 1, True)
    return result


def expand_grid(spec, grid):
    if not isinstance(grid, dict) or len(grid) > 20:
        raise ValueError("grid must be an object with at most 20 parameter names")
    keys = list(grid)
    size = 1
    for key, values in grid.items():
        if not isinstance(key, str) or not key or any(not p for p in key.split(".")):
            raise ValueError("Invalid parameter path")
        if not isinstance(values, list) or not values:
            raise ValueError("Each grid value must be a nonempty list")
        size *= len(values)
    if size > 256:
        raise ValueError("At most 256 experiments can be submitted together")
    results = []
    for combination in itertools.product(*(grid[k] for k in keys)):
        item = copy.deepcopy(spec)
        for key, value in zip(keys, combination):
            target = item.setdefault("params", {})
            pieces = key.split(".")
            for piece in pieces[:-1]:
                target = target.setdefault(piece, {})
                if not isinstance(target, dict):
                    raise ValueError("Grid parameter overlaps a scalar value")
            target[pieces[-1]] = value
        results.append(validate_task(item))
    return results


def api_request(url, token, payload=None, timeout=10):
    data = None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)
