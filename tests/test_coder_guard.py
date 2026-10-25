import datetime as dt
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault("boto3", types.ModuleType("boto3"))
spec = importlib.util.spec_from_file_location("guard", ROOT / "assets/coder/stop_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def machine(deadline=None, manager="coder-workspaces"):
    tags = [{"Key": "ManagedBy", "Value": manager}]
    if deadline is not None:
        tags.append({"Key": "LeaseExpiresAt", "Value": deadline})
    return {"InstanceId": "i-owned", "State": {"Name": "running"}, "Tags": tags}


class DeadlineTests(unittest.TestCase):
    now = dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc)

    def test_only_owned_expired_instances_stop(self):
        self.assertTrue(guard.expired(machine("2026-10-24T00:00:00Z"), self.now))
        self.assertFalse(guard.expired(machine("2026-10-26T00:00:00Z"), self.now))
        self.assertFalse(guard.expired(machine(manager="someone-else"), self.now))

    def test_missing_invalid_or_naive_deadline_fails_closed(self):
        for deadline in (None, "bad", "2026-10-24T00:00:00"):
            with self.subTest(deadline=deadline):
                self.assertTrue(guard.expired(machine(deadline), self.now))

    def test_renewal_seen_by_second_read_prevents_stop(self):
        ec2 = Mock()
        ec2.get_paginator.return_value.paginate.return_value = [
            {"Reservations": [{"Instances": [machine("2020-01-01T00:00:00Z")]}]}]
        ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [machine("2099-01-01T00:00:00Z")]}]}
        with patch.object(guard.boto3, "client", return_value=ec2, create=True):
            self.assertEqual(guard.handler({}, None), {"stopped": []})
        ec2.stop_instances.assert_not_called()

    def test_expired_owned_instance_is_stopped(self):
        ec2 = Mock()
        expired = {"Reservations": [{"Instances": [machine("2020-01-01T00:00:00Z")]}]}
        ec2.get_paginator.return_value.paginate.return_value = [expired]
        ec2.describe_instances.return_value = expired
        with patch.object(guard.boto3, "client", return_value=ec2, create=True):
            self.assertEqual(guard.handler({}, None), {"stopped": ["i-owned"]})
        ec2.stop_instances.assert_called_once_with(InstanceIds=["i-owned"])


if __name__ == "__main__":
    unittest.main()
