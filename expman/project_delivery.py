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
import threading
from . import common


class ProjectDelivery:
    def __init__(self, agent):
        self.agent = agent
        self.items = agent._meta("project_deliveries", {})
        self.installing = None

    def reports(self):
        return [{"digest": digest, **{key: item[key] for key in ("revision", "status", "detail")}}
                for digest, item in list(self.items.items())[-100:]]

    def accept(self, deployments):
        if not isinstance(deployments, list) or len(deployments) > 100:
            raise ValueError("Invalid project delivery queue")
        for item in deployments:
            digest, size, revision = item.get("digest"), item.get("size"), item.get("revision")
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= 32 * 1024**3
                    or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
                raise ValueError("Invalid project delivery declaration")
            previous = self.items.get(digest)
            if previous is None or previous["revision"] < revision:
                self.items[digest] = {"size": size, "revision": revision, "status": "downloading", "detail": "Waiting to download"}
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
            # A new explicit retry can supersede an older installation report.
            if item["revision"] == operation["revision"]:
                try:
                    if "error" in operation:
                        raise ValueError(operation["error"])
                    if common.read_json(self.agent.config_path) != operation["before"]:
                        raise ValueError("Node configuration changed during installation; retry delivery")
                    updated = operation["result"]["config"]
                    for key in ("node_id", "token", "hub_url", "root", "policy", "gpu_policy"):
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
