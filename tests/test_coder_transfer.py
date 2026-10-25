import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "transfer", Path(__file__).resolve().parents[1] / "scripts/coder_workspace/transfer.py")
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


class TransferTests(unittest.TestCase):
    def fixture(self, root):
        source = root / "source"
        source.mkdir()
        transfer.git(source, "init", "--quiet")
        (source / "file.txt").write_text("committed\n")
        (source / "binary").write_bytes(bytes(range(256)))
        (source / ".gitignore").write_text("out/\n")
        transfer.git(source, "add", ".")
        transfer.git(source, "commit", "--quiet", "-m", "test: source fixture", "-m", "Generated-by: GPT 6.1 Sol")
        (source / "file.txt").write_text("local changes\n")
        (source / "binary").write_bytes(b"\x00changed\xff")
        (source / "included.txt").write_text("explicit input\n")
        (source / "excluded.txt").write_text("not selected\n")
        manifest = root / "workspace.json"
        (root / "build-config.txt").write_text("shared configuration\n")
        manifest.write_text(json.dumps({"version": 1, "projects": [
            {"name": "main", "path": "source", "destination": "src/main",
             "include_untracked": ["included.txt"], "artifacts": ["out", "missing-output"]}],
            "files": [{"source": "build-config.txt", "destination": "shared/config.txt"}]}))
        return source, manifest

    def test_round_trip_preserves_commits_dirty_files_artifacts_and_local_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest = self.fixture(root)
            refs = transfer.git(source, "show-ref")
            plan = transfer.pack(manifest, root / "input")
            self.assertEqual(transfer.git(source, "show-ref"), refs)
            home = root / "remote"
            home.mkdir()
            with patch.object(transfer.Path, "home", return_value=home):
                projects = transfer.prepare(root / "input")
                remote = Path(projects["main"])
                self.assertEqual((remote / "file.txt").read_text(), "local changes\n")
                self.assertEqual((remote / "binary").read_bytes(), b"\x00changed\xff")
                self.assertFalse((remote / "excluded.txt").exists())
                transfer.git(remote, "add", "file.txt", "binary", "included.txt")
                transfer.git(remote, "commit", "--quiet", "-m", "test: remote commit", "-m", "Generated-by: GPT 6.1 Sol")
                (remote / "file.txt").write_text("remote uncommitted\n")
                (remote / "new.txt").write_text("new remote source\n")
                (remote / "out").mkdir()
                (remote / "out/build.bin").write_bytes(b"artifact\x00")
                (home / "projects/snapshots" / plan["snapshot"] / "shared/config.txt").write_text("remote configuration\n")
                result = Path(transfer.collect(plan["snapshot"]))
                (source / "file.txt").write_text("concurrent local edit\n")
                restored = transfer.restore(result)
                self.assertIn("missing-output", restored["projects"][0]["missing_artifacts"])
                self.assertEqual((result / "checkouts/main/file.txt").read_text(), "remote uncommitted\n")
                self.assertEqual((result / "checkouts/main/new.txt").read_text(), "new remote source\n")
                self.assertEqual((result / "main/artifacts/out/build.bin").read_bytes(), b"artifact\x00")
                self.assertEqual((result / "extra/shared/config.txt").read_text(), "remote configuration\n")
                self.assertEqual((source / "file.txt").read_text(), "concurrent local edit\n")
                with self.assertRaises(ValueError):
                    transfer.prepare(root / "input")

    def test_selected_revision_is_not_replaced_by_the_current_clean_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest = self.fixture(root)
            first = transfer.git(source, "rev-parse", "HEAD").decode().strip()
            transfer.git(source, "add", "file.txt", "binary")
            transfer.git(source, "commit", "--quiet", "-m", "test: later revision", "-m", "Generated-by: GPT 6.1 Sol")
            config = json.loads(manifest.read_text())
            config["projects"][0]["revision"] = first
            manifest.write_text(json.dumps(config))
            plan = transfer.pack(manifest, root / "input")
            home = root / "remote"
            home.mkdir()
            with patch.object(transfer.Path, "home", return_value=home):
                project = Path(transfer.prepare(root / "input")["main"])
            self.assertEqual((project / "file.txt").read_text(), "committed\n")
            self.assertEqual(plan["projects"][0]["commit"], first)

    def test_rejects_path_traversal_symlinks_and_overlapping_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest = self.fixture(root)
            with self.assertRaises(ValueError):
                transfer.contained(source, "../outside")
            (source / "outside").symlink_to(root)
            with self.assertRaises(ValueError):
                transfer.selected_files(source, ["outside/workspace.json"])
            config = json.loads(manifest.read_text())
            config["files"] = [{"source": "workspace.json", "destination": "src/main/config.json"}]
            manifest.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                transfer.inventory(manifest)

    def test_rejects_archive_git_metadata_and_hash_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.tar"
            with tarfile.open(archive, "w") as output:
                info = tarfile.TarInfo(".git/config")
                info.size = 4
                output.addfile(info, io.BytesIO(b"evil"))
            with self.assertRaises(ValueError):
                transfer.extract_files(archive, root / "destination")
            source, manifest = self.fixture(root)
            transfer.pack(manifest, root / "input")
            (root / "input/main/changes.patch").write_text("corrupt")
            home = root / "remote"
            home.mkdir()
            with patch.object(transfer.Path, "home", return_value=home), self.assertRaises(ValueError):
                transfer.prepare(root / "input")


if __name__ == "__main__":
    unittest.main()
