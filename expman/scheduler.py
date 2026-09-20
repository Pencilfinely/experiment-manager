"""Conservative, explainable admission; no claim of GPU memory isolation."""
from __future__ import annotations

import copy
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
    gpu_uuids = task.get("scheduling", {}).get("gpu_uuids", [])
    # Older workers ignore GPU constraints; never assign constrained work to them.
    if gpu_uuids and "scheduler-v2" not in snapshot.get("capabilities", []):
        return False
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
               (not gpu_uuids or gpu.get("uuid") in gpu_uuids) and
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
    # Free memory includes external processes. Without per-task attribution the
    # current usage of our active tasks may be zero, so reserve their full RAM
    # budgets for growth even if this also counts some already allocated memory.
    if free_ram - used_ram < resources["ram_mb"] or disk < policy.get("min_disk_free_mb", 1024):
        return None
    profiles = snapshot.get("profiles", {})
    candidates = []
    for gpu in snapshot.get("gpus", []):
        gpu_uuids = task.get("scheduling", {}).get("gpu_uuids", [])
        if gpu_uuids and gpu.get("uuid") not in gpu_uuids:
            continue
        if not _valid_number(gpu.get("free_mb")) or gpu["free_mb"] < 0:
            continue
        colocated = [item for item in active if item.get("gpu_uuid") == gpu["uuid"]]
        if len(colocated) >= gpu.get("max_jobs", 1):
            continue
        if colocated and (resources["exclusive"] or any(i["spec"]["resources"]["exclusive"] for i in colocated)):
            continue
        reserved = sum(item["spec"]["resources"]["gpu_memory_mb"] for item in colocated)
        # Total-used VRAM cannot offset this reservation: it may all belong to
        # external workloads. Only measured free VRAM is available for sharing.
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


def _matching_template(task, snapshot):
    return next((template for template in snapshot.get("task_templates", [])
                 if template.get("project_bundle_id") == task.get("project_bundle_id")
                 and template.get("experiment_id") == task.get("experiment_id")), None)


def _bind_project(task, snapshot, original):
    """Rebind deployment paths and environments without changing experiment inputs."""
    if not task.get("project_bundle_id"):
        return task
    template = _matching_template(task, snapshot)
    if template is None:
        return None
    bound = copy.deepcopy(task)
    bound["source"] = copy.deepcopy(template["source"])
    bound["environments"] = copy.deepcopy(template["environments"])
    deployed_tags = task.get("deployment_tags", original.get("tags", []) if original else [])
    extra_tags = [tag for tag in task.get("tags", []) if tag not in deployed_tags and not tag.startswith("hnode-")]
    bound["tags"] = list(dict.fromkeys(template.get("tags", []) + extra_tags))
    bound["deployment_tags"] = list(template.get("tags", []))
    aliases = template.get("asset_aliases", {})
    bound["assets"] = list(dict.fromkeys(aliases.get(asset, asset) for asset in task.get("assets", [])))
    if aliases:
        bound["asset_aliases"] = copy.deepcopy(aliases)
    return bound


def choose_assignment(task, nodes, counts):
    """Return an eligible node and the exact node-local spec to persist atomically."""
    candidates = []
    timestamp = now()
    original = None
    if task.get("project_bundle_id"):
        for node in nodes:
            template = _matching_template(task, node.get("snapshot", {}))
            if template and template.get("source") == task.get("source"):
                original = template
                break
    scheduling = task.get("scheduling", {})
    allowed = scheduling.get("node_ids", [])
    preferred = scheduling.get("preferred_node_ids", [])
    for node in nodes:
        if allowed and node["id"] not in allowed:
            continue
        snapshot = node.get("snapshot", {})
        if timestamp - node.get("last_seen", 0) > 45 or node.get("mode", "run") != "run":
            continue
        bound = _bind_project(task, snapshot, original)
        if bound is None or not eligible(bound, snapshot):
            continue
        policy = snapshot.get("policy", {})
        count = counts.get(node["id"], 0)
        if count >= policy.get("max_prefetch", 4):
            continue
        speed = max(0.1, float(policy.get("speed", 1)))
        rank = preferred.index(node["id"]) if node["id"] in preferred else len(preferred)
        candidates.append((rank, (count + 1) / speed, node["id"], bound))
    if not candidates:
        return None
    _, _, node_id, spec = min(candidates, key=lambda value: value[:3])
    return {"node_id": node_id, "spec": spec}


def choose_node(task, nodes, counts):
    assignment = choose_assignment(task, nodes, counts)
    return assignment["node_id"] if assignment else None
