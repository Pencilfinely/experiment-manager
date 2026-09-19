"""Durable experiment matrices, atomic launches and inspectable Markdown results."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import html
import json
import re
import uuid
import zipfile

from .common import expand_grid, now, safe_child, validate_task
from .scheduler import choose_assignment


def _error(status, message):
    from .hub import APIError
    return APIError(status, message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise _error(400, "Matrix id must be a UUID hex string")
    return value


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS matrices (
            id TEXT PRIMARY KEY, definition TEXT NOT NULL, revision INTEGER NOT NULL,
            count INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, deleted_at REAL);
        CREATE TABLE IF NOT EXISTS matrix_runs (
            id TEXT PRIMARY KEY, matrix_id TEXT NOT NULL REFERENCES matrices(id),
            revision INTEGER NOT NULL, request_id TEXT NOT NULL, definition TEXT NOT NULL,
            created REAL NOT NULL, UNIQUE(matrix_id,request_id));
        CREATE TABLE IF NOT EXISTS matrix_jobs (
            run_id TEXT NOT NULL REFERENCES matrix_runs(id), job_id TEXT NOT NULL REFERENCES jobs(id),
            ordinal INTEGER NOT NULL, PRIMARY KEY(run_id,job_id));
        CREATE INDEX IF NOT EXISTS matrix_runs_matrix ON matrix_runs(matrix_id,created);
    """)


def expand_matrix(payload):
    if not isinstance(payload, dict):
        raise ValueError("Matrix must be an object")
    name, description = payload.get("name"), payload.get("description", "")
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise ValueError("Matrix name must contain 1-160 characters")
    if not isinstance(description, str) or len(description) > 4000:
        raise ValueError("Matrix description must be at most 4000 characters")
    spec = copy.deepcopy(payload.get("spec"))
    if not isinstance(spec, dict):
        raise ValueError("Select an experiment task template for the matrix")
    for key in ("scheduling", "priority"):
        if key in payload:
            spec[key] = copy.deepcopy(payload[key])
    for key in ("matrix_id", "matrix_run_id", "matrix_revision", "dataset_name"):
        spec.pop(key, None)
    spec = validate_task(spec)
    datasets = copy.deepcopy(payload.get("datasets", []))
    if not isinstance(datasets, list) or len(datasets) > 256:
        raise ValueError("datasets must be a list of at most 256 datasets")
    names = set()
    for dataset in datasets:
        if not isinstance(dataset, dict) or set(dataset) - {"name", "params", "assets"}:
            raise ValueError("Each dataset must contain name, params, and optional assets")
        label = dataset.get("name")
        if not isinstance(label, str) or not label.strip() or len(label) > 160 or label in names:
            raise ValueError("Dataset names must be unique nonempty strings of at most 160 characters")
        names.add(label)
        if not isinstance(dataset.setdefault("params", {}), dict):
            raise ValueError("Dataset params must be an object")
    grid = copy.deepcopy(payload.get("grid", {}))
    # Validate every generated task before returning any of them to a launcher.
    specs = []
    for dataset in datasets or [{"name": "default", "params": {}}]:
        item = copy.deepcopy(spec)
        item["params"].update(dataset["params"])
        if "assets" in dataset:
            item["assets"] = copy.deepcopy(dataset["assets"])
        variants = expand_grid(item, grid)
        for variant in variants:
            variant["dataset_name"] = dataset["name"]
            variant["name"] = f"{name} / {dataset['name']} / {len(specs) + 1}"[:200]
            specs.append(variant)
        if len(specs) > 256:
            raise ValueError("At most 256 experiments can be launched in one matrix")
    definition = {"name": name.strip(), "description": description, "spec": spec,
                  "datasets": datasets, "grid": grid}
    return definition, specs


