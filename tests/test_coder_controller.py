import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

exceptions = types.ModuleType("botocore.exceptions")
exceptions.ClientError = type("ClientError", (Exception,), {})
spec = importlib.util.spec_from_file_location(
    "controller", Path(__file__).resolve().parents[1] / "scripts/coder-aws.py")
controller = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
with patch.dict(os.environ, {"CODER_WORKSPACES_CONFIG": "/nonexistent/coder-test/config.json"}), patch.dict(sys.modules, {"boto3": types.ModuleType("boto3"),
                             "botocore": types.ModuleType("botocore"), "botocore.exceptions": exceptions}):
    spec.loader.exec_module(controller)


def workspace(status="running", build="build-1", template="aws-private"):
    return {"id": "workspace-1", "name": "smoke", "template_name": template,
            "latest_build": {"id": build, "status": status}}


class ControllerTests(unittest.TestCase):
    def test_project_registry_recovers_from_the_owned_instance_snapshot_tag(self):
        snapshot = "a" * 24
        machine = {"InstanceId": "i-owned", "Tags": [{"Key": "SourceSnapshot", "Value": snapshot}]}
        plan = {"snapshot": snapshot, "projects": [{"name": "main", "destination": "src/main"}]}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(controller, "STATE", Path(directory)), \
                patch.object(controller, "remote", return_value=Mock(stdout=json.dumps(plan))):
            record = controller.project_record("smoke", machine)
        self.assertEqual(record["projects"]["main"], "/home/ubuntu/projects/snapshots/" + snapshot + "/src/main")

    def test_project_registry_rejects_a_different_instance(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(controller, "STATE", Path(directory)):
            path = Path(directory) / "manifests"
            path.mkdir()
            (path / "smoke.json").write_text(json.dumps({"instance_id": "i-other"}))
            with self.assertRaises(RuntimeError):
                controller.project_record("smoke", {"InstanceId": "i-owned"})

    def test_thread_origin_builds_a_real_environment_route(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "userdata").mkdir()
            (home / "userdata/environment-id").write_text("environment-1")
            with patch.dict(controller.os.environ, {"T3CODE_HOME": directory}), \
                 patch.dict(controller.SETTINGS, {"thread_base_url": "https://threads.example.com"}):
                origin = controller.thread_origin("thread-1")
            self.assertEqual(origin, {"t3_thread_id": "thread-1",
                "t3_thread_url": "https://threads.example.com/environment-1/thread-1"})

    def test_thread_origin_rejects_unsafe_urls_and_invalid_ids(self):
        for thread, url in [("../wrong", "https://example.com"),
                            ("thread-1", "javascript:alert(1)"),
                            ("thread-1", "https://user:password@example.com/thread"),
                            ("thread-1", "https://example.com/" + "x" * 256)]:
            with self.subTest(thread=thread, url=url), self.assertRaises(RuntimeError):
                controller.thread_origin(thread, url)

    def test_coder_link_does_not_require_aws_access(self):
        with patch.object(controller, "api", return_value={"owner_name": "a user"}), \
                patch("builtins.print") as output:
            controller.show_links("smoke")
        self.assertIn("http://127.0.0.1:7080/@a%20user/smoke", output.call_args_list[0].args[0])

    def test_only_new_successful_owned_template_builds_connect(self):
        self.assertTrue(controller.should_connect(workspace(), {}))
        self.assertFalse(controller.should_connect(workspace(), {"build": "build-1"}))
        self.assertTrue(controller.should_connect(workspace(), {"build": "build-1"}, bridge_alive=False))
        self.assertFalse(controller.should_connect(workspace(status="starting"), {}))
        self.assertFalse(controller.should_connect(workspace(template="other"), {}))

    def test_repeated_reconciliation_does_not_renew_idle_lease(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(controller, "STATE", Path(directory)), \
                patch.object(controller, "api", return_value={"workspaces": [workspace()]}), \
                patch.object(controller, "aws_session"), patch.object(controller, "check_plan"), \
                patch.object(controller, "instance", return_value={"InstanceId": "i-owned", "State": {"Name": "running"}}), \
                patch.object(controller, "connect") as connect, \
                patch.object(controller.subprocess, "run", return_value=Mock(returncode=0)):
            controller.reconcile()
            controller.reconcile()
            connect.assert_called_once()

    def test_reconciliation_never_restarts_an_externally_stopped_worker(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(controller, "STATE", Path(directory)), \
                patch.object(controller, "api", return_value={"workspaces": [workspace()]}), \
                patch.object(controller, "aws_session"), patch.object(controller, "check_plan"), \
                patch.object(controller, "instance", return_value={"State": {"Name": "stopped"}}), \
                patch.object(controller, "connect") as connect:
            controller.reconcile()
            connect.assert_not_called()

    def test_upgrade_applies_active_template_without_starting_worker(self):
        with patch.object(controller, "instance", return_value={"State": {"Name": "stopped"}}), \
                patch.object(controller, "api", return_value={"latest_build": {"job": {"status": "failed"}}}), \
                patch.object(controller, "transition") as transition:
            controller.upgrade(Mock(), "smoke")
            transition.assert_called_once_with("smoke", "stop")

    def test_upgrade_rejects_running_worker_or_in_progress_build(self):
        for state, job in (("running", "succeeded"), ("stopped", "running")):
            with self.subTest(state=state, job=job), \
                    patch.object(controller, "instance", return_value={"State": {"Name": state}}), \
                    patch.object(controller, "api", return_value={"latest_build": {"job": {"status": job}}}), \
                    patch.object(controller, "transition") as transition:
                with self.assertRaises(RuntimeError):
                    controller.upgrade(Mock(), "smoke")
                transition.assert_not_called()

    def test_controller_can_terminate_only_owned_instances_without_iam_authority(self):
        policy = controller.controller_policy("123456789012", {"vpc": "vpc-owned", "ids": ["subnet-owned"]})
        allowed = [a for s in policy["Statement"] if s["Effect"] == "Allow"
                   for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
        termination = [s for s in policy["Statement"]
                       if s["Effect"] == "Allow" and "ec2:TerminateInstances" in s["Action"]]
        self.assertEqual(len(termination), 1)
        self.assertEqual(termination[0]["Resource"],
                         "arn:aws:ec2:eu-central-1:123456789012:instance/*")
        self.assertEqual(termination[0]["Condition"],
                         {"StringEquals": {"ec2:ResourceTag/ManagedBy": "coder-workspaces"}})
        self.assertFalse(any(a.startswith("iam:") or a == "*" for a in allowed))
        removal = next(s for s in policy["Statement"]
                       if s["Effect"] == "Allow" and "ec2:DeleteTags" in s["Action"])
        self.assertEqual(removal["Condition"],
                         {"StringEquals": {"ec2:ResourceTag/ManagedBy": "coder-workspaces"}})
        ownership = next(s for s in policy["Statement"]
                         if s["Effect"] == "Deny" and s["Action"] == "ec2:DeleteTags")
        self.assertEqual(ownership["Condition"],
                         {"ForAnyValue:StringEquals": {"aws:TagKeys": "ManagedBy"}})
        runtime = next(s for s in policy["Statement"] if "ec2:StopInstances" in s["Action"])
        self.assertEqual(runtime["Condition"]["StringEquals"]["ec2:ResourceTag/ManagedBy"], "coder-workspaces")


if __name__ == "__main__":
    unittest.main()
