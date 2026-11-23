import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coder_workspace.providers.oci import OciProvider
from coder_workspace.settings import load as load_settings


class OciProviderTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(Path("/nonexistent/coder-test/config.json"))

    def _make_provider(self, directory, overrides=None, clients=None):
        state = Path(directory)
        (state / "oci-infra.json").write_text(json.dumps({
            "tenancy_id": "ocid1.tenancy.oc1..test",
            "compartment_id": "ocid1.compartment.oc1..test",
            "vcn_id": "ocid1.vcn.oc1..test",
            "subnet_id": "ocid1.subnet.oc1..test",
            "availability_domain": "AD-1",
            "region": "eu-turin-1",
            "profile": "AWS_INFRA",
        }))
        prov = OciProvider(self.settings | (overrides or {}), state_dir=state, clients=clients or {})
        prov._config = {"tenancy": "ocid1.tenancy.oc1..test", "region": "eu-turin-1"}
        return prov

    def test_trial_expiry_blocks_paid_shapes_but_allows_always_free(self):
        limits = Mock()
        limits.get_resource_availability.return_value = types.SimpleNamespace(
            data=types.SimpleNamespace(available=200, effective_quota_value=200))
        with tempfile.TemporaryDirectory() as directory:
            prov = self._make_provider(directory, {"oci_trial_expires": "2020-01-01"}, clients={"limits": limits})
            with self.assertRaises(RuntimeError) as ctx:
                prov.assert_guard(instance_type="e5-4c-16g", check_create=True)
            self.assertIn("trial cutoff", str(ctx.exception))
            # Always Free shape remains allowed after trial expiry.
            prov.assert_guard(instance_type="a1-2c-12g", check_create=True)

    def test_spend_limit_blocks_paid_shapes(self):
        usage = Mock()
        usage.request_summarized_usages.return_value = types.SimpleNamespace(
            data=types.SimpleNamespace(items=[types.SimpleNamespace(computed_amount=260.0)]))
        with tempfile.TemporaryDirectory() as directory:
            prov = self._make_provider(
                directory,
                {"oci_trial_expires": "2099-01-01", "oci_trial_spend_limit_usd": "250"},
                clients={"usage": usage},
            )
            with self.assertRaises(RuntimeError) as ctx:
                prov.assert_guard(instance_type="e5-2c-8g", check_create=True)
            self.assertIn("oci_trial_spend_limit_usd", str(ctx.exception))

    def test_quota_preflight_reports_zero_e5_quota_clearly(self):
        usage = Mock()
        usage.request_summarized_usages.return_value = types.SimpleNamespace(
            data=types.SimpleNamespace(items=[]))
        limits = Mock()
        limits.get_resource_availability.return_value = types.SimpleNamespace(
            data=types.SimpleNamespace(available=0, effective_quota_value=0))
        with tempfile.TemporaryDirectory() as directory:
            prov = self._make_provider(
                directory,
                {"oci_trial_expires": "2099-01-01"},
                clients={"usage": usage, "limits": limits},
            )
            with self.assertRaises(RuntimeError) as ctx:
                prov.assert_guard(instance_type="e5-2c-8g", check_create=True)
            self.assertIn("standard-e5-core-count", str(ctx.exception))
            self.assertIn("quota policy", str(ctx.exception))
            # Resume skips quota double-counting (check_quota=False) while still running spend/expiry checks.
            prov.assert_guard(instance_type="e5-2c-8g", check_create=True, check_quota=False)

    def test_quota_preflight_catches_zero_regional_a1_quota(self):
        limits = Mock()
        def avail_for(**kwargs):
            val = 0 if "regional" in kwargs["limit_name"] else 200
            return types.SimpleNamespace(data=types.SimpleNamespace(available=val, effective_quota_value=val))
        limits.get_resource_availability.side_effect = avail_for
        with tempfile.TemporaryDirectory() as directory:
            prov = self._make_provider(directory, clients={"limits": limits})
            with self.assertRaises(RuntimeError) as ctx:
                prov.assert_guard(instance_type="a1-2c-12g", check_create=True)
            self.assertIn("standard-a1-core-regional-count", str(ctx.exception))
            self.assertIn("quota policy", str(ctx.exception))

    def test_nsg_ssh_reconciliation_updates_stale_cidr_even_when_current_exists(self):
        old_rule = types.SimpleNamespace(
            id="rule-old",
            protocol="6",
            source="198.51.100.1/32",
            tcp_options=types.SimpleNamespace(
                destination_port_range=types.SimpleNamespace(min=22, max=22)),
        )
        cur_rule = types.SimpleNamespace(
            id="rule-cur",
            protocol="6",
            source="203.0.113.5/32",
            tcp_options=types.SimpleNamespace(
                destination_port_range=types.SimpleNamespace(min=22, max=22)),
        )
        network = Mock()
        network.list_network_security_group_security_rules.return_value = types.SimpleNamespace(
            data=[old_rule, cur_rule])
        with tempfile.TemporaryDirectory() as directory:
            prov = self._make_provider(directory, clients={"network": network})
            from coder_workspace.providers.base import Instance
            machine = Instance(
                id="ocid1.instance.oc1..test", name="oci-smoke", state="running",
                public_ip="129.152.0.1", instance_type="a1-2c-12g", provider="oci",
                raw={"nsg_ids": ["ocid1.networksecuritygroup.oc1..test"]},
            )
            self.assertTrue(prov.ensure_ssh_access(machine, "203.0.113.5/32"))
        network.add_network_security_group_security_rules.assert_not_called()
        network.remove_network_security_group_security_rules.assert_called_once()

    def test_teardown_network_refuses_external_subnet(self):
        with tempfile.TemporaryDirectory() as directory:
            prov = self._make_provider(
                directory,
                {"oci_subnet_id": "ocid1.subnet.oc1.eu-turin-1.aaaaaaaa123456"},
            )
            with self.assertRaises(RuntimeError):
                prov.teardown_network()


if __name__ == "__main__":
    unittest.main()
