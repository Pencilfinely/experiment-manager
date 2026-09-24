import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import threading
import types
import unittest
import urllib.error
import zipfile
from unittest.mock import patch

from expman import common
from expman.agent import Agent
from expman.hub import APIError, Hub, make_server
from tests.support import temporary_directory


class ProjectDeliveryTests(unittest.TestCase):
    """Transport/auth/recovery tests isolate the independently tested bundle format."""

    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.hub = Hub(self.root / "hub")
        self.worker_token = self.hub.add_node("worker")
        self.other_token = self.hub.add_node("other")
        self.snapshot = {"capabilities": ["project-bundle-v1"]}
        self.hub.sync("worker", {"node_id": "worker", "snapshot": self.snapshot})
        self.body = b"Transport fixture content; real ZIP parsing is tested separately."
        self.digest = hashlib.sha256(self.body).hexdigest()
        self.manifest = {"schema": 1, "project_id": "algorithm", "name": "Example algorithm", "bundle_id": "abc"}
        self.server = make_server(self.hub, port=0)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.agent = None
        self.module = types.ModuleType("expman.harness_project")
        self.module.read_bundle = lambda path: self.manifest
        self.module.install_bundle = self.install
        self.module_patch = patch.dict(sys.modules, {"expman.harness_project": self.module})
        self.module_patch.start()

    def tearDown(self):
        if self.agent:
            self.agent.close()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(3)
        self.hub.close()
        self.module_patch.stop()
        self.temporary.__exit__(None, None, None)

    def install(self, path, root, config):
        self.assertEqual(path.read_bytes(), self.body)
        updated = copy.deepcopy(config)
        updated["allowed_repos"] = [str(root / "source")]
        updated["task_templates"] = [common.validate_task({"backend": "demo", "name": "Example preset", "project_id": "algorithm"})]
        return {"config": updated, "templates": updated["task_templates"], "project": self.manifest}

    def chunk(self, offset, block, upload_id="a" * 32, **extra):
        return common.api_request(self.url + "/api/projects/upload", self.hub.config["admin_token"],
            {"upload_id": upload_id, "offset": offset, "size": len(self.body), "data": base64.b64encode(block).decode(), **extra})

    def upload(self):
        return self.chunk(0, self.body)["project"]

    def deploy(self):
        return common.api_request(self.url + "/api/projects/deploy", self.hub.config["admin_token"], {"digest": self.digest, "node_ids": ["worker"]})

    def create_agent(self):
        config_path = self.root / "node.json"
        common.atomic_json(config_path, {"node_id": "worker", "hub_url": self.url, "token": self.worker_token,
            "root": str(self.root / "node"), "allow_demo": True, "policy": {"run_enabled": True}})
        self.agent = Agent(config_path)
        return self.agent

    def test_resumable_upload_persistent_queue_auth_and_stale_retry_report(self):
        self.assertEqual(self.chunk(0, self.body[:7])["offset"], 7)
        self.assertEqual(self.chunk(0, b"")["offset"], 7)
        self.assertEqual(self.chunk(0, self.body[:7])["offset"], 7)
        result = self.chunk(7, self.body[7:])
        self.assertTrue(result["complete"])
        self.assertEqual(result["project"]["digest"], self.digest)
        self.assertEqual(self.chunk(0, b"")["project"], result["project"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            common.api_request(self.url + "/api/projects/deploy", self.worker_token, {"digest": self.digest, "node_ids": ["worker"]})
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(APIError) as error:
            self.hub.project_deploy({"digest": self.digest, "node_ids": ["worker", "other"]})
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.hub.state()["projects"][0]["deployments"], [])
        self.deploy()
        for token in (self.other_token, self.hub.config["admin_token"]):
            with self.assertRaises(urllib.error.HTTPError) as error:
                common.api_request(self.url + f"/api/projects/download?digest={self.digest}&offset=0", token)
            self.assertEqual(error.exception.code, 403)
        data = common.api_request(self.url + f"/api/projects/download?digest={self.digest}&offset=0", self.worker_token)
        self.assertEqual(base64.b64decode(data["data"]), self.body)
        self.deploy()  # Explicit retry advances generation, old report cannot undo it.
        response = self.hub.sync("worker", {"node_id": "worker", "snapshot": self.snapshot,
            "project_reports": [{"digest": self.digest, "revision": 1, "status": "failed", "detail": "old attempt"}]})
        self.assertEqual(response["project_deployments"][0]["revision"], 2)
        self.hub.close()
        self.hub = Hub(self.root / "hub")
        deployment = self.hub.state()["projects"][0]["deployments"][0]
        self.assertEqual((deployment["status"], deployment["revision"]), ("queued", 2))

    def test_bad_digest_never_publishes_project(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.chunk(0, self.body, sha256="0" * 64)
        self.assertEqual(error.exception.code, 422)
        self.assertEqual(self.hub.state()["projects"], [])

    def test_archive_validation_does_not_block_heartbeats_or_unrelated_uploads(self):
        entered, release = threading.Event(), threading.Event()
        result = {}
        def read(path):
            if path.name.startswith("a" * 32):
                entered.set()
                if not release.wait(5):
                    raise ValueError("Test validation timeout")
            return self.manifest
        self.module.read_bundle = read
        def upload():
            try:
                result["value"] = self.upload()
            except Exception as error:
                result["error"] = error
        thread = threading.Thread(target=upload)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            state = common.api_request(self.url + "/api/state", self.hub.config["admin_token"], timeout=1)
            self.assertEqual(state["projects"], [])
            heartbeat = common.api_request(self.url + "/api/sync", self.worker_token,
                {"node_id": "worker", "snapshot": self.snapshot}, timeout=1)
            self.assertIn("ack", heartbeat)
            self.assertTrue(self.chunk(0, self.body, upload_id="b" * 32)["complete"])
        finally:
            release.set()
            thread.join(5)
        self.assertNotIn("error", result)
        self.assertEqual(result["value"]["digest"], self.digest)
        self.assertEqual(self.hub.project_upload_locks, {})

    def test_bad_zip_is_reported_as_invalid_input(self):
        def invalid(path):
            raise zipfile.BadZipFile("Not a ZIP file")
        self.module.read_bundle = invalid
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.chunk(0, self.body)
        self.assertEqual(error.exception.code, 422)
        self.assertEqual(self.hub.state()["projects"], [])

    def test_http_delivery_installs_without_restart_and_preserves_jobs_identity(self):
        self.upload()
        self.deploy()
        agent = self.create_agent()
        original = copy.deepcopy(agent.config)
        record = {"id": "f" * 32, "spec": common.validate_task({"backend": "demo"}), "state": "succeeded", "seq": 1}
        agent._save(record)
        # Keep the local historical job out of Hub reports in this transport-only test.
        with agent.db:
            agent.db.execute("INSERT INTO report_acks VALUES (?,?)", (record["id"], record["seq"]))
        agent._sync(self.snapshot)
        agent.project_delivery.tick()
        agent.project_delivery.installing["thread"].join(3)
        agent.project_delivery.tick()
        self.assertEqual(agent.project_delivery.reports()[0]["status"], "installed")
        self.assertEqual(agent.config["token"], original["token"])
        self.assertEqual(agent.config["policy"], original["policy"])
        self.assertEqual(agent.records(), [record])
        self.assertEqual(common.read_json(agent.config_path), agent.config)
        agent._sync({**self.snapshot, "task_templates": agent.config["task_templates"]})
        self.assertEqual(self.hub.state()["projects"][0]["deployments"][0]["status"], "installed")
        self.assertEqual(self.hub.state()["nodes"][1]["snapshot"]["task_templates"][0]["name"], "Example preset")
        agent.close()
        self.agent = Agent(agent.config_path)
        self.assertEqual(self.agent.project_delivery.reports()[0]["status"], "installed")

    def test_external_config_change_is_not_overwritten(self):
        self.upload()
        self.deploy()
        agent = self.create_agent()
        agent._sync(self.snapshot)
        agent.project_delivery.tick()
        agent.project_delivery.installing["thread"].join(3)
        edited = {**agent.config, "tags": ["manual-change"]}
        common.atomic_json(agent.config_path, edited)
        agent.project_delivery.tick()
        self.assertEqual(agent.project_delivery.reports()[0]["status"], "failed")
        self.assertEqual(common.read_json(agent.config_path), edited)

    def test_project_cannot_enable_host_setup_network(self):
        self.upload()
        self.deploy()
        agent = self.create_agent()
        original = copy.deepcopy(agent.config)

        def install(path, root, config):
            result = self.install(path, root, config)
            result['config']['setup_network'] = 'host'
            return result

        self.module.install_bundle = install
        agent._sync(self.snapshot)
        agent.project_delivery.tick()
        agent.project_delivery.installing['thread'].join(3)
        agent.project_delivery.tick()
        self.assertEqual(agent.project_delivery.reports()[0]['status'], 'failed')
        self.assertEqual(common.read_json(agent.config_path), original)

    def test_declared_no_resume_rejects_resume(self):
        job_id = self.hub.submit({"request_id": "no-resume", "spec": {"backend": "demo", "resume_supported": False}})["ids"][0]
        with self.assertRaises(APIError) as error:
            self.hub.action({"job_id": job_id, "action": "resume"})
        self.assertEqual(error.exception.status, 409)
        self.assertIn("native resume", str(error.exception))


if __name__ == "__main__":
    unittest.main()
