"""Durable resource preferences, bounded by each worker's local authorization."""
from __future__ import annotations

import copy
import json
import math


CAPABILITY = "node-resource-policy-v1"
MAX_REVISION = 2**53 - 1
POLICY_LIMITS = {"max_running": 256, "max_prefetch": 1024,
                 "cpu_budget": 65536, "ram_budget_mb": 2**40}
GPU_LIMITS = {"max_jobs": 256, "reserve_mb": 2**40}


def _number(value, name, maximum, *, integer=True):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (integer and not isinstance(value, int))
            or not 0 <= value <= maximum or not math.isfinite(value)):
        kind = "integer" if integer else "number"
        raise ValueError(f"{name} must be a finite {kind} between 0 and {maximum}")
    return value


def _object(value, name, keys=None):
    if not isinstance(value, dict) or (keys is not None and set(value) - set(keys)):
        raise ValueError(f"Invalid {name} fields")
    return value


def validate(value):
    """Validate syntax independently on both sides, before persisting an overlay."""
    _object(value, "resource_policy", ("revision", "policy", "gpu_policy"))
    revision = _number(value.get("revision"), "revision", MAX_REVISION)
    policy = _object(value.get("policy", {}), "policy", POLICY_LIMITS)
    for key, item in policy.items():
        _number(item, "policy." + key, POLICY_LIMITS[key], integer=key != "cpu_budget")
    gpus = _object(value.get("gpu_policy", {}), "gpu_policy")
    if len(gpus) > 256:
        raise ValueError("gpu_policy may contain at most 256 GPUs")
    for identity, values in gpus.items():
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 200:
            raise ValueError("Invalid GPU UUID")
        _object(values, "GPU policy", GPU_LIMITS)
        for key, item in values.items():
            _number(item, "gpu_policy." + key, GPU_LIMITS[key])
    return copy.deepcopy({"revision": revision, "policy": policy, "gpu_policy": gpus})


def validate_snapshot(snapshot):
    """Optional fields keep older workers compatible with the heartbeat API."""
    if "resource_policy_revision" in snapshot:
        _number(snapshot["resource_policy_revision"], "resource_policy_revision", MAX_REVISION)
    if "resource_policy_error" in snapshot:
        error = snapshot["resource_policy_error"]
        if not isinstance(error, str) or len(error) > 1000:
            raise ValueError("Invalid resource_policy_error")
    if "resource_policy_limits" in snapshot:
        limits = _object(snapshot["resource_policy_limits"], "resource_policy_limits", ("cpu_budget", "ram_budget_mb"))
        for key, item in limits.items():
            if item is not None:
                _number(item, "resource_policy_limits." + key, POLICY_LIMITS[key], integer=key != "cpu_budget")
    for gpu in snapshot.get("gpus", []):
        if "local_enabled" in gpu and not isinstance(gpu["local_enabled"], bool):
            raise ValueError("GPU.local_enabled must be boolean")


def validate_for_worker(value, snapshot):
    value = validate(value)
    limits = snapshot.get("resource_policy_limits", {})
    for key in ("cpu_budget", "ram_budget_mb"):
        if key in value["policy"]:
            maximum = limits.get(key)
            if maximum is None:
                raise ValueError(f"Worker has not reported its physical {key} limit")
            _number(maximum, key + " limit", POLICY_LIMITS[key], integer=key != "cpu_budget")
            _number(value["policy"][key], "policy." + key, maximum, integer=key != "cpu_budget")
    available = {gpu["uuid"]: gpu for gpu in snapshot.get("gpus", [])}
    for identity, settings in value["gpu_policy"].items():
        gpu = available.get(identity)
        if gpu is None or gpu.get("local_enabled") is not True:
            raise ValueError(f"GPU {identity} is not locally enabled and verified")
        if "reserve_mb" in settings:
            _number(settings["reserve_mb"], "reserve_mb", gpu["total_mb"])
    return value


def apply(snapshot, value):
    """Overlay admission only; never change a task, container, or local config."""
    if value is None:
        return snapshot
    policy = snapshot["policy"]
    limits = snapshot.get("resource_policy_limits", {})
    for key, item in value["policy"].items():
        if key in ("cpu_budget", "ram_budget_mb"):
            maximum = limits.get(key)
            # Unknown or reduced hardware must never expand the local budget.
            item = min(item, maximum if maximum is not None else policy.get(key, 0))
        policy[key] = item
    for gpu in snapshot.get("gpus", []):
        settings = value["gpu_policy"].get(gpu["uuid"], {})
        if gpu.get("local_enabled") is not True:
            continue
        gpu.update(settings)
        gpu["reserve_mb"] = min(gpu["reserve_mb"], gpu["total_mb"])
    return snapshot


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS node_resource_policies ("
               "node_id TEXT PRIMARY KEY REFERENCES nodes(id), value TEXT NOT NULL)")


class NodePolicyHubMixin:
    def _node_resource_policy(self, node_id):
        row = self.db.execute("SELECT value FROM node_resource_policies WHERE node_id=?", (node_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_node_policy(self, payload):
        from .hub import APIError
        _object(payload, "request", ("node_id", "revision", "policy", "gpu_policy"))
        node_id = payload.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise APIError(400, "node_id is required")
        desired = validate({key: item for key, item in payload.items() if key != "node_id"})
        with self.transaction():
            node = self.db.execute("SELECT snapshot FROM nodes WHERE id=?", (node_id,)).fetchone()
            if node is None:
                raise APIError(404, "Node not found")
            snapshot = json.loads(node[0])
            if CAPABILITY not in snapshot.get("capabilities", []):
                raise APIError(409, "此 Worker 尚不支持远程资源设置，请先升级 Worker 并等待一次连接")
            previous = self._node_resource_policy(node_id)
            revision = previous["revision"] if previous else 0
            unchanged = previous is not None and all(desired[key] == previous[key] for key in ("policy", "gpu_policy"))
            if unchanged and desired["revision"] in (revision, revision - 1):
                return {"node_id": node_id, "resource_policy": previous}
            if desired["revision"] != revision:
                raise APIError(409, "资源设置已被修改，请刷新后重试")
            desired = validate_for_worker(desired, snapshot)
            if revision == MAX_REVISION:
                raise APIError(409, "Resource policy revision limit reached")
            desired["revision"] = revision + 1
            self.db.execute("INSERT OR REPLACE INTO node_resource_policies VALUES (?,?)",
                            (node_id, json.dumps(desired, allow_nan=False, sort_keys=True)))
            return {"node_id": node_id, "resource_policy": desired}
