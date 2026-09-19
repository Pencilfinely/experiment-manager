import copy
import json
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest.mock import patch

from expman.common import now, validate_task
from expman.hub import APIError, Hub, make_server
from expman.matrix import expand_matrix
from expman.scheduler import choose_assignment, choose_node, eligible, select_device
from tests.support import temporary_directory
from tests.test_core import gpu_task, snapshot


SPEC = {"name": "base", "backend": "demo", "params": {"steps": 2, "delay": 0}}
SNAPSHOT = {"allow_demo": True, "policy": {"max_prefetch": 32}, "tags": [], "assets": []}


class AllocationTests(unittest.TestCase):
    def nodes(self):
        return [{"id": name, "last_seen": now(), "snapshot": copy.deepcopy(SNAPSHOT)} for name in ("a", "b", "c")]

    def test_manual_candidate_and_preference_constraints(self):
        nodes = self.nodes()
        task = validate_task({**SPEC, "scheduling": {"mode": "manual", "node_ids": ["b"]}})
        self.assertEqual(choose_node(task, nodes, {}), "b")
        nodes[1]["last_seen"] = 0
        self.assertIsNone(choose_node(task, nodes, {}))
        nodes = self.nodes()
        task = validate_task({**SPEC, "scheduling": {"mode": "assisted", "node_ids": ["b", "c"], "preferred_node_ids": ["c"]}})
        self.assertEqual(choose_node(task, nodes, {"c": 20}), "c")
        self.assertEqual(choose_node(task, nodes, {"c": 32}), "b")
        self.assertIsNone(choose_node(task, nodes, {"c": 32, "b": 32}))

    def test_gpu_constraint_is_enforced_on_hub_and_worker(self):
        task, snap = gpu_task(), snapshot()
        snap["gpus"].append({**snap["gpus"][0], "uuid": "GPU-2"})
        task["scheduling"] = {"mode": "manual", "node_ids": ["a"], "gpu_uuids": ["GPU-2"]}
        task = validate_task(task)
        self.assertFalse(eligible(task, snap))  # Old workers cannot enforce it.
        snap["capabilities"] = ["scheduler-v2"]
        self.assertTrue(eligible(task, snap))
        self.assertEqual(select_device(task, snap, [])["gpu_uuid"], "GPU-2")
        snap["gpus"][1]["free_mb"] = 0
        self.assertIsNone(select_device(task, snap, []))  # Never silently use GPU-1.
        snap["gpus"] = snap["gpus"][:1]
        self.assertFalse(eligible(task, snap))

    def test_invalid_allocation_never_silently_becomes_auto(self):
        for scheduling in (None, {"mode": "typo"}, {"mode": "manual"},
                           {"mode": "manual", "node_ids": ["a", "b"]},
                           {"node_ids": "a"}, {"node_ids": [True]},
                           {"node_ids": ["a"], "preferred_node_ids": ["b"]},
                           {"gpu_ids": ["GPU-1"]}):
            with self.subTest(scheduling=scheduling), self.assertRaises(ValueError):
                validate_task({**SPEC, "scheduling": scheduling})
        with self.assertRaises(ValueError):
            validate_task({**SPEC, "scheduling": {"gpu_uuids": ["GPU-1"]}})

    def test_project_binding_uses_each_nodes_deployment_and_preserves_inputs(self):
        nodes, templates = [], []
        for index, name in enumerate(("a", "b")):
            template, snap = gpu_task(), snapshot()
            template.update(project_bundle_id="bundle", experiment_id="preset", project_id="project")
            template["source"] = {"repo": "/installed/" + name, "commit": name * 40}
            template["environments"] = [{"profile": "runtime-" + name, "image": "runtime@sha256:" + name * 64}]
            template["tags"] = ["hnode-" + name, "deployment-" + name]
            template["assets"] = ["dataset-v1"]
            template["asset_aliases"] = {"dataset": "dataset-v1"}
            snap.update(task_templates=[template], allowed_repos=[template["source"]["repo"]],
                        profiles={template["environments"][0]["profile"]: template["environments"][0]["image"]},
                        tags=template["tags"] + ["user-required"], assets=["dataset-v1"])
            snap["gpus"][0]["profiles"] = [template["environments"][0]["profile"]]
            nodes.append({"id": name, "last_seen": now(), "snapshot": snap})
            templates.append(template)
        task = copy.deepcopy(templates[0])
        task.update(params={"learning_rate": 0.001, "dataset": "movies"}, priority=19,
                    scheduling={"mode": "manual", "node_ids": ["b"]}, assets=["dataset"])
        task["resources"]["ram_mb"] = 2048
        task["tags"].append("user-required")
        nodes[0]["mode"] = "drain"
        before = copy.deepcopy(task)
        choice = choose_assignment(task, nodes, {})
        self.assertEqual(choice["node_id"], "b")
        bound = choice["spec"]
        self.assertEqual(bound["source"], templates[1]["source"])
        self.assertEqual(bound["environments"], templates[1]["environments"])
        self.assertEqual(bound["tags"], templates[1]["tags"] + ["user-required"])
        self.assertEqual(bound["assets"], ["dataset-v1"])
        for field in ("params", "resources", "priority", "command", "scheduling"):
            self.assertEqual(bound[field], before[field])
        self.assertEqual(task, before)
        self.assertIsNotNone(select_device(bound, nodes[1]["snapshot"], []))
        with temporary_directory() as directory:
            hub = Hub(directory)
            try:
                for node in nodes:
                    self.assertNotIn("deployment_tags", node["snapshot"]["task_templates"][0])
                    hub.add_node(node["id"])
                    hub.sync(node["id"], {"node_id": node["id"], "snapshot": node["snapshot"]})
                hub.set_mode({"node_id": "a", "mode": "drain"})
                automatic = {**task, "scheduling": {"mode": "auto"}}
                matrix = hub.matrix_save({"name": "Cross-node", "spec": automatic})
                self.assertEqual(hub.matrix_preview({"id": matrix["id"]})["allocations"][0]["node_id"], "b")
                identity = hub.matrix_start({"id": matrix["id"], "request_id": "cross-node"})["ids"][0]
                persisted = hub.job(identity)
                self.assertEqual(persisted["node_id"], "b")
                self.assertEqual(persisted["spec"]["source"], templates[1]["source"])
                self.assertEqual(persisted["spec"]["tags"], templates[1]["tags"] + ["user-required"])
                self.assertEqual(persisted["spec"]["params"], task["params"])
                self.assertEqual(hub.matrix_item(matrix["id"])["spec"]["source"], templates[0]["source"])
            finally:
                hub.close()
        nodes[1]["snapshot"]["task_templates"] = []
        self.assertIsNone(choose_assignment(task, nodes, {}))


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.hub = Hub(self.root)
        for name in ("a", "b"):
            self.hub.add_node(name)

    def tearDown(self):
        self.hub.close()
        self.temporary.__exit__(None, None, None)

    def definition(self):
        return {"name": "SASRec comparison", "description": "五数据集组合", "spec": copy.deepcopy(SPEC),
                "datasets": [{"name": "dataset-a", "params": {"dataset": "a"}},
                             {"name": "dataset-b", "params": {"dataset": "b"}}],
                "grid": {"seed": [1, 2], "model.hidden": [16, 32]}}

    def save(self):
        return self.hub.matrix_save(self.definition())

    def start(self, matrix, request_id="launch"):
        return self.hub.matrix_start({"id": matrix["id"], "revision": matrix["revision"], "request_id": request_id})

    def test_cartesian_preview_and_saved_definition_are_independent(self):
        payload = self.definition()
        definition, specs = expand_matrix(payload)
        self.assertEqual(len(specs), 8)
        self.assertEqual({(item["params"]["dataset"], item["params"]["seed"], item["params"]["model"]["hidden"]) for item in specs},
                         {(dataset, seed, hidden) for dataset in ("a", "b") for seed in (1, 2) for hidden in (16, 32)})
        specs[0]["params"]["model"]["hidden"] = 99
        self.assertNotEqual(specs[1]["params"]["model"]["hidden"], 99)
        self.assertEqual(payload, self.definition())
        self.assertNotIn("model", definition["spec"]["params"])
        matrix = self.save()
        preview = self.hub.matrix_preview({"id": matrix["id"]})
        self.assertEqual(preview["count"], 8)
        self.assertTrue(preview["warnings"])
        self.assertEqual(self.hub.state()["jobs"], [])

    def test_start_is_atomic_idempotent_and_persistent(self):
        matrix = self.save()
        replies = []
        threads = [threading.Thread(target=lambda: replies.append(self.start(matrix))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(len(self.hub.state()["jobs"]), 8)
        for job in self.hub.state()["jobs"]:
            self.assertEqual(job["spec"]["matrix_id"], matrix["id"])
            self.assertEqual(job["spec"]["matrix_run_id"], replies[0]["run_id"])
        self.hub.close()
        self.hub = Hub(self.root)
        self.assertEqual(self.start(matrix), replies[0])
        self.assertEqual(self.hub.matrix_item(matrix["id"])["job_count"], 8)

    def test_validation_and_assignment_failure_leave_no_partial_runs(self):
        matrix = self.save()
        definition = self.definition()
        definition["grid"] = {"seed": [1, -1]}
        with self.assertRaises(ValueError):
            self.hub.matrix_save(definition)
        original = self.hub._assign
        self.hub._assign = lambda: (_ for _ in ()).throw(RuntimeError("simulated transaction failure"))
        try:
            with self.assertRaises(RuntimeError):
                self.start(matrix)
        finally:
            self.hub._assign = original
        self.assertEqual(self.hub.state()["jobs"], [])
        self.assertEqual(self.hub.matrix_item(matrix["id"])["runs"], [])
        self.assertEqual(len(self.start(matrix)["ids"]), 8)

    def test_matrix_size_and_dataset_validation(self):
        for change in ({"datasets": [{"name": "same"}, {"name": "same"}]},
                       {"grid": {"seed": list(range(129))}},
                       {"datasets": [{"name": "bad", "assets": ["../bad"]}]},
                       {"datasets": [{"name": "bad", "params": []}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.hub.matrix_save({**self.definition(), **change})
        self.assertEqual(self.hub.matrices()["matrices"], [])

    def test_edit_does_not_rewrite_existing_runs_or_allow_stale_launch(self):
        matrix = self.save()
        launched = self.start(matrix)
        updated = self.hub.matrix_save({**self.definition(), "id": matrix["id"], "revision": 1, "grid": {"seed": [9]}})
        self.assertEqual(updated["revision"], 2)
        with self.assertRaises(APIError) as error:
            self.start(matrix, "other")
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.start(matrix), launched)  # Exact old retry remains valid.
        with self.assertRaises(APIError):
            self.start(updated)  # Same request_id cannot mean revision 2.
        self.assertEqual(len(self.start(updated, "new-launch")["ids"]), 2)
        self.assertNotEqual(self.hub.job(launched["ids"][0])["spec"]["params"]["seed"], 9)
        with self.assertRaises(APIError):
            self.hub.matrix_save({**self.definition(), "id": matrix["id"], "revision": 1})

    def test_delete_blocks_active_work_and_preserves_results(self):
        matrix = self.save()
        launched = self.start(matrix)
        with self.assertRaises(APIError) as error:
            self.hub.matrix_delete({"id": matrix["id"]})
        self.assertEqual(error.exception.status, 409)
        for identity in launched["ids"]:
            self.hub.action({"job_id": identity, "action": "cancel"})
        self.assertTrue(self.hub.matrix_delete({"id": matrix["id"]})["deleted"])
        self.assertTrue(self.hub.matrix_delete({"id": matrix["id"]})["deleted"])
        self.assertEqual(self.hub.matrices()["matrices"], [])
        with self.assertRaises(APIError):
            self.hub.matrix_item(matrix["id"])
        self.assertIn(launched["ids"][0], self.hub.matrix_report(matrix["id"]).decode())
        self.assertEqual(len(self.hub.state()["jobs"]), 8)

    def test_manual_allocation_and_offline_ownership(self):
        for node in ("a", "b"):
            self.hub.sync(node, {"node_id": node, "snapshot": SNAPSHOT})
        matrix = self.hub.matrix_save({**self.definition(), "scheduling": {"mode": "manual", "node_ids": ["b"]}})
        launched = self.start(matrix)
        self.assertEqual({job["node_id"] for job in self.hub.matrix_item(matrix["id"])["jobs"]}, {"b"})
        self.hub.db.execute("UPDATE nodes SET last_seen=0 WHERE id='b'")
        self.hub.sync("a", {"node_id": "a", "snapshot": SNAPSHOT})
        self.assertTrue(all(self.hub.job(identity)["node_id"] == "b" for identity in launched["ids"]))
        with self.assertRaises(APIError):
            self.hub.matrix_save({**self.definition(), "scheduling": {"mode": "manual", "node_ids": ["typo"]}})

    def test_published_project_checks_all_parameter_rows_before_start(self):
        from expman.harness_project import _encoded, _digest, MANIFEST, bundle_asset_aliases
        manifest = {"project_id": "project", "experiments": [{"id": "preset"}], "assets": {"dataset": {}}, "files": {},
                    "harness": {"schema_version": 1, "command": ["python", "train.py"],
                        "parameters": {"dataset": {"type": "string"}, "seed": {"type": "integer"}},
                        "fixed_params": {"data_dir": "{assets.dataset}/"},
                        "bindings": [{"param": "dataset", "flag": "--dataset"}]}}
        manifest["bundle_id"] = _digest(_encoded(manifest))
        digest = "f" * 64
        directory = self.root / "projects"
        directory.mkdir()
        with zipfile.ZipFile(directory / (digest + ".zip"), "w") as archive:
            archive.writestr(MANIFEST, _encoded(manifest))
        self.hub.db.execute("INSERT INTO projects(digest,project_id,name,size,created,bundle_id) VALUES (?,?,?,?,?,?)",
                            (digest, "project", "Project", 1, now(), manifest["bundle_id"]))
        task = gpu_task()
        task.update(project_bundle_id=manifest["bundle_id"], experiment_id="preset",
                    assets=list(bundle_asset_aliases(manifest).values()), params={"dataset": "a", "seed": 1})
        payload = {"name": "Project matrix", "spec": task,
                   "datasets": [{"name": "a", "params": {"dataset": "a"}}, {"name": "b", "params": {"dataset": "b"}}],
                   "grid": {"seed": [1, 2]}}
        matrix = self.hub.matrix_save(payload)
        for params in ({"unknown": 3}, {"data_dir": "/other/path"}, {"seed": "not-an-integer"}):
            with self.subTest(params=params), self.assertRaises(APIError):
                self.hub.matrix_preview({**payload, "grid": {}, "datasets": [{"name": "invalid", "params": params}]})
        with self.assertRaisesRegex(APIError, "placeholder"):
            self.hub.matrix_save({**payload, "datasets": [{"name": "missing-assets", "assets": [], "params": {}}]})
        valid_alias = self.hub.matrix_preview({**payload, "datasets": [{"name": "alias", "assets": ["dataset"], "params": {}}]})
        self.assertEqual(valid_alias["count"], 2)
        self.assertEqual(valid_alias["specs"][0]["assets"], list(bundle_asset_aliases(manifest).values()))
        with patch("expman.matrix.zipfile.ZipFile", wraps=zipfile.ZipFile) as opened:
            launched = self.start(matrix)
            self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(launched["ids"]), 4)
        # A corrupt saved definition cannot bypass the launch-time validation.
        stored = json.loads(self.hub.db.execute("SELECT definition FROM matrices WHERE id=?", (matrix["id"],)).fetchone()[0])
        stored["grid"] = {"seed": [1, "bad-last-row"]}
        self.hub.db.execute("UPDATE matrices SET definition=? WHERE id=?", (json.dumps(stored), matrix["id"]))
        with self.assertRaises(APIError):
            self.start(matrix, "invalid-launch")
        self.assertEqual(self.hub.matrix_item(matrix["id"])["job_count"], 4)

    def test_priority_is_used_before_assignment(self):
        low = self.hub.submit({"request_id": "low", "spec": {**SPEC, "priority": -10}})["ids"][0]
        high = self.hub.submit({"request_id": "high", "spec": {**SPEC, "priority": 100}})["ids"][0]
        self.hub.sync("a", {"node_id": "a", "snapshot": {**SNAPSHOT, "policy": {"max_prefetch": 1}}})
        self.assertEqual(self.hub.job(high)["node_id"], "a")
        self.assertIsNone(self.hub.job(low)["node_id"])

    def test_report_marks_missing_partial_and_distinct_protocols(self):
        matrix = self.save()
        launched = self.start(matrix)
        self.hub.db.execute("UPDATE jobs SET state='succeeded',metrics=? WHERE id=?", (json.dumps({"eval": {"NDCG@10": 0.52}}), launched["ids"][0]))
        self.hub.db.execute("UPDATE jobs SET state='failed',detail=?,metrics=? WHERE id=?", ("a|b\n<script>", json.dumps({"loss": 0.4}), launched["ids"][1]))
        report = self.hub.matrix_report(matrix["id"]).decode()
        self.assertIn("eval.NDCG@10", report)
        self.assertIn("0.52", report)
        self.assertIn("failed", report)
        self.assertIn("| — |", report)
        self.assertIn("a&#124;b<br>&lt;script&gt;", report)
        self.assertIn("不跨指标协议排名或求平均", report)

    def test_clone_and_timing_completeness_preserve_meaning(self):
        matrix = self.save()
        clone = self.hub.matrix_save({**matrix, "id": None, "name": "Copy"})
        self.assertNotEqual(clone["id"], matrix["id"])
        self.assertEqual(clone["job_count"], 0)
        identity = self.start(matrix)["ids"][0]
        self.hub.db.execute("UPDATE jobs SET state='running',timing=? WHERE id=?", (json.dumps({"elapsed_seconds": 1, "complete": True}), identity))
        report = self.hub.matrix_report(matrix["id"]).decode()
        self.assertIn("1.00（进行中）", report)
        self.assertNotIn("1.00（进行中）（计时记录不完整）", report)
        self.hub.db.execute("UPDATE jobs SET state='succeeded',timing=? WHERE id=?", (json.dumps({"elapsed_seconds": 3, "complete": False}), identity))
        report = self.hub.matrix_report(matrix["id"]).decode()
        self.assertIn("3.00（计时记录不完整）", report)
        self.assertNotIn("3.00（进行中）", report)

    def test_http_matrix_routes_and_authentication(self):
        server = make_server(self.hub, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        def request(path, payload=None, token=None):
            body = None if payload is None else json.dumps(payload).encode()
            return urllib.request.urlopen(urllib.request.Request(base + path, data=body, headers={
                "Authorization": "Bearer " + (token or self.hub.config["admin_token"]), "Content-Type": "application/json"}))
        try:
            with request("/api/matrices/save", self.definition()) as response:
                matrix = json.load(response)
            with request("/api/matrices/item?id=" + matrix["id"]) as response:
                self.assertEqual(json.load(response)["count"], 8)
            with request("/api/matrices/report.md?id=" + matrix["id"]) as response:
                self.assertIn("text/markdown", response.headers["Content-Type"])
                self.assertIn("attachment", response.headers["Content-Disposition"])
                self.assertIn(matrix["id"], response.read().decode())
            with self.assertRaises(urllib.error.HTTPError) as error:
                request("/api/matrices", token=self.hub.config["nodes"]["a"])
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
