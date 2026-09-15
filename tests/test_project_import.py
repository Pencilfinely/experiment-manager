"""The desktop folder workflow uses the same real project library as ZIP uploads."""
import copy
import json
from pathlib import Path
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest.mock import patch

from expman import common
from expman.hub import Hub, make_server
from expman.project_import import BUSY, is_loopback
from tests.support import temporary_directory


ENTRY = '''import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--lr', type=float, default=0.001)
parser.add_argument('--epochs', type=int, default=300)
parser.add_argument('--data_dir', default='data/')
parser.add_argument('--output_dir', default='outputs/')
args = parser.parse_args()
raise RuntimeError('Importing an algorithm must never execute this source')
'''


class ProjectImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.source = self.root / "Original Algorithm"
        self.source.mkdir()
        (self.source / "main.py").write_text(ENTRY, encoding="utf-8")
        (self.source / "data").mkdir()
        (self.source / "data" / "sequences.txt").write_text("1 1 2 3\n")
        self.hub = Hub(self.root / "controller")
        self.imports = self.hub.project_imports()
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(3)
        self.hub.close()
        self.temporary.__exit__(None, None, None)

    def wait(self, identity):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = self.imports.item(identity)
            if value["status"] not in BUSY:
                return value
            time.sleep(0.01)
        self.fail("Import did not reach a visible outcome")

    def scan(self):
        state = self.imports.start({"source": str(self.source)})
        state = self.wait(state["id"])
        self.assertEqual(state["status"], "ready", state.get("error"))
        return state

    def hashes(self):
        return {str(path.relative_to(self.source)): common.sha256_file(path)
                for path in self.source.rglob("*") if path.is_file()}

    def test_folder_review_publish_preserves_source_and_uses_real_library(self):
        before = self.hashes()
        state = self.scan()
        draft = state["draft"]
        self.assertEqual(draft["discovery"]["selected_entry"], "main.py")
        self.assertEqual(draft["harness"]["parameters"]["lr"]["default"], 0.001)
        self.assertFalse(draft["harness"]["resume"]["supported"])
        self.assertEqual(draft["preview"]["source_files"], 1)
        self.assertEqual(draft["preview"]["asset_files"], 1)
        self.assertFalse(draft["project"]["reviewed"])
        draft["experiments"][0]["params"]["epochs"] = 3
        second = copy.deepcopy(draft["experiments"][0])
        second.update(id="formal", name="Formal")
        second["params"]["epochs"] = 300
        draft["experiments"].append(second)
        self.imports.save({"id": state["id"], **draft})
        saved = self.wait(state["id"])
        self.assertEqual(saved["status"], "ready", saved.get("error"))
        self.assertEqual(saved["draft"]["experiments"][0]["params"]["epochs"], 3)
        with self.assertRaisesRegex(ValueError, "Review"):
            self.imports.publish({"id": state["id"]})
        self.imports.publish({"id": state["id"], "reviewed": True})
        published = self.wait(state["id"])
        self.assertEqual(published["status"], "published", published.get("error"))
        project = published["project"]
        self.assertEqual(self.hub.state()["projects"][0]["digest"], project["digest"])
        with zipfile.ZipFile(self.hub.root / "projects" / (project["digest"] + ".zip")) as archive:
            manifest = archive.read("project-manifest.json").decode()
            self.assertNotIn(str(self.source), manifest)
            self.assertNotIn(self.hub.config["admin_token"], manifest)
            self.assertEqual(len(json.loads(manifest)["experiments"]), 2)
        self.assertEqual(self.hashes(), before)
        self.assertEqual(self.imports.publish({"id": state["id"], "reviewed": True})["project"]["digest"], project["digest"])

    def test_failed_edits_keep_previous_draft_and_cannot_switch_source(self):
        state = self.scan()
        draft = copy.deepcopy(state["draft"])
        draft["project"]["source"] = str(self.root)
        with self.assertRaisesRegex(ValueError, "source root"):
            self.imports.save({"id": state["id"], **draft})
        draft = copy.deepcopy(state["draft"])
        draft["experiments"][0]["params"]["epochs"] = "wrong type"
        with self.assertRaises(ValueError):
            self.imports.save({"id": state["id"], **draft})
        draft = copy.deepcopy(state["draft"])
        draft["project"]["include"] = ["absent.py"]
        self.imports.save({"id": state["id"], **draft})
        failed = self.wait(state["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Source selection is empty", failed["error"])
        self.assertEqual(failed["draft"]["project"]["include"], state["draft"]["project"]["include"])

    def test_scanning_does_not_hold_hub_lock_or_block_other_requests(self):
        entered, release = threading.Event(), threading.Event()
        from expman.project_import import prepare_project
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test did not release discovery")
            return prepare_project(*args, **kwargs)
        with patch("expman.project_import.prepare_project", side_effect=delayed):
            state = self.imports.start({"source": str(self.source)})
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.hub.lock.acquire(timeout=1))
                self.hub.lock.release()
                self.assertEqual(self.imports.item(state["id"])["status"], "scanning")
                self.assertEqual(self.hub.state()["jobs"], [])
            finally:
                release.set()
            self.assertEqual(self.wait(state["id"])["status"], "ready")

    def test_publishing_builds_a_private_generation_while_visible_draft_is_immutable(self):
        state = self.scan()
        visible = self.imports.root / state["id"] / self.imports._load(state["id"])["draft_directory"]
        before = {str(path.relative_to(visible)): path.read_bytes() for path in visible.rglob("*") if path.is_file()}
        entered, release = threading.Event(), threading.Event()
        build_directories = []
        from expman.project_import import build_project
        def delayed(directory, bundle):
            build_directories.append(Path(directory))
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test did not release publication")
            return build_project(directory, bundle)
        with patch("expman.project_import.build_project", side_effect=delayed):
            self.imports.publish({"id": state["id"], "reviewed": True})
            try:
                self.assertTrue(entered.wait(2))
                self.assertNotEqual(build_directories[0], visible)
                self.assertTrue(common.read_json(build_directories[0] / "project.json")["reviewed"])
                for _ in range(5):
                    polled = self.imports.item(state["id"])
                    self.assertEqual(polled["status"], "building")
                    self.assertFalse(polled["draft"]["project"]["reviewed"])
                    self.assertEqual(polled["draft"], state["draft"])
            finally:
                release.set()
            published = self.wait(state["id"])
        self.assertEqual(published["status"], "published", published.get("error"))
        after = {str(path.relative_to(visible)): path.read_bytes() for path in visible.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_initial_scan_only_exposes_complete_draft_after_preview(self):
        entered, release = threading.Event(), threading.Event()
        from expman.project_import import _preview
        def delayed(project):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test did not release preview")
            return _preview(project)
        with patch("expman.project_import._preview", side_effect=delayed):
            state = self.imports.start({"source": str(self.source)})
            try:
                self.assertTrue(entered.wait(2))
                polled = self.imports.item(state["id"])
                self.assertEqual(polled["status"], "scanning")
                self.assertNotIn("draft", polled)
            finally:
                release.set()
            ready = self.wait(state["id"])
        self.assertEqual(ready["status"], "ready", ready.get("error"))
        self.assertEqual(ready["draft"]["preview"]["source_files"], 1)

    def test_failed_initial_preview_exposes_a_stable_draft_for_correction(self):
        with patch("expman.project_import._preview", side_effect=ValueError("Selection needs correction")):
            state = self.imports.start({"source": str(self.source)})
            failed = self.wait(state["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("draft", failed)
        self.imports.save({"id": state["id"], **failed["draft"]})
        self.assertEqual(self.wait(state["id"])["status"], "ready")

    def test_restart_marks_inflight_import_interrupted_but_preserves_draft(self):
        state = self.scan()
        self.imports._set(state["id"], status="building")
        self.hub.close()
        self.hub = Hub(self.root / "controller")
        self.imports = self.hub.project_imports()
        recovered = self.imports.item(state["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertIn("project", recovered["draft"])

    def test_loopback_check_rejects_remote_and_forwarded_addresses(self):
        for peer in ("127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(is_loopback(peer))
        for peer in ("100.105.176.121", "192.168.1.1", "0.0.0.0", "localhost", "127.0.0.1, 10.0.0.1"):
            self.assertFalse(is_loopback(peer))

    def request(self, token=None, payload=None, forwarded=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if forwarded:
            headers["X-Forwarded-For"] = forwarded
        url = "http://127.0.0.1:" + str(self.server.server_address[1]) + "/api/local/imports"
        if payload is not None:
            url += "/start"
        request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_http_requires_admin_and_actual_loopback_peer(self):
        node_token = self.hub.add_node("node-a")
        self.server = make_server(self.hub, port=0)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.request(node_token)[0], 403)
        self.assertEqual(self.request(self.hub.config["admin_token"])[0], 200)
        with patch("expman.project_import.is_loopback", return_value=False):
            status, result = self.request(self.hub.config["admin_token"],
                                          {"source": str(self.source)}, forwarded="127.0.0.1")
        self.assertEqual(status, 403)
        self.assertIn("controller computer", result["error"])
        self.assertEqual(self.imports.listing()["imports"], [])


if __name__ == "__main__":
    unittest.main()