class MatrixHubMixin:
    def _matrix_find(self, identity, include_deleted=False):
        row = self.db.execute("SELECT * FROM matrices WHERE id=?", (_identity(identity),)).fetchone()
        if row is None or row["deleted_at"] is not None and not include_deleted:
            raise _error(404, "Unknown experiment matrix")
        return row

    def _matrix_jobs(self, identity):
        return [self._job(row) for row in self.db.execute(
            "SELECT j.* FROM jobs j JOIN matrix_jobs mj ON j.id=mj.job_id "
            "JOIN matrix_runs mr ON mr.id=mj.run_id WHERE mr.matrix_id=? ORDER BY mr.created,mj.ordinal", (identity,))]

    def _matrix_public(self, row, full=False):
        definition = json.loads(row["definition"])
        jobs = self._matrix_jobs(row["id"])
        states = {}
        for job in jobs:
            states[job["state"]] = states.get(job["state"], 0) + 1
        result = {key: row[key] for key in ("id", "revision", "count", "created", "updated")}
        result.update(name=definition["name"], description=definition["description"],
                      job_count=len(jobs), states=states)
        if full:
            result.update(definition)
            for job in jobs:
                job.pop("log_tail", None)
            result["jobs"] = jobs
            result["runs"] = [dict(item) for item in self.db.execute(
                "SELECT id,revision,request_id,created FROM matrix_runs WHERE matrix_id=? ORDER BY created", (row["id"],))]
        return result

    def matrices(self):
        with self.lock:
            return {"matrices": [self._matrix_public(row) for row in self.db.execute(
                "SELECT * FROM matrices WHERE deleted_at IS NULL ORDER BY updated DESC,id")]}

    def matrix_item(self, identity):
        with self.lock:
            return self._matrix_public(self._matrix_find(identity), full=True)

    def _validate_scheduling_nodes(self, specs):
        registered = {row[0] for row in self.db.execute("SELECT id FROM nodes")}
        manifests = {}
        for spec in specs:
            scheduling = spec.get("scheduling", {})
            referenced = set(scheduling.get("node_ids", [])) | set(scheduling.get("preferred_node_ids", []))
            if referenced - registered:
                raise _error(400, "Unknown scheduling nodes: " + ", ".join(sorted(referenced - registered)))
            checker = getattr(self, "_project_is_deleted", None)
            if checker is not None and checker(spec):
                raise _error(409, "This algorithm project was deleted; import it again before launching")
            bundle_id = spec.get("project_bundle_id")
            if not bundle_id:
                continue
            if bundle_id not in manifests:
                record = self.db.execute("SELECT digest FROM projects WHERE bundle_id=? AND deleted_at IS NULL ORDER BY created DESC LIMIT 1", (bundle_id,)).fetchone()
                manifests[bundle_id] = None
                if record:
                    from .harness_project import MANIFEST, _digest, _encoded
                    path = safe_child(self.root, "projects/" + record["digest"] + ".zip")
                    if not path.is_file():
                        raise _error(409, "Project archive is missing; import this project again before launching")
                    with zipfile.ZipFile(path) as archive:
                        if archive.getinfo(MANIFEST).file_size > 4 * 1024**2:
                            raise _error(409, "Project manifest is oversized")
                        manifest = json.loads(archive.read(MANIFEST))
                    identity = dict(manifest)
                    if identity.pop("bundle_id", None) != bundle_id or _digest(_encoded(identity)) != bundle_id:
                        raise _error(409, "Project manifest identity changed; import the project again")
                    from .harness import validate_manifest
                    from .harness_project import bundle_asset_aliases
                    manifests[bundle_id] = (manifest, bundle_asset_aliases(manifest), validate_manifest(manifest["harness"]))
            manifest = manifests[bundle_id]
            if manifest is not None:
                self._validate_project_parameters(spec, *manifest)

    @staticmethod
    def _validate_project_parameters(spec, manifest, aliases, harness):
        from .harness import _parameters, _argv, _render
        if spec.get("experiment_id") not in {item["id"] for item in manifest["experiments"]}:
            raise _error(400, "Unknown experiment preset in this project")
        spec["assets"] = list(dict.fromkeys(aliases.get(asset, asset) for asset in spec.get("assets", [])))
        spec["asset_aliases"] = dict(aliases)
        selected = set(spec["assets"])
        context = {"python": "python", "workspace": "/output/work-attempt-1", "output": "/output",
                   "assets": {alias: "/assets/" + asset for alias, asset in aliases.items() if asset in selected},
                   "env": {"CUDA_VISIBLE_DEVICES": "GPU-assigned"}, "params": {}}
        try:
            context["params"] = _parameters(harness, spec["params"], context)
            _argv(harness["command"], harness, context)
            for config in harness["config_files"]:
                _render(config.get("values", config.get("template", "")), context)
            _render(harness["environment"], context)
        except ValueError as error:
            raise _error(400, "Invalid matrix/project parameters for " + spec.get("dataset_name", spec["name"]) + ": " + str(error)) from error

    def _allocation_preview(self, specs):
        from .hub import TERMINAL
        nodes = []
        for row in self.db.execute("SELECT * FROM nodes"):
            item = dict(row)
            item["snapshot"] = json.loads(item["snapshot"])
            nodes.append(item)
        counts = {}
        for row in self.db.execute("SELECT node_id,state FROM jobs WHERE node_id IS NOT NULL"):
            if row["state"] not in TERMINAL:
                counts[row["node_id"]] = counts.get(row["node_id"], 0) + 1
        allocations = []
        for spec in specs:
            assignment = choose_assignment(spec, nodes, counts)
            node = assignment["node_id"] if assignment else None
            if node:
                counts[node] = counts.get(node, 0) + 1
            allocations.append({"node_id": node, "reason": "按节点偏好、资源兼容性与队列负载分配" if node else
                "等待兼容节点：检查在线状态、运行时段、候选节点、GPU、数据集、环境和预取容量"})
        warnings = []
        if any(item["node_id"] is None for item in allocations):
            warnings.append("部分实验暂时无法分配，启动后会留在队列中等待符合条件的节点。")
        return {"count": len(specs), "specs": specs, "allocations": allocations, "warnings": warnings}

    def scheduling_preview(self, payload):
        spec = validate_task(payload.get("spec"))
        specs = expand_grid(spec, payload["grid"]) if "grid" in payload else [spec]
        with self.lock:
            self._validate_scheduling_nodes(specs)
            return self._allocation_preview(specs)

    def matrix_preview(self, payload):
        with self.lock:
            definition = json.loads(self._matrix_find(payload["id"])["definition"]) if "id" in payload and "spec" not in payload else payload
            _, specs = expand_matrix(definition)
            self._validate_scheduling_nodes(specs)
            return self._allocation_preview(specs)

    def matrix_save(self, payload):
        definition, specs = expand_matrix(payload)
        with self.transaction():
            self._validate_scheduling_nodes(specs)
            timestamp = now()
            if payload.get("id"):
                row = self._matrix_find(payload["id"])
                if "revision" in payload and payload["revision"] != row["revision"]:
                    raise _error(409, "Matrix changed; reload it before saving")
                identity = row["id"]
                self.db.execute("UPDATE matrices SET definition=?,revision=revision+1,count=?,updated=? WHERE id=?",
                                (_json(definition), len(specs), timestamp, identity))
            else:
                identity = uuid.uuid4().hex
                self.db.execute("INSERT INTO matrices VALUES (?,?,1,?,?,?,NULL)",
                                (identity, _json(definition), len(specs), timestamp, timestamp))
            return self._matrix_public(self._matrix_find(identity), full=True)

    def matrix_start(self, payload):
        identity = _identity(payload.get("id"))
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise _error(400, "request_id must contain 1-200 characters")
        with self.transaction():
            row = self._matrix_find(identity)
            revision = payload.get("revision", row["revision"])
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise _error(400, "revision must be an integer")
            previous = self.db.execute("SELECT * FROM matrix_runs WHERE matrix_id=? AND request_id=?", (identity, request_id)).fetchone()
            if previous:
                if revision != previous["revision"]:
                    raise _error(409, "request_id already identifies a different matrix revision")
                ids = [item[0] for item in self.db.execute("SELECT job_id FROM matrix_jobs WHERE run_id=? ORDER BY ordinal", (previous["id"],))]
                return {"matrix_id": identity, "run_id": previous["id"], "ids": ids}
            if revision != row["revision"]:
                raise _error(409, "Matrix changed; preview the current revision before starting")
            definition, specs = expand_matrix(json.loads(row["definition"]))
            self._validate_scheduling_nodes(specs)
            run_id, timestamp, ids = uuid.uuid4().hex, now(), []
            self.db.execute("INSERT INTO matrix_runs VALUES (?,?,?,?,?,?)", (run_id, identity, revision, request_id, _json(definition), timestamp))
            for ordinal, spec in enumerate(specs):
                spec.update(matrix_id=identity, matrix_run_id=run_id, matrix_revision=revision)
                job_id = uuid.uuid4().hex
                ids.append(job_id)
                self.db.execute("INSERT INTO jobs(id,spec,state,created,updated) VALUES (?,?,?,?,?)", (job_id, _json(spec), "queued", timestamp, timestamp))
                self.db.execute("INSERT INTO matrix_jobs VALUES (?,?,?)", (run_id, job_id, ordinal))
                self._event(job_id, "submitted", {"request_id": request_id, "matrix_id": identity, "run_id": run_id})
            self._assign()
            return {"matrix_id": identity, "run_id": run_id, "ids": ids}

    def matrix_delete(self, payload):
        from .hub import TERMINAL
        with self.transaction():
            row = self._matrix_find(payload.get("id"), include_deleted=True)
            if any(job["state"] not in TERMINAL or job["command_id"] > job["command_ack"] for job in self._matrix_jobs(row["id"])):
                raise _error(409, "Stop or cancel active matrix experiments and wait for worker confirmation before deleting")
            self.db.execute("UPDATE matrices SET deleted_at=COALESCE(deleted_at,?),updated=? WHERE id=?", (now(), now(), row["id"]))
            return {"id": row["id"], "deleted": True}

    def matrix_report(self, identity):
        with self.lock:
            row = self._matrix_find(identity, include_deleted=True)
            matrix = self._matrix_public(row, full=True)
            return render_report(matrix).encode("utf-8")


