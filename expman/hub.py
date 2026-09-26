"""Durable experiment coordinator and authenticated, resumable result archive.

The service is intended for localhost or a trusted private network. Put TLS in
front of it before carrying bearer tokens over an untrusted network.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
import csv
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit
import uuid
import zipfile

from . import __version__
from .common import atomic_json, expand_grid, now, read_json, safe_child, sha256_file, validate_task
from .scheduler import choose_assignment
from .matrix import MatrixHubMixin, initialize as initialize_matrices
from .mobile import MobileHubMixin, initialize as initialize_mobile
from .node_policy import (NodePolicyHubMixin, initialize as initialize_node_policies,
                          validate_snapshot as validate_policy_snapshot, CAPABILITY as NODE_POLICY_CAPABILITY)


TERMINAL = frozenset(("succeeded", "canceled", "failed", "paused", "interrupted"))
STATES = TERMINAL | {"queued", "assigned", "preparing", "ready", "starting", "running"}
PROGRESS = {state: rank for rank, state in enumerate(("queued", "assigned", "preparing", "ready", "starting", "running"))}
MAX_CHUNK = 512 * 1024
MAX_BODY = 2 * 1024 * 1024
MAX_PROJECT_SIZE = 32 * 1024**3
MAX_INTEGER = 2**63 - 1


class APIError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _integer(value, name, minimum=0, maximum=MAX_INTEGER):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise APIError(400, f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _identifier(value, name="job_id"):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise APIError(400, f"{name} must be a UUID hex string")
    return value


def _object(value, name):
    if not isinstance(value, dict):
        raise APIError(400, f"{name} must be an object")
    return value


def _number(value, name, minimum=0, maximum=10**12):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise APIError(400, f"Invalid numeric {name}")


def _timing(value):
    """Validate a complete worker sample without relying on wall-clock order."""
    value = _object(value, "timing")
    fields = ("started_at", "finished_at", "elapsed_seconds", "observed_at", "complete")
    if any(key not in value for key in fields):
        raise APIError(400, "timing must include started_at, finished_at, elapsed_seconds, observed_at and complete")
    for key in fields[:-1]:
        if key in ("started_at", "finished_at") and value[key] is None:
            continue
        try:
            _number(value[key], "timing." + key, maximum=float("inf"))
        except OverflowError as error:
            raise APIError(400, "Invalid numeric timing." + key) from error
    if not isinstance(value["complete"], bool):
        raise APIError(400, "timing.complete must be boolean")
    return {key: value[key] for key in fields}


def _snapshot(value):
    """Reject shapes which could crash scheduling; unknown descriptive keys pass."""
    value = _object(value, "snapshot")
    if len(_json(value)) > 256 * 1024:
        raise APIError(400, "snapshot is too large")
    policy = _object(value.get("policy", {}), "snapshot.policy")
    for key in ("run_enabled",):
        if key in policy and not isinstance(policy[key], bool):
            raise APIError(400, f"policy.{key} must be boolean")
    for key in ("max_running", "max_prefetch", "ram_budget_mb", "min_disk_free_mb"):
        if key in policy:
            _integer(policy[key], "policy." + key)
    for key in ("cpu_budget", "speed"):
        if key in policy:
            _number(policy[key], "policy." + key)
    windows = policy.get("windows", [])
    if not isinstance(windows, list) or any(not isinstance(x, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", x) for x in windows):
        raise APIError(400, "policy.windows must contain HH:MM-HH:MM windows")
    if "allow_demo" in value and not isinstance(value["allow_demo"], bool):
        raise APIError(400, "allow_demo must be boolean")
    for key in ("tags", "assets", "allowed_repos"):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > 1000 or any(not isinstance(x, str) for x in items):
            raise APIError(400, f"snapshot.{key} must be a string list")
    profiles = _object(value.get("profiles", {}), "snapshot.profiles")
    capabilities = value.get("capabilities", [])
    if not isinstance(capabilities, list) or len(capabilities) > 32 or any(not isinstance(item, str) for item in capabilities):
        raise APIError(400, "snapshot.capabilities must be a string list")
    if any(not isinstance(x, str) for x in profiles.values()):
        raise APIError(400, "snapshot.profiles must map names to image strings")
    templates = value.get('task_templates', [])
    if not isinstance(templates, list) or len(templates) > 100:
        raise APIError(400, 'snapshot.task_templates must contain at most 100 tasks')
    for template in templates:
        if not isinstance(template, dict) or 'token' in template or 'admin_token' in template:
            raise APIError(400, 'Invalid task template')
        validate_task(template)
    for key in ("free_ram_mb", "disk_free_mb"):
        if value.get(key) is not None:
            _number(value[key], key)
    gpus = value.get("gpus", [])
    if not isinstance(gpus, list) or len(gpus) > 256:
        raise APIError(400, "snapshot.gpus must be an array")
    uuids = set()
    for gpu in gpus:
        _object(gpu, "GPU")
        identity = gpu.get("uuid")
        if not isinstance(identity, str) or not identity or len(identity) > 200 or identity in uuids:
            raise APIError(400, "GPU UUIDs must be nonempty and unique")
        uuids.add(identity)
        for key in ("free_mb", "total_mb", "reserve_mb"):
            if gpu.get(key) is not None:
                _number(gpu[key], "GPU." + key)
        if "reserve_mb" in gpu and gpu["reserve_mb"] is None:
            raise APIError(400, "GPU.reserve_mb cannot be null")
        if "max_jobs" in gpu:
            _integer(gpu["max_jobs"], "GPU.max_jobs")
        names = gpu.get("profiles", [])
        if not isinstance(names, list) or any(not isinstance(x, str) for x in names):
            raise APIError(400, "GPU.profiles must be a string list")
    validate_policy_snapshot(value)
    return value


def inspect_update_state(db):
    """Inspect committed work using either the live DB or a read-only connection."""
    terminal = tuple(TERMINAL)
    placeholders = ",".join("?" for _ in terminal)
    active = db.execute(f"SELECT COUNT(*) FROM jobs WHERE state NOT IN ({placeholders}) "
                        "OR command_id>command_ack", terminal).fetchone()[0]
    uploads = db.execute("SELECT COUNT(*) FROM uploads u WHERE NOT EXISTS "
                         "(SELECT 1 FROM artifacts a WHERE a.job_id=u.job_id AND a.sha256=u.sha256)").fetchone()[0]
    projects = db.execute("SELECT COUNT(*) FROM project_uploads WHERE completed_digest IS NULL").fetchone()[0]
    deployments = db.execute("SELECT COUNT(*) FROM project_deployments WHERE status NOT IN ('installed','failed','deleted','delete_failed')").fetchone()[0]
    uncertain = 0
    for row in db.execute("SELECT id,last_seen,snapshot FROM nodes"):
        node_id, last_seen, serialized = row
        # Unused enrollments do not need telemetry. Missing or stale proof from
        # other nodes is diagnostic: controller-owned work is checked above and
        # remote nodes can resume synchronization after the controller restarts.
        has_work = db.execute("SELECT 1 FROM jobs WHERE node_id=? LIMIT 1", (node_id,)).fetchone()
        if last_seen == 0 and not has_work:
            continue
        snapshot = json.loads(serialized)
        if now() - last_seen > 45 or snapshot.get("update_quiescent") is not True:
            uncertain += 1
    counts = dict(active_jobs=active, pending_uploads=uploads, pending_projects=projects + deployments,
                  unverified_nodes=uncertain)
    reasons = []
    if active:
        reasons.append(f"{active} 个实验或操作尚未完成")
    if uploads or projects or deployments:
        reasons.append("文件回传或项目分发尚未完成")
    return dict(ready_for_update=not reasons, detail="；".join(reasons) or
                "管理端当前无未完成实验或已登记的文件传输，可以安装更新；离线节点恢复后继续同步", **counts)


class Hub(MatrixHubMixin, NodePolicyHubMixin, MobileHubMixin):
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.updating = False
        self.active_update_requests = 0
        self.project_upload_locks = {}
        self.local_imports = None
        self.config = read_json(self.root / "hub.json")
        if self.config is None:
            self.config = {"admin_token": secrets.token_urlsafe(32), "nodes": {}}
            atomic_json(self.root / "hub.json", self.config)
            try:
                (self.root / "hub.json").chmod(0o600)
            except OSError:
                pass
        if not isinstance(self.config, dict) or not isinstance(self.config.get("admin_token"), str) or not isinstance(self.config.get("nodes"), dict):
            raise ValueError("Invalid hub.json; do not replace it while jobs exist")
        self.db = sqlite3.connect(self.root / "hub.sqlite3", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY, last_seen REAL NOT NULL DEFAULT 0,
                snapshot TEXT NOT NULL DEFAULT '{}', mode TEXT NOT NULL DEFAULT 'run');
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, spec TEXT NOT NULL, node_id TEXT REFERENCES nodes(id),
                state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                updated REAL NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                seq INTEGER NOT NULL DEFAULT 0, metrics TEXT NOT NULL DEFAULT '{}',
                command_id INTEGER NOT NULL DEFAULT 0, command_ack INTEGER NOT NULL DEFAULT 0,
                action TEXT, resume_after_attempt INTEGER, log_tail TEXT NOT NULL DEFAULT '', timing TEXT);
            CREATE TABLE IF NOT EXISTS submissions (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, ids TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
                time REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_job ON events(job_id, event_id);
            CREATE TABLE IF NOT EXISTS artifacts (
                job_id TEXT NOT NULL REFERENCES jobs(id), name TEXT NOT NULL, sha256 TEXT NOT NULL,
                size INTEGER NOT NULL, uploaded REAL NOT NULL,
                PRIMARY KEY(job_id, name, sha256));
            CREATE TABLE IF NOT EXISTS uploads (
                job_id TEXT NOT NULL REFERENCES jobs(id), sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                PRIMARY KEY(job_id, sha256));
            CREATE TABLE IF NOT EXISTS project_uploads (
                id TEXT PRIMARY KEY, size INTEGER NOT NULL, digest TEXT, completed_digest TEXT);
            CREATE TABLE IF NOT EXISTS projects (
                digest TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
                size INTEGER NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS project_deployments (
                digest TEXT NOT NULL REFERENCES projects(digest), node_id TEXT NOT NULL REFERENCES nodes(id),
                revision INTEGER NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL, PRIMARY KEY(digest,node_id));
        """)
        with self.transaction():
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
            if "log_tail" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN log_tail TEXT NOT NULL DEFAULT ''")
            if "timing" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN timing TEXT")
            project_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(projects)")}
            if "bundle_id" not in project_columns:
                self.db.execute("ALTER TABLE projects ADD COLUMN bundle_id TEXT NOT NULL DEFAULT ''")
            if "deleted_at" not in project_columns:
                self.db.execute("ALTER TABLE projects ADD COLUMN deleted_at REAL")
            if "cleanup_error" not in project_columns:
                self.db.execute("ALTER TABLE projects ADD COLUMN cleanup_error TEXT NOT NULL DEFAULT ''")
        for node_id in self.config["nodes"]:
            self.db.execute("INSERT OR IGNORE INTO nodes(id) VALUES (?)", (node_id,))
        self._cleanup_project_archives()
        initialize_matrices(self.db)
        initialize_node_policies(self.db)
        initialize_mobile(self.db)

    @contextmanager
    def transaction(self):
        with self.lock:
            if self.updating:
                raise APIError(503, "Controller is stopping for an update; retry after it restarts")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def ai_settings(self, payload=None):
        from .ai_assist import AIAssist
        return AIAssist(self).settings(payload)

    def ai_test(self, payload):
        from .ai_assist import AIAssist
        return AIAssist(self).test(payload)

    def import_assist(self, payload):
        from .ai_assist import AIAssist
        return AIAssist(self).assist(payload)

    @contextmanager
    def update_request(self):
        """Fence new HTTP mutations while an atomic update stop is accepted."""
        with self.lock:
            if self.updating:
                raise APIError(503, "Controller is stopping for an update; retry after it restarts")
            self.active_update_requests += 1
        try:
            yield
        finally:
            with self.lock:
                self.active_update_requests -= 1

    def update_status(self, stop=False):
        with self.lock:
            result = inspect_update_state(self.db)
            busy_imports = self.local_imports is not None and bool(self.local_imports.threads)
            if self.active_update_requests or busy_imports or self.project_upload_locks:
                result.update(ready_for_update=False, detail="正在处理请求或导入项目，请完成后重试")
            if stop and result["ready_for_update"]:
                # No mutation can enter after this check. Existing background
                # imports and uploads were checked above, under the same lock.
                self.updating = True
            return result

    def close(self):
        if self.local_imports is not None:
            self.local_imports.close()
        with self.lock:
            self.db.close()

    def project_imports(self):
        with self.lock:
            if self.local_imports is None:
                from .project_import import ProjectImports
                self.local_imports = ProjectImports(self)
            return self.local_imports

    def add_node(self, node_id):
        if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", node_id):
            raise ValueError("Node ID must contain 1-64 letters, digits, _, . or -")
        with self.lock:
            # The enrollment CLI can run while this server is online.
            self.config = read_json(self.root / "hub.json", self.config)
            if node_id not in self.config["nodes"]:
                self.config["nodes"][node_id] = secrets.token_urlsafe(32)
                atomic_json(self.root / "hub.json", self.config)
            self.db.execute("INSERT OR IGNORE INTO nodes(id) VALUES (?)", (node_id,))
            return self.config["nodes"][node_id]

    def authenticate(self, header):
        if not isinstance(header, str) or not header.startswith("Bearer "):
            raise APIError(401, "Bearer authentication is required")
        token = header[7:]
        if not token or len(token) > 512:
            raise APIError(401, "Invalid bearer token")
        with self.lock:
            self.config = read_json(self.root / "hub.json", self.config)
            if secrets.compare_digest(token, self.config["admin_token"]):
                return "admin", None
            for node_id, configured in self.config["nodes"].items():
                if secrets.compare_digest(token, configured):
                    return "node", node_id
        raise APIError(401, "Invalid bearer token")

    def enroll(self, payload):
        from .pairing import validate_pairing
        # Validate all fields before changing the durable enrollment registry.
        pairing = validate_pairing(dict(payload, schema=1, token='validation-placeholder-only'))
        pairing['token'] = self.add_node(pairing['node_id'])
        return pairing

    def _event(self, job_id, kind, data):
        self.db.execute("INSERT INTO events(job_id,time,kind,data) VALUES (?,?,?,?)", (job_id, now(), kind, _json(data)))

    @staticmethod
    def _job(row):
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        result["metrics"] = json.loads(result["metrics"])
        result["timing"] = json.loads(result["timing"]) if result["timing"] is not None else None
        result.pop("resume_after_attempt", None)
        return result

    def _find_job(self, job_id):
        _identifier(job_id)
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise APIError(404, "Unknown job")
        return row

    def _owned(self, job_id, node_id):
        row = self._find_job(job_id)
        if row["node_id"] != node_id:
            raise APIError(403, "Job does not belong to this node")
        return row

    def state(self):
        with self.lock:
            jobs = [self._job(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY created DESC,id")]
            for job in jobs:
                job.pop("log_tail", None)
            nodes = []
            timestamp = now()
            for row in self.db.execute("SELECT * FROM nodes ORDER BY id"):
                item = dict(row)
                item["snapshot"] = json.loads(item["snapshot"])
                item["online"] = timestamp - item["last_seen"] <= 45
                item["resource_policy"] = self._node_resource_policy(item["id"])
                nodes.append(item)
            projects, project_deletions = [], []
            for row in self.db.execute("SELECT * FROM projects ORDER BY created DESC"):
                project = dict(row)
                project["deployments"] = [dict(item) for item in self.db.execute(
                    "SELECT node_id,revision,status,detail,updated FROM project_deployments WHERE digest=? ORDER BY node_id", (row["digest"],))]
                (project_deletions if project["deleted_at"] is not None else projects).append(project)
            deleted_bundles = {item["bundle_id"] for item in project_deletions if item["bundle_id"]} - {item["bundle_id"] for item in projects}
            for node in nodes:
                node["snapshot"]["task_templates"] = [item for item in node["snapshot"].get("task_templates", [])
                    if item.get("project_bundle_id") not in deleted_bundles]
            return {"jobs": jobs, "nodes": nodes, "projects": projects, "project_deletions": project_deletions,
                    "time": timestamp, "version": __version__}

    def submit(self, payload):
        _object(payload, "request")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise APIError(400, "request_id must be a nonempty string of at most 200 characters")
        spec = validate_task(payload.get("spec"))
        specs = expand_grid(spec, payload["grid"]) if "grid" in payload else [spec]
        fingerprint = hashlib.sha256(_json(specs).encode()).hexdigest()
        with self.transaction():
            self._validate_scheduling_nodes(specs)
            previous = self.db.execute("SELECT * FROM submissions WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise APIError(409, "request_id already identifies a different submission")
                return {"ids": json.loads(previous["ids"])}
            ids, timestamp = [], now()
            for item in specs:
                job_id = uuid.uuid4().hex
                ids.append(job_id)
                self.db.execute("INSERT INTO jobs(id,spec,state,created,updated) VALUES (?,?,?,?,?)", (job_id, _json(item), "queued", timestamp, timestamp))
                self._event(job_id, "submitted", {"request_id": request_id})
            self.db.execute("INSERT INTO submissions VALUES (?,?,?)", (request_id, fingerprint, _json(ids)))
            self._assign()
            return {"ids": ids}

    def set_mode(self, payload):
        _object(payload, "request")
        mode, node_id = payload.get("mode"), payload.get("node_id")
        if mode not in ("run", "drain") or not isinstance(node_id, str):
            raise APIError(400, "node_id and mode run/drain are required")
        with self.transaction():
            if "expected_mode" in payload:
                node = self.db.execute("SELECT mode FROM nodes WHERE id=?", (node_id,)).fetchone()
                if node is None:
                    raise APIError(404, "Unknown node")
                if node["mode"] != payload["expected_mode"]:
                    raise APIError(409, "节点策略已变化，请刷新后确认")
            if self.db.execute("UPDATE nodes SET mode=? WHERE id=?", (mode, node_id)).rowcount != 1:
                raise APIError(404, "Unknown node")
            self._assign()
        return {"node_id": node_id, "mode": mode}

    def action(self, payload):
        _object(payload, "request")
        action = payload.get("action")
        if action not in ("stop", "resume", "cancel"):
            raise APIError(400, "action must be stop, resume or cancel")
        with self.transaction():
            row = self._find_job(payload.get("job_id"))
            if "expected_command_id" in payload and row["command_id"] != payload["expected_command_id"]:
                raise APIError(409, "此实验已收到其他操作，请刷新后确认结果")
            if action == "resume" and self._project_is_deleted(json.loads(row["spec"])):
                raise APIError(409, "This project was deleted; import and deploy it again before submitting a new experiment")
            if action == "resume" and json.loads(row["spec"]).get("resume_supported") is False:
                raise APIError(409, "This algorithm does not provide configured native resume; submit a new experiment")
            if row["action"] == action and row["command_id"] > row["command_ack"]:
                return {"id": row["id"], "command_id": row["command_id"], "action": action}
            if action == "resume" and row["state"] not in ("interrupted", "paused", "failed"):
                raise APIError(409, "Only interrupted, paused or failed jobs can resume")
            if action == "stop" and row["state"] in TERMINAL and row["resume_after_attempt"] is None:
                raise APIError(409, "This job has already stopped")
            if action == "cancel" and row["state"] == "canceled":
                return {"id": row["id"], "command_id": row["command_id"], "action": action}
            if action == "cancel" and row["state"] == "succeeded":
                raise APIError(409, "A successful job cannot be canceled")
            command_id = row["command_id"] + 1
            timestamp = now()
            # A queued, unowned job has no worker to acknowledge the command.
            if row["node_id"] is None:
                if action == "resume":
                    raise APIError(409, "An unassigned job cannot resume")
                self.db.execute("UPDATE jobs SET state='canceled', updated=?, action=?, command_id=?, command_ack=? WHERE id=?", (timestamp, action, command_id, command_id, row["id"]))
            else:
                resume_after = row["attempt"] if action == "resume" else row["resume_after_attempt"]
                self.db.execute("UPDATE jobs SET updated=?,action=?,command_id=?,resume_after_attempt=? WHERE id=?", (timestamp, action, command_id, resume_after, row["id"]))
            self._event(row["id"], "command", {"action": action, "command_id": command_id})
            return {"id": row["id"], "command_id": command_id, "action": action}

    def _assign(self):
        nodes = []
        # Drained source nodes still supply legacy deployment metadata needed
        # to rebind their templates; choose_assignment filters destinations.
        for row in self.db.execute("SELECT * FROM nodes"):
            item = dict(row)
            item["snapshot"] = json.loads(item["snapshot"])
            nodes.append(item)
        counts = {}
        for row in self.db.execute("SELECT node_id,state FROM jobs WHERE node_id IS NOT NULL"):
            if row["state"] not in TERMINAL:
                counts[row["node_id"]] = counts.get(row["node_id"], 0) + 1
        pending = list(self.db.execute("SELECT id,spec,created FROM jobs WHERE state='queued' AND node_id IS NULL"))
        pending.sort(key=lambda row: (-json.loads(row["spec"])["priority"], row["created"], row["id"]))
        for row in pending:
            assignment = choose_assignment(json.loads(row["spec"]), nodes, counts)
            if assignment is None:
                continue
            node_id = assignment["node_id"]
            self.db.execute("UPDATE jobs SET node_id=?,spec=?,state='assigned',updated=? WHERE id=?", (node_id, _json(assignment["spec"]), now(), row["id"]))
            counts[node_id] = counts.get(node_id, 0) + 1
            self._event(row["id"], "assigned", {"node_id": node_id})

    def sync(self, node_id, payload):
        _object(payload, "request")
        if payload.get("node_id") != node_id:
            raise APIError(403, "node_id does not match bearer token")
        snapshot = _snapshot(payload.get("snapshot"))
        reports = payload.get("reports", [])
        if not isinstance(reports, list) or len(reports) > 1000:
            raise APIError(400, "reports must be an array with at most 1000 entries")
        project_reports = payload.get("project_reports", [])
        if not isinstance(project_reports, list) or len(project_reports) > 100:
            raise APIError(400, "project_reports must be an array with at most 100 entries")
        with self.transaction():
            project_checked = []
            for report in project_reports:
                _object(report, "project report")
                digest = self._project_digest(report.get("digest"))
                revision = _integer(report.get("revision"), "revision", 1)
                status, detail = report.get("status"), report.get("detail", "")
                if status not in ("downloading", "installing", "installed", "failed", "deleting", "deleted", "delete_failed") or not isinstance(detail, str) or len(detail) > 1000:
                    raise APIError(400, "Invalid project installation report")
                owned = self.db.execute("SELECT d.revision,d.status,p.deleted_at FROM project_deployments d JOIN projects p ON p.digest=d.digest WHERE d.digest=? AND d.node_id=?", (digest, node_id)).fetchone()
                if not owned:
                    raise APIError(403, "Project was not deployed to this node")
                deleting = owned["deleted_at"] is not None
                if (revision == owned["revision"] and deleting == (status in ("deleting", "deleted", "delete_failed"))
                        and (owned["status"] not in ("deleted", "delete_failed") or status == owned["status"])):
                    project_checked.append((digest, revision, status, detail))
            # Validate the entire batch before accepting any report or heartbeat.
            checked = []
            for report in reports:
                _object(report, "report")
                row = self._owned(report.get("id"), node_id)
                seq = _integer(report.get("seq"), "seq", 1)
                attempt = _integer(report.get("attempt", 0), "attempt")
                state = {"completed": "succeeded", "cancelled": "canceled"}.get(report.get("state"), report.get("state"))
                if not isinstance(state, str) or state not in STATES or state == "queued":
                    raise APIError(400, "Invalid reported state")
                detail = report.get("detail", "")
                if not isinstance(detail, str) or len(detail) > 8192:
                    raise APIError(400, "detail must be a string of at most 8192 characters")
                metrics = _object(report.get("metrics", {}), "metrics")
                if len(_json(metrics)) > 128 * 1024:
                    raise APIError(400, "metrics are too large")
                log_tail = report.get("log_tail")
                if "log_tail" in report and (not isinstance(log_tail, str) or len(log_tail.encode("utf-8")) > 16 * 1024):
                    raise APIError(400, "log_tail must be a string of at most 16 KiB UTF-8")
                command_ack = _integer(report.get("command_ack", 0), "command_ack", 0, row["command_id"])
                timing = _timing(report["timing"]) if "timing" in report else None
                checked.append((report, seq, attempt, state, detail, metrics, command_ack, log_tail, timing))
            self.db.execute("UPDATE nodes SET last_seen=?,snapshot=? WHERE id=?", (now(), _json(snapshot), node_id))
            for digest, revision, status, detail in project_checked:
                self.db.execute("UPDATE project_deployments SET status=?,detail=?,updated=? WHERE digest=? AND node_id=? AND revision=?", (status, detail, now(), digest, node_id, revision))
            ack = {}
            for report, seq, attempt, state, detail, metrics, command_ack, log_tail, timing in checked:
                row = self._owned(report["id"], node_id)
                # Commands are acknowledged independently of report sequence.
                if command_ack > row["command_ack"]:
                    self.db.execute("UPDATE jobs SET command_ack=? WHERE id=?", (command_ack, row["id"]))
                if seq <= row["seq"]:
                    ack[row["id"]] = row["seq"]
                    continue
                resume = row["resume_after_attempt"] is not None and attempt > row["resume_after_attempt"]
                cancel = row["action"] == "cancel" and state == "canceled" and attempt == row["attempt"]
                valid = attempt >= row["attempt"]
                if attempt > max(1, row["attempt"]) and not resume:
                    valid = False
                if row["state"] in TERMINAL and (state != row["state"] or attempt != row["attempt"]) and not (resume or cancel):
                    valid = False
                if attempt == row["attempt"] and state in PROGRESS and row["state"] in PROGRESS and PROGRESS[state] < PROGRESS[row["state"]]:
                    valid = False
                if valid:
                    timestamp = now()
                    # The worker owns accumulated runtime, including offline runs.
                    # An old worker's report makes timing unknown instead of leaving
                    # a stale running sample to continue ticking after a transition.
                    serialized_timing = _json({**timing, "received_at": timestamp}) if timing is not None else None
                    self.db.execute("UPDATE jobs SET state=?,detail=?,attempt=?,seq=?,metrics=?,updated=?,resume_after_attempt=?,log_tail=?,timing=? WHERE id=?", (state, detail, attempt, seq, _json(metrics), timestamp, None if resume or cancel else row["resume_after_attempt"], row["log_tail"] if log_tail is None else log_tail, serialized_timing, row["id"]))
                    if state != row["state"] or attempt != row["attempt"]:
                        self._event(row["id"], "state", {"state": state, "detail": detail, "attempt": attempt})
                    if _json(metrics) != row["metrics"] or attempt != row["attempt"] and metrics:
                        self._event(row["id"], "metrics", {"metrics": metrics, "attempt": attempt})
                else:
                    # Consume stale state too, preventing an endless resend loop.
                    self.db.execute("UPDATE jobs SET seq=? WHERE id=?", (seq, row["id"]))
                    self._event(row["id"], "ignored_report", {"state": state, "attempt": attempt, "seq": seq})
                ack[row["id"]] = seq
            self._assign()
            jobs = []
            for row in self.db.execute("SELECT * FROM jobs WHERE node_id=? ORDER BY created,id", (node_id,)):
                if row["state"] not in TERMINAL or row["command_id"] > row["command_ack"]:
                    jobs.append({"id": row["id"], "spec": json.loads(row["spec"]), "command_id": row["command_id"], "action": row["action"] if row["command_id"] > row["command_ack"] else None})
            mode = self.db.execute("SELECT mode FROM nodes WHERE id=?", (node_id,)).fetchone()[0]
            deployments = [dict(row) for row in self.db.execute(
                "SELECT d.digest,p.size,p.project_id,p.bundle_id,d.revision,CASE WHEN p.deleted_at IS NULL THEN 'install' ELSE 'delete' END AS action FROM project_deployments d JOIN projects p ON p.digest=d.digest WHERE d.node_id=? AND d.status NOT IN ('installed','failed','deleted','delete_failed') ORDER BY d.updated LIMIT 100", (node_id,))]
            capabilities = snapshot.get("capabilities", [])
            deployments = [item for item in deployments if
                ("project-delete-v1" if item["action"] == "delete" else "project-bundle-v1") in capabilities]
            response = {"jobs": jobs, "mode": mode, "ack": ack,
                        "project_deployments": deployments}
            if NODE_POLICY_CAPABILITY in capabilities:
                response["resource_policy"] = self._node_resource_policy(node_id)
            return response

    @staticmethod
    def _project_digest(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise APIError(400, "Project digest must be 64 lowercase hexadecimal characters")
        return value

    @contextmanager
    def _project_upload_guard(self, upload_id):
        # Serialize retries for this file, not node heartbeats or other uploads.
        with self.lock:
            entry = self.project_upload_locks.setdefault(upload_id, {"lock": threading.Lock(), "users": 0})
            entry["users"] += 1
        try:
            with entry["lock"]:
                yield
        finally:
            with self.lock:
                entry["users"] -= 1
                if not entry["users"]:
                    del self.project_upload_locks[upload_id]

    def project_upload(self, payload):
        """Resumable admin upload; the final ZIP identity is computed by the server."""
        from .harness_project import read_bundle
        upload_id = _identifier(payload.get("upload_id"), "upload_id")
        size = _integer(payload.get("size"), "size", 1, MAX_PROJECT_SIZE)
        offset = _integer(payload.get("offset"), "offset", 0, size)
        expected = payload.get("sha256")
        if expected is not None:
            self._project_digest(expected)
        encoded = payload.get("data")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_CHUNK + 2) // 3) * 4:
            raise APIError(400, "Project chunk exceeds 512 KiB")
        try:
            block = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise APIError(400, "Project data must be base64") from error
        if len(block) > MAX_CHUNK or offset + len(block) > size:
            raise APIError(400, "Project chunk exceeds declared size")
        with self._project_upload_guard(upload_id):
            with self.transaction():
                row = self.db.execute("SELECT * FROM project_uploads WHERE id=?", (upload_id,)).fetchone()
                if row and (row["size"] != size or row["digest"] != expected):
                    raise APIError(409, "upload_id already identifies another upload")
                if row and row["completed_digest"]:
                    project = self.db.execute("SELECT * FROM projects WHERE digest=?", (row["completed_digest"],)).fetchone()
                    if project["deleted_at"] is not None:
                        raise APIError(409, "This upload belongs to a deleted project; start a new import/upload")
                    return {"offset": size, "complete": True, "project": dict(project)}
                self.db.execute("INSERT OR IGNORE INTO project_uploads VALUES (?,?,?,NULL)", (upload_id, size, expected))
                partial = safe_child(self.root, f"project-partials/{upload_id}.part")
                partial.parent.mkdir(parents=True, exist_ok=True)
                current = partial.stat().st_size if partial.exists() else 0
                if not (offset == 0 and not block) and offset != current:
                    return {"offset": current, "complete": False}
                if block:
                    with partial.open("ab") as stream:
                        stream.write(block)
                        stream.flush()
                        os.fsync(stream.fileno())
                    current += len(block)
                if current < size:
                    return {"offset": current, "complete": False}
            # These streaming checks may take minutes for a large dataset. The
            # SQLite transaction and the coordinator lock have already ended.
            digest = sha256_file(partial)
            if expected is not None and digest != expected:
                partial.unlink()
                raise APIError(422, "Project checksum mismatch; upload restarts at byte 0")
            try:
                manifest = read_bundle(partial)
            except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as error:
                partial.unlink()
                raise APIError(422, "Invalid external harness project bundle: " + str(error)[:300]) from error
            final = safe_child(self.root, f"projects/{digest}.zip")
            final.parent.mkdir(parents=True, exist_ok=True)
            with self.transaction():
                pending = self.db.execute("SELECT 1 FROM project_deployments d JOIN projects p ON p.digest=d.digest WHERE p.bundle_id=? AND p.deleted_at IS NOT NULL AND d.status!='deleted'", (manifest.get("bundle_id", ""),)).fetchone()
                if pending:
                    partial.unlink(missing_ok=True)
                    raise APIError(409, "Wait for all workers to finish deleting this snapshot before importing it again")
                deleted = self.db.execute("SELECT deleted_at FROM projects WHERE digest=?", (digest,)).fetchone()
                if deleted and deleted["deleted_at"] is not None:
                    pending = self.db.execute("SELECT 1 FROM project_deployments WHERE digest=? AND status!='deleted'", (digest,)).fetchone()
                    if pending:
                        partial.unlink(missing_ok=True)
                        raise APIError(409, "Wait for all workers to finish deleting this project before importing the identical bundle again")
                    self.db.execute("UPDATE projects SET deleted_at=NULL,cleanup_error='',created=? WHERE digest=?", (now(), digest))
                os.replace(partial, final)
                self.db.execute("INSERT OR IGNORE INTO projects(digest,project_id,name,size,created,bundle_id) VALUES (?,?,?,?,?,?)", (digest, manifest["project_id"], manifest["name"], size, now(), manifest.get("bundle_id", "")))
                self.db.execute("UPDATE project_uploads SET completed_digest=? WHERE id=?", (digest, upload_id))
                project = self.db.execute("SELECT * FROM projects WHERE digest=?", (digest,)).fetchone()
                return {"offset": size, "complete": True, "project": dict(project)}

    def project_deploy(self, payload):
        digest = self._project_digest(payload.get("digest"))
        node_ids = payload.get("node_ids")
        if not isinstance(node_ids, list) or not 1 <= len(node_ids) <= 100 or any(not isinstance(item, str) for item in node_ids):
            raise APIError(400, "Select 1-100 worker node IDs")
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM projects WHERE digest=? AND deleted_at IS NULL", (digest,)).fetchone():
                raise APIError(404, "Upload this project before deploying")
            for node_id in set(node_ids):
                row = self.db.execute("SELECT snapshot FROM nodes WHERE id=?", (node_id,)).fetchone()
                if row is None:
                    raise APIError(404, "Unknown worker " + node_id)
                if "project-bundle-v1" not in json.loads(row["snapshot"]).get("capabilities", []):
                    raise APIError(409, "Worker " + node_id + " must be upgraded and connected once before automatic project delivery")
            for node_id in set(node_ids):
                self.db.execute("INSERT INTO project_deployments VALUES (?,?,1,'queued','',?) ON CONFLICT(digest,node_id) DO UPDATE SET revision=revision+1,status='queued',detail='',updated=excluded.updated", (digest, node_id, now()))
        return {"digest": digest, "node_ids": sorted(set(node_ids)), "status": "queued"}

    def _project_is_deleted(self, spec):
        bundle_id = spec.get("project_bundle_id")
        return bool(bundle_id and self.db.execute(
            "SELECT 1 FROM projects WHERE bundle_id=? AND deleted_at IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM projects WHERE bundle_id=? AND deleted_at IS NULL)",
            (bundle_id, bundle_id)).fetchone())

    def project_delete(self, payload):
        """Hide immediately and retain a durable per-worker cleanup tombstone."""
        digest = self._project_digest(payload.get("digest"))
        if not isinstance(payload.get("retry", False), bool):
            raise APIError(400, "retry must be a boolean")
        with self.transaction():
            project = self.db.execute("SELECT * FROM projects WHERE digest=?", (digest,)).fetchone()
            if project is None:
                raise APIError(404, "Unknown project")
            bundle_id = project["bundle_id"]
            if not bundle_id:
                from .harness_project import read_bundle
                bundle_id = read_bundle(safe_child(self.root, f"projects/{digest}.zip"))["bundle_id"]
                self.db.execute("UPDATE projects SET bundle_id=? WHERE digest=?", (bundle_id, digest))
            # Different ZIP encodings may name the same installed snapshot. Its
            # packages share a lifecycle, so no alias can redeploy a deleted copy.
            aliases = [row[0] for row in self.db.execute("SELECT digest FROM projects WHERE bundle_id=? AND (? IS NULL OR deleted_at IS NOT NULL)", (bundle_id, project['deleted_at']))]
            for row in self.db.execute("SELECT id,spec,state,command_id,command_ack FROM jobs"):
                if project["deleted_at"] is None and json.loads(row["spec"]).get("project_bundle_id") == bundle_id and (
                        row["state"] not in TERMINAL or row["command_id"] > row["command_ack"]):
                    raise APIError(409, "Stop or cancel this project's active experiments and wait for worker acknowledgement before deleting: " + row["id"])
            timestamp = now()
            for alias in aliases:
                changed = self.db.execute("UPDATE projects SET deleted_at=? WHERE digest=? AND deleted_at IS NULL", (timestamp, alias)).rowcount
                if changed:
                    self.db.execute("UPDATE project_deployments SET revision=revision+1,status='delete_pending',detail='Waiting for worker cleanup; offline workers retry on reconnect. Older workers need an upgrade.',updated=? WHERE digest=?", (timestamp, alias))
                elif payload.get("retry"):
                    self.db.execute("UPDATE project_deployments SET revision=revision+1,status='delete_pending',detail='Cleanup retry requested',updated=? WHERE digest=? AND status='delete_failed'", (timestamp, alias))
            deployments = [dict(row) for row in self.db.execute(
                "SELECT d.digest,d.node_id,d.revision,d.status,d.detail,d.updated FROM project_deployments d JOIN projects p ON p.digest=d.digest WHERE p.bundle_id=? AND p.deleted_at IS NOT NULL ORDER BY d.node_id,d.digest", (bundle_id,))]
        # Commit deletion intent before unlinking any copy. Replays and Hub
        # restart finish interrupted controller cleanup without reviving a file.
        self._cleanup_project_archives(aliases)
        with self.lock:
            errors = [row[0] for row in self.db.execute("SELECT cleanup_error FROM projects WHERE bundle_id=? AND cleanup_error!=''", (bundle_id,))]
        return {"digest": digest, "deleted": True, "cleanup_pending": bool(errors) or any(item["status"] != "deleted" for item in deployments),
                "deployments": deployments, "controller_cleanup_error": "; ".join(errors)}

    def _cleanup_project_archives(self, digests=None):
        with self.lock:
            rows = list(self.db.execute("SELECT digest FROM projects WHERE deleted_at IS NOT NULL"))
            for row in rows:
                digest = row["digest"]
                if digests is not None and digest not in digests:
                    continue
                error = ""
                try:
                    safe_child(self.root, f"projects/{digest}.zip").unlink(missing_ok=True)
                    # Local import ZIPs are owned build products; source paths
                    # and editable import drafts are never deletion targets.
                    for state_file in (self.root / "imports").glob("*/state.json"):
                        state = read_json(state_file, {})
                        # Saving an unpublished import draft sets project=None.
                        if (state.get("project") or {}).get("digest") != digest:
                            continue
                        for archive in state_file.parent.glob("bundle-*.zip"):
                            safe_child(self.root, archive.relative_to(self.root).as_posix()).unlink(missing_ok=True)
                except (OSError, ValueError) as failure:
                    error = str(failure)[:1000]
                self.db.execute("UPDATE projects SET cleanup_error=? WHERE digest=? AND deleted_at IS NOT NULL", (error, digest))

    def project_download(self, node_id, digest, offset):
        digest = self._project_digest(digest)
        offset = _integer(offset, "offset", 0, MAX_PROJECT_SIZE)
        with self.lock:
            row = self.db.execute("SELECT p.size FROM projects p JOIN project_deployments d ON p.digest=d.digest WHERE p.digest=? AND d.node_id=? AND p.deleted_at IS NULL", (digest, node_id)).fetchone()
            if row is None:
                raise APIError(403, "Project was not deployed to this node")
            if offset > row["size"]:
                raise APIError(400, "Offset exceeds project size")
            path = safe_child(self.root, f"projects/{digest}.zip")
            with path.open("rb") as stream:
                stream.seek(offset)
                block = stream.read(MAX_CHUNK)
            return {"data": base64.b64encode(block).decode("ascii"), "offset": offset + len(block), "size": row["size"]}

    def job(self, job_id):
        with self.lock:
            result = self._job(self._find_job(job_id))
            events = list(self.db.execute("SELECT * FROM events WHERE job_id=? ORDER BY event_id DESC LIMIT 200", (job_id,)))
            result["events"] = [{**dict(row), "data": json.loads(row["data"])} for row in reversed(events)]
            result["artifacts"] = [dict(row) for row in self.db.execute("SELECT name,size,sha256,uploaded FROM artifacts WHERE job_id=? ORDER BY name,uploaded", (job_id,))]
            result["time"] = now()
            return result

    def _archive_paths(self, job_id, digest):
        # The storage keys are validated identifiers, never client file names.
        partial = safe_child(self.root, f"partials/{job_id}/{digest}.part")
        final = safe_child(self.root, f"archives/{job_id}/{digest}")
        return partial, final

    def upload(self, node_id, payload):
        _object(payload, "request")
        job_id = _identifier(payload.get("job_id"))
        digest, name = payload.get("sha256"), payload.get("name")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise APIError(400, "sha256 must be 64 lowercase hexadecimal characters")
        if not isinstance(name, str) or len(name) > 1024 or any(ord(c) < 32 for c in name):
            raise APIError(400, "Invalid artifact name")
        safe_child(self.root, name)  # Validate traversal and symlinks without creating it.
        size = _integer(payload.get("size"), "size", 0, 16 * 1024**4)
        offset = _integer(payload.get("offset"), "offset", 0, size)
        data = payload.get("data")
        if not isinstance(data, str) or len(data) > ((MAX_CHUNK + 2) // 3) * 4:
            raise APIError(400, "Upload chunk exceeds 512 KiB")
        try:
            block = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as error:
            raise APIError(400, "data must be valid base64") from error
        if len(block) > MAX_CHUNK or offset + len(block) > size:
            raise APIError(400, "Chunk exceeds declared size")
        with self.transaction():
            self._owned(job_id, node_id)
            partial, final = self._archive_paths(job_id, digest)
            existing = self.db.execute("SELECT size FROM uploads WHERE job_id=? AND sha256=?", (job_id, digest)).fetchone()
            if existing and existing["size"] != size:
                raise APIError(409, "The same hash was declared with a different size")
            if final.exists():
                if final.stat().st_size != size:
                    raise APIError(409, "Archived size disagrees with request")
                self.db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?)", (job_id, name, digest, size, now()))
                return {"offset": size, "complete": True}
            self.db.execute("INSERT OR IGNORE INTO uploads VALUES (?,?,?)", (job_id, digest, size))
            partial.parent.mkdir(parents=True, exist_ok=True)
            current = partial.stat().st_size if partial.exists() else 0
            if current > size:
                partial.unlink()
                current = 0
            query = offset == 0 and not block
            if not query and offset != current:
                return {"offset": current, "complete": False}
            if block:
                with partial.open("ab") as stream:
                    stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                current += len(block)
            elif size == 0 and not partial.exists():
                partial.touch()
            if current < size:
                return {"offset": current, "complete": False}
            if sha256_file(partial) != digest:
                partial.unlink()
                # Keep the declaration, but allow re-upload from byte zero.
                raise APIError(422, "SHA256 verification failed; upload restarts at offset 0")
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(partial, final)
            self.db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?)", (job_id, name, digest, size, now()))
            self._event(job_id, "artifact", {"name": name, "sha256": digest, "size": size})
            return {"offset": size, "complete": True}

    def artifact(self, job_id, digest):
        _identifier(job_id)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise APIError(400, "Invalid sha256")
        with self.lock:
            self._find_job(job_id)
            row = self.db.execute("SELECT size FROM artifacts WHERE job_id=? AND sha256=? LIMIT 1", (job_id, digest)).fetchone()
            _, path = self._archive_paths(job_id, digest)
            if row is None or not path.is_file():
                raise APIError(404, "Artifact not found")
            return path

    def results_csv(self):
        def flatten(value, prefix, result):
            if isinstance(value, dict):
                for key, item in value.items():
                    flatten(item, prefix + "." + str(key), result)
            else:
                result[prefix] = _json(value) if isinstance(value, list) else value

        def cell(value):
            # Spreadsheet clients interpret leading =,+,-,@ as formulas.
            if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
                return "'" + value
            return value

        with self.lock:
            rows = []
            timing_fields = ["started_at", "finished_at", "elapsed_seconds", "observed_at", "complete"]
            fields = ["id", "name", "algorithm", "group", "metric_protocol", "node_id", "state", "attempt", "updated"]
            fields += ["timing." + key for key in timing_fields]
            extra = set()
            for record in self.db.execute("SELECT * FROM jobs ORDER BY created,id"):
                job = self._job(record)
                result = {key: job.get(key, job["spec"].get(key, "")) for key in fields}
                for key in timing_fields:
                    result["timing." + key] = job["timing"][key] if job["timing"] is not None else ""
                flatten(job["spec"]["params"], "params", result)
                flatten(job["metrics"], "metrics", result)
                extra.update(set(result) - set(fields))
                rows.append(result)
            fields += sorted(extra)
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow([cell(field) for field in fields])
            for row in rows:
                writer.writerow([cell(row.get(field, "")) for field in fields])
            return stream.getvalue().encode("utf-8-sig")


def make_server(hub, host="127.0.0.1", port=8765):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ExperimentManager/0.2"

        def log_message(self, fmt, *args):
            # Default logs include no request headers or bearer tokens.
            if os.environ.get("EXPMAN_HTTP_LOG") == "1":
                super().log_message(fmt, *args)

        def log_error(self, fmt, *args):
            super().log_message(fmt, *args)

        def _bytes(self, data, content_type="application/json; charset=utf-8", status=200, disposition=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if self.path.split("?", 1)[0].startswith(("/mobile/", "/mobile-access")) or self.path == "/mobile":
                self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; "
                                 "script-src 'self'; style-src 'self'; img-src 'self'; "
                                 "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()
            self.wfile.write(data)

        def _send(self, result, status=200):
            self._bytes(_json(result).encode("utf-8"), status=status)

        def _body(self):
            if self.headers.get("Transfer-Encoding"):
                raise APIError(400, "Chunked HTTP request bodies are not supported")
            length = self.headers.get("Content-Length", "")
            if not length.isdigit() or int(length) > MAX_BODY:
                raise APIError(413, "A JSON body of at most 2 MiB is required")
            if self.headers.get_content_type() != "application/json":
                raise APIError(415, "Content-Type must be application/json")
            data = self.rfile.read(int(length))
            if len(data) != int(length):
                raise APIError(400, "Incomplete request body")
            try:
                def invalid_constant(_):
                    raise ValueError("Non-finite JSON numbers are not allowed")
                return _object(json.loads(data.decode("utf-8"), parse_constant=invalid_constant), "request")
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise APIError(400, "Invalid JSON") from error

        def _route(self):
            url = urlsplit(self.path)
            path = url.path
            mobile_files = {"/mobile": "mobile/index.html", "/mobile/": "mobile/index.html",
                            "/mobile/mobile.js": "mobile/mobile.js", "/mobile/model.js": "mobile/model.js",
                            "/mobile/mobile.css": "mobile/mobile.css", "/mobile-access": "mobile/access.html",
                            "/mobile/access.js": "mobile/access.js"}
            if self.command == "GET" and path in mobile_files:
                static = Path(__file__).parent / "static" / mobile_files[path]
                content_type = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                                ".css": "text/css; charset=utf-8"}[static.suffix]
                self._bytes(static.read_bytes(), content_type)
                return
            if self.command == "GET" and path in ("/", "/app.js", "/timing.js", "/style.css", "/favicon.ico"):
                filename = {"/": "index.html", "/app.js": "app.js", "/timing.js": "timing.js", "/style.css": "style.css", "/favicon.ico": "favicon.ico"}[path]
                static = Path(__file__).parent / "static" / filename
                if not static.is_file():
                    raise APIError(404, "Web interface files have not been installed")
                content_type = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "timing.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8", "favicon.ico": "image/vnd.microsoft.icon"}[filename]
                self._bytes(static.read_bytes(), content_type)
                return
            if not path.startswith("/api/"):
                raise APIError(404, "Not found")
            if path.startswith("/api/mobile/") and path not in ("/api/mobile/devices", "/api/mobile/revoke"):
                query = parse_qs(url.query)
                identities = query.get("id", [])
                if path == "/api/mobile/job" and len(identities) != 1:
                    raise APIError(400, "Exactly one id query parameter is required")
                payload = self._body() if self.command == "POST" else None
                with hub.update_request():
                    self._send(hub.mobile_request(self.headers.get("Authorization"), self.command, path,
                                                 payload, identities[0] if identities else None))
                return
            role, node_id = hub.authenticate(self.headers.get("Authorization"))
            required_role = "node" if path in ("/api/sync", "/api/upload", "/api/node-info", "/api/projects/download") else "admin"
            if role != required_role:
                raise APIError(403, "Token does not have permission for this endpoint")
            if path.startswith("/api/local/"):
                from .project_import import is_loopback
                if not is_loopback(self.client_address[0]):
                    raise APIError(403, "Local folders are accessible only from this controller computer; upload a project ZIP remotely")
            query = parse_qs(url.query)
            def parameter(name):
                values = query.get(name, [])
                if len(values) != 1:
                    raise APIError(400, f"Exactly one {name} query parameter is required")
                return values[0]
            if self.command == "GET":
                if path == "/api/mobile/devices":
                    self._send(hub.mobile_devices())
                elif path == "/api/local/imports":
                    self._send(hub.project_imports().listing())
                elif path == "/api/local/imports/item":
                    self._send(hub.project_imports().item(parameter("id")))
                elif path == "/api/ai/settings":
                    self._send(hub.ai_settings())
                elif path == "/api/node-info":
                    self._send({"node_id": node_id, "paired": True})
                elif path == "/api/projects/download":
                    offset = parameter("offset")
                    if not offset.isdigit():
                        raise APIError(400, "offset must be nonnegative integer")
                    self._send(hub.project_download(node_id, parameter("digest"), int(offset)))
                elif path == "/api/setup-info":
                    from .pairing import address_candidates
                    self._send({"addresses": address_candidates(self.server.server_address[1])})
                elif path == "/api/state":
                    self._send(hub.state())
                elif path == "/api/update-status":
                    self._send(hub.update_status())
                elif path == "/api/job":
                    self._send(hub.job(parameter("id")))
                elif path == "/api/results.csv":
                    self._bytes(hub.results_csv(), "text/csv; charset=utf-8", disposition='attachment; filename="results.csv"')
                elif path == "/api/matrices":
                    self._send(hub.matrices())
                elif path == "/api/matrices/item":
                    self._send(hub.matrix_item(parameter("id")))
                elif path == "/api/matrices/report.md":
                    identity = parameter("id")
                    self._bytes(hub.matrix_report(identity), "text/markdown; charset=utf-8",
                                disposition=f'attachment; filename="matrix-{identity}.md"')
                elif path == "/api/artifact":
                    artifact = hub.artifact(parameter("job_id"), parameter("sha256"))
                    # Stream checkpoints without loading the whole file into RAM.
                    with artifact.open("rb") as stream:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
                        self.send_header("Content-Disposition", f'attachment; filename="{artifact.name}"')
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            self.wfile.write(block)
                else:
                    raise APIError(404, "Not found")
            elif self.command == "POST":
                payload = self._body()
                routes = {"/api/jobs": hub.submit, "/api/node-mode": hub.set_mode, "/api/action": hub.action,
                          "/api/mobile/devices": hub.mobile_enroll, "/api/mobile/revoke": hub.mobile_revoke,
                          "/api/node-policy": hub.set_node_policy,
                          "/api/scheduling/preview": hub.scheduling_preview,
                          "/api/matrices/save": hub.matrix_save, "/api/matrices/preview": hub.matrix_preview,
                          "/api/matrices/start": hub.matrix_start, "/api/matrices/delete": hub.matrix_delete,
                          "/api/enroll": hub.enroll,
                          "/api/ai/settings": hub.ai_settings, "/api/ai/test": hub.ai_test,
                          "/api/projects/upload": hub.project_upload, "/api/projects/deploy": hub.project_deploy,
                          "/api/projects/delete": hub.project_delete,
                          "/api/sync": lambda p: hub.sync(node_id, p), "/api/upload": lambda p: hub.upload(node_id, p)}
                if path.startswith("/api/local/imports/"):
                    imports = hub.project_imports()
                    routes.update({"/api/local/imports/start": imports.start,
                                   "/api/local/imports/browse": imports.browse,
                                   "/api/local/imports/save": imports.save,
                                   "/api/local/imports/assist": hub.import_assist,
                                   "/api/local/imports/test-metrics": imports.test_metrics,
                                   "/api/local/imports/publish": imports.publish})
                if path not in routes:
                    raise APIError(404, "Not found")
                with hub.update_request():
                    self._send(routes[path](payload))
            else:
                raise APIError(405, "Method not allowed")

        def _handle(self):
            self.connection.settimeout(30)
            try:
                self._route()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass  # Peer may retry; persisted operations remain idempotent.
            except APIError as error:
                self._send({"error": str(error)}, error.status)
            except (ValueError, TypeError, OverflowError) as error:
                self._send({"error": str(error)}, 400)
            except Exception:
                # Do not expose local paths, SQL, or credentials in HTTP errors.
                self.log_error("Internal request error")
                self._send({"error": "Internal service error; inspect local service state"}, 500)

        do_GET = _handle
        do_POST = _handle

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.hub = hub
    return server


def serve(root, host="127.0.0.1", port=8765):
    hub = Hub(root)
    server = make_server(hub, host, port)
    print(f"Experiment Manager: http://{host}:{server.server_address[1]}", flush=True)
    print(f"Admin token is stored in {hub.root / 'hub.json'}; never share it with workers.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        hub.close()
