import contextlib
import io
import json
import os
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


class DiscoveryHandler(BaseHTTPRequestHandler):
    response = {"data": [
        {"id": "reasoning-a", "reasoning_levels": ["low", "high"]},
        {"id": "plain-b", "reasoning_levels": ["low", "high"]},
    ]}
    status = 200
    reject_bearer = False
    requests = []

    def do_GET(self):
        type(self).requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "x_api_key": self.headers.get("x-api-key"),
        })
        if type(self).reject_bearer and self.headers.get("Authorization"):
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps(type(self).response).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        DiscoveryHandler.response = {"data": [
            {"id": "reasoning-a", "reasoning_levels": ["low", "high"]},
            {"id": "plain-b", "reasoning_levels": ["low", "high"]},
        ]}
        DiscoveryHandler.status = 200
        DiscoveryHandler.reject_bearer = False
        DiscoveryHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DiscoveryHandler)
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

    def add_provider(self, config, models=None):
        if models is None:
            models = ["reasoning-a", "plain-b"]
        config["config"]["providerConfigRules"]["providerRules"].append({
            "providerId": "new-provider",
            "providerName": "Test Relay",
            "config": {
                "access": {"type": "api-key", "apiKey": "test-key"},
                "api": {"type": "openai-chat-completions", "baseUrl": self.url},
                "modelOrder": models,
                "personalModelIds": models,
            },
        })

    def sync_args(self, config_path, use_config=False, models="", reasoning=""):
        return SimpleNamespace(
            config=Path(config_path), provider="new-provider", models=models,
            use_config=use_config, reasoning=reasoning, timeout=5, insecure=False,
        )

    def args(self, config_path, api_type="auto", reasoning=""):
        return SimpleNamespace(
            config=Path(config_path), name="", url=self.url, key="test-key",
            api_type=api_type, models="", reasoning=reasoning,
            position="bottom", provider_id="", timeout=5, insecure=False,
        )

    def test_parse_model_catalog_preserves_ids_and_common_explicit_levels(self):
        ids, levels = relay_cli.parse_model_catalog(DiscoveryHandler.response)
        self.assertEqual(ids, ["reasoning-a", "plain-b"])
        self.assertEqual(levels, ["low", "high"])

    def test_catalog_without_explicit_levels_has_no_reasoning_fallback(self):
        payload = {"data": [{"id": "plain"}]}
        self.assertIsNone(relay_cli.parse_reasoning_levels(payload))

    def test_partial_model_capabilities_do_not_advertise_unsupported_global_levels(self):
        payload = {"data": [
            {"id": "reasoning-a", "reasoning_levels": ["low", "high"]},
            {"id": "unknown-b"},
        ]}
        self.assertIsNone(relay_cli.parse_reasoning_levels(payload))

    def test_explicit_empty_capability_does_not_fall_back_to_global_levels(self):
        payload = {
            "data": [
                {"id": "no-reasoning", "reasoning_levels": []},
                {"id": "reasoning-b", "reasoning_levels": ["low"]},
            ],
            "capabilities": {"reasoning_levels": ["low"]},
        }
        self.assertEqual(relay_cli.parse_reasoning_levels(payload), [])

    def test_builtin_paths_find_install_template_from_portable_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "ZCode"
            builtin = root / "resources" / "config" / "provider" / "zcode-builtin.json"
            builtin.parent.mkdir(parents=True)
            builtin.write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {
                "RELAY_BUILTIN_DIR": "",
                "RELAY_ZCODE_INSTALL_DIR": str(root),
            }, clear=False):
                paths = relay_cli.builtin_json_paths()
            self.assertIn(str(builtin.resolve()), paths)

    def test_auto_protocol_falls_back_to_anthropic_headers(self):
        DiscoveryHandler.reject_bearer = True
        endpoint, models, levels, protocol, errors = relay_cli.discover_models(
            self.url, "test-key", "auto", 5
        )
        self.assertEqual(endpoint, self.url + "/v1/models")
        self.assertEqual(protocol, "anthropic")
        self.assertEqual(models, ["reasoning-a", "plain-b"])
        self.assertEqual(levels, ["low", "high"])
        self.assertIsNone(errors)
        self.assertTrue(any(r["authorization"] for r in DiscoveryHandler.requests))
        self.assertTrue(any(r["x_api_key"] == "test-key" for r in DiscoveryHandler.requests))

    def test_add_discovers_models_syncs_template_and_defaults_name(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "provider_config.json"
            builtin_path = Path(td) / "zcode-builtin.json"
            config_path.write_text(json.dumps(self.config()), encoding="utf-8")
            self.builtin(builtin_path)
            out = io.StringIO()
            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin_path]):
                with contextlib.redirect_stdout(out):
                    relay_cli.cmd_add(self.args(config_path))
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            rule = saved["config"]["providerConfigRules"]["providerRules"][0]
            self.assertEqual(rule["providerName"], "127.0.0.1")
            self.assertEqual(rule["config"]["api"]["type"], "openai-chat-completions")
            self.assertEqual(rule["config"]["modelOrder"], ["reasoning-a", "plain-b"])
            self.assertTrue(rule.get("templateId"))
            built = json.loads(builtin_path.read_text(encoding="utf-8"))
            template = built["config"]["providerConfigRules"]["templateRules"][0]
            self.assertEqual(template["config"]["builtinModelIds"], ["reasoning-a", "plain-b"])
            levels = built["config"]["modelConfigRules"]["providerSiteRules"][0]
            self.assertEqual(levels["config"]["optionSpecs"]["reasoningLevel"]["values"], ["low", "high"])

    def test_empty_add_uses_discovered_reasoning_levels(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "provider_config.json"
            builtin_path = Path(td) / "zcode-builtin.json"
            config_path.write_text(json.dumps(self.config()), encoding="utf-8")
            self.builtin(builtin_path)
            args = self.args(config_path, reasoning="")
            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin_path]):
                with contextlib.redirect_stdout(io.StringIO()):
                    relay_cli.cmd_add(args)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            rules = saved["config"]["modelConfigRules"]["providerModelRules"]
            self.assertTrue(rules)
            for rule in rules:
                self.assertEqual(
                    rule["config"]["optionSpecs"]["reasoningLevel"]["values"],
                    ["low", "high"],
                )

    def test_empty_sync_template_uses_discovered_reasoning_levels(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "provider_config.json"
            builtin_path = Path(td) / "zcode-builtin.json"
            config = self.config()
            self.add_provider(config)
            config_path.write_text(json.dumps(config), encoding="utf-8")
            self.builtin(builtin_path)
            args = self.sync_args(config_path, reasoning="")
            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin_path]):
                with contextlib.redirect_stdout(io.StringIO()):
                    relay_cli.cmd_sync_template(args)
            built = json.loads(builtin_path.read_text(encoding="utf-8"))
            levels = built["config"]["modelConfigRules"]["providerSiteRules"][0]
            self.assertEqual(
                levels["config"]["optionSpecs"]["reasoningLevel"]["values"],
                ["low", "high"],
            )

    def test_sync_template_updates_provider_model_list_and_preserves_enabled_state(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "provider_config.json"
            builtin_path = Path(td) / "zcode-builtin.json"
            config = self.config()
            self.add_provider(config, models=[])
            config["config"]["modelConfigRules"]["providerModelRules"] = [{
                "providerId": "new-provider", "modelId": "reasoning-a",
                "config": {"enabled": True},
            }]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            self.builtin(builtin_path)
            args = self.sync_args(config_path)
            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin_path]):
                with contextlib.redirect_stdout(io.StringIO()):
                    relay_cli.cmd_sync_template(args)

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            rule = saved["config"]["providerConfigRules"]["providerRules"][0]
            self.assertEqual(rule["config"]["modelOrder"], ["reasoning-a", "plain-b"])
            self.assertEqual(rule["config"]["personalModelIds"], ["reasoning-a", "plain-b"])
            models = saved["config"]["modelConfigRules"]["providerModelRules"]
            enabled = {entry["modelId"]: entry["config"]["enabled"] for entry in models}
            self.assertEqual(enabled, {"reasoning-a": True, "plain-b": False})

    def test_add_without_capability_metadata_does_not_write_reasoning_rule(self):
        DiscoveryHandler.response = {"data": [{"id": "plain-a"}, {"id": "plain-b"}]}
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "provider_config.json"
            builtin_path = Path(td) / "zcode-builtin.json"
            config_path.write_text(json.dumps(self.config()), encoding="utf-8")
            self.builtin(builtin_path)
            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin_path]):
                with contextlib.redirect_stdout(io.StringIO()):
                    relay_cli.cmd_add(self.args(config_path))
            built = json.loads(builtin_path.read_text(encoding="utf-8"))
            self.assertEqual(built["config"]["modelConfigRules"]["providerSiteRules"], [])

    def test_sync_template_without_capability_metadata_removes_stale_rule(self):
        DiscoveryHandler.response = {"data": [{"id": "plain-a"}, {"id": "plain-b"}]}
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "provider_config.json"
            builtin_path = Path(td) / "zcode-builtin.json"
            config = self.config()
            self.add_provider(config)
            config_path.write_text(json.dumps(config), encoding="utf-8")
            self.builtin(builtin_path)
            built = json.loads(builtin_path.read_text(encoding="utf-8"))
            built["config"]["modelConfigRules"]["providerSiteRules"] = [{
                "modelMatch": "^(?:reasoning-a|plain-b)$",
                "baseUrlMatch": relay_cli.regex_escape(self.url) + r"/?",
                "apiTypeMatch": "openai-chat-completions",
                "config": {"optionSpecs": {"reasoningLevel": {"values": ["low"]}}},
            }]
            builtin_path.write_text(json.dumps(built), encoding="utf-8")
            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin_path]):
                with contextlib.redirect_stdout(io.StringIO()):
                    relay_cli.cmd_sync_template(self.sync_args(config_path))
            built = json.loads(builtin_path.read_text(encoding="utf-8"))
            self.assertEqual(built["config"]["modelConfigRules"]["providerSiteRules"], [])

    def test_sync_template_use_config_reports_empty_models_without_unbound_errors(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "provider_config.json"
            builtin_path = Path(td) / "zcode-builtin.json"
            config = self.config()
            self.add_provider(config, models=[])
            config_path.write_text(json.dumps(config), encoding="utf-8")
            self.builtin(builtin_path)
            args = self.sync_args(config_path, use_config=True)
            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin_path]):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        relay_cli.cmd_sync_template(args)
            self.assertEqual(raised.exception.code, 2)

    def test_add_fetch_failure_does_not_write_provider(self):
        DiscoveryHandler.status = 500
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "provider_config.json"
            builtin_path = Path(td) / "zcode-builtin.json"
            original = self.config()
            config_path.write_text(json.dumps(original), encoding="utf-8")
            self.builtin(builtin_path)
            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin_path]):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        relay_cli.cmd_add(self.args(config_path))
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), original)
            self.assertEqual(json.loads(builtin_path.read_text(encoding="utf-8"))["config"]["providerConfigRules"]["templateRules"], [])

    def test_add_with_explicit_models_does_not_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "provider_config.json"
            builtin_path = Path(td) / "zcode-builtin.json"
            config_path.write_text(json.dumps(self.config()), encoding="utf-8")
            self.builtin(builtin_path)
            args = self.args(config_path, api_type="openai-chat-completions")
            args.models = "chosen-model"
            with patch.object(relay_cli, "builtin_json_paths", return_value=[builtin_path]):
                with contextlib.redirect_stdout(io.StringIO()):
                    relay_cli.cmd_add(args)
            self.assertEqual(DiscoveryHandler.requests, [])
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["config"]["providerConfigRules"]["providerRules"][0]["config"]["modelOrder"], ["chosen-model"])


if __name__ == "__main__":
    unittest.main()
