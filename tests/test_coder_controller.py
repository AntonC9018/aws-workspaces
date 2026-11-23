import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coder_workspace import controller
from coder_workspace.providers import AwsProvider, Instance
from coder_workspace.providers.aws import controller_policy
from coder_workspace.settings import load as load_settings


def workspace(status="running", build="build-1", template="aws-private", name="smoke"):
    return {"id": "workspace-1", "name": name, "template_name": template,
            "latest_build": {"id": build, "status": status}}


def inst(name="smoke", state="running", provider="aws", tags=None, public_ip="1.2.3.4", raw=None, id="i-owned"):
    return Instance(id=id, name=name, state=state, public_ip=public_ip,
                    instance_type="t3.small", provider=provider, tags=dict(tags or {}), raw=raw or {})


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(Path("/nonexistent/coder-test/config.json"))

    def test_project_registry_recovers_from_the_owned_instance_snapshot_tag(self):
        snapshot = "a" * 24
        machine = inst(tags={"SourceSnapshot": snapshot})
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
                controller.project_record("smoke", inst())

    def test_thread_origin_builds_a_real_environment_route(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "userdata").mkdir()
            (home / "userdata/environment-id").write_text("environment-1")
            settings = self.settings | {"thread_base_url": "https://threads.example.com"}
            with patch.dict(controller.os.environ, {"T3CODE_HOME": directory}):
                origin = controller.thread_origin(settings, "thread-1")
            self.assertEqual(origin, {"t3_thread_id": "thread-1",
                "t3_thread_url": "https://threads.example.com/environment-1/thread-1"})

    def test_thread_origin_rejects_unsafe_urls_and_invalid_ids(self):
        for thread, url in [("../wrong", "https://example.com"),
                            ("thread-1", "javascript:alert(1)"),
                            ("thread-1", "https://user:password@example.com/thread"),
                            ("thread-1", "https://example.com/" + "x" * 256)]:
            with self.subTest(thread=thread, url=url), self.assertRaises(RuntimeError):
                controller.thread_origin(self.settings, thread, url)

    def test_coder_link_does_not_require_aws_access(self):
        with patch.object(controller, "api", return_value={"owner_name": "a user"}), \
                patch("builtins.print") as output:
            controller.show_links("smoke", self.settings, AwsProvider.filter_hint)
        self.assertIn("http://127.0.0.1:7080/@a%20user/smoke", output.call_args_list[0].args[0])

    def test_only_new_successful_owned_template_builds_connect(self):
        self.assertTrue(controller.should_connect(workspace(), {}))
        self.assertTrue(controller.should_connect(workspace(template="oci-private"), {}))
        self.assertFalse(controller.should_connect(workspace(), {"build": "build-1"}))
        self.assertTrue(controller.should_connect(workspace(), {"build": "build-1"}, bridge_alive=False))
        self.assertFalse(controller.should_connect(workspace(status="starting"), {}))
        self.assertFalse(controller.should_connect(workspace(template="other"), {}))

    def test_repeated_reconciliation_does_not_renew_idle_lease(self):
        from coder_workspace.providers import Provider
        prov = Mock(spec=Provider)
        prov.name = "aws"
        prov.template_name = "aws-private"
        prov.find_instance.return_value = inst(state="running")
        prov.list_instances.return_value = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(controller, "STATE", Path(directory)), \
                patch.object(controller, "api", side_effect=[
                    {"workspaces": [workspace()]}, {"id": "owner-1"},
                    {"workspaces": [workspace()]}, {"id": "owner-1"},
                ]), \
                patch.object(controller, "connect") as connect, \
                patch.object(controller.subprocess, "run", return_value=Mock(returncode=0)):
            controller.reconcile(self.settings, providers=[prov])
            controller.reconcile(self.settings, providers=[prov])
            connect.assert_called_once()

    def test_reconciliation_never_restarts_an_externally_stopped_worker(self):
        prov = Mock(name="aws")
        prov.name = "aws"
        prov.template_name = "aws-private"
        prov.find_instance.return_value = inst(state="stopped")
        prov.list_instances.return_value = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(controller, "STATE", Path(directory)), \
                patch.object(controller, "api", side_effect=[
                    {"workspaces": [workspace()]}, {"id": "owner-1"},
                ]), \
                patch.object(controller, "connect") as connect:
            controller.reconcile(self.settings, providers=[prov])
            connect.assert_not_called()

    def test_upgrade_applies_active_template_without_starting_worker(self):
        prov = Mock()
        prov.find_instance.return_value = inst(state="stopped")
        with patch.object(controller, "api", side_effect=[{"id": "owner-1"}, {"latest_build": {"job": {"status": "failed"}}}]), \
                patch.object(controller, "transition") as transition:
            controller.upgrade(prov, "smoke")
            transition.assert_called_once_with("smoke", "stop")

    def test_upgrade_rejects_running_worker_or_in_progress_build(self):
        for state, job in (("running", "succeeded"), ("stopped", "running")):
            prov = Mock()
            prov.find_instance.return_value = inst(state=state)
            with self.subTest(state=state, job=job), \
                    patch.object(controller, "api", side_effect=[{"id": "owner-1"}, {"latest_build": {"job": {"status": job}}}]), \
                    patch.object(controller, "transition") as transition:
                with self.assertRaises(RuntimeError):
                    controller.upgrade(prov, "smoke")
                transition.assert_not_called()

    def test_controller_can_terminate_only_owned_instances_without_iam_authority(self):
        policy = controller_policy(self.settings, "123456789012", {"vpc": "vpc-owned", "ids": ["subnet-owned"]})
        allowed = [a for s in policy["Statement"] if s["Effect"] == "Allow"
                   for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
        termination = [s for s in policy["Statement"]
                       if s["Effect"] == "Allow" and "ec2:TerminateInstances" in s["Action"]]
        self.assertEqual(len(termination), 1)
        self.assertEqual(termination[0]["Resource"], "arn:aws:ec2:eu-central-1:123456789012:instance/*")
        self.assertEqual(termination[0]["Condition"], {"StringEquals": {"ec2:ResourceTag/ManagedBy": "coder-workspaces"}})
        self.assertFalse(any(a.startswith("iam:") or a == "*" for a in allowed))
        self.assertNotIn("freetier:GetAccountPlanState", allowed)

    def test_aws_ensure_ssh_access_updates_stale_cidr(self):
        ec2 = Mock()
        ec2.describe_security_groups.return_value = {"SecurityGroups": [{
            "GroupId": "sg-123",
            "Tags": [{"Key": "ManagedBy", "Value": "coder-workspaces"}],
            "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                               "IpRanges": [{"CidrIp": "198.51.100.1/32"}]}],
        }]}
        session = Mock()
        session.client.return_value = ec2
        prov = AwsProvider(self.settings, session=session)
        machine = inst(raw={"SecurityGroups": [{"GroupId": "sg-123"}]})
        self.assertTrue(prov.ensure_ssh_access(machine, "203.0.113.9/32"))
        ec2.authorize_security_group_ingress.assert_called_once()
        ec2.revoke_security_group_ingress.assert_called_once()

    def test_aws_assert_guard_blocks_create_during_aws_guard_lockdown(self):
        with tempfile.TemporaryDirectory() as directory:
            lockdown_file = Path(directory) / "lockdown.json"
            lockdown_file.write_text("{}")
            prov = AwsProvider(self.settings, session=Mock())
            with patch("coder_workspace.providers.aws.AWS_GUARD_LOCKDOWN", lockdown_file):
                with self.assertRaises(RuntimeError) as ctx:
                    prov.assert_guard(check_create=True)
            self.assertIn("lockdown", str(ctx.exception))

    def test_delete_refuses_running_job_unless_forced_and_cleans_local_state(self):
        prov = Mock()
        prov.name = "aws"
        prov.list_instances.return_value = [inst(state="running")]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(controller, "STATE", Path(directory)), \
                patch.object(controller, "api", side_effect=[{"id": "owner-1"}, {"id": "ws-1"}]), \
                patch.object(controller, "ensure_controller_ssh"), \
                patch.object(controller, "active_jobs", return_value=["coder-job-abc.service"]):
            with self.assertRaises(RuntimeError):
                controller.delete_workspace(prov, "smoke", force=False)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "manifests").mkdir()
            (state / "manifests/smoke.json").write_text("{}")
            (state / "smoke-build.log").write_text("log")
            with patch.object(controller, "STATE", state), \
                    patch.object(controller, "api", side_effect=[{"id": "owner-1"}, {"id": "ws-1"}]), \
                    patch.object(controller, "run") as run_mock, \
                    patch.object(controller.subprocess, "run"):
                controller.delete_workspace(prov, "smoke", force=True)
            run_mock.assert_called_once_with(["coder", "delete", "smoke", "--yes"])
            self.assertFalse((state / "manifests/smoke.json").exists())
            self.assertFalse((state / "smoke-build.log").exists())

    def test_gc_stops_expired_marks_stopped_and_deletes_stale_non_kept_workspaces(self):
        now = dt.datetime.now(dt.timezone.utc)
        old_stop = (now - dt.timedelta(hours=30)).isoformat()
        expired_running = inst(id="i-1", name="run-exp", state="running", tags={"LeaseExpiresAt": "2020-01-01T00:00:00+00:00"})
        fresh_stopped = inst(id="i-2", name="stop-new", state="stopped", tags={})
        stale_stopped = inst(id="i-3", name="stop-old", state="stopped", tags={"StoppedSince": old_stop})
        kept_stopped = inst(id="i-4", name="stop-keep", state="stopped", tags={"StoppedSince": old_stop, "KeepWorkspace": "true"})
        prov = Mock()
        prov.name = "oci"
        prov.template_name = "oci-private"
        prov.list_instances.return_value = [expired_running, fresh_stopped, stale_stopped, kept_stopped]
        prov.get_instance.side_effect = lambda iid: next(
            i for i in [expired_running, fresh_stopped, stale_stopped, kept_stopped] if i.id == iid
        )
        ws_list = {"workspaces": [
            workspace(name="stop-old", template="oci-private"),
            workspace(name="stop-keep", template="oci-private"),
        ]}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(controller, "STATE", Path(directory)), \
                patch.object(controller, "api", side_effect=[{"id": "owner-1"}, ws_list]), \
                patch.object(controller, "run") as run_mock, \
                patch.object(controller, "notify") as notify_mock, \
                patch.object(controller.subprocess, "run"):
            result = controller.gc_workspaces([prov], self.settings)
        self.assertEqual(result["stopped"], ["oci:run-exp"])
        self.assertEqual(result["marked"], ["oci:stop-new"])
        self.assertEqual(result["deleted"], ["oci:stop-old"])
        prov.stop_instance.assert_called_once_with(expired_running)
        prov.set_tags.assert_called_once()
        run_mock.assert_called_once_with(["coder", "delete", "stop-old", "--yes"])
        notify_mock.assert_called_once()

    def test_gc_does_not_delete_other_provider_workspace_on_name_collision(self):
        old_stop = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)).isoformat()
        aws_orphan = inst(name="dev", state="stopped", tags={"StoppedSince": old_stop})
        prov = Mock()
        prov.name = "aws"
        prov.template_name = "aws-private"
        prov.list_instances.return_value = [aws_orphan]
        prov.get_instance.return_value = aws_orphan
        ws_list = {"workspaces": [workspace(name="dev", template="oci-private")]}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(controller, "STATE", Path(directory)), \
                patch.object(controller, "api", side_effect=[{"id": "owner-1"}, ws_list]), \
                patch.object(controller, "run") as run_mock:
            result = controller.gc_workspaces([prov], self.settings)
        self.assertEqual(result["deleted"], [])
        run_mock.assert_not_called()

    def test_reconcile_skips_bake_builders_and_runs_gc_despite_connect_error(self):
        from coder_workspace.providers import Provider
        prov = Mock(spec=Provider)
        prov.name = "oci"
        prov.template_name = "oci-private"
        prov.find_instance.side_effect = RuntimeError("missing instance")
        prov.list_instances.return_value = []
        ws_list = {"workspaces": [
            workspace(name="bake-deadbeef-1234", template="oci-private"),
            workspace(name="broken-ws", template="oci-private"),
        ]}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(controller, "STATE", Path(directory)), \
                patch.object(controller, "api", side_effect=[ws_list, {"id": "owner-1"}]), \
                patch.object(controller, "gc_workspaces") as gc_mock:
            controller.reconcile(self.settings, providers=[prov])
        prov.find_instance.assert_called_once_with("broken-ws", "owner-1")
        gc_mock.assert_called_once()

    def test_wait_for_job_returns_exit_code_when_job_completes(self):
        prov = Mock()
        machine = inst(state="running")
        with patch.object(controller, "ensure_controller_ssh"), \
                patch.object(controller, "active_jobs", side_effect=[["coder-job-1.service"], []]), \
                patch.object(controller, "read_job_logs", return_value=({"job": "coder-job-1", "exit_status": 0}, "ok\n")), \
                patch.object(controller.time, "sleep"):
            code = controller.wait_for_job(prov, "smoke", machine, job_id="coder-job-1.service", poll=1)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
