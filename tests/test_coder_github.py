import datetime as dt
import base64
import subprocess
import importlib.util
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

app = load("github_app", "scripts/coder_workspace/github_app.py")
helper = load("github_credential", "assets/coder/github_credential.py")


class GitHubTests(unittest.TestCase):
    def test_app_jwt_has_valid_signature_and_bounded_lifetime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["openssl", "genrsa", "-out", str(root / "private-key.pem"), "2048"],
                           capture_output=True, check=True)
            (root / "app.json").write_text(json.dumps({"id": 123}))
            value = app.jwt(root)
            header, claims, signature = value.split(".")
            payload = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
            self.assertEqual(payload["iss"], "123")
            self.assertLessEqual(payload["exp"] - payload["iat"], 600)
            (root / "signature").write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
            public = subprocess.run(["openssl", "rsa", "-in", str(root / "private-key.pem"), "-pubout"],
                                    capture_output=True, check=True).stdout
            (root / "public.pem").write_bytes(public)
            subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(root / "public.pem"),
                            "-signature", str(root / "signature")], input=(header + "." + claims).encode(),
                           capture_output=True, check=True)

    def test_default_token_is_read_only_and_limited_to_one_repository(self):
        with patch.object(app, "jwt", return_value="jwt"), \
             patch.object(app, "request", side_effect=[{"id": 42}, {"token": "test-token"}]) as request:
            app.mint(Path("unused"), "example-user/private-repo")
        self.assertEqual(request.call_args.args, ("/app/installations/42/access_tokens", "jwt",
            {"repositories": ["private-repo"], "permissions": {"contents": "read"}}))

    def test_write_permission_requires_explicit_selection(self):
        with patch.object(app, "jwt", return_value="jwt"), \
             patch.object(app, "request", side_effect=[{"id": 42}, {}]) as request:
            app.mint(Path("unused"), "example-user/private-repo", write=True)
        self.assertEqual(request.call_args.args[2]["permissions"]["contents"], "write")
        for value in ("../repo", "owner/repo?token=bad", "owner/..", "owner/repo/other"):
            with self.assertRaises(ValueError):
                app.repository(value)

    def test_helper_rejects_other_hosts_repositories_protocols_and_expired_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / (hashlib.sha256(b"owner/repo").hexdigest() + ".json")
            valid = {"token": "test-token", "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()}
            path.write_text(json.dumps(valid))
            values = {"protocol": "https", "host": "github.com", "path": "owner/repo.git"}
            self.assertEqual(helper.lookup(values, root), "test-token")
            for changes in ({"host": "evil.com"}, {"host": "github.com:443"}, {"protocol": "http"},
                            {"path": "owner/other.git"}, {"path": "../repo"}):
                self.assertIsNone(helper.lookup(values | changes, root))
            valid["expires_at"] = "2020-01-01T00:00:00Z"
            path.write_text(json.dumps(valid))
            self.assertIsNone(helper.lookup(values, root))

    def test_dashboard_login_requests_email_without_repository_access(self):
        value = app.login_manifest("http://127.0.0.1:8787", "nonce", "Workspaces", "https://coder.example.com")
        self.assertEqual(value["default_permissions"], {"emails": "read"})
        self.assertEqual(value["callback_urls"], ["https://coder.example.com/api/v2/users/oauth2/github/callback"])
        self.assertNotIn("hook_attributes", value)
        environment = app.login_environment({"client_id": "test-id", "client_secret": "test-secret"}, "https://coder.example.com")
        self.assertIn('CODER_OAUTH2_GITHUB_DEVICE_FLOW="false"', environment)
        self.assertIn('CODER_OAUTH2_GITHUB_DEFAULT_PROVIDER_ENABLE="false"', environment)
        self.assertIn('CODER_OAUTH2_GITHUB_ALLOW_SIGNUPS="false"', environment)

    def test_app_manifest_uses_private_app_and_no_user_oauth(self):
        value = app.manifest("http://127.0.0.1:8786", "nonce")
        self.assertFalse(value["public"])
        self.assertFalse(value["request_oauth_on_install"])
        self.assertNotIn("hook_attributes", value)
        self.assertIn("state=nonce", value["setup_url"])

    def test_app_credentials_are_saved_outside_public_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private/app.json"
            app.save(path, "private data")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
