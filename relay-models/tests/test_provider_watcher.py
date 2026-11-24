import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import provider_watcher


class ProviderWatcherTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config_path = self.root / "provider_config.json"
        self.state_path = self.root / "watcher-state.json"
        self.builtin_path = self.root / "zcode-builtin.json"
        self.config_path.write_text(json.dumps({
            "revision": 0, "config": {
                "providerOrder": ["gpt"],
                "providerConfigRules": {"providerRules": [{
                    "providerId": "gpt", "providerName": "Relay",
                    "config": {"group": "standard-personal",
                        "access": {"type": "api-key", "apiKey": "secret-key"},
                        "api": {"type": "openai-chat-completions",
                                "baseUrl": "https://relay.example/v1"},
                        "modelOrder": ["model-a"], "personalModelIds": ["model-a"]},
                }]},
                "modelConfigRules": {"providerModelRules": [
                    {"providerId": "gpt", "modelId": "model-a",
                     "config": {"enabled": True}},
                    {"providerId": "gpt", "modelId": "stale",
                     "config": {"enabled": False}},
                ], "templateModelRules": [], "providerSiteRules": []},
            },
        }), encoding="utf-8")
        self.builtin_path.write_text(json.dumps({"revision": 0, "config": {
            "providerConfigRules": {"templateRules": []},
            "modelConfigRules": {"templateModelRules": [], "providerSiteRules": []},
        }}), encoding="utf-8")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_refresh_updates_all_candidates_and_preserves_enabled_state(self):
        with patch.object(provider_watcher.relay_cli, "builtin_json_paths",
                          return_value=[self.builtin_path]), patch.object(
                provider_watcher.relay_cli, "discover_models",
                return_value=("url", ["model-a", "model-b"], None, "openai", [])) as discover:
            result = provider_watcher.refresh_changed_providers(
                self.config_path, self.state_path, now=1000)
            self.assertEqual(result["refreshed"], ["gpt"])
            saved = json.loads(self.config_path.read_text(encoding="utf-8"))
            rule = saved["config"]["providerConfigRules"]["providerRules"][0]
            self.assertEqual(rule["config"]["modelOrder"], ["model-a", "model-b"])
            models = saved["config"]["modelConfigRules"]["providerModelRules"]
            enabled = {m["modelId"]: m["config"]["enabled"] for m in models}
            self.assertEqual(enabled, {"model-a": True, "model-b": False})
            self.assertNotIn("secret-key", self.state_path.read_text(encoding="utf-8"))
            builtin = json.loads(self.builtin_path.read_text(encoding="utf-8"))
            tpl = builtin["config"]["providerConfigRules"]["templateRules"][0]
            self.assertEqual(tpl["config"]["builtinModelIds"], ["model-a", "model-b"])
            template_enabled = {
                model["modelId"]: model["config"]["enabled"]
                for model in builtin["config"]["modelConfigRules"]["templateModelRules"]
            }
            self.assertEqual(template_enabled, {"model-a": True, "model-b": False})
            provider_watcher.refresh_changed_providers(
                self.config_path, self.state_path, now=1001)
            self.assertEqual(discover.call_count, 1)

    def test_key_change_triggers_refresh_and_failed_refresh_keeps_config(self):
        answers = [("url", ["model-a"], None, "openai", []),
                   ("url", ["model-b"], None, "openai", []),
                   ("url", ["model-c"], None, "openai", [])]
        with patch.object(provider_watcher.relay_cli, "builtin_json_paths",
                          return_value=[self.builtin_path]), patch.object(
                provider_watcher.relay_cli, "discover_models", side_effect=answers) as discover:
            provider_watcher.refresh_changed_providers(self.config_path, self.state_path, now=1000)
            saved = json.loads(self.config_path.read_text(encoding="utf-8"))
            changed = saved["config"]["providerConfigRules"]["providerRules"][0]["config"]
            changed["access"]["apiKey"] = "new-secret"
            changed["api"]["baseUrl"] = "https://relay-new.example/v1"
            self.config_path.write_text(json.dumps(saved), encoding="utf-8")
            result = provider_watcher.refresh_changed_providers(self.config_path, self.state_path, now=1001)
            self.assertEqual(result["refreshed"], ["gpt"])
            self.assertEqual(discover.call_count, 2)
            self.assertNotIn("new-secret", self.state_path.read_text(encoding="utf-8"))
            changed["api"]["baseUrl"] = "https://url-only-change.example/v1"
            self.config_path.write_text(json.dumps(saved), encoding="utf-8")
            result = provider_watcher.refresh_changed_providers(self.config_path, self.state_path, now=1002)
            self.assertEqual(result["refreshed"], ["gpt"])
            self.assertEqual(discover.call_count, 3)

        changed = json.loads(self.config_path.read_text(encoding="utf-8"))
        changed["config"]["providerConfigRules"]["providerRules"][0]["config"]["access"]["apiKey"] = "failed-secret"
        self.config_path.write_text(json.dumps(changed), encoding="utf-8")
        original = self.config_path.read_text(encoding="utf-8")
        with patch.object(provider_watcher.relay_cli, "discover_models",
                          return_value=("", [], None, "openai", ["HTTP 401"])):
            result = provider_watcher.refresh_changed_providers(self.config_path, self.state_path, now=2000)
            self.assertEqual(result["failed"], ["gpt"])
            self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_concurrent_settings_save_is_not_overwritten(self):
        original_sync = provider_watcher.relay_cli.sync_template_config

        def save_during_sync(cfg, rule, config_path, models, levels, **kwargs):
            live = json.loads(config_path.read_text(encoding="utf-8"))
            live["concurrentSetting"] = "keep"
            config_path.write_text(json.dumps(live), encoding="utf-8")
            return original_sync(cfg, rule, config_path, models, levels, **kwargs)

        with patch.object(provider_watcher.relay_cli, "builtin_json_paths",
                          return_value=[self.builtin_path]), patch.object(
                provider_watcher.relay_cli, "discover_models",
                return_value=("url", ["model-a", "model-b"], None, "openai", [])), patch.object(
                provider_watcher.relay_cli, "sync_template_config",
                side_effect=save_during_sync):
            result = provider_watcher.refresh_changed_providers(
                self.config_path, self.state_path, now=1000)

        self.assertEqual(result["failed"], ["gpt"])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["concurrentSetting"], "keep")
        rule = saved["config"]["providerConfigRules"]["providerRules"][0]
        self.assertEqual(rule["config"]["modelOrder"], ["model-a"])


if __name__ == "__main__":
    unittest.main()

