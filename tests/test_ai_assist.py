import copy
import io
import json
from pathlib import Path
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

from expman.ai_assist import AIAssist, _NoRedirect
from expman.common import api_request
from expman.hub import Hub, make_server
from expman.project_import import BUSY
from tests.support import temporary_directory


class AISettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.hub = Hub(self.root / "hub")
        self.ai = AIAssist(self.hub)

    def tearDown(self):
        self.hub.close()
        self.temporary.__exit__(None, None, None)

    def test_secret_is_persisted_but_not_returned_or_in_state(self):
        result = self.ai.settings({"enabled": True, "api_key": "test-private-key"})
        self.assertTrue(result["has_api_key"])
        self.assertNotIn("api_key", result)
        self.assertNotIn("test-private-key", json.dumps(self.hub.state()))
        self.ai.settings({"api_key": ""})
        self.assertEqual(AIAssist(self.hub)._config()["api_key"], "test-private-key")
        self.assertFalse(self.ai.settings({"clear_api_key": True})["has_api_key"])

    def test_rejects_unsafe_endpoint_and_header_values_without_changing_settings(self):
        for endpoint in ("http://example.com", "file:///etc/passwd", "https://user:pass@example.com", "https://api.example/?key=bad", "https://api.example/chat/completions"):
            with self.assertRaises(ValueError):
                self.ai.settings({"base_url": endpoint})
        with self.assertRaises(ValueError):
            self.ai.settings({"api_key": "secret\nHeader: injected"})
        self.assertEqual(self.ai.settings()["base_url"], "https://api.deepseek.com")
        self.ai.settings({"base_url": "http://127.0.0.1:11434/v1"})
        self.assertFalse(self.ai.settings()["has_api_key"])

    def test_disabled_never_calls_provider_and_redirects_are_not_followed(self):
        with patch("urllib.request.build_opener") as opener:
            with self.assertRaisesRegex(ValueError, "启用"):
                self.ai.test({})
            opener.assert_not_called()
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.example"))

    def test_provider_transport_and_bad_reply_do_not_expose_input_or_credentials(self):
        self.ai.settings({"enabled": True, "api_key": "private-provider-key"})
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}]}).encode())
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(self.ai._complete("JSON", {"api_key": "other", "text": self.hub.config["admin_token"]}), {"ok": True})
            request = opener.return_value.open.call_args.args[0]
            text = request.data.decode()
            self.assertNotIn("other", text)
            self.assertNotIn(self.hub.config["admin_token"], text)
            self.assertEqual(request.get_header("Authorization"), "Bearer private-provider-key")
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 25)
        error = urllib.error.HTTPError("https://api.deepseek.com", 401, "private-provider-key", {}, None)
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(ValueError, "HTTP 401") as caught:
                self.ai.test({})
            self.assertNotIn("private-provider-key", str(caught.exception))

    def test_http_is_admin_only_and_import_assist_is_loopback_only(self):
        worker_token = self.hub.add_node("worker")
        server = make_server(self.hub, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                api_request(base + "/api/ai/settings", worker_token)
            self.assertEqual(caught.exception.code, 403)
            self.assertFalse(api_request(base + "/api/ai/settings", self.hub.config["admin_token"])["enabled"])
            with patch("expman.project_import.is_loopback", return_value=False):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    api_request(base + "/api/local/imports/assist", self.hub.config["admin_token"], {})
                self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def scan(self):
        source = self.root / "algorithm"
        source.mkdir()
        (source / "main.py").write_text("import argparse\np=argparse.ArgumentParser()\np.add_argument('--epochs',type=int,default=3)\nraise RuntimeError('never execute source')\n", encoding="utf-8")
        imports = self.hub.project_imports()
        item = imports.start({"source": str(source)})
        deadline = time.monotonic() + 10
        while item["status"] in BUSY and time.monotonic() < deadline:
            time.sleep(0.01)
            item = imports.item(item["id"])
        self.assertEqual(item["status"], "ready")
        return item

    def test_suggestion_is_validated_preview_source_opt_in_and_original_unchanged(self):
        item = self.scan()
        before = copy.deepcopy(item["draft"])
        suggestion = {"summary": "说明", "harness": {"metrics": [{"pattern": r"epoch=(?P<step>\d+) loss=(?P<loss>[\d.]+)", "values": {"loss": "loss"}}]}}
        with patch.object(self.ai, "_complete", return_value=suggestion) as complete:
            result = self.ai.assist({"id": item["id"], "sample_log": "epoch=1 loss=0.3"})
            self.assertTrue(result["changes"])
            sent = complete.call_args.args[1]
            self.assertNotIn("entry_source", sent)
            self.assertNotIn(item["source"], json.dumps(sent))
            self.ai.assist({"id": item["id"], "include_source": True})
            self.assertIn("never execute source", complete.call_args.args[1]["entry_source"])
        self.assertEqual(self.hub.project_imports().item(item["id"])["draft"], before)
        self.assertEqual(self.hub.state()["projects"], [])

    def test_malicious_or_invalid_proposal_cannot_change_source_or_enable_resume(self):
        item = self.scan()
        for proposal in ({"project": {"source": "C:/"}}, {"project": {"assets": {}}}, {"harness": {"resume": {"supported": True}}}, {"harness": {"metrics": [{"pattern": "(?P<step>x)", "values": {"loss": "absent"}}]}}):
            with patch.object(self.ai, "_complete", return_value=proposal):
                with self.assertRaises(ValueError):
                    self.ai.assist({"id": item["id"]})
        self.assertEqual(self.hub.project_imports().item(item["id"])["draft"], item["draft"])

    def test_actual_python_metric_regex_and_failure(self):
        imports = self.hub.project_imports()
        rules = [{"pattern": r"epoch=(?P<step>\d+) loss=(?P<loss>[\d.eE+-]+)", "values": {"loss": "loss"}}]
        reply = imports.test_metrics({"metrics": rules, "sample_log": "epoch=1 loss=0.25\nepoch=2 loss=1e-2"})
        self.assertEqual(reply["matches"][1]["values"]["loss"], 0.01)
        self.assertEqual(reply["warnings"], [])
        self.assertTrue(imports.test_metrics({"metrics": rules, "sample_log": "unmatched"})["warnings"])
        with self.assertRaises(ValueError):
            imports.test_metrics({"metrics": [{"pattern": "[", "values": {"loss": "loss"}}], "sample_log": "test"})
        # A catastrophic regex is terminated in the isolated process.
        with self.assertRaisesRegex(ValueError, "2 秒"):
            imports.test_metrics({"metrics": [{"pattern": "(?P<step>(a+)+)$", "values": {"value": "step"}}], "sample_log": "a" * 100 + "!"})


if __name__ == "__main__":
    unittest.main()
