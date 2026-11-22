import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import relay_cli


class ModelsHandler(BaseHTTPRequestHandler):
    response = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
    status = 200
    requests = 0

    def do_GET(self):
        type(self).requests += 1
        body = json.dumps(type(self).response).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class AddFetchModelsTests(unittest.TestCase):
    def setUp(self):
        ModelsHandler.response = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
        ModelsHandler.status = 200
        ModelsHandler.requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def config(self):
        return {
            "revision": 0,
            "config": {
                "providerOrder": [],
                "providerConfigRules": {"providerRules": []},
                "modelConfigRules": {
                    "providerModelRules": [],
                    "templateModelRules": [],
                    "providerSiteRules": [],
                },
            },
        }

    def builtin(self, path):
        path.write_text(json.dumps({
            "revision": 0,
            "config": {
                "providerConfigRules": {"templateRules": []},
                "modelConfigRules": {"templateModelRules": [], "providerSiteRules": []},
            },
        }), encoding="utf-8")

    def args(self, path, models=""):
        return SimpleNamespace(
            config=Path(path),
            name="Test Relay",
            url=self.url,
            key="test-key",
            api_type="openai-chat-completions",
            models=models,
            reasoning="none",
            position="bottom",
            provider_id="",
            timeout=5,
            insecure=False,
        )

    def test_empty_models_fetches_and_stores_remote_models(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider_config.json"
            builtin = Path(td) / "zcode-builtin.json"
            path.write_text(json.dumps(self.config()), encoding="utf-8")
            self.builtin(builtin)

            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin]):
                relay_cli.cmd_add(self.args(path))

            saved = json.loads(path.read_text(encoding="utf-8"))
            rule = saved["config"]["providerConfigRules"]["providerRules"][0]
            self.assertEqual(rule["config"]["modelOrder"], ["model-a", "model-b"])
            self.assertEqual(rule["config"]["personalModelIds"], ["model-a", "model-b"])
            self.assertEqual(
                [m["modelId"] for m in saved["config"]["modelConfigRules"]["providerModelRules"]],
                ["model-a", "model-b"],
            )
            self.assertGreater(ModelsHandler.requests, 0)

    def test_fetch_failure_does_not_write_empty_provider(self):
        ModelsHandler.status = 500
        ModelsHandler.response = {"error": "denied"}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider_config.json"
            builtin = Path(td) / "zcode-builtin.json"
            original = json.dumps(self.config(), ensure_ascii=False, indent=2)
            path.write_text(original, encoding="utf-8")
            self.builtin(builtin)

            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin]):
                with self.assertRaises(SystemExit) as raised:
                    relay_cli.cmd_add(self.args(path))

            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["config"]["providerConfigRules"]["providerRules"], [])

    def test_explicit_models_does_not_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider_config.json"
            builtin = Path(td) / "zcode-builtin.json"
            path.write_text(json.dumps(self.config()), encoding="utf-8")
            self.builtin(builtin)

            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin]):
                relay_cli.cmd_add(self.args(path, models="chosen-model"))

            self.assertEqual(ModelsHandler.requests, 0)
            saved = json.loads(path.read_text(encoding="utf-8"))
            rule = saved["config"]["providerConfigRules"]["providerRules"][0]
            self.assertEqual(rule["config"]["modelOrder"], ["chosen-model"])


if __name__ == "__main__":
    unittest.main()
