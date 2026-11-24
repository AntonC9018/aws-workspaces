import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coder_workspace import controller, images
from coder_workspace.providers import AwsProvider, Instance


class ImageSpecTests(unittest.TestCase):
    def test_auto_scopes_multiple_images_per_project_and_hashes_deterministically(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "colorshoot"
            (repo / ".coder-images").mkdir(parents=True)
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:AntonC9018/colorshoot.git"], check=True)
            (repo / ".coder-images/bake-unity.sh").write_text("#!/bin/sh\necho unity\n")
            spec_a = repo / ".coder-images/unity-6000.json"
            spec_b = repo / ".coder-images/webgl-test.json"
            spec_a.write_text(json.dumps({
                "version": 1, "disk_gb": 80, "packages": ["xvfb"], "setup_script": "bake-unity.sh",
            }))
            spec_b.write_text(json.dumps({
                "version": 1, "name": "colorshoot-webgl-test", "commands": ["echo webgl"],
            }))
            loaded_a = images.load_spec(spec_a)
            loaded_b = images.load_spec(spec_b)
            self.assertEqual(loaded_a["name"], "colorshoot-unity-6000")
            self.assertEqual(loaded_b["name"], "colorshoot-webgl-test")
            self.assertEqual(loaded_a["project"], "colorshoot")
            self.assertEqual(len(loaded_a["hash"]), 16)
            # Changing setup_script changes the content hash.
            (repo / ".coder-images/bake-unity.sh").write_text("#!/bin/sh\necho unity-v2\n")
            self.assertNotEqual(images.load_spec(spec_a)["hash"], loaded_a["hash"])

    def test_rejects_unsafe_packages_and_escaping_scripts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = root / "bad.json"
            spec.write_text(json.dumps({"version": 1, "packages": ["curl; rm -rf /"]}))
            with self.assertRaises(ValueError):
                images.load_spec(spec)
            spec.write_text(json.dumps({"version": 1, "setup_script": "../../etc/passwd"}))
            with self.assertRaises(ValueError):
                images.load_spec(spec)

    def test_bake_image_skips_when_matching_hash_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            spec_file = Path(directory) / "demo.json"
            spec_file.write_text(json.dumps({"version": 1, "project": "demo", "commands": ["true"]}))
            loaded = images.load_spec(spec_file)
            from coder_workspace.providers import Provider
            prov = Mock(spec=Provider)
            prov.name = "oci"
            prov.list_images.return_value = [{"id": "ocid1.image.existing", "name": loaded["name"], "hash": loaded["hash"]}]
            result = controller.bake_image(prov, {}, spec_path=str(spec_file))
            self.assertEqual(result["id"], "ocid1.image.existing")
            prov.create_image.assert_not_called()

    def test_aws_delete_image_deregisters_ami_and_deletes_backing_snapshots(self):
        ec2 = Mock()
        ec2.describe_images.return_value = {"Images": [{
            "ImageId": "ami-owned",
            "Name": "coder-colorshoot-unity-12345678",
            "State": "available",
            "CreationDate": "2026-11-23T00:00:00Z",
            "Tags": [{"Key": "ManagedBy", "Value": "coder-workspaces"}, {"Key": "ImageName", "Value": "colorshoot-unity"}],
            "BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-123"}}],
        }]}
        session = Mock()
        session.client.return_value = ec2
        from coder_workspace.settings import load as load_settings
        prov = AwsProvider(load_settings(Path("/nonexistent/config.json")), session=session)
        deleted = prov.delete_image("colorshoot-unity")
        self.assertEqual(deleted["id"], "ami-owned")
        ec2.deregister_image.assert_called_once_with(ImageId="ami-owned")
        ec2.delete_snapshot.assert_called_once_with(SnapshotId="snap-123")

    def test_oci_list_images_paginates_past_platform_images(self):
        from types import SimpleNamespace
        import datetime as dt
        from coder_workspace.providers import OciProvider
        from coder_workspace.settings import load as load_settings
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "oci-infra.json").write_text(json.dumps({"compartment_id": "ocid1.comp.test"}))
            prov = OciProvider(load_settings(Path("/nonexistent/config.json")), state_dir=state_dir)
            fake_img = SimpleNamespace(
                id="ocid1.image.custom",
                display_name="coder-colorshoot-unity-6000-e2b17183",
                lifecycle_state="AVAILABLE",
                time_created=dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc),
                size_in_mbs=18000,
                freeform_tags={
                    "ManagedBy": "coder-workspaces",
                    "ImageName": "colorshoot-unity-6000",
                    "ImageHash": "e2b1718322025c5d",
                    "ImageArch": "amd64",
                },
            )
            fake_platform = SimpleNamespace(
                id="ocid1.image.platform",
                display_name="Canonical-Ubuntu",
                lifecycle_state="AVAILABLE",
                time_created=dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc),
                size_in_mbs=4000,
                freeform_tags={},
            )
            compute = Mock()
            with patch.object(prov, "_client", return_value=compute), \
                 patch("oci.pagination.list_call_get_all_results",
                       return_value=SimpleNamespace(data=[fake_platform, fake_img])) as paginate:
                imgs = prov.list_images()
            paginate.assert_called_once()
            self.assertEqual(len(imgs), 1)
            self.assertEqual(imgs[0]["id"], "ocid1.image.custom")
            self.assertEqual(imgs[0]["name"], "colorshoot-unity-6000")

    def test_authorize_unity_uses_unity_cli_personal_flow(self):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(".config/unity-cli/auth.json")
            payload = b'{"token":"ok"}'
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            machine = Instance(id="i-1", name="dev", state="running", public_ip="1.2.3.4",
                               instance_type="e5-2c-8g", provider="oci",
                               tags={"LeaseExpiresAt": "2099-01-01T00:00:00+00:00"})
            from coder_workspace.providers import Provider
            prov = Mock(spec=Provider)
            prov.name = "oci"
            prov.find_instance.return_value = machine
            prov.get_instance.return_value = machine
            ok = subprocess.CompletedProcess([], 0, stdout=buf.getvalue(), stderr=b"")
            lease_release = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"until": 1300, "remaining": 300.0}), stderr="")
            def fake_remote(_machine, cmd, **_kwargs):
                if cmd[:3] == ["sudo", "/usr/local/bin/workspace-lease", "release"]:
                    return lease_release
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with patch.object(controller, "STATE", state), \
                 patch.object(controller, "api", return_value={"id": "owner-1"}), \
                 patch.object(controller, "ensure_controller_ssh"), \
                 patch.object(controller, "renew"), \
                 patch.object(controller, "remote", side_effect=fake_remote) as remote_mock, \
                 patch.object(controller.subprocess, "run", return_value=ok) as run_mock:
                controller.authorize_unity(prov, "dev", login=True)
            self.assertTrue((state / "unity/auth-bundle.tar").is_file())
            remote_calls = [c.args[1] for c in remote_mock.call_args_list]
            lease_calls = [c for c in remote_calls if c[:2] == ["sudo", "/usr/local/bin/workspace-lease"]]
            self.assertEqual(len(lease_calls), 2)
            self.assertTrue(lease_calls[0][5].startswith("op:unity-"))
            self.assertEqual(lease_calls[1][2:5], ["release", "--owner", lease_calls[0][5]])
            self.assertIn(["env", *controller.UNITY_FILE_STORE_ENV, "unity", "license", "activate", "--personal", "--accept-eula"], remote_calls)
            self.assertIn(["env", *controller.UNITY_FILE_STORE_ENV, "unity", "license", "status"], remote_calls)
            self.assertTrue(any("SystemCallFilter=~@keyring" in " ".join(c.args[0])
                                and "auth login --insecure-storage" in " ".join(c.args[0])
                                for c in run_mock.call_args_list))

    def test_unity_auth_bundle_rejects_paths_outside_unity_roots_and_empty_tar(self):
        import io
        import tarfile
        empty_buf = io.BytesIO()
        with tarfile.open(fileobj=empty_buf, mode="w"):
            pass
        self.assertEqual(controller._validate_unity_auth_bundle(empty_buf.getvalue()), 0)

        poison_buf = io.BytesIO()
        with tarfile.open(fileobj=poison_buf, mode="w") as tf:
            info = tarfile.TarInfo(".ssh/authorized_keys")
            payload = b"ssh-ed25519 AAAA evil\n"
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        with self.assertRaises(RuntimeError):
            controller._validate_unity_auth_bundle(poison_buf.getvalue())

    def test_unity_pack_and_unpack_roundtrip_snapshots_wal_sqlite_and_clears_stale_sidecars(self):
        import io
        import os
        import sqlite3
        import stat
        import tarfile

        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dst_dir:
            src_home = Path(src_dir)
            dst_home = Path(dst_dir)
            hub = src_home / ".config/unityhub"
            hub.mkdir(parents=True)
            # Keep WAL uncheckpointed so the inserted row lives only in accounts.db-wal.
            conn = sqlite3.connect(str(hub / "accounts.db"))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, email TEXT)")
            conn.execute("INSERT INTO accounts VALUES ('u1', 'dev@example.com')")
            conn.commit()
            self.assertTrue((hub / "accounts.db-wal").is_file())
            self.assertGreater((hub / "accounts.db-wal").stat().st_size, 0)

            broker = src_home / ".local/share/UnityAuthBroker"
            broker.mkdir(parents=True)
            (broker / "token-store.seal").write_bytes(b"machine-bound-seal")
            (broker / "dpop-tpm2").mkdir()
            (broker / "dpop-tpm2/key.bin").write_bytes(b"tpm-key")
            keyring = src_home / ".local/share/unity-keyring"
            keyring.mkdir(parents=True)
            (keyring / "credentials.json").write_text('{"refresh":"tok"}')
            (keyring / "insecure-storage").write_text("plain-file mode\n")
            (keyring / "credentials.lock").write_bytes(b"")

            # Excluded files that must not be bundled.
            (hub / "logs").mkdir()
            (hub / "logs/cli.log").write_text("noise\n")
            (hub / "licenses").mkdir()
            (hub / "licenses/Unity_lic.ulf").write_text("secret-license")
            (hub / "Editor").mkdir()
            (hub / "Editor/blob.bin").write_bytes(b"editor")
            (hub / "external-modules").mkdir()
            (hub / "external-modules/mod.bin").write_bytes(b"module")

            try:
                packed = subprocess.run(
                    [sys.executable, "-c", controller._unity_pack_script()],
                    env={**os.environ, "HOME": str(src_home)},
                    capture_output=True, check=True,
                ).stdout
            finally:
                conn.close()

            self.assertGreater(controller._validate_unity_auth_bundle(packed), 0)
            with tarfile.open(fileobj=io.BytesIO(packed), mode="r") as tf:
                names = set(tf.getnames())
            self.assertIn(".config/unityhub/accounts.db", names)
            self.assertIn(".local/share/unity-keyring/credentials.json", names)
            self.assertIn(".local/share/unity-keyring/insecure-storage", names)
            self.assertFalse(any(n.endswith((".db-wal", ".db-shm", ".log", ".seal", ".lock")) for n in names))
            self.assertFalse(any("/licenses/" in n or "/Editor/" in n or "/logs/" in n
                                 or "/external-modules/" in n or "/dpop-tpm2/" in n for n in names))

            # Seed destination with a stale WAL-mode DB and stale machine seal that must be cleared on unpack.
            dst_hub = dst_home / ".config/unityhub"
            dst_hub.mkdir(parents=True)
            dst_broker = dst_home / ".local/share/UnityAuthBroker"
            dst_broker.mkdir(parents=True)
            (dst_broker / "token-store.seal").write_bytes(b"old-seal")
            stale = sqlite3.connect(str(dst_hub / "accounts.db"))
            stale.execute("PRAGMA journal_mode=WAL")
            stale.execute("PRAGMA wal_autocheckpoint=0")
            stale.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, email TEXT)")
            stale.execute("INSERT INTO accounts VALUES ('stale', 'old@example.com')")
            stale.commit()
            stale.close()
            (dst_hub / "accounts.db-wal").write_bytes(b"stale-wal-garbage")
            (dst_hub / "accounts.db-shm").write_bytes(b"stale-shm-garbage")

            subprocess.run(
                [sys.executable, "-c", controller._unity_unpack_script()],
                input=packed,
                env={**os.environ, "HOME": str(dst_home)},
                check=True,
            )

            self.assertFalse((dst_hub / "accounts.db-wal").exists())
            self.assertFalse((dst_hub / "accounts.db-shm").exists())
            self.assertFalse((dst_broker / "token-store.seal").exists())
            self.assertEqual(stat.S_IMODE((dst_hub / "accounts.db").stat().st_mode), 0o600)
            self.assertEqual((dst_home / ".local/share/unity-keyring/credentials.json").read_text(), '{"refresh":"tok"}')

            check_conn = sqlite3.connect(str(dst_hub / "accounts.db"))
            try:
                rows = check_conn.execute("SELECT id, email FROM accounts").fetchall()
            finally:
                check_conn.close()
            self.assertEqual(rows, [("u1", "dev@example.com")])

    def test_authorize_unity_replay_error_includes_unity_auth_status_output(self):
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(".config/unityhub/accounts.db")
            payload = b"dummy"
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "unity").mkdir(parents=True)
            (state / "unity/auth-bundle.tar").write_bytes(buf.getvalue())
            machine = Instance(id="i-1", name="dev", state="running", public_ip="1.2.3.4",
                               instance_type="e5-2c-8g", provider="oci",
                               tags={"LeaseExpiresAt": "2099-01-01T00:00:00+00:00"})
            from coder_workspace.providers import Provider
            prov = Mock(spec=Provider)
            prov.name = "oci"
            prov.find_instance.return_value = machine
            prov.get_instance.return_value = machine
            lease_release = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"until": 1300, "remaining": 300.0}), stderr="")
            def fake_remote(_machine, cmd, **_kwargs):
                if cmd[:3] == ["sudo", "/usr/local/bin/workspace-lease", "release"]:
                    return lease_release
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            def fake_subproc(cmd, **_kwargs):
                if "unity auth status" in cmd[-1]:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="token decryption failed")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            with patch.object(controller, "STATE", state), \
                 patch.object(controller, "api", return_value={"id": "owner-1"}), \
                 patch.object(controller, "ensure_controller_ssh"), \
                 patch.object(controller, "renew"), \
                 patch.object(controller, "remote", side_effect=fake_remote), \
                 patch.object(controller.subprocess, "run", side_effect=fake_subproc):
                with self.assertRaisesRegex(RuntimeError, "token decryption failed"):
                    controller.authorize_unity(prov, "dev", login=False)

    def test_authorize_unity_return_clears_session_on_personal_non_returnable_and_preserves_on_other_errors(self):
        machine = Instance(id="i-1", name="dev", state="running", public_ip="1.2.3.4",
                           instance_type="e5-2c-8g", provider="oci",
                           tags={"LeaseExpiresAt": "2099-01-01T00:00:00+00:00"})
        from coder_workspace.providers import Provider
        prov = Mock(spec=Provider)
        prov.name = "oci"
        prov.find_instance.return_value = machine
        prov.get_instance.return_value = machine
        lease_release = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"until": 1300, "remaining": 300.0}), stderr="")

        def fake_remote(_machine, cmd, **_kwargs):
            if cmd[:3] == ["sudo", "/usr/local/bin/workspace-lease", "release"]:
                return lease_release
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        personal_err = subprocess.CompletedProcess(
            [], 6, stdout="",
            stderr="Error: Unable to return one or more licenses. Some licenses, such as free Personal licenses, can't be returned.",
        )
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(controller, "STATE", Path(directory)), \
             patch.object(controller, "api", return_value={"id": "owner-1"}), \
             patch.object(controller, "ensure_controller_ssh"), \
             patch.object(controller, "renew"), \
             patch.object(controller, "remote", side_effect=fake_remote) as remote_mock, \
             patch.object(controller.subprocess, "run", return_value=personal_err):
            controller.authorize_unity(prov, "dev", return_license=True)
        self.assertTrue(any("rm -rf ~/.config/unity*" in " ".join(c.args[1]) for c in remote_mock.call_args_list))

        network_err = subprocess.CompletedProcess([], 6, stdout="", stderr="Error: network timeout")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(controller, "STATE", Path(directory)), \
             patch.object(controller, "api", return_value={"id": "owner-1"}), \
             patch.object(controller, "ensure_controller_ssh"), \
             patch.object(controller, "renew"), \
             patch.object(controller, "remote", side_effect=fake_remote) as remote_mock, \
             patch.object(controller.subprocess, "run", return_value=network_err):
            with self.assertRaisesRegex(RuntimeError, "network timeout"):
                controller.authorize_unity(prov, "dev", return_license=True)
        self.assertFalse(any("rm -rf ~/.config/unity*" in " ".join(c.args[1]) for c in remote_mock.call_args_list))

    def test_load_spec_rejects_symlink_setup_script_inside_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real.sh").write_text("#!/bin/sh\ntrue\n")
            (root / "link.sh").symlink_to(root / "real.sh")
            spec = root / "demo.json"
            spec.write_text(json.dumps({"version": 1, "setup_script": "link.sh"}))
            with self.assertRaises(ValueError):
                images.load_spec(spec)

    def test_ssh_args_scopes_known_hosts_per_instance_id(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            m1 = Instance(id="ocid1.instance.one", name="a", state="running", public_ip="92.4.223.1",
                          instance_type="e5-2c-8g", provider="oci", tags={})
            m2 = Instance(id="ocid1.instance.two", name="b", state="running", public_ip="92.4.223.1",
                          instance_type="e5-2c-8g", provider="oci", tags={})
            with patch.object(controller, "STATE", state):
                args1 = controller.ssh_args(m1)
                args2 = controller.ssh_args(m2)
            kh1 = [a for a in args1 if a.startswith("UserKnownHostsFile=")][0]
            kh2 = [a for a in args2 if a.startswith("UserKnownHostsFile=")][0]
            self.assertNotEqual(kh1, kh2)
            self.assertIn("HostKeyAlias=ocid1.instance.one", args1)
            self.assertIn("HostKeyAlias=ocid1.instance.two", args2)


if __name__ == "__main__":
    unittest.main()
