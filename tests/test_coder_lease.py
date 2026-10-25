import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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

    def test_completion_returns_long_job_to_five_minute_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "lease.json"
            state.write_text(json.dumps({"until": 10000}))
            with patch.object(lease, "STATE", state), patch.object(lease.time, "time", return_value=1000):
                with patch("sys.argv", ["workspace-lease", "complete"]):
                    lease.main()
                self.assertEqual(json.loads(state.read_text())["until"], 1300)


if __name__ == "__main__":
    unittest.main()
