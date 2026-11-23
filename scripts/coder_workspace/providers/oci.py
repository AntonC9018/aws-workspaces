"""Oracle Cloud Infrastructure (OCI) provider backend with trial & quota safeguards."""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path
import time

from .base import Instance, Provider

OCI_SHAPES = {
    "a1-2c-12g": {"shape": "VM.Standard.A1.Flex", "ocpus": 2, "memory_gb": 12, "arch": "arm64",
                  "always_free": True, "core_limit": "standard-a1-core-count", "mem_limit": "standard-a1-memory-count",
                  "regional_core_limit": "standard-a1-core-regional-count", "regional_mem_limit": "standard-a1-memory-regional-count"},
    "a1-4c-24g": {"shape": "VM.Standard.A1.Flex", "ocpus": 4, "memory_gb": 24, "arch": "arm64",
                  "always_free": False, "core_limit": "standard-a1-core-count", "mem_limit": "standard-a1-memory-count",
                  "regional_core_limit": "standard-a1-core-regional-count", "regional_mem_limit": "standard-a1-memory-regional-count"},
    "e5-2c-8g": {"shape": "VM.Standard.E5.Flex", "ocpus": 2, "memory_gb": 8, "arch": "amd64",
                 "always_free": False, "core_limit": "standard-e5-core-count", "mem_limit": "standard-e5-memory-count"},
    "e5-4c-16g": {"shape": "VM.Standard.E5.Flex", "ocpus": 4, "memory_gb": 16, "arch": "amd64",
                  "always_free": False, "core_limit": "standard-e5-core-count", "mem_limit": "standard-e5-memory-count"},
    "e5-8c-32g": {"shape": "VM.Standard.E5.Flex", "ocpus": 8, "memory_gb": 32, "arch": "amd64",
                  "always_free": False, "core_limit": "standard-e5-core-count", "mem_limit": "standard-e5-memory-count"},
}
BOOT_VOLUME_GB = 50
STATE_DIR = Path.home() / ".local/state/coder-workspaces"


def _normalize_state(raw_state: str) -> str:
    return {
        "PROVISIONING": "starting",
        "STARTING": "starting",
        "RUNNING": "running",
        "STOPPING": "stopping",
        "STOPPED": "stopped",
    }.get(raw_state, raw_state.lower())


