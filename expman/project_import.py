"""Local, asynchronous project import for the controller's desktop interface.

Only the loopback-only authenticated HTTP routes expose this module. Drafts and
snapshots stay under the controller root; discovery never executes source code.
"""
from __future__ import annotations

import base64
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import threading
import uuid

from . import common
from .harness import _parameters, validate_manifest
from .harness_environment import validate_runtime
from .harness_project import MAX_BYTES, MAX_FILES, _files, _slug, build_project, prepare_project


BUSY = frozenset(("scanning", "saving", "building", "browsing"))

_METRIC_TEST = r'''
import json, math, re, sys
try:
    data = json.load(sys.stdin)
    matches, warnings = [], []
    for index, rule in enumerate(data["metrics"]):
        pattern = re.compile(rule["pattern"])
        step_group = rule.get("step", "step")
        values = rule["values"]
        if not isinstance(values, dict) or not values or step_group not in pattern.groupindex or any(g not in pattern.groupindex for g in values.values()):
            raise ValueError("step 和指标需要引用已定义的命名分组。")
        found = False
        for line in data["sample"].splitlines():
            match = pattern.search(line)
            if not match:
                continue
            found = True
            step = int(match.group(step_group))
            numbers = {key:float(match.group(group)) for key, group in values.items()}
            if not math.isfinite(step) or any(not math.isfinite(v) for v in numbers.values()):
                raise ValueError("指标与 step 必须是有限数值。")
            matches.append({"rule":index,"step":step,"values":numbers})
            if len(matches) >= 200:
                break
        if not found:
            warnings.append("规则 %d 没有匹配到示例日志。" % (index+1))
        if len(matches) >= 200:
            warnings.append("仅显示前 200 条匹配。")
            break
    print(json.dumps({"matches":matches,"warnings":warnings},ensure_ascii=True))
except Exception as error:
    print(json.dumps({"error":"指标规则无效："+str(error)[:500]},ensure_ascii=True))
'''


def is_loopback(peer):
    """Use the actual socket peer, never an untrusted forwarded-for header."""
    try:
        address = ipaddress.ip_address(peer.split("%", 1)[0])
        return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Import ID must be a UUID hex string")
    return value


def _strings(value, name):
    if not isinstance(value, list) or not value or len(value) > 1000 or any(
            not isinstance(item, str) or not item or len(item) > 1000 for item in value):
        raise ValueError(name + " must contain 1-1000 nonempty strings")
    return value


def _preview(project):
    result = {"source_files": 0, "source_bytes": 0, "asset_files": 0,
              "asset_bytes": 0, "files": [], "truncated": False}
    selections = [("source", Path(project["source"]), project["include"], project["exclude_directories"])]
    selections.extend(("asset:" + alias, Path(asset["path"]), asset["include"],
                       [".git", "__pycache__", ".venv"]) for alias, asset in project["assets"].items())
    for kind, root, include, excluded in selections:
        for relative, file in _files(root.resolve(), include, excluded):
            prefix = "source" if kind == "source" else "asset"
            size = file.stat().st_size
            result[prefix + "_files"] += 1
            result[prefix + "_bytes"] += size
            if len(result["files"]) < 200:
                result["files"].append({"path": relative, "bytes": size, "kind": kind})
            else:
                result["truncated"] = True
            if result["source_files"] + result["asset_files"] > MAX_FILES:
                raise ValueError("Selection exceeds 20,000 files; narrow the include patterns")
            if result["source_bytes"] + result["asset_bytes"] > MAX_BYTES:
                raise ValueError("Selection exceeds 32 GiB; narrow the dataset selection")
    if not result["source_files"]:
        raise ValueError("Source selection is empty; check the include patterns")
    return result


def _draft(directory):
    return {"project": common.read_json(directory / "project.json"),
            "harness": common.read_json(directory / "harness.json"),
            "experiments": [common.read_json(path) for path in sorted((directory / "experiments").glob("*.json"))],
            "discovery": common.read_json(directory / "discovery.json", {})}


