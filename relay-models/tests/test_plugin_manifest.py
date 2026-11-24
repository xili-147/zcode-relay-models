import json
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class PluginManifestTests(unittest.TestCase):
    def test_zcode_manifest_registers_session_start_hook(self):
        manifest = json.loads(
            (PLUGIN_ROOT / ".zcode-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        hook_path = manifest.get("hooks")
        self.assertIsNotNone(hook_path)
        hooks = json.loads((PLUGIN_ROOT / hook_path).read_text(encoding="utf-8"))
        self.assertIn("SessionStart", hooks.get("hooks", {}))


if __name__ == "__main__":
    unittest.main()
