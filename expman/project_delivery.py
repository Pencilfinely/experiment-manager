"""Incremental authenticated project downloads and isolated installation work.

Only the owning agent thread writes its configuration/database. Long filesystem
verification and Git snapshot construction run independently of task heartbeats.
"""
from __future__ import annotations

import base64
import binascii
import copy
import os
import re
import subprocess
import threading
from . import common


class ProjectDelivery:
    def __init__(self, agent):
        self.agent = agent
        self.items = agent._meta("project_deliveries", {})
        self.installing = None
        self.report_cursor = 0

    def reports(self):
        items = list(self.items.items())
        if len(items) > 100:
            start = self.report_cursor % len(items)
            items = (items[start:] + items[:start])[:100]
            self.report_cursor = (start + len(items)) % len(self.items)
        return [{"digest": digest, **{key: item[key] for key in ("revision", "status", "detail")}}
                for digest, item in items]

    def accept(self, deployments):
        if not isinstance(deployments, list) or len(deployments) > 100:
            raise ValueError("Invalid project delivery queue")
        for item in deployments:
            digest, size, revision = item.get("digest"), item.get("size"), item.get("revision")
            action = item.get("action", "install")
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= 32 * 1024**3
                    or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
                raise ValueError("Invalid project delivery declaration")
            if action not in ("install", "delete"):
                raise ValueError("Invalid project delivery action")
            if action == "delete" and (not isinstance(item.get("project_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", item["project_id"])
                    or not isinstance(item.get("bundle_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["bundle_id"])):
                raise ValueError("Invalid project deletion identity")
            previous = self.items.get(digest)
            if previous is None or previous["revision"] < revision:
                self.items[digest] = {"size": size, "revision": revision, "action": action,
                    "project_id": item.get("project_id"), "bundle_id": item.get("bundle_id"),
                    "status": "deleting" if action == "delete" else "downloading",
                    "detail": "Waiting to remove managed project copies" if action == "delete" else "Waiting to download"}
                # Explicit retries rebuild ownership/reference checks against the
                # latest configuration. Same-revision crash recovery keeps its plan.
            elif previous["size"] != size:
                raise ValueError("Controller changed immutable project size")
        self._save()

    def _save(self):
        self.agent._set_meta("project_deliveries", self.items)

    def tick(self):
        if self.installing:
            operation = self.installing
            if not operation["done"].is_set():
                return
            item = self.items[operation["digest"]]
            if operation.get("kind") in ("plan-delete", "delete"):
                if item["revision"] == operation["revision"]:
                    try:
                        if "error" in operation:
                            raise ValueError(operation["error"])
                        if operation["kind"] == "plan-delete":
                            if common.read_json(self.agent.config_path) != operation["before"]:
                                raise ValueError("Node configuration changed during cleanup planning; retry deletion")
                            # Persist removal intent before changing registrations.
                            # A crash at either write can safely replay the plan.
                            item["cleanup_plan"] = operation["result"]["plan"]
                            item["cleanup_config"] = operation["result"]["config"]
                            item["cleanup_before"] = operation["before"]
                            item["cleanup_receipt"] = operation["result"]["ownership_receipt"]
                            self._save()
                            self._apply_cleanup_config(item)
                            item.update(status="deleting", detail="Removing owned containers and managed source/data/cache copies")
                        else:
                            item.update(status="deleted", detail="项目部署已删除；原始文件、实验结果、共享基础镜像、镜像仓库与构建缓存保留")
                            item.pop("cleanup_plan", None)
                    except (OSError, ValueError, KeyError) as error:
                        item.update(status="delete_failed", detail=str(error)[:1000])
                self.installing = None
                self._save()
                return
            # A new explicit retry can supersede an older installation report.
            if item["revision"] == operation["revision"]:
                try:
                    if "error" in operation:
                        raise ValueError(operation["error"])
                    if common.read_json(self.agent.config_path) != operation["before"]:
                        raise ValueError("Node configuration changed during installation; retry delivery")
                    updated = operation["result"]["config"]
                    for key in ("node_id", "token", "hub_url", "root", "policy", "gpu_policy", "setup_network"):
                        if updated.get(key) != operation["before"].get(key):
                            raise ValueError("Project attempted to change node identity or runtime policy")
                    for key in ("profiles", "assets"):
                        previous = operation["before"].get(key, {})
                        if any(updated.get(key, {}).get(name) != value for name, value in previous.items()):
                            raise ValueError("Project attempted to replace an existing " + key + " entry")
                    if not set(operation["before"].get("allowed_repos", [])).issubset(updated.get("allowed_repos", [])):
                        raise ValueError("Project attempted to remove existing allowed repositories")
                    common.atomic_json(self.agent.config_path, updated)
                    try:
                        self.agent.config_path.chmod(0o600)
                    except OSError:
                        pass
                    self.agent.config = updated
                    item.update(status="installed", detail="Installed; experiment presets are available on this node")
                except (OSError, ValueError, KeyError) as error:
                    item.update(status="failed", detail=str(error)[:1000])
            self.installing = None
            self._save()
            return
        for digest, item in self.items.items():
            if item["status"] == "deleting":
                try:
                    self._delete(digest, item)
                except (OSError, ValueError, RuntimeError, KeyError) as error:
                    item.update(status="delete_failed", detail=str(error)[:1000])
                self._save()
                break
            if item["status"] not in ("downloading", "installing"):
                continue
            try:
                self._download(digest, item)
            except (OSError, TimeoutError) as error:
                # Interrupted network downloads retain their offset and retry.
                item.update(status="downloading", detail="Download deferred: " + str(error)[:800])
            except (ValueError, RuntimeError, KeyError, binascii.Error) as error:
                item.update(status="failed", detail=str(error)[:1000])
            self._save()
            break

    def _apply_cleanup_config(self, item):
        if "cleanup_config" not in item:
            return
        updated = item["cleanup_config"]
        actual = common.read_json(self.agent.config_path)
        if actual not in (item["cleanup_before"], updated):
            raise ValueError("Node configuration changed before cleanup; restore the expected mapping before retrying")
        if item.get("cleanup_receipt"):
            from .project_cleanup import owned_path
            receipt = item["cleanup_receipt"]
            path = owned_path(self.agent.root, receipt["path"])
            # Legacy templates can be the last copy of proven image ownership.
            # Save that evidence before unregistering them so rechecks, restarts
            # and explicit retries can still reclaim the same owned runtime.
            if path.parent.is_dir():
                common.atomic_json(path, receipt["value"])
        common.atomic_json(self.agent.config_path, updated)
        try:
            self.agent.config_path.chmod(0o600)
        except OSError:
            pass
        self.agent.config = updated
        item.pop("cleanup_config", None)
        item.pop("cleanup_before", None)
        item.pop("cleanup_receipt", None)
        self._save()

    def _delete(self, digest, item):
        from .project_cleanup import execute_cleanup, plan_cleanup
        self._apply_cleanup_config(item)
        planning = not item.get("cleanup_plan")
        operation = {"digest": digest, "revision": item["revision"], "done": threading.Event(),
                     "kind": "plan-delete" if planning else "delete",
                     "before": copy.deepcopy(common.read_json(self.agent.config_path) if planning else self.agent.config)}
        if not planning and common.read_json(self.agent.config_path) != self.agent.config:
            raise ValueError("Node configuration changed externally; restart this worker before retrying cleanup")
        records, declaration = copy.deepcopy(self.agent.records()), {"digest": digest, **copy.deepcopy(item)}
        def remove():
            try:
                if planning:
                    operation["result"] = plan_cleanup(self.agent.root, operation["before"], records, declaration)
                else:
                    # Tasks may have arrived while planning ran. Recheck all
                    # references before executing the persisted removal intent.
                    current = plan_cleanup(self.agent.root, operation["before"], records, declaration)["plan"]
                    approved = item["cleanup_plan"]
                    safe = {**approved, "paths": [path for path in approved["paths"] if path in current["paths"]],
                            "images": [name for name in approved["images"] if name in current["images"]]}
                    execute_cleanup(self.agent.root, safe, self.agent._exec)
            except Exception as error:
                operation["error"] = str(error)[:1000]
            finally:
                operation["done"].set()
        operation["thread"] = threading.Thread(target=remove, name="project-cleanup", daemon=True)
        self.installing = operation
        operation["thread"].start()

    def _download(self, digest, item):
        root = self.agent.root / "project-downloads"
        root.mkdir(exist_ok=True)
        partial, final = root / (digest + ".part"), root / (digest + ".zip")
        size = item["size"]
        if not final.exists():
            if not self.agent.online:
                return
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > size:
                partial.unlink()
                offset = 0
            # At most 2 MiB per tick: task reports/commands retain priority.
            for _ in range(4):
                if offset >= size:
                    break
                reply = common.api_request(self.agent.config["hub_url"].rstrip("/") +
                    f"/api/projects/download?digest={digest}&offset={offset}", self.agent.config["token"], timeout=5)
                block = base64.b64decode(reply["data"], validate=True)
                if (not block or len(block) > 512 * 1024 or offset + len(block) > size
                        or reply.get("offset") != offset + len(block) or reply.get("size") != size):
                    raise ValueError("Invalid project download chunk")
                with partial.open("ab") as stream:
                    stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                offset += len(block)
            item.update(status="downloading", detail=f"Downloaded {offset} / {size} bytes")
            if offset != size:
                return
            os.replace(partial, final)
        operation = {"digest": digest, "revision": item["revision"], "done": threading.Event(),
                     "before": copy.deepcopy(self.agent.config)}
        self.installing = operation
        item.update(status="installing", detail="Verifying archive and installing separate source/config/data snapshot")

        def install():
            try:
                from .harness_project import install_bundle
                if final.stat().st_size != size or common.sha256_file(final) != digest:
                    final.unlink()
                    raise ValueError("Project SHA256 verification failed; retry distribution")
                operation["result"] = install_bundle(final, self.agent.root / "distributed-projects", operation["before"])
            except Exception as error:
                operation["error"] = str(error)[:1000]
            finally:
                operation["done"].set()

        operation["thread"] = threading.Thread(target=install, name="project-install", daemon=True)
        operation["thread"].start()
