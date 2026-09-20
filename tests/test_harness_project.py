"""Real project snapshots through HTTP delivery; only Docker checks are mocked."""
import copy
import hashlib
import json
from pathlib import Path
import threading
import unittest
import zipfile
from unittest.mock import patch

from expman import common, scheduler
from expman.agent import Agent
from expman.harness_project import build_project, install_bundle, prepare_project, publish_bundle, read_bundle
from expman.hub import Hub, make_server
from tests.support import temporary_directory


ENTRY = '''import argparse
import os
parser = argparse.ArgumentParser()
parser.add_argument('--data_dir', default='../data/')
parser.add_argument('--output_dir', default='output/')
parser.add_argument('--data_name', default='Video_Games')
parser.add_argument('--gpu_id', type=str, default='0')
parser.add_argument('--lr', type=float, default=0.001)
parser.add_argument('--epochs', type=int, default=10)
args = parser.parse_args()
os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_id
data_file = args.data_dir + args.data_name + '.txt'
raise RuntimeError('Discovery must not execute the original entry')
'''


class HarnessProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.source = self.root / "research" / "SASRec_Original"
        self.source.mkdir(parents=True)
        (self.source / "main.py").write_text(ENTRY, encoding="utf-8")
        (self.source / "model.py").write_text("# model source\n", encoding="utf-8")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "keep").write_text("existing research history")
        (self.source / "output").mkdir()
        (self.source / "output" / "private.log").write_text("existing research output")
        data = self.source.parent / "data"
        data.mkdir()
        (data / "Video_Games.txt").write_text("1 1 2 3\n")
        self.external = self.root / "separate-config"
        self.storage = self.root / "worker-projects"
        self.image = "example/runtime@sha256:" + "a" * 64
        self.config = {"node_id": "gpu-a", "token": "private-token", "hub_url": "http://127.0.0.1:8765",
            "root": str(self.root / "node"), "tags": ["shared-lab"],
            "policy": {"max_running": 2}, "gpu_policy": {"GPU-local": {"max_jobs": 1}},
            "profiles": {"verified": {"verified": True, "image": self.image, "gpu_name_patterns": ["Example"]}},
            "assets": {"old-data": str(self.root / "old-data")}, "allowed_repos": [str(self.root / "old-repo")],
            "task_templates": [common.validate_task({"name": "Original task", "backend": "demo"})]}
        self.hub = self.server = self.agent = None
        self.environment_patch = patch("expman.harness_environment.ensure_environment", side_effect=self.environment)
        self.environment_patch.start()

    def tearDown(self):
        if self.agent:
            self.agent.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(3)
        if self.hub:
            self.hub.close()
        self.environment_patch.stop()
        # Only isolated test fixture Git object files need Windows readonly reset.
        for item in self.root.rglob("*"):
            if item.is_file() and not item.is_symlink():
                item.chmod(0o600)
        self.temporary.__exit__(None, None, None)

    def environment(self, runtime, config, storage):
        return {"config": copy.deepcopy(config), "environments": [{"profile": "verified", "image": self.image}]}

    def source_hashes(self):
        return {path.relative_to(self.source).as_posix(): common.sha256_file(path)
                for path in self.source.rglob("*") if path.is_file()}

    def draft(self):
        prepare_project(self.source, self.external, project_id="sasrec-original")
        project = common.read_json(self.external / "project.json")
        project["reviewed"] = True
        common.atomic_json(self.external / "project.json", project)
        formal = common.read_json(self.external / "experiments/default.json")
        formal.update(id="formal", name="Formal")
        formal["params"]["epochs"] = 20
        common.atomic_json(self.external / "experiments/formal.json", formal)
        return project

    def bundle(self, filename="project.zip"):
        output = self.root / filename
        build_project(self.external, output)
        return output

    def test_static_draft_separates_fixed_paths_from_experiment_params_without_execution(self):
        before = self.source_hashes()
        report = prepare_project(self.source, self.external, project_id="sasrec-original")
        self.assertEqual(report["entry"], "main.py")
        harness = common.read_json(self.external / "harness.json")
        project = common.read_json(self.external / "project.json")
        experiment = common.read_json(self.external / "experiments/default.json")
        self.assertFalse(project['resources']['exclusive'])
        self.assertEqual(harness["command"], ["{python}", "main.py"])
        self.assertEqual(harness["fixed_params"]["gpu_id"], "{env.CUDA_VISIBLE_DEVICES}")
        self.assertEqual(harness["fixed_params"]["data_dir"], "{assets.dataset}/")
        self.assertTrue(harness["fixed_params"]["output_dir"].startswith("{output}/"))
        self.assertEqual(experiment["params"]["lr"], 0.001)
        self.assertEqual(experiment["params"]["data_name"], "Video_Games")
        self.assertNotIn("data_dir", experiment["params"])
        self.assertFalse(project["reviewed"])
        with self.assertRaisesRegex(ValueError, "Review"):
            self.bundle()
        self.assertEqual(self.source_hashes(), before)
        self.assertFalse((self.root / "project.zip").exists())
        with self.assertRaisesRegex(ValueError, "already exists"):
            prepare_project(self.source, self.external)

    def test_bundle_roundtrip_excludes_original_outputs_and_rejects_tamper_and_traversal(self):
        self.draft()
        before = self.source_hashes()
        bundle = self.bundle()
        manifest = read_bundle(bundle)
        self.assertEqual([item["id"] for item in manifest["experiments"]], ["default", "formal"])
        with zipfile.ZipFile(bundle) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        self.assertIn("source/main.py", members)
        self.assertIn("assets/dataset/Video_Games.txt", members)
        self.assertNotIn("source/.git/keep", members)
        self.assertNotIn("source/output/private.log", members)
        self.assertNotIn("private-token", b"".join(members.values()).decode("utf-8"))
        tampered = self.root / "tampered.zip"
        with zipfile.ZipFile(tampered, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content.replace(b"model source", b"model change") if name == "source/model.py" else content)
        with self.assertRaisesRegex(ValueError, "checksum"):
            read_bundle(tampered)
        # Recompute self-consistent metadata so traversal is rejected by path checks.
        traversal = copy.deepcopy(manifest)
        data = members.pop("source/model.py")
        traversal["files"]["source/../../escape.py"] = traversal["files"].pop("source/model.py")
        traversal.pop("bundle_id")
        encoded = lambda value: (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        traversal["bundle_id"] = hashlib.sha256(encoded(traversal)).hexdigest()
        members["project-manifest.json"] = encoded(traversal)
        members["source/../../escape.py"] = data
        unsafe = self.root / "unsafe.zip"
        with zipfile.ZipFile(unsafe, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        with self.assertRaisesRegex(ValueError, "Unsafe project path"):
            read_bundle(unsafe)
        self.assertEqual(self.source_hashes(), before)
        self.assertFalse((self.root / "escape.py").exists())

    def test_install_is_idempotent_and_later_code_version_preserves_previous_registrations(self):
        self.draft()
        before = self.source_hashes()
        bundle = self.bundle()
        original_config = copy.deepcopy(self.config)
        first = install_bundle(bundle, self.storage, self.config)
        self.assertEqual(self.config, original_config)
        for field in ("token", "hub_url", "root", "policy", "gpu_policy", "profiles"):
            self.assertEqual(first["config"][field], self.config[field])
        self.assertEqual(first["config"]["assets"]["old-data"], self.config["assets"]["old-data"])
        self.assertIn(self.config["allowed_repos"][0], first["config"]["allowed_repos"])
        template = first["templates"][0]
        self.assertEqual(common.validate_task(template), template)
        self.assertFalse(template["resume_supported"])
        self.assertEqual(template["project_id"], "sasrec-original")
        self.assertTrue((Path(template["source"]["repo"]) / ".git").is_dir())
        self.assertEqual(self.source_hashes(), before)
        repeated = install_bundle(bundle, self.storage, first["config"])
        self.assertEqual(repeated, first)
        (self.source / "model.py").write_text("# new model version\n", encoding="utf-8")
        second_bundle = self.bundle("new-project.zip")
        second = install_bundle(second_bundle, self.storage, first["config"])
        self.assertNotEqual(first["project"]["bundle_id"], second["project"]["bundle_id"])
        self.assertNotEqual(template["source"]["repo"], second["templates"][0]["source"]["repo"])
        for key, value in first["config"]["assets"].items():
            self.assertEqual(second["config"]["assets"][key], value)
        self.assertTrue(set(first["config"]["allowed_repos"]).issubset(second["config"]["allowed_repos"]))
        self.assertTrue(all(old in second["config"]["task_templates"] for old in first["config"]["task_templates"]))
        self.assertEqual((Path(template["source"]["repo"]) / "project/model.py").read_text(), "# model source\n")

    def test_real_bundle_upload_and_background_install_publishes_valid_node_presets(self):
        self.draft()
        bundle = self.bundle()
        self.hub = Hub(self.root / "center")
        token = self.hub.add_node(self.config["node_id"])
        self.server = make_server(self.hub, port=0)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        url = "http://127.0.0.1:" + str(self.server.server_address[1])
        config = {**self.config, "hub_url": url, "token": token}
        config_path = self.root / "worker.json"
        common.atomic_json(config_path, config)
        self.agent = Agent(config_path)
        snapshot = {"capabilities": ["project-bundle-v1"]}
        self.agent._sync(snapshot)
        publication = publish_bundle(bundle, url, self.hub.config["admin_token"])
        common.api_request(url + "/api/projects/deploy", self.hub.config["admin_token"],
            {"digest": publication["digest"], "node_ids": [config["node_id"]]})
        self.agent._sync(snapshot)
        self.agent.project_delivery.tick()
        self.agent.project_delivery.installing["thread"].join(20)
        self.agent.project_delivery.tick()
        self.assertEqual(self.agent.project_delivery.reports()[0]["status"], "installed",
                         self.agent.project_delivery.reports())
        self.agent._sync({**snapshot, "task_templates": self.agent.config["task_templates"]})
        state = self.hub.state()
        self.assertEqual(state["projects"][0]["deployments"][0]["status"], "installed")
        templates = state["nodes"][0]["snapshot"]["task_templates"]
        preset = next(item for item in templates if item.get("project_id") == "sasrec-original")
        self.assertEqual(preset["project_bundle_id"], publication["bundle_id"])
        self.assertEqual(common.validate_task(preset), preset)
        self.assertEqual(self.agent.config["token"], token)
        self.assertEqual(self.agent.config["policy"], config["policy"])
        self.assertEqual(state["jobs"], [])  # Installing a project does not launch training.

    def test_selected_node_is_enforced_even_with_identical_paths_and_gpu_profiles(self):
        self.draft()
        bundle = self.bundle()
        first = install_bundle(bundle, self.storage, self.config)
        # Identical layouts/profile names on two machines must not defeat the UI
        # worker selection: only their node identities differ in this fixture.
        other_config = {**self.config, "node_id": "gpu-b"}
        second = install_bundle(bundle, self.storage, other_config)
        def snapshot(config):
            return {"tags": config["tags"], "assets": list(config["assets"]),
                "allowed_repos": config["allowed_repos"], "profiles": {"verified": self.image},
                "gpus": [{"total_mb": 12000, "reserve_mb": 1024, "max_jobs": 1, "profiles": ["verified"]}]}
        self.assertTrue(scheduler.eligible(first["templates"][0], snapshot(first["config"])))
        self.assertFalse(scheduler.eligible(first["templates"][0], snapshot(second["config"])))

    def test_data_revision_keeps_old_data_and_changed_installed_source_is_rejected(self):
        self.draft()
        first = install_bundle(self.bundle(), self.storage, self.config)
        old_asset_id = first["templates"][0]["assets"][0]
        old_data = Path(first["config"]["assets"][old_asset_id]) / "Video_Games.txt"
        (self.source.parent / "data/Video_Games.txt").write_text("1 4 5 6\n")
        second_bundle = self.bundle("data-revision.zip")
        second = install_bundle(second_bundle, self.storage, first["config"])
        new_asset_id = second["templates"][0]["assets"][0]
        self.assertNotEqual(new_asset_id, old_asset_id)
        self.assertEqual(old_data.read_text(), "1 1 2 3\n")
        self.assertEqual((Path(second["config"]["assets"][new_asset_id]) / "Video_Games.txt").read_text(), "1 4 5 6\n")
        private_source = Path(second["templates"][0]["source"]["repo"]) / "project/model.py"
        private_source.write_text("# locally changed installed version\n")
        with self.assertRaisesRegex(ValueError, "immutable file changed"):
            install_bundle(second_bundle, self.storage, second["config"])
        self.assertEqual(private_source.read_text(), "# locally changed installed version\n")


if __name__ == "__main__":
    unittest.main()