def _validate_draft(payload, source):
    project = copy.deepcopy(payload.get("project"))
    if not isinstance(project, dict) or project.get("schema_version") != 1:
        raise ValueError("project must be a schema_version 1 object")
    if Path(project.get("source", "")).expanduser().resolve() != Path(source):
        raise ValueError("The source root belongs to this import; start another import to change it")
    project["source"] = str(Path(source))
    _slug(project.get("project_id"))
    if not isinstance(project.get("name"), str) or not 1 <= len(project["name"]) <= 100:
        raise ValueError("Project display name needs 1-100 characters")
    _strings(project.get("include"), "Source include")
    exclusions = project.get("exclude_directories")
    if not isinstance(exclusions, list) or len(exclusions) > 1000 or any(not isinstance(item, str) or not item for item in exclusions):
        raise ValueError("exclude_directories must be a string list")
    assets = project.get("assets", {})
    if not isinstance(assets, dict) or len(assets) > 100:
        raise ValueError("assets must contain at most 100 dataset selections")
    for alias, asset in assets.items():
        _slug(alias)
        if not isinstance(asset, dict) or not isinstance(asset.get("path"), str) or not Path(asset["path"]).is_absolute():
            raise ValueError("Dataset paths must be absolute local folders")
        asset["path"] = str(Path(asset["path"]).expanduser().resolve())
        asset["include"] = _strings(asset.get("include", ["**/*"]), "Dataset include")
    project["assets"] = assets
    project["runtime"] = validate_runtime(project.get("runtime", {}))
    resources = project.get("resources")
    if not isinstance(resources, dict):
        raise ValueError("resources must be an object")
    for key in ("gpu_memory_mb", "cpu", "ram_mb"):
        value = resources.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 10**9:
            raise ValueError("Resource budget must be positive: " + key)
    if not isinstance(resources.get("exclusive", False), bool):
        raise ValueError("resources.exclusive must be boolean")
    harness = validate_manifest(payload.get("harness"))
    experiments = copy.deepcopy(payload.get("experiments"))
    if not isinstance(experiments, list) or not 1 <= len(experiments) <= 100:
        raise ValueError("Provide 1-100 experiment configurations")
    names = set()
    placeholders = {"python": "python", "workspace": "/workspace", "output": "/output",
                    "assets": {alias: "/assets/" + alias for alias in assets},
                    "env": {"CUDA_VISIBLE_DEVICES": "GPU-assigned"}}
    for experiment in experiments:
        if not isinstance(experiment, dict):
            raise ValueError("Each experiment must be an object")
        name = _slug(experiment.get("id"))
        if name in names:
            raise ValueError("Experiment IDs must be unique")
        names.add(name)
        if not isinstance(experiment.get("params"), dict):
            raise ValueError("Experiment params must be an object")
        _parameters(harness, experiment["params"], placeholders)
    project["reviewed"] = False
    return {"project": project, "harness": harness, "experiments": experiments}


