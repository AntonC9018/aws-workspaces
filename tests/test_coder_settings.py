import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("settings", Path(__file__).resolve().parents[1] / "scripts/coder_workspace/settings.py")
settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(settings)


class SettingsTests(unittest.TestCase):
    def test_defaults_require_no_personal_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            value = settings.load(Path(directory) / "missing.json")
        self.assertEqual(value["managed_by"], "coder-workspaces")
        self.assertEqual(value["coder_url"], "http://127.0.0.1:7080")
        self.assertEqual(value["github_owner"], "")

    def test_existing_deployment_names_and_urls_can_be_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            values = {"managed_by": "existing-manager", "guard_name": "existing-guard",
                      "iam_user": "existing-controller", "coder_url": "https://coder.example.com/"}
            path.write_text(json.dumps(values))
            loaded = settings.load(path)
        self.assertEqual(loaded["managed_by"], values["managed_by"])
        self.assertEqual(loaded["coder_url"], "https://coder.example.com")

    def test_invalid_configuration_fails_before_aws_operations(self):
        for value in ({"region": "us-east-1"}, {"managed_by": "../bad"},
                      {"coder_url": "https://user:secret@example.com"}, {"unknown": "value"}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    settings.load(path)
