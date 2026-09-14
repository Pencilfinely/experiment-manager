"""Durable, single-owner execution node. Docker is invoked only through argv.

The built-in CPU demonstration is the only program this agent runs on the host.
Scientific commands run in explicitly approved images with read-only inputs.
"""
from __future__ import annotations

import base64
import copy
import csv
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse

from . import common, scheduler

TERMINAL = {"succeeded", "failed", "paused", "interrupted", "canceled"}
ACTIVE = {"starting", "running"}


def _pid_alive(pid):
    """Read-only process existence test; os.kill(pid, 0) is unsafe on Windows."""
    if not pid:
        return False
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _free_ram_mb():
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in
                ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.available // (1024 * 1024)
        return None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


class Agent:
    @classmethod
    def inspect_config(cls, config_path):
        """Read-only diagnostics: no lock, database, recovery or directory creation."""
        item = object.__new__(cls)
        item.config_path = Path(config_path).resolve()
        item.config = common.read_json(item.config_path)
        root = Path(item.config["root"]).expanduser()
        item.root = (root if root.is_absolute() else item.config_path.parent / root).resolve()
        while not item.root.exists() and item.root != item.root.parent:
            item.root = item.root.parent
        item.mode = "run"
        item.preparation = None
        return item.snapshot()

    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.config = common.read_json(self.config_path)
        if not isinstance(self.config, dict):
            raise ValueError("Node config must be a JSON object")
        for key in ("node_id", "hub_url", "token", "root"):
            if not isinstance(self.config.get(key), str) or not self.config[key]:
                raise ValueError(f"Node config requires {key}")
        parsed = urllib.parse.urlsplit(self.config["hub_url"])
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError("hub_url must be an HTTP(S) origin without credentials")
        root = Path(self.config["root"]).expanduser()
        self.root = (root if root.is_absolute() else self.config_path.parent / root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = open(self.root / "agent.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.lock.seek(0)
                if not self.lock.read(1):
                    self.lock.write(b"0")
                    self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.lock.close()
            raise RuntimeError(f"Another agent already owns {self.root}") from error
        self.db = sqlite3.connect(self.root / "node.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS report_acks (job_id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS uploads (
                job_id TEXT, name TEXT, sha TEXT, size INTEGER, path TEXT,
                offset INTEGER DEFAULT 0, complete INTEGER DEFAULT 0,
                PRIMARY KEY(job_id,name,sha));
        """)
        self.db.commit()
        self.processes = {}
        self.online = False
        self.last_error = None
        self.closed = False
        self.preparation = None
        self.prep_process = None
        self.prep_mutex = threading.Lock()
        self.log_checked = {}
        self.mode = self._meta("hub_mode", "run")
        for folder in ("runs", "repos", "worktrees", "upload_cache"):
            (self.root / folder).mkdir(exist_ok=True)
        for record in self.records():
            if record["state"] in ACTIVE and record["spec"]["backend"] == "demo":
                # A recorded PID is never authority to kill an unknown process.
                common.atomic_json(self._output(record) / "STOP", {"reason": "agent restarted"})
                self._save(record, state="interrupted", detail="Agent restarted; demo is not launched again automatically")

    def close(self):
        if self.closed:
            return
        self.closed = True
        # Only a child created by this preparation thread is terminated here.
        with self.prep_mutex:
            process = self.prep_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self.preparation:
            self.preparation["thread"].join(timeout=6)
        self.db.close()
        if os.name == "nt":
            import msvcrt
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
        self.lock.close()

    def _meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))

    def records(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT record FROM tasks ORDER BY id")]

    def _save(self, record, **changes):
        record.update(changes)
        record["seq"] = record.get("seq", 0) + 1
        record["updated"] = common.now()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?)", (
                record["id"], json.dumps(record, ensure_ascii=False, allow_nan=False)))

    def _output(self, record):
        return common.safe_child(self.root / "runs", record["id"])

    def _exec(self, argv, timeout=20, check=True):
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", shell=False)
        if self.preparation and threading.current_thread() is self.preparation["thread"]:
            with self.prep_mutex:
                if self.closed:
                    raise RuntimeError("Agent closed during preparation")
                process = subprocess.Popen(argv, **kwargs)
                self.prep_process = process
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            finally:
                with self.prep_mutex:
                    self.prep_process = None
            result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        else:
            result = subprocess.run(argv, timeout=timeout, **kwargs)
        if check and result.returncode:
            # Tokens are never command-line arguments; limit logs returned to Hub.
            raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {result.stderr[-1500:]}")
        return result

    def snapshot(self):
        policy = copy.deepcopy(self.config.get("policy", {}))
        if self.mode == "drain":
            policy["run_enabled"] = False
        profiles = {name: item["image"] for name, item in self.config.get("profiles", {}).items()
                    if item.get("verified") is True and isinstance(item.get("image"), str)}
        result = {"allow_demo": bool(self.config.get("allow_demo", False)),
                  "policy": policy, "tags": self.config.get("tags", []),
                  "allowed_repos": self.config.get("allowed_repos", []),
                  "profiles": profiles, "assets": [], "gpus": [],
                  "task_templates": self.config.get("task_templates", []),
                  "free_ram_mb": _free_ram_mb(),
                  "disk_free_mb": shutil.disk_usage(self.root).free // (1024 * 1024),
                  "cpu_count": os.cpu_count(), "local_time": time.strftime("%H:%M"),
                  "platform": sys.platform, "docker_available": False}
        result["pending_uploads"] = (self.db.execute("SELECT COUNT(*) FROM uploads WHERE complete=0").fetchone()[0]
                                     if hasattr(self, "db") else None)
        for name, value in self.config.get("assets", {}).items():
            path = Path(value)
            if path.is_absolute() and path.exists():
                result["assets"].append(name)
        # Windows host execution is intentionally demo-only.
        if sys.platform != "linux":
            return result
        try:
            self._exec(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=8)
            result["docker_available"] = True
            query = self._exec(["nvidia-smi", "--query-gpu=uuid,name,memory.total,memory.free",
                                "--format=csv,noheader,nounits"], timeout=8)
            for row in csv.reader(io.StringIO(query.stdout)):
                if len(row) != 4:
                    continue
                gpu_uuid, name, total, free = [value.strip() for value in row]
                configured = self.config.get("gpu_policy", {}).get(gpu_uuid)
                if not isinstance(configured, dict):
                    continue
                try:
                    total_mb, free_mb = int(total), int(free)
                except ValueError:
                    continue
                if not 0 <= free_mb <= total_mb or total_mb <= 0:
                    continue
                gpu_profiles = [key for key in profiles if any(
                    pattern.casefold() in name.casefold()
                    for pattern in self.config["profiles"][key].get("gpu_name_patterns", [])
                    if isinstance(pattern, str) and pattern)]
                result["gpus"].append({"uuid": gpu_uuid, "name": name, "total_mb": total_mb,
                    "free_mb": free_mb, "reserve_mb": configured.get("reserve_mb", 2048),
                    "max_jobs": configured.get("max_jobs", 1), "profiles": gpu_profiles})
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            # A failed query never means that all VRAM is available.
            result["gpus"] = []
        return result

    def _report(self, record):
        report = {key: record.get(key, default) for key, default in (
            ("id", None), ("seq", 0), ("state", "assigned"), ("detail", ""),
            ("attempt", 1), ("metrics", {}), ("command_ack", 0), ("log_tail", ""))}
        # The server limit is bytes, not characters (Chinese logs can use 3 bytes/character).
        report["log_tail"] = report["log_tail"].encode("utf-8")[-16000:].decode("utf-8", errors="ignore")
        return report

    def _sync(self, snapshot):
        confirmed = dict(self.db.execute("SELECT job_id,seq FROM report_acks"))
        pending = [record for record in self.records() if record["seq"] > confirmed.get(record["id"], 0)]
        # Older unsent reports are drained before newly updated active jobs.
        pending.sort(key=lambda record: (record.get("updated", 0), record["id"]))
        payload = {"node_id": self.config["node_id"], "snapshot": snapshot, "reports": []}
        body_bytes = len(json.dumps(payload, allow_nan=False).encode("utf-8"))
        for record in pending:
            report = self._report(record)
            report_bytes = len(json.dumps(report, allow_nan=False).encode("utf-8")) + 2
            # Leave room beneath the Hub's 2 MiB request limit, including escaped Unicode.
            if len(payload["reports"]) >= 500 or body_bytes + report_bytes > 1536 * 1024:
                break
            payload["reports"].append(report)
            body_bytes += report_bytes
        try:
            response = common.api_request(self.config["hub_url"].rstrip("/") + "/api/sync",
                self.config["token"], payload, timeout=min(10, self.config.get("sync_timeout", 10)))
            self.online = True
            self.last_error = None
            self.mode = response.get("mode", self.mode)
            self._set_meta("hub_mode", self.mode)
            sent = {report["id"]: report["seq"] for report in payload["reports"]}
            with self.db:
                for job_id, seq in response.get("ack", {}).items():
                    if job_id not in sent or isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                        continue
                    # Never acknowledge an update that this request did not carry.
                    acknowledged = min(seq, sent[job_id])
                    if acknowledged > confirmed.get(job_id, 0):
                        self.db.execute("INSERT INTO report_acks(job_id,seq) VALUES (?,?) "
                            "ON CONFLICT(job_id) DO UPDATE SET seq=MAX(seq,excluded.seq)", (job_id, acknowledged))
            existing = {record["id"]: record for record in self.records()}
            for job in response.get("jobs", []):
                if not re.fullmatch(r"[0-9a-f]{32}", str(job.get("id", ""))):
                    continue
                record = existing.get(job["id"])
                if record is None:
                    spec = common.validate_task(job["spec"])
                    record = {"id": job["id"], "spec": spec, "state": "assigned", "attempt": 1,
                              "seq": 0, "command_ack": 0, "prepared": False, "metrics": {}}
                    self._save(record, detail="Persisted on assigned node")
                    existing[record["id"]] = record
                elif record["spec"] != common.validate_task(job["spec"]):
                    raise ValueError("Hub attempted to change immutable task specification")
                if job.get("command_id", 0) > record.get("command_ack", 0):
                    self._command(record, job.get("action"), job["command_id"])
            # Commands are polled even with no reports. Lost responses leave reports unacknowledged.
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self.online = False
            self.last_error = str(error)[:1000]

    def _checkpoint(self, record):
        try:
            manifest = common.read_json(self._output(record) / "checkpoint.json")
            if not isinstance(manifest, dict):
                return False
            checkpoint = common.safe_child(self._output(record), manifest["path"])
            return checkpoint.is_file() and common.sha256_file(checkpoint) == manifest["sha256"]
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _command(self, record, action, command_id):
        if action in ("stop", "cancel"):
            if record["state"] in ACTIVE:
                common.atomic_json(self._output(record) / "STOP", {"action": action})
                self._save(record, stop_at=common.now(), stop_action=action,
                           command_ack=command_id, detail="Cooperative stop requested")
            elif record["state"] not in TERMINAL:
                self._save(record, state="canceled" if action == "cancel" else "paused",
                           command_ack=command_id, detail="Stopped before execution")
            elif action == "cancel" and record["state"] not in {"succeeded", "canceled"}:
                self._save(record, state="canceled", command_ack=command_id, detail="Task canceled; retained outputs remain archived")
            else:
                self._save(record, command_ack=command_id)
        elif action == "resume" and record["state"] in TERMINAL - {"succeeded", "canceled"}:
            if record["spec"]["backend"] == "demo" and _pid_alive(record.get("worker_pid")):
                # Do not acknowledge yet: safe to retry after the old process exits.
                self._save(record, detail="Waiting for previous demo process to exit before resume")
                return
            if record["spec"]["backend"] == "docker" and record.get("container_name"):
                try:
                    old = self._inspect(record)
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                    self._save(record, detail="Cannot verify previous container stopped: " + str(error)[:500])
                    return
                if old and (old["State"].get("Running") or old["State"].get("Restarting")):
                    self._save(record, detail="Previous container still running; resume is waiting, no second attempt started")
                    return
            valid_checkpoint = self._checkpoint(record)
            if record.get("ever_started") and not valid_checkpoint:
                self._save(record, command_ack=command_id,
                    detail="Resume refused: previous execution has no verified checkpoint; submit a new task to start afresh")
                return
            output = self._output(record)
            (output / "STOP").unlink(missing_ok=True)
            self._save(record, state="ready" if record.get("prepared") else "assigned",
                attempt=record["attempt"] + 1, command_ack=command_id, stop_at=None,
                stop_action=None, container_name=None, worker_pid=None, gpu_uuid=None,
                environment=None, archive_scanned=False, resume_from_checkpoint=valid_checkpoint,
                detail="Resume accepted; checkpoint will be validated by algorithm")
        else:
            self._save(record, command_ack=command_id, detail="Command has no effect in current state")

    def _prepare(self, record, snapshot):
        if self.preparation is not None:
            return
        spec = record["spec"]
        if spec["backend"] == "demo" and not self.config.get("allow_demo", False):
            raise ValueError("Demo execution is disabled on this node")
        if spec["backend"] == "docker":
            if sys.platform != "linux":
                raise ValueError("Docker tasks must run inside WSL2 or Ubuntu, not native Windows")
            if spec["source"]["repo"] not in self.config.get("allowed_repos", []):
                raise ValueError("Repository is absent from node allowed_repos")
            if not snapshot.get("docker_available"):
                return
            if not self.online:
                return
        self._save(record, state="preparing", detail="Preparing fixed source, image and assets")
        if spec["backend"] == "demo":
            environments = self._prepare_inputs(record, snapshot)
            self._save(record, state="ready", prepared=True, cached_environments=environments,
                       detail="Built-in demo ready")
            return
        preparation = {"id": record["id"], "done": threading.Event()}

        def prepare():
            try:
                preparation["environments"] = self._prepare_inputs(copy.deepcopy(record), snapshot)
            except Exception as error:
                preparation["error"] = str(error)[:1000]
            finally:
                preparation["done"].set()

        preparation["thread"] = threading.Thread(target=prepare, name="expman-prepare", daemon=True)
        self.preparation = preparation
        preparation["thread"].start()

    def _poll_preparation(self):
        preparation = self.preparation
        if not preparation or not preparation["done"].is_set():
            return
        self.preparation = None
        record = next(item for item in self.records() if item["id"] == preparation["id"])
        if "error" in preparation:
            if record["state"] == "preparing":
                self._save(record, state="failed", detail="Preparation failed: " + preparation["error"])
        else:
            self._save(record, state="ready" if record["state"] == "preparing" else record["state"],
                prepared=True, cached_environments=preparation["environments"], archive_scanned=False,
                detail="Inputs cached; task can start while Hub is offline")

    def _prepare_inputs(self, record, snapshot):
        spec = record["spec"]
        output = self._output(record)
        output.mkdir(parents=True, exist_ok=True)
        common.atomic_json(output / "params.json", spec["params"])
        common.atomic_json(output / "task.json", {"id": record["id"], "spec": spec})
        for asset in spec["assets"]:
            if asset not in snapshot["assets"]:
                raise ValueError(f"Required asset is missing: {asset}")
        environments = []
        if spec["backend"] == "docker":
            repo, commit = spec["source"]["repo"], spec["source"]["commit"]
            cache = self.root / "repos" / hashlib.sha256(repo.encode()).hexdigest()
            if not cache.exists():
                self._exec(["git", "clone", "--mirror", "--", repo, str(cache)], timeout=600)
            exists = self._exec(["git", "-C", str(cache), "cat-file", "-e", commit + "^{commit}"], check=False)
            if exists.returncode:
                self._exec(["git", "-C", str(cache), "fetch", "origin"], timeout=600)
            actual = self._exec(["git", "-C", str(cache), "rev-parse", "--verify", commit + "^{commit}"]).stdout.strip()
            if actual.lower() != commit:
                raise ValueError("Git commit verification failed")
            checkout = self.root / "worktrees" / record["id"]
            if not checkout.exists():
                self._exec(["git", "-C", str(cache), "worktree", "add", "--detach", str(checkout), commit], timeout=120)
            self._verify_checkout(checkout, commit)
            available = {name for gpu in snapshot["gpus"] for name in gpu["profiles"]}
            for environment in spec["environments"]:
                if environment["profile"] not in available or snapshot["profiles"].get(environment["profile"]) != environment["image"]:
                    continue
                cached = self._exec(["docker", "image", "inspect", environment["image"]], check=False)
                if cached.returncode:
                    self._exec(["docker", "pull", environment["image"]], timeout=1800)
                image_info = json.loads(self._exec(["docker", "image", "inspect", environment["image"]]).stdout)[0]
                environments.append({**environment, "image_id": image_info["Id"]})
            if not environments:
                raise ValueError("No explicitly verified environment matches an allowed GPU")
        return environments

    def _verify_checkout(self, checkout, commit):
        actual = self._exec(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip()
        dirty = self._exec(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"]).stdout.strip()
        if actual.lower() != commit or dirty:
            raise ValueError("Cached source was changed; refusing to run a different experiment")

    def _environment(self, record):
        output = self._output(record)
        return {"EXPERIMENT_OUTPUT": str(output), "EXPERIMENT_PARAMS": str(output / "params.json"),
                "EXPERIMENT_RESUME": "1" if record.get("resume_from_checkpoint") else "0",
                "EXPERIMENT_AGENT_PID": str(os.getpid()), "EXPERIMENT_ATTEMPT": str(record["attempt"])}

    def _start(self, record, choice):
        spec, output = record["spec"], self._output(record)
        if spec["backend"] == "docker" and sys.platform != "linux":
            raise ValueError("Docker tasks must run inside WSL2 or Ubuntu, not native Windows")
        # Intent is durable before a create/spawn side effect.
        self._save(record, state="starting", gpu_uuid=choice["gpu_uuid"], environment=choice["environment"],
                   container_name=f"expman-{record['id']}-{record['attempt']}", detail="Start intent persisted")
        execution = {"node_id": self.config["node_id"], "job_id": record["id"], "attempt": record["attempt"],
                     "gpu_uuid": choice["gpu_uuid"], "environment": choice["environment"],
                     "source": spec["source"], "prepared_at": common.now(),
                     "resume_from_checkpoint": bool(record.get("resume_from_checkpoint"))}
        if spec["backend"] == "demo":
            environment = dict(os.environ)
            environment.update(self._environment(record))
            package_root = str(Path(__file__).resolve().parent.parent)
            environment["PYTHONPATH"] = package_root + os.pathsep + environment.get("PYTHONPATH", "")
            common.atomic_json(output / f"execution-attempt-{record['attempt']}.json", execution)
            self._save(record, ever_started=True)
            with open(output / f"attempt-{record['attempt']}.log", "ab") as log:
                process = subprocess.Popen([sys.executable, "-m", "expman.demo_workload"],
                    cwd=package_root, env=environment, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, shell=False)
            self.processes[record["id"]] = process
            self._save(record, state="running", worker_pid=process.pid, detail="Built-in CPU demonstration running")
            return
        environment = choice["environment"]
        cached = next((item for item in record.get("cached_environments", [])
                       if item["image"] == environment["image"] and item["profile"] == environment["profile"]), None)
        if cached is None:
            raise ValueError("Chosen image was not prepared")
        current = json.loads(self._exec(["docker", "image", "inspect", environment["image"]]).stdout)[0]
        if current["Id"] != cached["image_id"]:
            raise ValueError("Cached image identity changed")
        execution["image_id"] = current["Id"]
        # Match the owner of params/checkpoints/assets on Linux bind mounts.
        # Root without DAC_OVERRIDE cannot access another user's private files.
        container_user = f"{os.getuid()}:{os.getgid()}"
        execution["container_user"] = container_user
        # WSL may expose every GPU despite Docker's device request. Select the
        # assigned physical UUID in CUDA too; logical device 0 is then this GPU.
        execution["cuda_visible_devices"] = choice["gpu_uuid"]
        common.atomic_json(output / f"execution-attempt-{record['attempt']}.json", execution)
        checkout = self.root / "worktrees" / record["id"]
        self._verify_checkout(checkout, spec["source"]["commit"])
        mounts = [(checkout, "/workspace/code", True), (output, "/workspace/run", False)]
        assets = {}
        for asset in spec["assets"]:
            source = Path(self.config["assets"][asset])
            if not source.is_absolute() or not source.exists():
                raise ValueError(f"Asset disappeared: {asset}")
            mounts.append((source, "/assets/" + asset, True))
            assets[asset] = "/assets/" + asset
        argv = ["docker", "create", "--name", record["container_name"], "--restart=no", "--pull=never",
                "--label", "expman.job=" + record["id"], "--label", "expman.node=" + self.config["node_id"],
                "--gpus", "device=" + choice["gpu_uuid"], "--cpus", str(spec["resources"]["cpu"]),
                "--memory", str(spec["resources"]["ram_mb"]) + "m", "--network", "none",
                "--user", container_user, "--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges",
                "--tmpfs", "/tmp:rw,nosuid,size=512m", "--workdir", "/workspace/code"]
        for source, destination, readonly in mounts:
            if "," in str(source):
                raise ValueError("Docker bind paths must not contain commas")
            value = f"type=bind,src={source},dst={destination}" + (",readonly" if readonly else "")
            argv += ["--mount", value]
        for key, value in {"EXPERIMENT_OUTPUT": "/workspace/run", "EXPERIMENT_PARAMS": "/workspace/run/params.json",
                           "EXPERIMENT_ASSETS": json.dumps(assets), "EXPERIMENT_RESUME": "1" if record.get("resume_from_checkpoint") else "0",
                           "EXPERIMENT_ATTEMPT": str(record["attempt"]), "PYTHONDONTWRITEBYTECODE": "1",
                           "CUDA_VISIBLE_DEVICES": choice["gpu_uuid"]}.items():
            argv += ["--env", key + "=" + value]
        argv += [environment["image"], *spec["command"]]
        self._exec(argv, timeout=60)
        self._save(record, ever_started=True)
        self._exec(["docker", "start", record["container_name"]], timeout=60)
        self._save(record, state="running", detail="Docker container running")

    def _inspect(self, record):
        inspected = self._exec(["docker", "inspect", record["container_name"]], check=False)
        if inspected.returncode:
            # Daemon unavailable is not evidence that the container vanished.
            self._exec(["docker", "info", "--format", "{{.ServerVersion}}"])
            if "No such" in inspected.stderr:
                return None
            raise RuntimeError(inspected.stderr[-500:])
        container = json.loads(inspected.stdout)[0]
        labels = container.get("Config", {}).get("Labels", {}) or {}
        if labels.get("expman.job") != record["id"] or labels.get("expman.node") != self.config["node_id"]:
            raise ValueError("Container ownership labels do not match; refusing to control it")
        return container

    def _finish(self, record, exit_code):
        action = record.get("stop_action")
        if action == "cancel":
            state = "canceled"
        elif action == "stop" or (self._output(record) / "STOP").exists():
            state = "paused" if self._checkpoint(record) else "interrupted"
        else:
            state = "succeeded" if exit_code == 0 else "failed"
        self._save(record, state=state, exit_code=exit_code, archive_scanned=False,
                   detail=f"Process exited with code {exit_code}" + ("; checkpoint verified" if state == "paused" else ""))

    def _reconcile(self, record):
        if record["state"] not in ACTIVE:
            return
        timeout = float(self.config.get("stop_grace_seconds", 60))
        overdue = record.get("stop_at") is not None and common.now() - record["stop_at"] >= timeout
        if record["spec"]["backend"] == "demo":
            process = self.processes.get(record["id"])
            if process is None:
                self._save(record, state="interrupted", detail="Demo process handle unavailable; not relaunched")
                return
            code = process.poll()
            if code is None and overdue:
                process.terminate()
                try:
                    code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    code = process.wait(timeout=5)
            if code is not None:
                self.processes.pop(record["id"], None)
                self._finish(record, code)
            return
        container = self._inspect(record)
        if container is None:
            self._save(record, state="interrupted", detail="Container missing; explicit resume required, no duplicate launch")
            return
        state = container["State"]
        if state.get("Status") == "created" and record["state"] == "starting":
            if record.get("stop_at"):
                self._finish(record, 143)
                return
            fresh = self.snapshot()
            fresh["gpus"] = [gpu for gpu in fresh["gpus"] if gpu["uuid"] == record.get("gpu_uuid")]
            active = [{"spec": item["spec"], "gpu_uuid": item.get("gpu_uuid")} for item in self.records()
                      if item["state"] in ACTIVE and item["id"] != record["id"]]
            admission_spec = copy.deepcopy(record["spec"])
            admission_spec["environments"] = [record["environment"]] if record.get("environment") else []
            choice = scheduler.select_device(admission_spec, fresh, active)
            if choice is None:
                return  # A recovered container is still subject to current admission policy.
            self._save(record, ever_started=True)
            self._exec(["docker", "start", record["container_name"]], timeout=60)
            self._save(record, state="running", detail="Recovered existing created container")
        elif state.get("Running"):
            if overdue:
                self._exec(["docker", "stop", "--time", "10", record["container_name"]], timeout=30)
            elif record["state"] != "running":
                self._save(record, state="running", detail="Reattached to existing container")
        elif state.get("Status") in ("exited", "dead"):
            # Docker retains logs; snapshot them only once the container is closed.
            with open(self._output(record) / f"attempt-{record['attempt']}.log", "wb") as log:
                subprocess.run(["docker", "logs", record["container_name"]], stdout=log, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, timeout=30, shell=False)
            self._finish(record, state.get("ExitCode", 1))

    def _metrics(self, record):
        try:
            value = common.read_json(self._output(record) / "latest_metrics.json", {})
            if isinstance(value, dict) and value != record.get("metrics"):
                json.dumps(value, allow_nan=False)
                self._save(record, metrics=value)
        except (ValueError, OSError):
            pass
        try:
            if record["spec"]["backend"] == "docker" and record["state"] in ACTIVE:
                if common.now() - self.log_checked.get(record["id"], 0) < 10:
                    return
                self.log_checked[record["id"]] = common.now()
                logs = self._exec(["docker", "logs", "--tail", "40", record["container_name"]], timeout=5)
                tail = (logs.stdout + logs.stderr)[-16000:]
            else:
                path = self._output(record) / f"attempt-{record['attempt']}.log"
                if not path.is_file():
                    return
                with open(path, "rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 16000))
                    tail = stream.read(16000).decode("utf-8", errors="replace")
            if tail != record.get("log_tail", ""):
                self._save(record, log_tail=tail)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            pass

    def _snapshot_files(self, record):
        if record.get("archive_scanned") or record["state"] not in TERMINAL:
            return
        if record["spec"]["backend"] == "demo" and _pid_alive(record.get("worker_pid")):
            return  # An interrupted orphan might still be flushing its checkpoint.
        output = self._output(record)
        cache = self.root / "upload_cache" / record["id"]
        cache.mkdir(parents=True, exist_ok=True)
        if output.exists():
            for parent, directories, files in os.walk(output, followlinks=False):
                directories[:] = [item for item in directories if not (Path(parent) / item).is_symlink()]
                for name in files:
                    if name == "STOP" or name.startswith(".write-") or name.endswith(".tmp"):
                        continue
                    source = Path(parent) / name
                    if source.is_symlink() or not source.is_file():
                        continue
                    relative = source.relative_to(output).as_posix()
                    common.safe_child(output, relative)
                    before = source.stat()
                    temporary = cache / "snapshot.tmp"
                    shutil.copyfile(source, temporary)
                    after = source.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        temporary.unlink(missing_ok=True)
                        return
                    sha = common.sha256_file(temporary)
                    destination = cache / sha
                    os.replace(temporary, destination)
                    with self.db:
                        self.db.execute("INSERT OR IGNORE INTO uploads(job_id,name,sha,size,path) VALUES(?,?,?,?,?)",
                                        (record["id"], relative, sha, before.st_size, str(destination)))
        self._save(record, archive_scanned=True)

    def _uploads(self, chunks=4):
        if not self.online:
            return
        for job, name, sha, size, path, offset in self.db.execute(
                "SELECT job_id,name,sha,size,path,offset FROM uploads WHERE complete=0 ORDER BY job_id,name").fetchall():
            if chunks <= 0:
                break
            base = {"job_id": job, "name": name, "sha256": sha, "size": size}
            try:
                url = self.config["hub_url"].rstrip("/") + "/api/upload"
                response = common.api_request(url, self.config["token"], {**base, "offset": 0, "data": ""})
                offset = response["offset"]
                if not isinstance(offset, int) or not 0 <= offset <= size:
                    raise ValueError("Hub returned invalid upload offset")
                while not response.get("complete") and chunks > 0:
                    with open(path, "rb") as stream:
                        stream.seek(offset)
                        block = stream.read(512 * 1024)
                    response = common.api_request(url, self.config["token"], {
                        **base, "offset": offset, "data": base64.b64encode(block).decode("ascii")})
                    next_offset = response["offset"]
                    if not isinstance(next_offset, int) or not offset <= next_offset <= size:
                        raise ValueError("Hub returned invalid upload offset")
                    if not response.get("complete") and next_offset <= offset:
                        raise ValueError("Upload made no progress")
                    offset = next_offset
                    chunks -= 1
                with self.db:
                    self.db.execute("UPDATE uploads SET offset=?,complete=? WHERE job_id=? AND name=? AND sha=?",
                                    (offset, int(bool(response.get("complete"))), job, name, sha))
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                self.last_error = "Archive sync deferred: " + str(error)[:500]
                return

    def tick(self):
        """One durable cycle; losing Hub connectivity never abandons local ownership."""
        self._poll_preparation()
        for record in self.records():
            try:
                self._reconcile(record)
                self._metrics(record)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                self.last_error = str(error)[:1000]
        snapshot = self.snapshot()
        self._sync(snapshot)
        if self.mode == "drain":
            snapshot["policy"]["run_enabled"] = False
        for record in self.records():
            if record["state"] in ("assigned", "preparing"):
                try:
                    self._prepare(record, snapshot)
                except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
                    self._save(record, state="failed", detail="Preparation failed: " + str(error)[:1000])
        for record in sorted(self.records(), key=lambda item: -item["spec"]["priority"]):
            if record["state"] != "ready":
                continue
            # Refresh telemetry immediately before each start, not just at heartbeat.
            fresh = self.snapshot()
            active = [{"spec": task["spec"], "gpu_uuid": task.get("gpu_uuid")}
                      for task in self.records() if task["state"] in ACTIVE]
            choice = scheduler.select_device(record["spec"], fresh, active)
            if choice is not None:
                try:
                    self._start(record, choice)
                except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
                    # If Docker create/start ACK was lost, keep persisted intent and inspect next tick.
                    if record["spec"]["backend"] == "docker" and record["state"] == "starting":
                        self._save(record, detail="Start outcome pending inspection: " + str(error)[:700])
                    else:
                        self._save(record, state="failed", detail="Start failed: " + str(error)[:1000])
        for record in self.records():
            try:
                self._snapshot_files(record)
            except (OSError, ValueError) as error:
                self.last_error = "Archive snapshot deferred: " + str(error)[:500]
        self._uploads()
        return {"online": self.online, "mode": self.mode, "jobs": self.records(), "error": self.last_error}


def run(config_path, once=False):
    agent = Agent(config_path)
    parsed = urllib.parse.urlsplit(agent.config["hub_url"])
    public_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    print(f"Node {agent.config['node_id']} connecting to {public_url}", flush=True)
    previous_status = None
    try:
        while True:
            state = agent.tick()
            status = (state["online"], state.get("error"))
            if status != previous_status:
                message = f"Node {agent.config['node_id']}: " + ("Hub online" if state["online"] else "Hub offline; cached ready tasks continue locally")
                if state.get("error"):
                    message += "; " + str(state["error"])
                print(message.replace(agent.config["token"], "[redacted]"), flush=True)
                previous_status = status
            if once:
                return
            time.sleep(max(0.1, float(agent.config.get("poll_seconds", 5))))
    except KeyboardInterrupt:
        # Docker containers intentionally survive node-agent shutdown. They are recovered by inspect.
        for record in agent.records():
            if record["spec"]["backend"] == "demo" and record["state"] in ACTIVE:
                common.atomic_json(agent._output(record) / "STOP", {"reason": "agent stopping"})
    finally:
        agent.close()
