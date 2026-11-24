import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "lease", Path(__file__).resolve().parents[1] / "assets/coder/workspace_lease.py")
lease = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lease)


class LeaseTests(unittest.TestCase):
    def test_default_is_five_minutes_and_expiry_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            with patch.object(lease, "STATE", state), patch.object(lease.time, "time", return_value=1000):
                with patch("sys.argv", ["workspace-lease", "hold"]):
                    lease.main()
                self.assertEqual(json.loads(state.read_text())["until"], 1300)
            with patch.object(lease, "STATE", state), patch.object(lease.subprocess, "run") as run:
                with patch("sys.argv", ["workspace-lease", "tick"]):
                    with patch.object(lease.time, "time", return_value=1299):
                        lease.main()
                        run.assert_not_called()
                    with patch.object(lease.time, "time", return_value=1300):
                        lease.main()
                        run.assert_called_once_with(["systemctl", "poweroff"], check=True)
                    # A second tick after expiry never re-grants boot grace.
                    with patch.object(lease.time, "time", return_value=1315):
                        lease.main()
                        self.assertEqual(run.call_count, 2)

    def test_uninitialized_tick_grants_boot_grace_that_release_boot_drops_to_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            with patch.object(lease, "STATE", state), patch.object(lease.subprocess, "run") as run:
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
            with patch.object(lease, "STATE", state), patch.object(lease.time, "time", return_value=1000):
                with patch("sys.argv", ["workspace-lease", "hold", "2700", "--owner", "job:coder-job-a"]):
                    lease.main()
                with patch("sys.argv", ["workspace-lease", "hold", "3600", "--owner", "job:coder-job-b"]):
                    lease.main()
                # Even if systemd reports no running job yet (pre-launch window), releasing job-a
                # removes only job-a and leaves job-b intact.
                with patch.object(lease, "other_job_running", return_value=False), \
                        patch("sys.argv", ["workspace-lease", "release", "--owner", "job:coder-job-a"]):
                    lease.main()
                data = json.loads(state.read_text())
                self.assertEqual(data["until"], 4600)
                self.assertEqual(list(data["leases"].keys()), ["job:coder-job-b"])

    def test_releasing_job_sync_or_boot_never_shortens_explicit_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            with patch.object(lease, "STATE", state), patch.object(lease.time, "time", return_value=1000):
                with patch("sys.argv", ["workspace-lease", "hold", "3600", "--owner", "hold"]):
                    lease.main()
                with patch("sys.argv", ["workspace-lease", "hold", "600", "--owner", "boot"]):
                    lease.main()
                with patch("sys.argv", ["workspace-lease", "hold", "900", "--owner", "op:sync-1"]):
                    lease.main()
                with patch("sys.argv", ["workspace-lease", "hold", "600", "--owner", "job:coder-job-1"]):
                    lease.main()
                for owner in ("boot", "op:sync-1", "job:coder-job-1"):
                    with patch("sys.argv", ["workspace-lease", "release", "--owner", owner]):
                        lease.main()
                    self.assertEqual(json.loads(state.read_text())["until"], 4600)

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
            with patch.object(lease, "STATE", state), patch.object(lease.time, "time", return_value=1000):
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


if __name__ == "__main__":
    unittest.main()