class ProjectImports:
    def __init__(self, hub):
        self.hub = hub
        self.root = hub.root / "imports"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.threads = set()
        self.closing = False
        for state_file in self.root.glob("*/state.json"):
            state = common.read_json(state_file)
            if isinstance(state, dict) and state.get("status") in BUSY:
                state.update(status="interrupted", phase="Controller restarted; open the draft and retry", updated=common.now())
                common.atomic_json(state_file, state)

    def close(self):
        with self.lock:
            self.closing = True
            threads = list(self.threads)
        # Publication uses Hub's SQLite connection: finish before Hub closes it.
        for thread in threads:
            thread.join()

    def _load(self, identity):
        identity = _identity(identity)
        state = common.read_json(self.root / identity / "state.json")
        if not state:
            raise ValueError("Unknown local import")
        return state

    def _set(self, identity, **fields):
        with self.lock:
            state = self._load(identity)
            state.update(fields, updated=common.now())
            common.atomic_json(self.root / identity / "state.json", state)
            return copy.deepcopy(state)

    def _public(self, state, full=False):
        value = {key: copy.deepcopy(item) for key, item in state.items() if key != "draft_directory"}
        if full and state.get("draft_directory"):
            directory = self.root / state["id"] / state["draft_directory"]
            value["draft"] = _draft(directory)
            value["draft"]["preview"] = common.read_json(directory / "preview.json", {})
        return value

    def listing(self):
        with self.lock:
            states = [common.read_json(path) for path in self.root.glob("*/state.json")]
            with self.hub.lock:
                deleted = {row[0] for row in self.hub.db.execute("SELECT digest FROM projects WHERE deleted_at IS NOT NULL")}
            return {"available": True, "picker_available": os.name == "nt", "imports":
                    [self._public(state) for state in sorted((s for s in states if isinstance(s, dict)
                        and (s.get("project") or {}).get("digest") not in deleted),
                                                             key=lambda s: s.get("updated", 0), reverse=True)[:100]]}

    def test_metrics(self, payload):
        """Test actual Python expressions without letting a bad regex hang the Hub."""
        import subprocess
        import sys
        metrics, sample = payload.get("metrics"), payload.get("sample_log")
        if not isinstance(metrics, list) or not 1 <= len(metrics) <= 30:
            raise ValueError("请提供 1–30 条指标规则。")
        if not isinstance(sample, str) or not sample.strip() or len(sample) > 16000:
            raise ValueError("请提供不超过 16,000 字符的示例日志。")
        if len(json.dumps(metrics)) > 32000:
            raise ValueError("指标规则过大。")
        try:
            result = subprocess.run([sys.executable, "-I", "-c", _METRIC_TEST],
                input=json.dumps({"metrics": metrics, "sample": sample}), text=True, encoding="utf-8",
                capture_output=True, timeout=2, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise ValueError("规则匹配超过 2 秒，请简化表达式；配置未修改。") from None
        if result.returncode:
            raise ValueError("规则解析失败，请检查表达式、step 和数值分组。")
        reply = json.loads(result.stdout)
        if "error" in reply:
            raise ValueError(reply["error"])
        return reply

    def item(self, identity):
        with self.lock:
            return self._public(self._load(identity), True)

    def _launch(self, identity, operation):
        with self.lock:
            if self.closing:
                raise ValueError("Controller is shutting down")
            if len(self.threads) >= 2:
                raise ValueError("Two imports are already active; wait for one to finish")
            def work():
                try:
                    operation()
                except Exception as error:
                    # Local administrator only; bounded detail, never a traceback.
                    self._set(identity, status="failed", phase="Import needs attention",
                              error=(str(error) or type(error).__name__)[:2000])
                finally:
                    with self.lock:
                        self.threads.discard(threading.current_thread())
            thread = threading.Thread(target=work, name="project-import-" + identity[:8], daemon=True)
            self.threads.add(thread)
            thread.start()

    def _new(self, status, **fields):
        identity = uuid.uuid4().hex
        directory = self.root / identity
        directory.mkdir()
        state = {"id": identity, "status": status, "phase": status, "created": common.now(),
                 "updated": common.now(), **fields}
        common.atomic_json(directory / "state.json", state)
        return state

    def start(self, payload):
        source = payload.get("source")
        if not isinstance(source, str) or not source.strip() or not Path(source).expanduser().is_absolute():
            raise ValueError("Choose an absolute local algorithm root folder")
        source = Path(source).expanduser().resolve()
        if not source.is_dir():
            raise ValueError("Algorithm root folder does not exist")
        if source.is_relative_to(self.hub.root) or self.hub.root.is_relative_to(source):
            raise ValueError("Choose the algorithm folder separately from the controller data folder")
        entry = payload.get("entry")
        if entry is not None and (not isinstance(entry, str) or Path(entry).is_absolute() or ".." in Path(entry).parts):
            raise ValueError("Entry must be a file path relative to the algorithm root")
        project_id = payload.get("project_id")
        if project_id is not None:
            _slug(project_id)
        with self.lock:
            if len(self.threads) >= 2:
                raise ValueError("Two imports are already active; wait for one to finish")
            state = self._new("scanning", source=str(source), name=source.name)
            identity = state["id"]
            def scan():
                draft = self.root / identity / "draft-0"
                self._set(identity, phase="Discovering entry and parameters without running source")
                prepare_project(source, draft, entry=entry, project_id=project_id)
                self._set(identity, phase="Counting selected code and dataset files")
                try:
                    preview = _preview(common.read_json(draft / "project.json"))
                except Exception:
                    # A failed selection remains editable, with no pending file writes.
                    self._set(identity, draft_directory="draft-0")
                    raise
                common.atomic_json(draft / "preview.json", preview)
                # Readers only see a complete generation, never files being replaced.
                self._set(identity, status="ready", draft_directory="draft-0",
                          phase="Review parameters, dependencies and selected files")
            self._launch(identity, scan)
            return self._public(state)

    def browse(self, payload):
        if os.name != "nt":
            raise ValueError("Native folder selection is available in the Windows controller; enter a local folder path")
        with self.lock:
            if len(self.threads) >= 2:
                raise ValueError("Two imports are already active; wait for one to finish")
            state = self._new("browsing", name="Choose algorithm folder")
            def choose():
                from .desktop import choose_directory
                source = choose_directory()
                self._set(state["id"], status="selected" if source else "canceled",
                          phase="Folder selected" if source else "Folder selection canceled", source=str(source or ""))
            self._launch(state["id"], choose)
            return self._public(state)

    def save(self, payload):
        identity = _identity(payload.get("id"))
        with self.lock:
            state = self._load(identity)
            if state["status"] in BUSY or not state.get("draft_directory"):
                raise ValueError("Wait for discovery or publication to finish before editing")
            if len(self.threads) >= 2:
                raise ValueError("Two imports are already active; wait for one to finish")
            draft = _validate_draft(payload, state["source"])
            # Make edits a distinct generation. Failure leaves the prior draft intact.
            generation = "draft-" + uuid.uuid4().hex
            directory = self.root / identity / generation
            directory.mkdir()
            common.atomic_json(directory / "project.json", draft["project"])
            common.atomic_json(directory / "harness.json", draft["harness"])
            for experiment in draft["experiments"]:
                common.atomic_json(directory / "experiments" / (experiment["id"] + ".json"), experiment)
            old = self.root / identity / state["draft_directory"]
            common.atomic_json(directory / "discovery.json", common.read_json(old / "discovery.json", {}))
            self._set(identity, status="saving", phase="Checking selected files", error=None)
            def save_files():
                # File scans do not hold either the import-state or Hub lock.
                common.atomic_json(directory / "preview.json", _preview(draft["project"]))
                self._set(identity, status="ready", phase="Configuration saved; review and publish",
                          draft_directory=generation, name=draft["project"]["name"], project=None)
            self._launch(identity, save_files)
            return self.item(identity)

    def publish(self, payload):
        identity = _identity(payload.get("id"))
        if payload.get("reviewed") is not True:
            raise ValueError("Review the configuration before publishing (reviewed: true)")
        with self.lock:
            state = self._load(identity)
            if state["status"] == "published":
                return self._public(state, True)
            if state["status"] in BUSY or not state.get("draft_directory"):
                raise ValueError("Finish discovery and save the configuration before publishing")
            if len(self.threads) >= 2:
                raise ValueError("Two imports are already active; wait for one to finish")
            directory = self.root / identity / state["draft_directory"]
            snapshot = _validate_draft(_draft(directory), state["source"])
            def build():
                # Keep the review generation immutable while the page polls it.
                # Windows readers may reject opening a file during replacement,
                # so the reviewed build state belongs in an unpublished directory.
                build_directory = self.root / identity / ("build-config-" + uuid.uuid4().hex)
                snapshot["project"]["reviewed"] = True
                common.atomic_json(build_directory / "project.json", snapshot["project"])
                common.atomic_json(build_directory / "harness.json", snapshot["harness"])
                for experiment in snapshot["experiments"]:
                    common.atomic_json(build_directory / "experiments" / (experiment["id"] + ".json"), experiment)
                bundle = self.root / identity / ("bundle-" + uuid.uuid4().hex + ".zip")
                result = build_project(build_directory, bundle)
                self._set(identity, phase="Adding the verified snapshot to the algorithm library",
                          bytes=result["bytes"], files=result["files"])
                upload_id = uuid.uuid4().hex
                offset = 0
                from .hub import MAX_CHUNK
                with bundle.open("rb") as stream:
                    while offset < result["bytes"]:
                        stream.seek(offset)
                        block = stream.read(MAX_CHUNK)
                        uploaded = self.hub.project_upload({"upload_id": upload_id, "sha256": result["sha256"],
                            "size": result["bytes"], "offset": offset, "data": base64.b64encode(block).decode("ascii")})
                        offset = uploaded["offset"]
                        self._set(identity, uploaded_bytes=offset)
                self._set(identity, status="published", phase="Available in the algorithm library; select workers to deploy",
                          project=uploaded["project"], error=None)
                # Deletion can race the brief interval between publishing the
                # library row and saving this import's final state.
                self.hub._cleanup_project_archives([uploaded["project"]["digest"]])
            self._set(identity, status="building", phase="Packaging an immutable snapshot; original source remains unchanged", error=None)
            self._launch(identity, build)
            return self.item(identity)
