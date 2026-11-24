import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lease", ROOT / "assets/coder/workspace_lease.py")
lease = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lease)


class LeaseTests(unittest.TestCase):
    def test_default_is_five_minutes_and_expiry_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-1"), \
                    patch.object(lease.time, "time", return_value=1000):
                with patch("sys.argv", ["workspace-lease", "hold"]):
                    lease.main()
                data = json.loads(state.read_text())
                self.assertEqual(data["until"], 1300)
                # Ownerless hold maps to 'legacy' so unpatched boot scripts never masquerade as user holds.
                self.assertEqual(list(data["leases"].keys()), ["legacy"])
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-1"), \
                    patch.object(lease.subprocess, "run") as run:
                with patch("sys.argv", ["workspace-lease", "tick"]):
                    with patch.object(lease.time, "time", return_value=1299):
                        lease.main()
                        run.assert_not_called()
                    with patch.object(lease.time, "time", return_value=1300):
                        lease.main()
                        run.assert_called_once_with(["systemctl", "poweroff"], check=True)
                    # A second tick after expiry never re-grants boot grace on the same boot.
                    with patch.object(lease.time, "time", return_value=1315):
                        lease.main()
                        self.assertEqual(run.call_count, 2)

    def test_new_boot_id_discards_pre_stop_holds_and_expired_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            # Case 1: long hold existed before stop; resume on a new boot must not keep it.
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-old"), \
                    patch.object(lease.time, "time", return_value=1000), \
                    patch("sys.argv", ["workspace-lease", "hold", "7200", "--owner", "hold"]):
                lease.main()
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-new"), \
                    patch.object(lease.time, "time", return_value=2000), \
                    patch.object(lease.subprocess, "run") as run:
                with patch("sys.argv", ["workspace-lease", "tick"]):
                    lease.main()
                run.assert_not_called()
                data = json.loads(state.read_text())
                self.assertEqual(data["until"], 2600)
                self.assertEqual(data["boot_id"], "boot-new")
                self.assertNotIn("hold", data["leases"])
            # Case 2: expired state from previous boot must grant boot grace on resume, not poweroff at 30s.
            state.write_text(json.dumps({"until": 1300, "idle_until": 1300, "leases": {}, "boot_id": "boot-expired"}))
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-resumed"), \
                    patch.object(lease.time, "time", return_value=5000), \
                    patch.object(lease.subprocess, "run") as run, \
                    patch("sys.argv", ["workspace-lease", "tick"]):
                lease.main()
                run.assert_not_called()
            self.assertEqual(json.loads(state.read_text())["until"], 5600)

    def test_uninitialized_tick_grants_boot_grace_that_release_boot_drops_to_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-1"), \
                    patch.object(lease.subprocess, "run") as run:
                with patch.object(lease.time, "time", return_value=1000), \
                        patch("sys.argv", ["workspace-lease", "tick"]):
                    lease.main()
                run.assert_not_called()
                self.assertEqual(json.loads(state.read_text())["until"], 1600)
                buf = io.StringIO()
                with patch.object(lease.time, "time", return_value=1060), \
                        patch("sys.argv", ["workspace-lease", "release", "--owner", "boot", "--json"]), \
                        patch("sys.stdout", buf):
                    lease.main()
                payload = json.loads(buf.getvalue())
                self.assertEqual(payload["until"], 1360)
                self.assertEqual(payload["remaining"], 300.0)

    def test_named_release_preserves_other_job_even_before_systemd_starts_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-1"), \
                    patch.object(lease.time, "time", return_value=1000):
                with patch("sys.argv", ["workspace-lease", "hold", "2700", "--owner", "job:coder-job-a"]):
                    lease.main()
                with patch("sys.argv", ["workspace-lease", "hold", "3600", "--owner", "job:coder-job-b"]):
                    lease.main()
                with patch.object(lease, "other_job_running", return_value=False), \
                        patch("sys.argv", ["workspace-lease", "release", "--owner", "job:coder-job-a"]):
                    lease.main()
                data = json.loads(state.read_text())
                self.assertEqual(data["until"], 4600)
                self.assertEqual(list(data["leases"].keys()), ["job:coder-job-b"])

    def test_releasing_job_sync_or_boot_never_shortens_explicit_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-1"), \
                    patch.object(lease.time, "time", return_value=1000):
                with patch("sys.argv", ["workspace-lease", "hold", "3600", "--owner", "hold"]):
                    lease.main()
                with patch("sys.argv", ["workspace-lease", "hold", "600", "--owner", "boot"]):
                    lease.main()
                with patch("sys.argv", ["workspace-lease", "hold", "900", "--owner", "op:sync-1"]):
                    lease.main()
                with patch("sys.argv", ["workspace-lease", "hold", "600", "--owner", "job:coder-job-1"]):
                    lease.main()
                for owner in ("boot", "op:sync-1", "job:coder-job-1"):
                    with patch.object(lease, "other_job_running", return_value=False), \
                            patch("sys.argv", ["workspace-lease", "release", "--owner", owner]):
                        lease.main()
                    self.assertEqual(json.loads(state.read_text())["until"], 4600)

    def test_legacy_interoperability_and_upgrade_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            # 1. Higher scalar 'until' written by an older helper is preserved.
            state.write_text(json.dumps({"until": 4600, "idle_until": 1400, "leases": {}, "boot_id": "boot-1"}))
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-1"), \
                    patch.object(lease.time, "time", return_value=2000), \
                    patch.object(lease.subprocess, "run") as run, \
                    patch("sys.argv", ["workspace-lease", "tick"]):
                lease.main()
                run.assert_not_called()
            self.assertEqual(json.loads(state.read_text())["until"], 4600)
            # 2. Releasing a job also clears migrated 'legacy' when no other job is running.
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-1"), \
                    patch.object(lease.time, "time", return_value=2000), \
                    patch.object(lease, "other_job_running", return_value=False), \
                    patch("sys.argv", ["workspace-lease", "release", "--owner", "job:coder-job-old"]):
                lease.main()
            self.assertEqual(json.loads(state.read_text())["until"], 2300)
            # 3. Releasing boot clears a pre-upgrade 600s boot legacy lease, but keeps a multi-hour legacy hold.
            state.write_text(json.dumps({"until": 2550}))
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-1"), \
                    patch.object(lease.time, "time", return_value=2000), \
                    patch("sys.argv", ["workspace-lease", "release", "--owner", "boot"]):
                lease.main()
            self.assertEqual(json.loads(state.read_text())["until"], 2300)
            state.write_text(json.dumps({"until": 9000}))
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-1"), \
                    patch.object(lease.time, "time", return_value=2000), \
                    patch("sys.argv", ["workspace-lease", "release", "--owner", "boot"]):
                lease.main()
            self.assertEqual(json.loads(state.read_text())["until"], 9000)

    def test_completion_returns_long_job_to_five_minute_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            state.write_text(json.dumps({"until": 10000}))
            with patch.object(lease, "STATE", state), patch.object(lease.time, "time", return_value=1000), \
                    patch.object(lease, "other_job_running", return_value=False):
                with patch("sys.argv", ["workspace-lease", "complete"]):
                    lease.main()
                self.assertEqual(json.loads(state.read_text())["until"], 1300)

    def test_completion_keeps_hold_of_a_job_that_already_started(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            state.write_text(json.dumps({"until": 10000}))
            with patch.object(lease, "STATE", state), patch.object(lease.time, "time", return_value=1000), \
                    patch.object(lease, "other_job_running", return_value=True):
                with patch("sys.argv", ["workspace-lease", "complete"]):
                    lease.main()
                self.assertEqual(json.loads(state.read_text())["until"], 10000)

    def test_legacy_complete_preserves_explicit_hold_and_fails_closed_on_systemctl_error(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-1"), \
                    patch.object(lease.time, "time", return_value=1000):
                with patch("sys.argv", ["workspace-lease", "hold", "3600", "--owner", "hold"]):
                    lease.main()
                with patch("sys.argv", ["workspace-lease", "hold", "1800", "--owner", "job:coder-job-1"]):
                    lease.main()
                with patch.object(lease, "other_job_running", return_value=False), \
                        patch("sys.argv", ["workspace-lease", "complete"]):
                    lease.main()
                data = json.loads(state.read_text())
                self.assertEqual(data["until"], 4600)
                self.assertNotIn("job:coder-job-1", data["leases"])
                self.assertIn("hold", data["leases"])
        with patch.object(lease.subprocess, "run", return_value=Mock(returncode=1, stdout="")):
            self.assertTrue(lease.other_job_running())

    def test_status_complete_owner_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            buf = io.StringIO()
            with patch.object(lease, "STATE", state), \
                    patch.object(lease, "current_boot_id", return_value="boot-1"), \
                    patch.object(lease.time, "time", return_value=1000):
                with patch("sys.argv", ["workspace-lease", "hold", "900", "--owner", "op:test"]), \
                        patch("sys.stdout", buf):
                    lease.main()
                with patch("sys.argv", ["workspace-lease", "status"]), patch("sys.stdout", buf):
                    lease.main()
                status_payload = json.loads(buf.getvalue())
                self.assertEqual(status_payload["remaining"], 900.0)
                with patch("sys.argv", ["workspace-lease", "complete", "--owner", "op:test"]):
                    lease.main()
                self.assertEqual(json.loads(state.read_text())["until"], 1300)
                with patch("sys.argv", ["workspace-lease", "release"]), self.assertRaises(SystemExit):
                    lease.main()
                with patch("sys.argv", ["workspace-lease", "hold", "300", "--owner", "bad/owner"]), \
                        self.assertRaises(SystemExit):
                    lease.main()

    def test_workspace_job_finalize_writes_atomically_and_releases_owned_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            recorded = home / "sudo-call.json"
            sudo_stub = fake_bin / "sudo"
            sudo_stub.write_text(
                f"#!{sys.executable}\nimport json, sys\n"
                f"open({str(recorded)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            )
            sudo_stub.chmod(0o755)
            env = {
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "SERVICE_RESULT": "success",
                "EXIT_CODE": "exited",
                "EXIT_STATUS": "0",
            }
            subprocess.run(
                [sys.executable, str(ROOT / "assets/coder/workspace_job.py"), "finalize", "coder-job-abc123"],
                env=env, check=True,
            )
            record = json.loads((home / ".local/state/coder-jobs/coder-job-abc123/result.json").read_text())
            self.assertEqual(record["service_result"], "success")
            self.assertFalse((home / ".local/state/coder-jobs/coder-job-abc123/result.json.tmp").exists())
            self.assertEqual(
                json.loads(recorded.read_text()),
                ["/usr/local/bin/workspace-lease", "release", "--owner", "job:coder-job-abc123"],
            )


if __name__ == "__main__":
    unittest.main()
