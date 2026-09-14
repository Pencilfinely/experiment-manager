"""Conservative, explainable admission; no claim of GPU memory isolation."""
from __future__ import annotations

import math
from .common import now


def _valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def in_window(snapshot):
    policy = snapshot.get("policy", {})
    if not policy.get("run_enabled", True):
        return False
    windows = policy.get("windows", [])
    if not windows:
        return True
    clock = snapshot.get("local_time")
    if not isinstance(clock, str):
        return False
    try:
        hour, minute = map(int, clock.split(":"))
        point = hour * 60 + minute
        for window in windows:
            start, end = window.split("-")
            ah, am = map(int, start.split(":"))
            bh, bm = map(int, end.split(":"))
            lo, hi = ah * 60 + am, bh * 60 + bm
            if (lo <= hi and lo <= point < hi) or (lo > hi and (point >= lo or point < hi)):
                return True
    except (ValueError, TypeError):
        return False
    return False


def eligible(task, snapshot):
    if not in_window(snapshot):
        return False
    if not set(task.get("tags", [])).issubset(snapshot.get("tags", [])):
        return False
    if not set(task.get("assets", [])).issubset(snapshot.get("assets", [])):
        return False
    resources = task["resources"]
    policy = snapshot.get("policy", {})
    if resources["cpu"] > policy.get("cpu_budget", 4) or resources["ram_mb"] > policy.get("ram_budget_mb", 8192):
        return False
    if task.get("backend") == "demo":
        return bool(snapshot.get("allow_demo", False))
    if task["source"]["repo"] not in snapshot.get("allowed_repos", []):
        return False
    profiles = snapshot.get("profiles", {})
    matching = {x["profile"] for x in task["environments"] if profiles.get(x["profile"]) == x["image"]}
    return any(_valid_number(gpu.get("total_mb")) and
               gpu["total_mb"] - gpu.get("reserve_mb", 2048) >= resources["gpu_memory_mb"] and
               gpu.get("max_jobs", 1) > 0 and matching.intersection(gpu.get("profiles", []))
               for gpu in snapshot.get("gpus", []))


def select_device(task, snapshot, active):
    if not eligible(task, snapshot):
        return None
    policy = snapshot.get("policy", {})
    resources = task["resources"]
    if len(active) >= policy.get("max_running", 2):
        return None
    used_cpu = sum(item["spec"]["resources"]["cpu"] for item in active)
    used_ram = sum(item["spec"]["resources"]["ram_mb"] for item in active)
    if used_cpu + resources["cpu"] > policy.get("cpu_budget", 4):
        return None
    if used_ram + resources["ram_mb"] > policy.get("ram_budget_mb", 8192):
        return None
    if task["backend"] == "demo":
        return {"gpu_uuid": None, "environment": None}
    free_ram, disk = snapshot.get("free_ram_mb"), snapshot.get("disk_free_mb")
    if not _valid_number(free_ram) or not _valid_number(disk):
        return None
    # Unknown per-process attribution: reserve growth headroom conservatively.
    if free_ram - used_ram < resources["ram_mb"] or disk < policy.get("min_disk_free_mb", 1024):
        return None
    profiles = snapshot.get("profiles", {})
    candidates = []
    for gpu in snapshot.get("gpus", []):
        if not _valid_number(gpu.get("free_mb")) or gpu["free_mb"] < 0:
            continue
        colocated = [item for item in active if item.get("gpu_uuid") == gpu["uuid"]]
        if len(colocated) >= gpu.get("max_jobs", 1):
            continue
        if colocated and (resources["exclusive"] or any(i["spec"]["resources"]["exclusive"] for i in colocated)):
            continue
        reserved = sum(item["spec"]["resources"]["gpu_memory_mb"] for item in colocated)
        available = gpu["free_mb"] - gpu.get("reserve_mb", 2048) - reserved
        if available < resources["gpu_memory_mb"]:
            continue
        environment = next((env for env in task["environments"]
                            if profiles.get(env["profile"]) == env["image"]
                            and env["profile"] in gpu.get("profiles", [])), None)
        if environment:
            candidates.append((available - resources["gpu_memory_mb"], gpu["uuid"], environment))
    if not candidates:
        return None
    # Best fit leaves a large card available for a larger future job.
    _, gpu_uuid, environment = min(candidates, key=lambda item: item[0])
    return {"gpu_uuid": gpu_uuid, "environment": environment}


def choose_node(task, nodes, counts):
    candidates = []
    timestamp = now()
    for node in nodes:
        snapshot = node.get("snapshot", {})
        if timestamp - node.get("last_seen", 0) > 45 or node.get("mode", "run") != "run":
            continue
        if not eligible(task, snapshot):
            continue
        policy = snapshot.get("policy", {})
        count = counts.get(node["id"], 0)
        if count >= policy.get("max_prefetch", 4):
            continue
        speed = max(0.1, float(policy.get("speed", 1)))
        candidates.append(((count + 1) / speed, node["id"]))
    return min(candidates)[1] if candidates else None