def _cell(value):
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        value = _json(value)
    value = html.escape(str(value)).replace("|", "&#124;").replace("\r", "").replace("\n", "<br>").replace("`", "&#96;")
    return re.sub(r"([\\\[\]*_!])", r"\\\1", value)


def _flatten(value, prefix=""):
    result = {}
    for key, item in value.items():
        path = prefix + "." + str(key) if prefix else str(key)
        if isinstance(item, dict):
            result.update(_flatten(item, path))
        else:
            result[path] = item
    return result


def render_report(matrix):
    """Do not rank or average incomparable protocols, missing metrics, or failures."""
    jobs = matrix["jobs"]
    metrics = [_flatten(job["metrics"]) for job in jobs]
    keys = sorted({key for values in metrics for key in values})
    timestamp = datetime.fromtimestamp(now(), timezone.utc).isoformat(timespec="seconds")
    lines = ["# " + _cell(matrix["name"]), "", _cell(matrix["description"]), "",
             "- 矩阵 ID：" + matrix["id"], "- 导出时间（UTC）：" + timestamp,
             f"- 配置版本：{matrix['revision']}；启动批次：{len(matrix['runs'])}；实验数：{len(jobs)}",
             "- 状态统计：" + ("，".join(f"{key}: {value}" for key, value in sorted(matrix["states"].items())) or "尚未启动"),
             "", "结果使用每次实验最新回传的指标；缺失值显示为 —。运行中和失败实验的指标不代表最终结果，不跨指标协议排名或求平均。", "",
             "## 实验结果", ""]
    headers = ["实验 ID", "批次 / 版本", "数据集", "算法", "指标协议", "状态", "节点", "运行秒数"] + keys
    lines += ["| " + " | ".join(map(_cell, headers)) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for job, values in zip(jobs, metrics):
        spec = job["spec"]
        timing = job.get("timing") or {}
        duration = timing.get("elapsed_seconds")
        if isinstance(duration, (int, float)):
            duration = f"{duration:.2f}"
            if job["state"] in ("starting", "running"):
                duration += "（进行中）"
            if not timing.get("complete"):
                duration += "（计时记录不完整）"
        cells = [job["id"], spec.get("matrix_run_id", "") + " / " + str(spec.get("matrix_revision", "")),
                 spec.get("dataset_name"), spec.get("algorithm"), spec.get("metric_protocol"),
                 job["state"], job.get("node_id"), duration] + [values.get(key) for key in keys]
        lines.append("| " + " | ".join(map(_cell, cells)) + " |")
    lines += ["", "## 参数与运行记录", ""]
    for job in jobs:
        spec = job["spec"]
        lines += ["### " + _cell(spec["name"]), "", "- 实验 ID：" + job["id"],
                  "- 参数：" + _cell(spec.get("params", {})), "- 数据资源：" + _cell(spec.get("assets", [])),
                  "- 源码版本：" + _cell(spec.get("source")), "- 运行环境：" + _cell(spec.get("environments", [])),
                  "- 调度：" + _cell(spec.get("scheduling", {})) + "；优先级：" + str(spec.get("priority", 0)),
                  "- 状态详情：" + _cell(job.get("detail") or "—"), "- 尝试次数：" + str(job.get("attempt", 0)), ""]
    return "\n".join(lines) + "\n"
