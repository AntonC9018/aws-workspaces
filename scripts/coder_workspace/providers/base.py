"""Provider-neutral instance representation and backend protocol."""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt


@dataclass
class Instance:
    """Normalized cloud VM representation shared across providers."""
    id: str
    name: str
    state: str  # "starting" | "running" | "stopping" | "stopped"
    public_ip: str
    instance_type: str
    provider: str
    tags: dict[str, str] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def lease_expires_at(self) -> dt.datetime | None:
        raw = self.tags.get("LeaseExpiresAt", "")
        if not raw:
            return None
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else None
        except (ValueError, TypeError):
            return None

    @property
    def stopped_since(self) -> dt.datetime | None:
        raw = self.tags.get("StoppedSince", "")
        if not raw:
            return None
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else None
        except (ValueError, TypeError):
            return None

    @property
    def keep_workspace(self) -> bool:
        return self.tags.get("KeepWorkspace", "").lower() == "true"


class Provider:
    """Interface implemented by AWS and OCI cloud backends."""
    name: str
    template_name: str
    display_name: str
    template_description: str
    default_instance_type: str
    instance_types: tuple[str, ...]
    filter_hint: str
    template_env_var: str

    def setup(self, *, bootstrap: bool = False) -> None:
        raise NotImplementedError

    def assert_guard(self, *, instance_type: str | None = None, check_create: bool = False, disk_gb: int | None = None) -> None:
        raise NotImplementedError

    def publish_variables(self, *, ssh_public_key: str, ssh_cidr: str,
                          agent_name: str, agent_email: str) -> dict[str, str]:
        raise NotImplementedError

    def list_instances(self, owner_id: str | None = None) -> list[Instance]:
        raise NotImplementedError

    def find_instance(self, name: str, owner_id: str) -> Instance:
        matches = [item for item in self.list_instances(owner_id) if item.name == name]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one owned {self.name.upper()} instance for {name}, found {len(matches)}")
        return matches[0]

    def get_instance(self, instance_id: str) -> Instance:
        raise NotImplementedError

    def set_tags(self, machine: Instance, tags: dict[str, str]) -> None:
        raise NotImplementedError

    def ensure_ssh_access(self, machine: Instance, ssh_cidr: str) -> bool:
        """Ensure TCP/22 ingress allows ssh_cidr; return True if modified."""
        return False

    def stop_instance(self, machine: Instance) -> None:
        raise NotImplementedError

    def read_userdata(self, machine: Instance) -> bytes:
        raise NotImplementedError
