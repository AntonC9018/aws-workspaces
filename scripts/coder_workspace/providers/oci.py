"""Oracle Cloud Infrastructure (OCI) provider backend with trial & quota safeguards."""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import time

from .base import Instance, Provider

OCI_IMAGE_RE = re.compile(r"^ocid1\.image\.oc1\.[a-z0-9-]+\.[a-z0-9]+$")


def _list_all(fn, *args, **kwargs) -> list:
    """Paginate an OCI SDK list_* call while staying compatible with simple unit-test mocks."""
    items = []
    call_kwargs = dict(kwargs)
    while True:
        resp = fn(*args, **call_kwargs)
        items.extend(resp.data or [])
        if getattr(resp, "has_next_page", False) and getattr(resp, "next_page", None):
            call_kwargs["page"] = resp.next_page
            continue
        break
    return items


def _delete_retry(fn, resource_id: str, *, attempts: int = 15, delay: float = 4.0) -> None:
    import oci
    for attempt in range(attempts):
        try:
            fn(resource_id)
            return
        except oci.exceptions.ServiceError as error:
            if error.status == 404:
                return
            if error.status == 409 and attempt < attempts - 1:
                time.sleep(delay)
                continue
            raise

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
    def filter_hint(self) -> str:
        return f"OCI compartment: {self.compartment_name} (tag ManagedBy={self.manager}); Coder filter: template:oci-private"

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
        network = self._client("network")
        if self.settings.get("oci_subnet_id"):
            subnet = network.get_subnet(self.settings["oci_subnet_id"]).data
            for sl_id in getattr(subnet, "security_list_ids", None) or []:
                try:
                    sl = network.get_security_list(sl_id).data
                    for rule in getattr(sl, "ingress_security_rules", None) or []:
                        tcp = getattr(rule, "tcp_options", None)
                        dport = getattr(tcp, "destination_port_range", None) if tcp else None
                        opens_ssh = (rule.protocol in ("all", "6") and
                                     (dport is None or (dport.min <= 22 <= dport.max)))
                        if opens_ssh and rule.source == "0.0.0.0/0":
                            print(f"warning: reused OCI subnet {subnet.id} has security list {sl_id} allowing "
                                  f"TCP/22 from 0.0.0.0/0; remove that rule to restrict SSH to the controller NSG",
                                  file=sys.stderr)
                except Exception:
                    pass
            return subnet.vcn_id, subnet.id
        import oci
        vcns = [v for v in _list_all(network.list_vcns, compartment_id=compartment_id)
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
        igws = [g for g in _list_all(network.list_internet_gateways, compartment_id=compartment_id, vcn_id=vcn.id)
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
        routes = [r for r in _list_all(network.list_route_tables, compartment_id=compartment_id, vcn_id=vcn.id)
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
        sec_lists = [s for s in _list_all(network.list_security_lists, compartment_id=compartment_id, vcn_id=vcn.id)
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
        subnets = [s for s in _list_all(network.list_subnets, compartment_id=compartment_id, vcn_id=vcn.id)
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
            existing = _list_all(budgets.list_budgets, compartment_id=self.tenancy_id)
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
        if self.settings.get("oci_subnet_id"):
            raise RuntimeError("Refusing to tear down an external oci_subnet_id network; remove oci_subnet_id only if tearing down a managed VCN")
        infra = self.load_infra()
        cid = infra["compartment_id"]
        vcn_id = infra["vcn_id"]
        active = [
            inst for inst in _list_all(self._client("compute").list_instances, compartment_id=cid)
            if inst.lifecycle_state != "TERMINATED" and (inst.freeform_tags or {}).get("ManagedBy") == self.manager
        ]
        if active:
            raise RuntimeError("Delete (and wait for termination of) all OCI coder workspaces before tearing down the shared network")
        network = self._client("network")
        vcn = network.get_vcn(vcn_id).data
        if (vcn.freeform_tags or {}).get("ManagedBy") != self.manager:
            raise RuntimeError(f"Refusing to delete VCN {vcn_id}: missing ManagedBy={self.manager} tag")
        for subnet in _list_all(network.list_subnets, compartment_id=cid, vcn_id=vcn_id):
            if (subnet.freeform_tags or {}).get("ManagedBy") == self.manager:
                _delete_retry(network.delete_subnet, subnet.id)
        for nsg in _list_all(network.list_network_security_groups, compartment_id=cid, vcn_id=vcn_id):
            if (nsg.freeform_tags or {}).get("ManagedBy") == self.manager:
                _delete_retry(network.delete_network_security_group, nsg.id)
        for rt in _list_all(network.list_route_tables, compartment_id=cid, vcn_id=vcn_id):
            if (rt.freeform_tags or {}).get("ManagedBy") == self.manager:
                _delete_retry(network.delete_route_table, rt.id)
        for sl in _list_all(network.list_security_lists, compartment_id=cid, vcn_id=vcn_id):
            if (sl.freeform_tags or {}).get("ManagedBy") == self.manager:
                _delete_retry(network.delete_security_list, sl.id)
        for igw in _list_all(network.list_internet_gateways, compartment_id=cid, vcn_id=vcn_id):
            if (igw.freeform_tags or {}).get("ManagedBy") == self.manager:
                _delete_retry(network.delete_internet_gateway, igw.id)
        _delete_retry(network.delete_vcn, vcn_id)
        self.infra_path.unlink(missing_ok=True)
        print("OCI shared workspace network removed; Internet Gateway quota freed")

    def _check_trial_spend(self) -> float:
        import oci
        now = dt.datetime.now(dt.timezone.utc)
        start = (now - dt.timedelta(days=31)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        details = oci.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=self.tenancy_id,
            time_usage_started=start,
            time_usage_ended=end,
            granularity="DAILY",
            query_type="COST",
        )
        total = 0.0
        page = None
        while True:
            kwargs = {"page": page} if page else {}
            resp = self._client("usage").request_summarized_usages(details, **kwargs)
            total += sum(float(item.computed_amount or 0.0) for item in (resp.data.items or []))
            if getattr(resp, "has_next_page", False) and getattr(resp, "next_page", None):
                page = resp.next_page
                continue
            break
        return total

    def _check_quota(self, infra: dict, shape_spec: dict, disk_gb: int = BOOT_VOLUME_GB) -> None:
        limits = self._client("limits")
        ad = infra["availability_domain"]
        checks = [
            ("compute", shape_spec["core_limit"], shape_spec["ocpus"], "OCPUs", ad),
            ("compute", shape_spec["mem_limit"], shape_spec["memory_gb"], "GB RAM", ad),
            ("block-storage", "total-storage-gb", disk_gb, "GB storage", ad),
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
                hint = (" Update your OCI tenancy quota policy to open this limit."
                        if shape_spec["always_free"]
                        else " Use a1-2c-12g (Always Free) or raise the trial quota in your OCI tenancy quota policy.")
                raise RuntimeError(
                    f"OCI quota insufficient for {limit_name}: need {needed} {unit}, "
                    f"available {available} (effective quota {avail.effective_quota_value}).{hint}"
                )

    def assert_guard(self, *, instance_type: str | None = None, check_create: bool = False,
                     check_quota: bool = True, disk_gb: int | None = None) -> None:
        infra = self.load_infra()
        if not instance_type:
            if check_create:
                raise RuntimeError("OCI instance size is missing; cannot verify trial and quota safeguards")
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
                        f"OCI paid shape {instance_type} blocked: 31-day spend ${spent:.2f} "
                        f"reached oci_trial_spend_limit_usd (${spend_limit_raw})."
                    )
        if check_create and check_quota:
            self._check_quota(infra, spec, disk_gb or BOOT_VOLUME_GB)

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
        items = _list_all(self._client("compute").list_instances, compartment_id=compartment_id)
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
        for attempt in range(8):
            current = dict(compute.get_instance(machine.id).data.freeform_tags or {})
            for key, value in tags.items():
                # Stay within OCI's 10-tag freeform limit; omit non-essential SourceProjects and clear empties.
                if key == "SourceProjects" or not value:
                    current.pop(key, None)
                else:
                    current[key] = value
            try:
                compute.update_instance(machine.id, oci.core.models.UpdateInstanceDetails(freeform_tags=current))
                machine.tags = current
                return
            except oci.exceptions.ServiceError as error:
                if error.status == 409 and attempt < 7:
                    time.sleep(2)
                    continue
                raise

    def ensure_ssh_access(self, machine: Instance, ssh_cidr: str) -> bool:
        """Update the workspace NSG's TCP/22 ingress rule if the controller's IPv4 changed or stale rules exist."""
        nsg_ids = machine.raw.get("nsg_ids") or []
        if not nsg_ids:
            return False
        import oci
        network = self._client("network")
        nsg_id = nsg_ids[0]
        rules = _list_all(network.list_network_security_group_security_rules, nsg_id, direction="INGRESS")
        ssh_rules = [
            r for r in rules
            if r.protocol == "6" and r.tcp_options and r.tcp_options.destination_port_range
            and r.tcp_options.destination_port_range.min == 22 and r.tcp_options.destination_port_range.max == 22
        ]
        stale_ids = [r.id for r in ssh_rules if r.source != ssh_cidr and r.id]
        has_current = any(r.source == ssh_cidr for r in ssh_rules)
        if has_current and not stale_ids:
            return False
        if not has_current:
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
        if stale_ids:
            network.remove_network_security_group_security_rules(
                nsg_id,
                oci.core.models.RemoveNetworkSecurityGroupSecurityRulesDetails(security_rule_ids=stale_ids),
            )
        return True

    def stop_instance(self, machine: Instance) -> None:
        self._client("compute").instance_action(machine.id, "SOFTSTOP")

    def terminate_instance(self, machine: Instance) -> None:
        self._client("compute").terminate_instance(machine.id)

    def read_userdata(self, machine: Instance) -> bytes:
        metadata = machine.raw.get("metadata")
        if not metadata or "user_data" not in metadata:
            metadata = self._client("compute").get_instance(machine.id).data.metadata or {}
        return base64.b64decode(metadata["user_data"])

    def default_builder_type(self, arch: str) -> str:
        return "a1-2c-12g" if arch == "arm64" else "e5-4c-16g"

    def base_image_id(self, arch: str) -> str:
        return self.settings["oci_arm_image_id"] if arch == "arm64" else self.settings["oci_x86_image_id"]

    def image_arch_for_size(self, instance_type: str) -> str:
        spec = OCI_SHAPES.get(instance_type)
        if not spec:
            raise RuntimeError(f"Unsupported OCI instance type: {instance_type}")
        return spec["arch"]

    def list_images(self, *, include_pending: bool = False) -> list[dict]:
        import oci
        if not self.infra_path.exists():
            return []
        infra = self.load_infra()
        kwargs = {"compartment_id": infra["compartment_id"]}
        if not include_pending:
            kwargs["lifecycle_state"] = "AVAILABLE"
        items = oci.pagination.list_call_get_all_results(
            self._client("compute").list_images, **kwargs).data
        results = []
        for img in items:
            if include_pending and img.lifecycle_state not in ("AVAILABLE", "PROVISIONING", "IMPORTING"):
                continue
            tags = dict(img.freeform_tags or {})
            if tags.get("ManagedBy") != self.manager:
                continue
            disk_gb = int(tags["ImageDiskGb"]) if tags.get("ImageDiskGb", "").isdigit() else None
            results.append({
                "id": img.id,
                "name": tags.get("ImageName", img.display_name or ""),
                "hash": tags.get("ImageHash", ""),
                "arch": tags.get("ImageArch", "amd64"),
                "disk_gb": disk_gb,
                "state": img.lifecycle_state.lower(),
                "created": img.time_created.isoformat() if img.time_created else "",
                "size_mb": getattr(img, "size_in_mbs", None),
                "provider": "oci",
            })
        return sorted(results, key=lambda r: r["created"], reverse=True)

    def resolve_image_info(self, ref: str) -> dict:
        if ref.startswith("ocid1.image."):
            if not OCI_IMAGE_RE.fullmatch(ref):
                raise RuntimeError(f"Invalid OCI image OCID: {ref}")
            owned = [img for img in self.list_images() if img["id"] == ref]
            return owned[0] if owned else {"id": ref, "name": ref, "arch": None, "disk_gb": None, "provider": "oci"}
        matches = [img for img in self.list_images() if img["name"] == ref]
        if not matches:
            raise RuntimeError(f"Private OCI image not found: {ref} (run coder-oci images)")
        return matches[0]

    def create_image(self, machine: Instance, *, image_name: str, image_hash: str,
                     arch: str, disk_gb: int = BOOT_VOLUME_GB) -> dict:
        import oci
        infra = self.load_infra()
        compute = self._client("compute")
        created = compute.create_image(oci.core.models.CreateImageDetails(
            compartment_id=infra["compartment_id"],
            instance_id=machine.id,
            display_name=f"coder-{image_name}-{image_hash[:8]}",
            freeform_tags={
                "ManagedBy": self.manager,
                "ImageName": image_name,
                "ImageHash": image_hash,
                "ImageArch": arch,
                "ImageDiskGb": str(disk_gb),
            },
        )).data
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            current = compute.get_image(created.id).data
            if current.lifecycle_state == "AVAILABLE":
                return {
                    "id": current.id, "name": image_name, "hash": image_hash,
                    "arch": arch, "disk_gb": disk_gb, "provider": "oci",
                    "size_mb": getattr(current, "size_in_mbs", None),
                }
            if current.lifecycle_state not in ("PROVISIONING", "IMPORTING"):
                raise RuntimeError(f"OCI image creation ended in state {current.lifecycle_state}")
            time.sleep(15)
        raise RuntimeError(f"Timed out waiting for OCI custom image {created.id} to become AVAILABLE")

    def delete_image(self, ref: str) -> dict:
        matches = [img for img in self.list_images(include_pending=True) if img["id"] == ref or img["name"] == ref]
        if not matches:
            raise RuntimeError(f"Owned OCI image not found: {ref}")
        target = matches[0]
        self._client("compute").delete_image(target["id"])
        return target
