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
            prov = Mock()
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


if __name__ == "__main__":
    unittest.main()