class OciProvider(Provider):
    name = "oci"
    template_name = "oci-private"
    display_name = "OCI private workspace"
    template_description = "Private SSH bridge on Oracle Cloud; five-minute leases, compartment isolation, quota & trial-expiry safeguards."
    default_instance_type = "a1-2c-12g"
    instance_types = tuple(OCI_SHAPES.keys())
    filter_hint = "OCI compartment: coder-workspaces (tag CreatedVia=Coder); Coder filter: template:oci-private"
    template_env_var = "CODER_OCI_TEMPLATE"

    def __init__(self, settings: dict, *, state_dir: Path | None = None, clients: dict | None = None):
        self.settings = settings
        self.profile = settings["oci_profile"]
        self.region = settings["oci_region"]
        self.manager = settings["managed_by"]
        self.compartment_name = settings["oci_compartment_name"]
        self.state_dir = state_dir or STATE_DIR
        self._config = None
        self._clients = clients or {}

    @property
    def infra_path(self) -> Path:
        return self.state_dir / "oci-infra.json"

    @property
    def config(self) -> dict:
        if self._config is None:
            import oci
            self._config = oci.config.from_file(profile_name=self.profile)
            if self.region:
                self._config["region"] = self.region
        return self._config

    @property
    def tenancy_id(self) -> str:
        return self.config["tenancy"]

    def _client(self, name: str):
        if name in self._clients:
            return self._clients[name]
        import oci
        factory = {
            "identity": oci.identity.IdentityClient,
            "compute": oci.core.ComputeClient,
            "network": oci.core.VirtualNetworkClient,
            "limits": oci.limits.LimitsClient,
            "usage": oci.usage_api.UsageapiClient,
            "budgets": oci.budget.BudgetClient,
        }[name]
        self._clients[name] = factory(self.config)
        return self._clients[name]

    def load_infra(self) -> dict:
        if not self.infra_path.exists():
            raise RuntimeError("OCI infrastructure is not initialized; run coder-oci setup first")
        return json.loads(self.infra_path.read_text())

    def _ensure_compartment(self) -> str:
        import oci
        identity = self._client("identity")
        existing = identity.list_compartments(
            compartment_id=self.tenancy_id, name=self.compartment_name, lifecycle_state="ACTIVE").data
        if existing:
            return existing[0].id
        created = identity.create_compartment(oci.identity.models.CreateCompartmentDetails(
            compartment_id=self.tenancy_id,
            name=self.compartment_name,
            description="Isolated compartment for Coder workspaces with bounded leases",
            freeform_tags={"ManagedBy": self.manager},
        )).data
        for _ in range(30):
            try:
                state = identity.get_compartment(created.id).data.lifecycle_state
                if state == "ACTIVE":
                    return created.id
            except oci.exceptions.ServiceError as error:
                if error.status != 404:
                    raise
            time.sleep(2)
        raise RuntimeError("Timed out waiting for OCI compartment to become ACTIVE")

    def _ensure_network(self, compartment_id: str) -> tuple[str, str]:
        if self.settings.get("oci_subnet_id"):
            subnet = self._client("network").get_subnet(self.settings["oci_subnet_id"]).data
            return subnet.vcn_id, subnet.id
        import oci
        network = self._client("network")
        vcns = [v for v in network.list_vcns(compartment_id=compartment_id).data
                if v.display_name == self.compartment_name and v.lifecycle_state == "AVAILABLE"]
        if vcns:
            vcn = vcns[0]
        else:
            vcn = network.create_vcn(oci.core.models.CreateVcnDetails(
                compartment_id=compartment_id,
                display_name=self.compartment_name,
                cidr_blocks=["10.42.0.0/16"],
                dns_label="coderws",
                freeform_tags={"ManagedBy": self.manager},
            )).data
        igws = [g for g in network.list_internet_gateways(compartment_id=compartment_id, vcn_id=vcn.id).data
                if g.lifecycle_state == "AVAILABLE"]
        if igws:
            igw = igws[0]
        else:
            try:
                igw = network.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
                    compartment_id=compartment_id,
                    vcn_id=vcn.id,
                    display_name=self.compartment_name,
                    is_enabled=True,
                    freeform_tags={"ManagedBy": self.manager},
                )).data
            except Exception as error:
                raise RuntimeError(
                    f"Could not create OCI Internet Gateway (Free Trial allows 1 IGW per tenancy). "
                    f"Set oci_subnet_id in config.json to reuse an existing public subnet. Detail: {error}"
                ) from error
        routes = [r for r in network.list_route_tables(compartment_id=compartment_id, vcn_id=vcn.id).data
                  if r.display_name == self.compartment_name]
        route_rule = oci.core.models.RouteRule(
            destination="0.0.0.0/0", destination_type="CIDR_BLOCK", network_entity_id=igw.id)
        if routes:
            rt = routes[0]
        else:
            rt = network.create_route_table(oci.core.models.CreateRouteTableDetails(
                compartment_id=compartment_id, vcn_id=vcn.id, display_name=self.compartment_name,
                route_rules=[route_rule], freeform_tags={"ManagedBy": self.manager},
            )).data
        sec_lists = [s for s in network.list_security_lists(compartment_id=compartment_id, vcn_id=vcn.id).data
                     if s.display_name == self.compartment_name]
        # SSH ingress is restricted per-workspace by an NSG; the subnet security list allows egress only.
        if sec_lists:
            sl = sec_lists[0]
        else:
            sl = network.create_security_list(oci.core.models.CreateSecurityListDetails(
                compartment_id=compartment_id, vcn_id=vcn.id, display_name=self.compartment_name,
                egress_security_rules=[oci.core.models.EgressSecurityRule(destination="0.0.0.0/0", protocol="all")],
                ingress_security_rules=[],
                freeform_tags={"ManagedBy": self.manager},
            )).data
        subnets = [s for s in network.list_subnets(compartment_id=compartment_id, vcn_id=vcn.id).data
                   if s.display_name == self.compartment_name and s.lifecycle_state == "AVAILABLE"]
        if subnets:
            subnet = subnets[0]
        else:
            subnet = network.create_subnet(oci.core.models.CreateSubnetDetails(
                compartment_id=compartment_id, vcn_id=vcn.id, display_name=self.compartment_name,
                cidr_block="10.42.1.0/24", dns_label="workers",
                route_table_id=rt.id, security_list_ids=[sl.id],
                prohibit_public_ip_on_vnic=False,
                freeform_tags={"ManagedBy": self.manager},
            )).data
        return vcn.id, subnet.id

    def _ensure_budget(self, compartment_id: str) -> None:
        limit_str = self.settings.get("oci_trial_spend_limit_usd", "")
        if not limit_str:
            return
        try:
            import oci
            budgets = self._client("budgets")
            existing = budgets.list_budgets(compartment_id=self.tenancy_id).data
            if any(b.display_name == self.compartment_name and b.lifecycle_state == "ACTIVE" for b in existing):
                return
            budgets.create_budget(oci.budget.models.CreateBudgetDetails(
                compartment_id=self.tenancy_id,
                target_type="COMPARTMENT",
                targets=[compartment_id],
                display_name=self.compartment_name,
                description="Spend safeguard budget for Coder OCI workspaces",
                amount=float(limit_str),
                reset_period="MONTHLY",
                freeform_tags={"ManagedBy": self.manager},
            ))
            print(f"OCI compartment budget created (${limit_str}/month)")
        except Exception as error:
            print(f"Note: could not create OCI budget alert ({error}); controller spend & expiry checks remain active")

    def setup(self, *, bootstrap: bool = False) -> None:
        compartment_id = self._ensure_compartment()
        vcn_id, subnet_id = self._ensure_network(compartment_id)
        ads = self._client("identity").list_availability_domains(compartment_id=self.tenancy_id).data
        if not ads:
            raise RuntimeError(f"No availability domains found in {self.region}")
        ad = ads[0].name
        self._ensure_budget(compartment_id)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        infra = {
            "tenancy_id": self.tenancy_id,
            "compartment_id": compartment_id,
            "vcn_id": vcn_id,
            "subnet_id": subnet_id,
            "availability_domain": ad,
            "region": self.region,
            "profile": self.profile,
        }
        temp = self.infra_path.with_suffix(".tmp")
        temp.write_text(json.dumps(infra, indent=2))
        temp.chmod(0o600)
        os.replace(temp, self.infra_path)
        print(f"OCI compartment {self.compartment_name} and shared network ready in {self.region} ({ad})")

    def teardown_network(self) -> None:
        """Delete the shared coder-workspaces VCN/subnet/IGW (e.g. to free the single Free Trial IGW)."""
        infra = self.load_infra()
        if self.list_instances():
            raise RuntimeError("Delete all OCI coder workspaces before tearing down the shared network")
        network = self._client("network")
        cid = infra["compartment_id"]
        vcn_id = infra["vcn_id"]
        for subnet in network.list_subnets(compartment_id=cid, vcn_id=vcn_id).data:
            if subnet.freeform_tags.get("ManagedBy") == self.manager:
                network.delete_subnet(subnet.id)
        for nsg in network.list_network_security_groups(compartment_id=cid, vcn_id=vcn_id).data:
            if nsg.freeform_tags.get("ManagedBy") == self.manager:
                network.delete_network_security_group(nsg.id)
        for rt in network.list_route_tables(compartment_id=cid, vcn_id=vcn_id).data:
            if rt.freeform_tags.get("ManagedBy") == self.manager:
                network.delete_route_table(rt.id)
        for sl in network.list_security_lists(compartment_id=cid, vcn_id=vcn_id).data:
            if sl.freeform_tags.get("ManagedBy") == self.manager:
                network.delete_security_list(sl.id)
        for igw in network.list_internet_gateways(compartment_id=cid, vcn_id=vcn_id).data:
            if igw.freeform_tags.get("ManagedBy") == self.manager:
                network.delete_internet_gateway(igw.id)
        network.delete_vcn(vcn_id)
        self.infra_path.unlink(missing_ok=True)
        print("OCI shared workspace network removed; Internet Gateway quota freed")

    def _check_trial_spend(self) -> float:
        import oci
        now = dt.datetime.now(dt.timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        result = self._client("usage").request_summarized_usages(
            oci.usage_api.models.RequestSummarizedUsagesDetails(
                tenant_id=self.tenancy_id,
                time_usage_started=start,
                time_usage_ended=end,
                granularity="MONTHLY",
                query_type="COST",
            )
        ).data
        return sum(float(item.computed_amount or 0.0) for item in (result.items or []))

    def _check_quota(self, infra: dict, shape_spec: dict) -> None:
        limits = self._client("limits")
        ad = infra["availability_domain"]
        checks = [
            ("compute", shape_spec["core_limit"], shape_spec["ocpus"], "OCPUs", ad),
            ("compute", shape_spec["mem_limit"], shape_spec["memory_gb"], "GB RAM", ad),
            ("block-storage", "total-storage-gb", BOOT_VOLUME_GB, "GB storage", ad),
        ]
        if shape_spec.get("regional_core_limit"):
            checks.append(("compute", shape_spec["regional_core_limit"], shape_spec["ocpus"], "regional OCPUs", None))
        if shape_spec.get("regional_mem_limit"):
            checks.append(("compute", shape_spec["regional_mem_limit"], shape_spec["memory_gb"], "regional GB RAM", None))
        for service, limit_name, needed, unit, domain in checks:
            kwargs = {"service_name": service, "limit_name": limit_name, "compartment_id": self.tenancy_id}
            if domain:
                kwargs["availability_domain"] = domain
            avail = limits.get_resource_availability(**kwargs).data
            available = avail.available or 0
            if available < needed:
                hint = (" Update aws-infra/src/Oracle/quotas.ts to open this quota."
                        if shape_spec["always_free"]
                        else " Use a1-2c-12g (Always Free) or raise the trial quota in aws-infra/src/Oracle/quotas.ts.")
                raise RuntimeError(
                    f"OCI quota insufficient for {limit_name}: need {needed} {unit}, "
                    f"available {available} (effective quota {avail.effective_quota_value}).{hint}"
                )

    def assert_guard(self, *, instance_type: str | None = None, check_create: bool = False) -> None:
        infra = self.load_infra()
        if not instance_type:
            return
        spec = OCI_SHAPES.get(instance_type)
        if not spec:
            raise RuntimeError(f"Unsupported OCI instance type: {instance_type}")
        if not spec["always_free"]:
            expires_raw = self.settings.get("oci_trial_expires", "")
            if expires_raw:
                expires = dt.date.fromisoformat(expires_raw)
                if dt.datetime.now(dt.timezone.utc).date() >= expires:
                    raise RuntimeError(
                        f"OCI paid shape {instance_type} blocked: trial cutoff {expires_raw} reached. "
                        f"Use Always Free a1-2c-12g or update oci_trial_expires."
                    )
            spend_limit_raw = self.settings.get("oci_trial_spend_limit_usd", "")
            if check_create and spend_limit_raw:
                spent = self._check_trial_spend()
                if spent >= float(spend_limit_raw):
                    raise RuntimeError(
                        f"OCI paid shape {instance_type} blocked: monthly spend ${spent:.2f} "
                        f"reached oci_trial_spend_limit_usd (${spend_limit_raw})."
                    )
        if check_create:
            self._check_quota(infra, spec)

    def publish_variables(self, *, ssh_public_key: str, ssh_cidr: str,
                          agent_name: str, agent_email: str) -> dict[str, str]:
        infra = self.load_infra()
        return {
            "oci_profile": self.profile,
            "region": self.region,
            "compartment_id": infra["compartment_id"],
            "vcn_id": infra["vcn_id"],
            "subnet_id": infra["subnet_id"],
            "availability_domain": infra["availability_domain"],
            "x86_image_id": self.settings["oci_x86_image_id"],
            "arm_image_id": self.settings["oci_arm_image_id"],
            "ssh_public_key": ssh_public_key,
            "ssh_cidr": ssh_cidr,
            "agent_name": agent_name,
            "agent_email": agent_email,
            "managed_by": self.manager,
            "coder_url": self.settings["coder_url"],
        }

    def _to_instance(self, raw_inst, compartment_id: str) -> Instance:
        tags = dict(raw_inst.freeform_tags or {})
        public_ip = ""
        nsg_ids = []
        state = _normalize_state(raw_inst.lifecycle_state)
        if state in ("running", "starting", "stopping"):
            attachments = self._client("compute").list_vnic_attachments(
                compartment_id=compartment_id, instance_id=raw_inst.id).data
            for att in attachments:
                if att.lifecycle_state == "ATTACHED" and att.vnic_id:
                    vnic = self._client("network").get_vnic(att.vnic_id).data
                    if vnic.public_ip:
                        public_ip = vnic.public_ip
                    nsg_ids.extend(vnic.nsg_ids or [])
                    break
        shape_cfg = raw_inst.shape_config
        label = tags.get("InstanceSize") or (
            f"{raw_inst.shape} ({int(shape_cfg.ocpus)}c/{int(shape_cfg.memory_in_gbs)}g)"
            if shape_cfg and shape_cfg.ocpus and shape_cfg.memory_in_gbs else raw_inst.shape
        )
        return Instance(
            id=raw_inst.id,
            name=tags.get("WorkspaceName", raw_inst.display_name or ""),
            state=state,
            public_ip=public_ip,
            instance_type=label,
            provider="oci",
            tags=tags,
            raw={"compartment_id": compartment_id, "nsg_ids": nsg_ids, "metadata": dict(raw_inst.metadata or {})},
        )

    def list_instances(self, owner_id: str | None = None) -> list[Instance]:
        if not self.infra_path.exists():
            return []
        infra = self.load_infra()
        compartment_id = infra["compartment_id"]
        items = self._client("compute").list_instances(compartment_id=compartment_id).data
        result = []
        for item in items:
            if item.lifecycle_state in ("TERMINATING", "TERMINATED"):
                continue
            tags = item.freeform_tags or {}
            if tags.get("ManagedBy") != self.manager:
                continue
            if owner_id and tags.get("OwnerId") != owner_id:
                continue
            result.append(self._to_instance(item, compartment_id))
        return result

    def get_instance(self, instance_id: str) -> Instance:
        infra = self.load_infra()
        item = self._client("compute").get_instance(instance_id).data
        return self._to_instance(item, infra["compartment_id"])

    def set_tags(self, machine: Instance, tags: dict[str, str]) -> None:
        import oci
        compute = self._client("compute")
        current = dict(compute.get_instance(machine.id).data.freeform_tags or {})
        for key, value in tags.items():
            # Stay within OCI's 10-tag freeform limit; omit non-essential SourceProjects and clear empties.
            if key == "SourceProjects" or not value:
                current.pop(key, None)
            else:
                current[key] = value
        compute.update_instance(machine.id, oci.core.models.UpdateInstanceDetails(freeform_tags=current))
        machine.tags = current

    def ensure_ssh_access(self, machine: Instance, ssh_cidr: str) -> bool:
        """Update the workspace NSG's TCP/22 ingress rule if the controller's IPv4 changed."""
        nsg_ids = machine.raw.get("nsg_ids") or []
        if not nsg_ids:
            return False
        import oci
        network = self._client("network")
        nsg_id = nsg_ids[0]
        rules = network.list_network_security_group_security_rules(nsg_id, direction="INGRESS").data
        ssh_rules = [
            r for r in rules
            if r.protocol == "6" and r.tcp_options and r.tcp_options.destination_port_range
            and r.tcp_options.destination_port_range.min == 22 and r.tcp_options.destination_port_range.max == 22
        ]
        if any(r.source == ssh_cidr for r in ssh_rules):
            return False
        network.add_network_security_group_security_rules(
            nsg_id,
            oci.core.models.AddNetworkSecurityGroupSecurityRulesDetails(
                security_rules=[oci.core.models.AddSecurityRuleDetails(
                    direction="INGRESS",
                    protocol="6",
                    source=ssh_cidr,
                    source_type="CIDR_BLOCK",
                    description="SSH from the controller only",
                    tcp_options=oci.core.models.TcpOptions(
                        destination_port_range=oci.core.models.PortRange(min=22, max=22)),
                )]
            ),
        )
        stale_ids = [r.id for r in ssh_rules if r.source != ssh_cidr and r.id]
        if stale_ids:
            network.remove_network_security_group_security_rules(
                nsg_id,
                oci.core.models.RemoveNetworkSecurityGroupSecurityRulesDetails(security_rule_ids=stale_ids),
            )
        return True

    def stop_instance(self, machine: Instance) -> None:
        self._client("compute").instance_action(machine.id, "SOFTSTOP")

    def read_userdata(self, machine: Instance) -> bytes:
        metadata = machine.raw.get("metadata")
        if not metadata or "user_data" not in metadata:
            metadata = self._client("compute").get_instance(machine.id).data.metadata or {}
        return base64.b64decode(metadata["user_data"])
