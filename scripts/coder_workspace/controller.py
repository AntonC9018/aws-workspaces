"""Provider-agnostic Coder workspace controller with bounded leases and automatic cleanup."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import gzip
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from coder_workspace import images as image_specs
from coder_workspace.providers import AwsProvider, Instance, OciProvider, Provider
from coder_workspace.settings import load as load_settings

STATE = Path.home() / ".local/state/coder-workspaces"
CONFIG = Path.home() / ".config/coderv2"
GC_INTERVAL_SECONDS = 300


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def api(path, payload=None):
    url = (CONFIG / "url").read_text().strip()
    token = (CONFIG / "session").read_text().strip()
    request = urllib.request.Request(
        url + "/api/v2/" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Coder-Session-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.URLError as error:
        if isinstance(error.reason, ConnectionRefusedError) or "Connection refused" in str(error):
            raise RuntimeError("Local Coder server is not running; start it with: coder-trial start") from error
        raise


def current_ipv4_cidr() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=15) as response:
        address = str(ipaddress.IPv4Address(response.read().decode().strip()))
    return address + "/32"


def key_path() -> Path:
    return STATE / "id_ed25519"


def bridge_unit(instance_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "-", instance_id)
    if len(safe) > 64:
        safe = safe[-48:]
    return "coder-bridge-" + safe


def notify(summary: str, body: str) -> None:
    try:
        subprocess.run(["notify-send", "--app-name=Coder workspaces", summary, body],
                       check=False, timeout=5, capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        pass


def make_provider(name: str, settings: dict, *, bootstrap: bool = False) -> Provider:
    if name == "aws":
        return AwsProvider(settings, bootstrap=bootstrap)
    if name == "oci":
        return OciProvider(settings, state_dir=STATE)
    raise ValueError(f"Unknown provider: {name}")


def publish(provider: Provider, settings: dict, *, refresh_guard: bool = True) -> None:
    if refresh_guard:
        provider.setup(bootstrap=True)
    else:
        provider.assert_guard()
    key = key_path()
    if not key.exists():
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
    ssh_cidr = current_ipv4_cidr()
    identity = run(["git", "var", "GIT_AUTHOR_IDENT"], capture_output=True).stdout
    agent_name, rest = identity.split(" <", 1)
    agent_email = rest.split(">", 1)[0]
    template_dir = STATE / ("template" if provider.name == "aws" else f"template-{provider.name}")
    template_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    template_dir.chmod(0o700)
    source_root = Path(os.environ[provider.template_env_var])
    for source in source_root.iterdir():
        target = template_dir / source.name
        shutil.copyfile(source, target)
        target.chmod(0o600)
    var_file = STATE / ("variables.json" if provider.name == "aws" else f"variables-{provider.name}.json")
    variables = provider.publish_variables(
        ssh_public_key=key.with_suffix(".pub").read_text(),
        ssh_cidr=ssh_cidr,
        agent_name=agent_name,
        agent_email=agent_email,
    )
    var_file.write_text(json.dumps(variables))
    var_file.chmod(0o600)
    run(["coder", "templates", "push", provider.template_name, "--directory", str(template_dir),
         "--variables-file", str(var_file), "--yes"])
    run(["coder", "templates", "edit", provider.template_name, "--default-ttl", "0h", "--yes",
         "--display-name", provider.display_name, "--description", provider.template_description])


def transition(name: str, action: str, parameters: dict | None = None) -> None:
    workspace = api("users/me/workspace/" + name)
    template = api("templates/" + workspace["template_id"])
    payload = {"transition": action, "template_version_id": template["active_version_id"]}
    if parameters is not None:
        payload["rich_parameter_values"] = [{"name": k, "value": v} for k, v in parameters.items()]
    build = api("workspaces/" + workspace["id"] + "/builds", payload)
    for _ in range(180):
        build = api("workspacebuilds/" + build["id"])
        if build["job"]["status"] == "succeeded":
            return
        if build["job"]["status"] in ("failed", "canceled"):
            raise RuntimeError(f"Coder {action} failed: {build['job'].get('error', 'inspect dashboard build logs')}")
        time.sleep(2)
    raise RuntimeError(f"Coder {action} did not finish within six minutes; inspect dashboard")


def upgrade(provider: Provider, name: str) -> None:
    """Apply the active template to a stopped worker without starting it."""
    owner = api("users/me")["id"]
    machine = provider.find_instance(name, owner)
    if machine.state != "stopped":
        raise RuntimeError("Stop the workspace before upgrading its template")
    workspace = api("users/me/workspace/" + name)
    if workspace["latest_build"]["job"]["status"] not in ("succeeded", "failed", "canceled"):
        raise RuntimeError("Wait for the current workspace build before upgrading")
    transition(name, "stop")
    print(f"{name}: active template applied; workspace remains stopped")


def renew(provider: Provider, machine: Instance, seconds: int, *, preserve: bool = True) -> None:
    provider.assert_guard(instance_type=machine.tags.get("InstanceSize") or None)
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds + 60)
    if preserve:
        latest = provider.get_instance(machine.id)
        existing = latest.lease_expires_at
        if existing is not None:
            deadline = max(deadline, existing)
    provider.set_tags(machine, {"LeaseExpiresAt": deadline.isoformat(), "StoppedSince": ""})


def known_hosts_path(instance_id: str) -> Path:
    directory = STATE / "known_hosts.d"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / (re.sub(r"[^A-Za-z0-9_.-]", "_", instance_id) + ".hosts")


def ssh_args(machine: Instance) -> list[str]:
    if not machine.public_ip:
        raise RuntimeError(f"Workspace {machine.name} has no public IP address yet")
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", machine.id)
    return [
        "ssh", "-i", str(key_path()), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ForwardAgent=no",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"HostKeyAlias={safe_id}",
        "-o", f"UserKnownHostsFile={known_hosts_path(machine.id)}",
        "ubuntu@" + machine.public_ip,
    ]


def remote(machine: Instance, command: list[str], **kwargs):
    return run(ssh_args(machine) + [shlex.join(command)], **kwargs)


DEFAULT_LEASE_SECONDS = 300
WORKER_HELPERS = (
    ("workspace_lease.py", "workspace-lease"),
    ("workspace_job.py", "workspace-job"),
    ("workspace_transfer.py", "workspace-transfer"),
    ("github_credential.py", "workspace-github-credential"),
)


def active_jobs(machine: Instance, *, include_deactivating: bool = True) -> list[str]:
    states = "activating,running,deactivating" if include_deactivating else "activating,running"
    active = remote(machine, ["systemctl", "list-units", f"--state={states}", "--no-legend",
                              "coder-job-*.service"], capture_output=True)
    return [line.split()[0] for line in active.stdout.splitlines() if line.strip()]


def _load_worker_helpers(provider: Provider) -> dict[str, str]:
    helpers = {}
    template_dir = os.environ.get(provider.template_env_var)
    if template_dir:
        root = Path(template_dir)
        for filename, target in WORKER_HELPERS:
            source = root / filename
            if source.is_file():
                helpers[target] = source.read_text()
        if helpers:
            return helpers
    repo_root = Path(__file__).resolve().parents[2]
    fallback_paths = {
        "workspace-lease": repo_root / "assets/coder/workspace_lease.py",
        "workspace-job": repo_root / "assets/coder/workspace_job.py",
        "workspace-transfer": Path(__file__).resolve().parent / "transfer.py",
        "workspace-github-credential": repo_root / "assets/coder/github_credential.py",
    }
    for target, source in fallback_paths.items():
        if source.is_file():
            helpers[target] = source.read_text()
    return helpers


def install_worker_helpers(provider: Provider, machine: Instance) -> bool:
    helpers = _load_worker_helpers(provider)
    if not helpers:
        return False
    installer = r'''
import json, os, pathlib, sys
for target, content in json.load(sys.stdin).items():
    dest = pathlib.Path("/usr/local/bin") / target
    temp = dest.with_suffix(".tmp")
    temp.write_text(content)
    temp.chmod(0o755)
    os.replace(temp, dest)
per_boot = pathlib.Path("/var/lib/cloud/scripts/per-boot/coder-lease")
if per_boot.is_file():
    original = per_boot.read_text()
    patched = original.replace("/usr/local/bin/workspace-lease hold 600\n",
                               "/usr/local/bin/workspace-lease hold 600 --owner boot\n")
    if patched != original:
        temp = per_boot.with_suffix(".tmp")
        temp.write_text(patched)
        temp.chmod(0o755)
        os.replace(temp, per_boot)
'''
    remote(machine, ["sudo", "python3", "-c", installer], input=json.dumps(helpers))
    return True


def guest_lease(provider: Provider, machine: Instance, lease_args: list[str],
                *, capture_json: bool = False) -> dict | None:
    cmd = ["sudo", "/usr/local/bin/workspace-lease", *lease_args]
    if capture_json:
        cmd.append("--json")
    try:
        res = remote(machine, cmd, capture_output=True)
    except subprocess.CalledProcessError as error:
        if error.returncode == 2 and install_worker_helpers(provider, machine):
            res = remote(machine, cmd, capture_output=True)
        else:
            raise
    if not capture_json:
        return None
    raw = getattr(res, "stdout", None)
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def acquire_lease(provider: Provider, machine: Instance, seconds: int, *, owner: str) -> None:
    renew(provider, machine, seconds, preserve=True)
    guest_lease(provider, machine, ["hold", str(seconds), "--owner", owner])


def sync_cloud_lease_from_guest(provider: Provider, machine: Instance, remaining: float) -> None:
    """Shorten or preserve the cloud deadline from guest state without letting the guest extend it."""
    provider.assert_guard(instance_type=machine.tags.get("InstanceSize") or None)
    now = dt.datetime.now(dt.timezone.utc)
    floor = now + dt.timedelta(seconds=DEFAULT_LEASE_SECONDS + 60)
    desired = now + dt.timedelta(seconds=max(DEFAULT_LEASE_SECONDS, math.ceil(remaining)) + 60)
    latest = provider.get_instance(machine.id)
    existing = latest.lease_expires_at if isinstance(latest, Instance) else None
    cap = max(existing, floor) if existing is not None else floor
    deadline = min(cap, desired)
    provider.set_tags(machine, {"LeaseExpiresAt": deadline.isoformat(), "StoppedSince": ""})


def release_lease(provider: Provider, machine: Instance, *, owner: str) -> None:
    state = guest_lease(provider, machine, ["release", "--owner", owner], capture_json=True)
    if isinstance(state, dict) and isinstance(state.get("remaining"), (int, float)):
        sync_cloud_lease_from_guest(provider, machine, float(state["remaining"]))
    else:
        renew(provider, machine, DEFAULT_LEASE_SECONDS, preserve=True)


@contextlib.contextmanager
def scoped_lease(provider: Provider, machine: Instance, seconds: int, owner: str):
    acquire_lease(provider, machine, seconds, owner=owner)
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            release_lease(provider, machine, owner=owner)
        except Exception as error:
            if not failed:
                print(f"warning: could not release temporary lease {owner} on {machine.name}: {error}",
                      file=sys.stderr)


BOOT_LEASE_UNIT = (
    "[Unit]\n"
    "Description=Grant startup workspace lease before lease timer ticks\n"
    "Before=coder-lease.timer\n"
    "[Service]\n"
    "Type=oneshot\n"
    "ExecStart=/usr/local/bin/workspace-lease hold 600 --owner boot\n"
    "[Install]\n"
    "WantedBy=multi-user.target\n"
)


def repair_bootstrap(provider: Provider, machine: Instance) -> None:
    """Reapply declarative bootstrap after an interrupted cloud-init and upgrade runtime helpers."""
    raw = provider.read_userdata(machine)
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    config = json.loads(raw.decode().split("\n", 1)[1])
    helpers = _load_worker_helpers(provider)
    if "workspace-lease" not in helpers or "workspace-job" not in helpers:
        raise RuntimeError(f"Could not load runtime helpers for {provider.name} (check {provider.template_env_var})")
    repair = r'''
import json, os, pathlib, pwd, grp, subprocess, sys
payload = json.load(sys.stdin)
config = payload["config"]
helpers = payload["helpers"]
boot_unit = payload["boot_unit"]
user = pwd.getpwnam("ubuntu")
os.chown(user.pw_dir, user.pw_uid, user.pw_gid)
for target, content in helpers.items():
    dest = pathlib.Path("/usr/local/bin") / target
    temp = dest.with_suffix(".tmp")
    temp.write_text(content)
    temp.chmod(0o755)
    os.replace(temp, dest)
helper_paths = {str(pathlib.Path("/usr/local/bin") / target) for target in helpers}
for item in config["write_files"]:
    path = pathlib.Path(item["path"])
    if str(path) in helper_paths:
        continue
    content = item["content"]
    if str(path) == "/var/lib/cloud/scripts/per-boot/coder-lease":
        content = content.replace("/usr/local/bin/workspace-lease hold 600\n",
                                  "/usr/local/bin/workspace-lease hold 600 --owner boot\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content)
    temp.chmod(int(item.get("permissions", "0644"), 8))
    if "owner" in item:
        owner_user, owner_group = item["owner"].split(":")
        os.chown(temp, pwd.getpwnam(owner_user).pw_uid, grp.getgrnam(owner_group).gr_gid)
    os.replace(temp, path)
boot_unit_path = pathlib.Path("/etc/systemd/system/coder-lease-boot.service")
temp = boot_unit_path.with_suffix(".tmp")
temp.write_text(boot_unit)
temp.chmod(0o644)
os.replace(temp, boot_unit_path)
for command in config["runcmd"]:
    if command.strip() == "/usr/local/bin/workspace-lease hold 600":
        command = "/usr/local/bin/workspace-lease hold 600 --owner boot"
    subprocess.run(command, shell=True, check=True)
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "enable", "coder-lease-boot.service"], check=True)
'''
    remote(machine, ["sudo", "python3", "-c", repair],
           input=json.dumps({"config": config, "helpers": helpers, "boot_unit": BOOT_LEASE_UNIT}))


def ensure_controller_ssh(provider: Provider, machine: Instance) -> bool:
    try:
        cidr = current_ipv4_cidr()
    except Exception:
        return False
    if provider.ensure_ssh_access(machine, cidr):
        print(f"{machine.name}: updated SSH firewall rule for controller IP {cidr}", flush=True)
        return True
    return False


def bridge_target_ip(instance_id: str) -> str | None:
    res = subprocess.run(
        ["systemctl", "--user", "show", "-p", "Description", "--value", bridge_unit(instance_id)],
        capture_output=True, text=True,
    )
    if res.returncode != 0 or not isinstance(res.stdout, str):
        return None
    parts = res.stdout.strip().split()
    if len(parts) == 4 and parts[0] == "Coder" and parts[1] == "bridge" and parts[2] == instance_id:
        return parts[3]
    return None


def bridge_matches_ip(machine: Instance) -> bool:
    return bool(machine.public_ip) and bridge_target_ip(machine.id) == machine.public_ip


def start_bridge(machine: Instance) -> None:
    unit = bridge_unit(machine.id)
    subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
    run(["systemd-run", "--user", "--quiet", "--collect", "--unit", unit,
         "--description", f"Coder bridge {machine.id} {machine.public_ip}",
         "--property", "Restart=on-failure", "--property", "RestartSec=5",
         *ssh_args(machine)[:-1], "-NT", "-o", "ExitOnForwardFailure=yes", "-R",
         "127.0.0.1:7080:127.0.0.1:7080", ssh_args(machine)[-1]])


def reconnect_bridge(provider: Provider, machine: Instance) -> None:
    """Restore a dropped or stale-IP SSH bridge without touching leases or running bootstrap repair."""
    ip_updated = ensure_controller_ssh(provider, machine)
    state = bridge_state({"instance": machine.id})
    if state in ("active", "activating") and not ip_updated and bridge_matches_ip(machine):
        return
    start_bridge(machine)


CLOUD_INIT_READY_PROBE = (
    "test -f /var/lib/cloud/instance/boot-finished "
    "|| (command -v cloud-init >/dev/null && cloud-init status 2>/dev/null "
    "| grep -Eq 'status: (done|error|degraded|disabled)')"
)


def connect(provider: Provider, name: str, *, wait_timeout: int = 240, restart_bridge: bool = True) -> Instance:
    owner = api("users/me")["id"]
    machine = provider.find_instance(name, owner)
    if machine.state != "running":
        raise RuntimeError(f"Workspace is {machine.state}; use coder-{provider.name} resume first")
    ip_updated = ensure_controller_ssh(provider, machine)
    unit = bridge_unit(machine.id)
    if (restart_bridge or ip_updated or bridge_state({"instance": machine.id}) == "inactive"
            or not bridge_matches_ip(machine)):
        start_bridge(machine)
    deadline = time.monotonic() + wait_timeout
    last_probe = ""
    for attempt in range(max(1, math.ceil(wait_timeout / 3) + 1)):
        if attempt > 0 and time.monotonic() >= deadline:
            break
        ready = subprocess.run(
            [*ssh_args(machine), CLOUD_INIT_READY_PROBE],
            capture_output=True, text=True,
        )
        if ready.returncode == 0:
            if active_jobs(machine):
                remember_bridge(name, machine)
                print(f"{name}: bridge restored; preserving the active job without bootstrap changes")
                return machine
            repair_bootstrap(provider, machine)
            release_lease(provider, machine, owner="boot")
            remember_bridge(name, machine)
            print(f"{name}: private Coder bridge ready; default idle lease is five minutes")
            return machine
        detail = (ready.stderr or ready.stdout or "").strip().splitlines()
        last_probe = f"exit {ready.returncode}" + (f": {detail[-1]}" if detail else "")
        if attempt % 3 == 2:
            state = provider.get_instance(machine.id).state
            if state != "running":
                subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
                raise RuntimeError("Workspace stopped while reconnecting; resume it before retrying")
        if time.monotonic() >= deadline:
            break
        time.sleep(min(3, max(0.0, deadline - time.monotonic())))
    raise RuntimeError(f"SSH/bootstrap did not become ready within its bounded startup allowance ({last_probe})")


def save_bridge_records(updates: dict, *, remove: list[str] | None = None) -> None:
    path = STATE / "dashboard-builds.json"
    with (STATE / "dashboard-builds.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = json.loads(path.read_text()) if path.exists() else {}
        records.update(updates)
        for key in remove or ():
            records.pop(key, None)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(records))
        os.replace(temp, path)


def remember_bridge(name: str, machine: Instance) -> None:
    workspace = api("users/me/workspace/" + name)
    save_bridge_records({workspace["id"]: {
        "build": workspace["latest_build"]["id"],
        "instance": machine.id,
        "provider": machine.provider,
        "public_ip": machine.public_ip,
    }})


def should_connect(workspace: dict, previous: dict, bridge_alive: bool = True,
                   templates: tuple[str, ...] = ("aws-private", "oci-private")) -> bool:
    build = workspace["latest_build"]
    return (workspace["template_name"] in templates
            and build["status"] == "running"
            and (previous.get("build") != build["id"] or not bridge_alive))


def thread_origin(settings: dict, thread_id: str | None, thread_url: str | None = None) -> dict[str, str]:
    if not thread_id:
        return {"t3_thread_id": "", "t3_thread_url": ""}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_%.-]{0,239}", thread_id):
        raise RuntimeError("Invalid T3 thread ID (use the app ID from t3_thread_configure, not a provider session ID)")
    if not thread_url:
        home = Path(os.environ.get("T3CODE_HOME", str(Path.home() / ".t3")))
        environment = (home / "userdata/environment-id").read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9-]+", environment):
            raise RuntimeError("Invalid local T3 environment ID; specify --thread-url explicitly")
        base = settings["thread_base_url"]
        if not base:
            raise RuntimeError("Set thread_base_url or pass --thread-url explicitly")
        thread_url = base + "/" + environment + "/" + urllib.parse.quote(thread_id, safe="")
    parsed = urllib.parse.urlsplit(thread_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise RuntimeError("Thread link must be an HTTP(S) URL without credentials")
    if len(thread_url) > 256:
        raise RuntimeError("Thread URL exceeds the cloud tag-value limit of 256 characters")
    return {"t3_thread_id": thread_id, "t3_thread_url": thread_url}


def show_links(name: str, settings: dict, filter_hint: str) -> None:
    workspace = api("users/me/workspace/" + name)
    owner = urllib.parse.quote(workspace["owner_name"], safe="")
    url = settings["coder_url"] + "/@" + owner + "/" + urllib.parse.quote(name, safe="")
    print(f"[Open {name} in Coder]({url})", flush=True)
    print(filter_hint, flush=True)


def library(name: str):
    filename = {"transfer": "transfer.py", "github": "github_app.py"}[name]
    source = os.environ.get("CODER_AWS_" + name.upper() + "_LIB",
                            str(Path(__file__).parent / filename))
    spec = importlib.util.spec_from_file_location("workspace_" + name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def transfer_library():
    return library("transfer")


def authorize_repository(provider: Provider, name: str, repo: str, *, write: bool = False) -> None:
    github = library("github")
    github.repository(repo)
    owner = api("users/me")["id"]
    machine = provider.find_instance(name, owner)
    if machine.state != "running":
        raise RuntimeError("Resume the worker before authorizing a repository")
    ensure_controller_ssh(provider, machine)
    credential = github.mint(STATE / "github", repo, write=write)
    with scoped_lease(provider, machine, 300, f"op:auth-{uuid.uuid4().hex[:8]}"):
        source = Path(os.environ["CODER_AWS_GITHUB_HELPER"])
        remote(machine, ["sudo", "tee", "/usr/local/bin/workspace-github-credential"],
               input=source.read_text(), stdout=subprocess.DEVNULL)
        remote(machine, ["sudo", "chmod", "0755", "/usr/local/bin/workspace-github-credential"])
        install = """
import hashlib, json, os, pathlib, subprocess, sys
os.umask(0o077)
value = json.load(sys.stdin)
directory = pathlib.Path.home() / ".config/coder-github"
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
directory.chmod(0o700)
path = directory / (hashlib.sha256(value["repo"].lower().encode()).hexdigest() + ".json")
path.write_text(json.dumps(value["credential"]))
path.chmod(0o600)
subprocess.run(["git", "config", "--global", "credential.https://github.com.helper",
                "/usr/local/bin/workspace-github-credential"], check=True)
subprocess.run(["git", "config", "--global", "credential.https://github.com.useHttpPath", "true"], check=True)
"""
        remote(machine, ["python3", "-c", install], input=json.dumps({"repo": repo, "credential": {
            "token": credential["token"], "expires_at": credential["expires_at"]}}))
    print(f"{name}: {repo} HTTPS authorization ({'write' if write else 'read-only'}), expires {credential['expires_at']}")


UNITY_AUTH_ROOTS = (
    ".config/unity-cli", ".local/share/unity-cli",
    ".config/Unity", ".local/share/Unity",
    ".config/unity3d", ".local/share/unity3d", ".unity",
    # Unity CLI 1.x keeps its session in the Hub account store and plain-file keyring.
    ".config/unityhub", ".local/share/UnityAuthBroker", ".local/share/unity-keyring",
)
UNITY_AUTH_FILES = (
    ".config/unityhub/accounts.db",
    ".config/unityhub/Settings",
    ".config/unityhub/user-settings.json",
    ".config/unityhub/languageConfig.json",
    ".config/unity3d/Unity/config/production.json",
    ".local/share/unity-keyring/credentials.json",
    ".local/share/unity-keyring/insecure-storage",
)
UNITY_AUTH_DIR_ALLOWLIST = (
    ".local/share/unity-keyring",
    ".config/unity-cli",
    ".local/share/unity-cli",
)
UNITY_FILE_STORE_ENV = ("UNITY_INSECURE_STORAGE=1", "UNITY_NO_AUTH_BROKER=1")
UNITY_PERSONAL_PRODUCTS = frozenset({"Unity Personal", "Asset Store"})


def _combine_process_output(proc: subprocess.CompletedProcess) -> str:
    parts = []
    for stream in (proc.stderr, proc.stdout):
        if isinstance(stream, bytes):
            text = stream.decode("utf-8", "replace").strip()
        elif isinstance(stream, str):
            text = stream.strip()
        else:
            text = ""
        if text:
            parts.append(text)
    return "\n".join(parts)


def _unity_sandboxed_cmd(args: list[str], *, extra_env: tuple[str, ...] = (), runtime_max_sec: int = 180) -> str:
    """Run Unity CLI under systemd with ~@keyring (EPERM) so it uses the plain-file store."""
    env_props = " ".join(
        f"-p {shlex.quote('Environment=' + item)}" for item in (*UNITY_FILE_STORE_ENV, *extra_env)
    )
    quoted_args = " ".join(shlex.quote(arg) for arg in args)
    return (
        'UNITY_BIN=$(command -v unity) && '
        'exec sudo systemd-run --quiet --collect --uid="$(id -u)" --gid="$(id -g)" --pipe --wait '
        f'-p UMask=0077 -p RuntimeMaxSec={int(runtime_max_sec)} '
        "-p SystemCallErrorNumber=EPERM -p 'SystemCallFilter=~@keyring' "
        f'-p "Environment=HOME=$HOME" {env_props} '
        f'"$UNITY_BIN" {quoted_args}'
    )


def _parse_unity_license_products(output: str) -> list[str]:
    products: list[str] = []
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return products
    start = 1 if lines[0].lower().startswith("product\t") else 0
    for line in lines[start:]:
        cols = line.split("\t")
        if cols and cols[0].strip():
            products.append(cols[0].strip())
    return products


def _unity_unpack_script() -> str:
    roots_literal = repr(UNITY_AUTH_ROOTS)
    return f"""
import io, os, pathlib, sys, tarfile, tempfile
os.umask(0o077)
home = pathlib.Path.home().resolve()
roots = {roots_literal}

def assert_no_symlinks(rel_parts):
    cur = home
    for part in rel_parts:
        cur = cur / part
        if cur.is_symlink():
            raise RuntimeError(f"refusing to traverse symlink in Unity auth path: {{cur}}")

entries = []
with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode="r") as tf:
    for member in tf.getmembers():
        if not member.isfile():
            raise RuntimeError("unsafe non-file entry in Unity auth bundle")
        rel = pathlib.PurePosixPath(member.name)
        posix = rel.as_posix().removeprefix("./")
        rel = pathlib.PurePosixPath(posix)
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError("unsafe path in Unity auth bundle")
        matched_root = next((r for r in roots if posix.startswith(r + "/")), None)
        if matched_root is None:
            raise RuntimeError("unsafe path outside Unity auth roots")
        assert_no_symlinks(rel.parts)
        root_dir = (home / matched_root).resolve()
        dest = (home / posix).resolve()
        if not dest.is_relative_to(root_dir) or not dest.is_relative_to(home):
            raise RuntimeError("unsafe path outside Unity auth root")
        data = tf.extractfile(member).read()
        entries.append((rel, dest, data))

for rel, dest, data in entries:
    assert_no_symlinks(rel.parts)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=dest.parent, prefix=dest.name + ".", suffix=".tmp", delete=False
        ) as fh:
            tmp_name = fh.name
            os.chmod(tmp_name, 0o600)
            fh.write(data)
        if dest.suffix == ".db" or data.startswith(b"SQLite format 3\\x00"):
            for sidecar in ("-wal", "-shm", "-journal"):
                stale = dest.with_name(dest.name + sidecar)
                if stale.exists() or stale.is_symlink():
                    stale.unlink()
        os.replace(tmp_name, dest)
        tmp_name = None
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)

seal_parts = (".local", "share", "UnityAuthBroker")
cur = home
seal_safe = True
for part in seal_parts:
    cur = cur / part
    if cur.is_symlink():
        seal_safe = False
        break
if seal_safe:
    stale_seal = cur / "token-store.seal"
    if stale_seal.exists() or stale_seal.is_symlink():
        stale_seal.unlink()
"""


def _unity_pack_script() -> str:
    files_literal = repr(UNITY_AUTH_FILES)
    dirs_literal = repr(UNITY_AUTH_DIR_ALLOWLIST)
    return f"""
import io, os, pathlib, sqlite3, sys, tarfile, tempfile, time
os.umask(0o077)
home = pathlib.Path.home().resolve()
allow_files = {files_literal}
allow_dirs = {dirs_literal}
excluded_suffixes = (".log", "-wal", "-shm", "-journal", ".seal", ".lock", ".ulf")

def has_symlink_ancestor(path):
    rel = path.relative_to(home)
    cur = home
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False

candidates = {{}}
for rel in allow_files:
    path = home / rel
    if path.exists() and not has_symlink_ancestor(path) and path.is_file():
        candidates[rel] = path
for rel in allow_dirs:
    base = home / rel
    if not base.exists() or has_symlink_ancestor(base) or not base.is_dir():
        continue
    for path in sorted(base.rglob("*")):
        if not path.is_file() or has_symlink_ancestor(path):
            continue
        sub = path.relative_to(home).as_posix()
        if sub.endswith(excluded_suffixes):
            continue
        candidates[sub] = path

buf = io.BytesIO()
with tempfile.TemporaryDirectory() as tmpdir, tarfile.open(fileobj=buf, mode="w") as tf:
    for idx, sub in enumerate(sorted(candidates)):
        path = candidates[sub]
        with path.open("rb") as fh:
            header = fh.read(16)
            if header == b"SQLite format 3\\x00":
                snap_path = pathlib.Path(tmpdir) / f"snap-{{idx}}.db"
                deadline = time.monotonic() + 10.0
                def _check_deadline(status, remaining, total):
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"timed out snapshotting SQLite database {{sub}}")
                src = None
                dst = None
                try:
                    src = sqlite3.connect(str(path), timeout=5.0)
                    dst = sqlite3.connect(str(snap_path))
                    src.backup(dst, pages=64, progress=_check_deadline)
                except (sqlite3.Error, TimeoutError) as error:
                    raise RuntimeError(f"failed to snapshot SQLite database {{sub}}: {{error}}") from error
                finally:
                    if dst is not None:
                        dst.close()
                    if src is not None:
                        src.close()
                payload = snap_path.read_bytes()
            else:
                payload = header + fh.read()
        info = tarfile.TarInfo(sub)
        info.size = len(payload)
        info.mode = 0o600
        tf.addfile(info, io.BytesIO(payload))
sys.stdout.buffer.write(buf.getvalue())
"""


def _validate_unity_auth_bundle(raw: bytes) -> int:
    """Ensure a Unity CLI auth tar contains only regular files under UNITY_AUTH_ROOTS."""
    import io
    import tarfile
    if not raw:
        return 0
    count = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                raise RuntimeError(f"Unsafe non-file entry in Unity auth bundle: {member.name!r}")
            rel = Path(member.name)
            if rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError(f"Unsafe path in Unity auth bundle: {member.name!r}")
            posix = rel.as_posix().removeprefix("./")
            if not any(posix.startswith(root + "/") for root in UNITY_AUTH_ROOTS):
                raise RuntimeError(f"Unexpected path outside Unity auth roots: {member.name!r}")
            count += 1
    return count


def authorize_unity(provider: Provider, name: str, *, license_path: str | None = None,
                    create_alf: bool = False, alf_out: str | None = None,
                    login: bool = False, return_license: bool = False) -> None:
    """Activate Unity on a worker via Unity CLI Personal login or Enterprise .ulf."""
    owner = api("users/me")["id"]
    machine = provider.find_instance(name, owner)
    if machine.state != "running":
        raise RuntimeError("Resume the worker before configuring Unity licensing")
    ensure_controller_ssh(provider, machine)
    lease = 600 if login else 300
    with scoped_lease(provider, machine, lease, f"op:unity-{uuid.uuid4().hex[:8]}"):
        _run_authorize_unity(
            provider, name, machine, license_path=license_path,
            create_alf=create_alf, alf_out=alf_out, login=login, return_license=return_license,
        )


def _pack_and_cache_unity_bundle(machine: Instance, bundle: Path, *, announce: bool = True) -> bool:
    try:
        packed = subprocess.run(
            ssh_args(machine) + [shlex.join(["python3", "-c", _unity_pack_script()])],
            capture_output=True, check=True, timeout=60,
        ).stdout
    except subprocess.CalledProcessError as error:
        detail = _combine_process_output(error)
        raise RuntimeError(f"Failed to pack Unity CLI auth state on {machine.name}: {detail}") from error
    if _validate_unity_auth_bundle(packed) > 0:
        tmp = bundle.with_suffix(".tmp")
        tmp.write_bytes(packed)
        tmp.chmod(0o600)
        os.replace(tmp, bundle)
        if announce:
            print(f"Cached Unity CLI auth state to {bundle} (mode 0600)", flush=True)
        return True
    if announce:
        print("warning: no Unity CLI auth files found to cache", file=sys.stderr, flush=True)
    return False


def _run_authorize_unity(provider: Provider, name: str, machine: Instance, *,
                         license_path: str | None = None, create_alf: bool = False,
                         alf_out: str | None = None, login: bool = False,
                         return_license: bool = False) -> None:
    unity_dir = STATE / "unity"
    unity_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    unity_dir.chmod(0o700)

    if return_license:
        has_cli = subprocess.run(ssh_args(machine) + ["command -v unity >/dev/null"], capture_output=True)
        wipe_cmd = [
            "bash", "-c",
            "rm -rf ~/.config/unity* ~/.local/share/unity* ~/.config/Unity ~/.local/share/Unity ~/.unity "
            "~/.local/share/UnityAuthBroker",
        ]
        if has_cli.returncode != 0:
            remote(machine, wipe_cmd)
            print(f"{name}: cleared worker Unity session (Unity CLI not installed)")
            return
        ret = subprocess.run(
            ssh_args(machine) + [_unity_sandboxed_cmd(["license", "return", "-y"])],
            capture_output=True, text=True,
        )
        if ret.returncode == 0:
            remote(machine, wipe_cmd)
            print(f"{name}: returned Unity license and cleared worker Unity session")
            return
        detail = _combine_process_output(ret)
        status = subprocess.run(
            ssh_args(machine) + [_unity_sandboxed_cmd(["license", "status"])],
            capture_output=True, text=True,
        )
        if status.returncode == 4:
            remote(machine, wipe_cmd)
            print(f"{name}: no active Unity license; cleared worker Unity session")
            return
        products = _parse_unity_license_products(status.stdout) if status.returncode == 0 else []
        if (products and set(products).issubset(UNITY_PERSONAL_PRODUCTS)
                and "Personal licenses, can't be returned" in detail):
            remote(machine, wipe_cmd)
            print(f"{name}: Unity Personal license cannot be returned remotely; cleared worker Unity session")
            return
        raise RuntimeError(
            f"Failed to return Unity license on {name} (exit {ret.returncode}); "
            f"keeping worker Unity session for retry.\n{detail}"
        )

    if create_alf:
        stage = remote(machine, ["mktemp", "-d", "/tmp/unity-alf-XXXXXXXX"], capture_output=True).stdout.strip()
        try:
            alf_log = stage + "/unity-alf.log"
            remote(machine, ["bash", "-c",
                f"cd {shlex.quote(stage)} && (unity-editor -batchmode -nographics -createManualActivationFile -logfile {shlex.quote(alf_log)} || true)"])
            found = remote(machine, ["bash", "-c", f"ls -1 {shlex.quote(stage)}/*.alf 2>/dev/null | head -n 1"],
                           capture_output=True).stdout.strip()
            if not found:
                log_tail = remote(machine, ["bash", "-c", f"tail -n 30 {shlex.quote(alf_log)} 2>/dev/null || true"],
                                  capture_output=True).stdout
                raise RuntimeError(f"Unity did not produce a .alf file:\n{log_tail}")
            target = Path(alf_out).expanduser() if alf_out else (unity_dir / Path(found).name)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = remote(machine, ["base64", found], capture_output=True).stdout
            import base64
            target.write_bytes(base64.b64decode("".join(data.split())))
            target.chmod(0o600)
        finally:
            remote(machine, ["rm", "-rf", stage])
        print(f"Saved Unity activation request to {target}")
        print("Offline .alf/.ulf activation is Enterprise/Industry only; for Personal seats run:")
        print(f"  coder-{provider.name} authorize-unity {name} --login")
        return

    if license_path:
        source = Path(license_path).expanduser()
        if not source.is_file():
            raise RuntimeError(f"Unity license file not found at {source}")
        install = """
import os, pathlib, sys
os.umask(0o077)
data = sys.stdin.buffer.read()
for rel in (".local/share/unity3d/Unity/Unity_lic.ulf", ".config/unity3d/Unity/Unity_lic.ulf"):
    target = pathlib.Path.home() / rel
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_bytes(data)
    target.chmod(0o600)
"""
        subprocess.run(ssh_args(machine) + [shlex.join(["python3", "-c", install])],
                       input=source.read_bytes(), check=True)
        print(f"{name}: delivered private Unity license from {source} (mode 0600)")
        return

    # Default Personal flow using official Unity CLI (/usr/local/bin/unity).
    has_cli = subprocess.run(ssh_args(machine) + ["command -v unity >/dev/null"], capture_output=True)
    if has_cli.returncode != 0:
        raise RuntimeError(
            f"Unity CLI not found on {name}. Bake /usr/local/bin/unity into the image "
            "or pass --license FILE for an Enterprise .ulf."
        )
    bundle = unity_dir / "auth-bundle.tar"
    replayed = False
    if not login and bundle.is_file():
        bundle_bytes = bundle.read_bytes()
        if _validate_unity_auth_bundle(bundle_bytes) == 0:
            raise RuntimeError(f"Cached Unity auth bundle at {bundle} is empty; run with --login")
        try:
            subprocess.run(
                ssh_args(machine) + [shlex.join(["python3", "-c", _unity_unpack_script()])],
                input=bundle_bytes, capture_output=True, check=True, timeout=60,
            )
        except subprocess.CalledProcessError as error:
            detail = _combine_process_output(error)
            raise RuntimeError(f"Failed to unpack Unity auth bundle on {name}: {detail}") from error
        status = subprocess.run(
            ssh_args(machine) + [_unity_sandboxed_cmd(["auth", "status"])],
            capture_output=True, text=True,
        )
        if status.returncode != 0:
            detail = _combine_process_output(status)
            suffix = f"\n{detail}" if detail else ""
            raise RuntimeError(
                f"Cached Unity session was rejected on {name}; re-authenticate with:\n"
                f"  coder-{provider.name} authorize-unity {name} --login{suffix}"
            )
        replayed = True
    else:
        print(f"{name}: starting Unity CLI browser sign-in (open the printed URL on your laptop)...", flush=True)
        # Unity CLI 1.x prefers the RAM-only Linux kernel keyring (keyutils) whenever @keyring syscalls
        # succeed, skipping credentials.json even with --insecure-storage. Run Unity CLI under ~@keyring
        # (matching standard container seccomp) so it reads and writes its plain-file store.
        subprocess.run(
            ssh_args(machine) + [_unity_sandboxed_cmd(
                ["auth", "login", "--insecure-storage"],
                extra_env=("BROWSER=true",),
                runtime_max_sec=600,
            )],
            check=True,
        )
        _pack_and_cache_unity_bundle(machine, bundle, announce=True)

    run(ssh_args(machine) + [_unity_sandboxed_cmd(["license", "activate", "--personal", "--accept-eula"])])
    run(ssh_args(machine) + [_unity_sandboxed_cmd(["license", "status"])])
    if replayed:
        try:
            _pack_and_cache_unity_bundle(machine, bundle, announce=False)
        except Exception as error:
            print(f"warning: could not refresh cached Unity auth bundle from {name}: {error}", file=sys.stderr)
    print(f"{name}: Unity Personal license activated")


def list_images_cmd(providers: list[Provider], *, as_json: bool = False) -> None:
    rows = []
    for prov in providers:
        try:
            rows.extend(prov.list_images(include_pending=True))
        except Exception as error:
            print(f"warning: could not query {prov.name} images: {error}", file=sys.stderr)
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No private baked images.")
        return
    print(f"{'NAME':30} {'PROV':4} {'ARCH':5} {'DISK':5} {'STATE':12} {'HASH':10} {'ID'}")
    for row in rows:
        disk = f"{row['disk_gb']}G" if row.get("disk_gb") else "-"
        print(f"{row['name']:30} {row['provider']:4} {row['arch']:5} {disk:5} "
              f"{row.get('state', 'available'):12} {row['hash'][:10]:10} {row['id']}")


def _reap_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def bake_image(provider: Provider, settings: dict, *, spec_path: str,
               instance_type: str | None = None, timeout: int = 2400, force: bool = False) -> dict:
    if timeout <= 0:
        raise ValueError("bake-image --timeout must be positive")
    min_gb = 50 if provider.name == "oci" else 30
    pre = image_specs.load_spec(spec_path, default_disk_gb=min_gb)
    raw_base = provider.base_image_id(pre["arch"]) if hasattr(provider, "base_image_id") else ""
    base_id = raw_base if isinstance(raw_base, str) else ""
    spec = image_specs.load_spec(spec_path, default_disk_gb=min_gb, base_image_id=base_id)
    disk_gb = max(min_gb, spec["disk_gb"])
    existing = provider.list_images()
    same_name = [img for img in existing if img["name"] == spec["name"] and img.get("arch", spec["arch"]) == spec["arch"]]
    if not force:
        exact = [img for img in same_name if img["hash"] == spec["hash"] and img.get("state", "available") == "available"]
        if exact:
            print(f"Image {spec['name']} ({spec['hash']}) already up to date: {exact[0]['id']}")
            return exact[0]
    size = instance_type or provider.default_builder_type(spec["arch"])
    if provider.name == "oci":
        is_arm_size = size.startswith("a1-")
        if (spec["arch"] == "arm64") != is_arm_size:
            raise RuntimeError(f"Spec arch {spec['arch']} does not match OCI builder size {size}")
    elif spec["arch"] != "amd64":
        raise RuntimeError("AWS template supports amd64 images only")
    provider.assert_guard(instance_type=size, check_create=True, disk_gb=disk_gb)

    builder = f"bake-{spec['hash'][:8]}-{uuid.uuid4().hex[:4]}"
    command = [
        "coder", "create", builder, "--template", provider.template_name, "--yes",
        "--automatic-updates", "always",
        "--parameter", "instance_type=" + size,
        "--parameter", "disk_gb=" + str(disk_gb),
        "--parameter", "image_id=",
        "--parameter", "keep_workspace=false",
        "--parameter", "t3_thread_id=",
        "--parameter", "t3_thread_url=",
    ]
    log = STATE / (builder + "-build.log")
    owner = api("users/me")["id"]
    failed = True
    with (STATE / (builder + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            print(f"Baking private {provider.name.upper()} image {spec['name']} ({spec['hash']}) on {builder} ({size}, {disk_gb} GiB)...", flush=True)
            with log.open("w") as output:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
                try:
                    process.wait(timeout=420)
                except subprocess.TimeoutExpired:
                    _reap_process(process)
                    raise RuntimeError(f"Builder creation timed out; see {log}")
            if process.returncode:
                tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:])
                raise RuntimeError(f"Builder creation failed; see {log}\n{tail}")
            machine = connect(provider, builder)
            acquire_lease(provider, machine, timeout + 600, owner="op:bake")
            if spec["packages"]:
                print(f"Installing {len(spec['packages'])} apt packages...", flush=True)
                remote(machine, ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get",
                                 "-o", "DPkg::Lock::Timeout=120", "update", "-qq"])
                remote(machine, ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get",
                                 "-o", "DPkg::Lock::Timeout=120", "install",
                                 "-y", "--no-install-recommends", *spec["packages"]])
            if spec["setup_script_text"]:
                print("Running image setup_script...", flush=True)
                remote(machine, ["sudo", "tee", "/tmp/coder-bake-setup.sh"],
                       input=spec["setup_script_text"], stdout=subprocess.DEVNULL)
                remote(machine, ["sudo", "chmod", "0755", "/tmp/coder-bake-setup.sh"])
                remote(machine, ["sudo", "-E", "bash", "/tmp/coder-bake-setup.sh"])
                remote(machine, ["sudo", "rm", "-f", "/tmp/coder-bake-setup.sh"])
            for cmd in spec["commands"]:
                print(f"Running image command: {cmd}", flush=True)
                remote(machine, ["sudo", "-E", "bash", "-lc", cmd])
            # Sanitize builder state, lease units, host keys, logs, and credentials before snapshotting.
            sanitize = (
                "set -e; "
                "sudo systemctl disable --now coder-workspace.service coder-lease-boot.service coder-lease.timer "
                "apt-daily.timer apt-daily-upgrade.timer || true; "
                "sudo rm -rf /etc/systemd/system/coder-workspace.service /etc/systemd/system/coder-lease-boot.service "
                "/etc/systemd/system/coder-lease.service /etc/systemd/system/coder-lease.timer "
                "/var/lib/cloud/scripts/per-boot/coder-lease "
                "/opt/coder-agent.sh /var/lib/coder-workspace/lease.json /var/lib/coder-workspace/lease.lock "
                "/home/ubuntu/.config/coder-github /home/ubuntu/.config/unity* /home/ubuntu/.local/share/unity* "
                "/home/ubuntu/.config/Unity /home/ubuntu/.local/share/Unity /home/ubuntu/.unity "
                "/home/ubuntu/.local/share/UnityAuthBroker /root/.local/share/UnityAuthBroker "
                "/root/.config/unity* /root/.local/share/unity* /root/.config/Unity /root/.local/share/Unity "
                "/root/.unity /root/.cache /root/.bash_history /home/ubuntu/.bash_history "
                "/home/ubuntu/.ssh/authorized_keys /root/.ssh/authorized_keys /etc/ssh/ssh_host_* "
                "/home/ubuntu/.local/state/coder-jobs /home/ubuntu/.cache/coder-transfers "
                "/home/ubuntu/projects/snapshots /tmp/coder-bake-* /tmp/unity-* /var/tmp/*; "
                # Setup runs as root with HOME preserved, so it can leave root-owned dirs in ~ubuntu.
                "sudo chown -R ubuntu:ubuntu /home/ubuntu; "
                "sudo apt-get -o DPkg::Lock::Timeout=120 clean || true; "
                "sudo rm -rf /var/lib/apt/lists/*; "
                "sudo journalctl --rotate --vacuum-time=1s || true; "
                "sudo truncate -s 0 /var/log/auth.log /var/log/syslog 2>/dev/null || true; "
                "sudo cloud-init clean --logs --machine-id; "
                "sync"
            )
            remote(machine, ["bash", "-c", sanitize])
            print("Stopping builder before snapshotting private image...", flush=True)
            transition(builder, "stop")
            subprocess.run(["systemctl", "--user", "stop", bridge_unit(machine.id)], capture_output=True)
            machine = provider.find_instance(builder, owner)
            print("Creating private cloud image (waiting for AVAILABLE)...", flush=True)
            created = provider.create_image(machine, image_name=spec["name"], image_hash=spec["hash"],
                                            arch=spec["arch"], disk_gb=disk_gb)
            print(f"Private {provider.name.upper()} image ready: {created['name']} -> {created['id']}", flush=True)
            for old in same_name:
                if old["id"] != created["id"]:
                    try:
                        provider.delete_image(old["id"])
                        print(f"Removed superseded image {old['id']}", flush=True)
                    except Exception as error:
                        print(f"warning: could not remove old image {old['id']}: {error}", file=sys.stderr)
            failed = False
            return created
        finally:
            print(f"Cleaning up temporary builder {builder}...", flush=True)
            try:
                delete_workspace(provider, builder, force=True, keep_log=failed)
            except Exception as error:
                print(f"warning: builder cleanup failed for {builder}: {error}", file=sys.stderr)


def project_record(name: str, machine: Instance | None = None) -> dict:
    path = STATE / "manifests" / (name + ".json")
    if not path.exists():
        tags = machine.tags if machine else {}
        snapshot = tags.get("SourceSnapshot", "")
        if not re.fullmatch(r"[0-9a-f]{24}", snapshot):
            raise RuntimeError("No source snapshot registered; run sync with a manifest first")
        root = "/home/ubuntu/projects/snapshots/" + snapshot
        plan = json.loads(remote(machine, ["cat", root + "/manifest.json"], capture_output=True).stdout)
        if plan["snapshot"] != snapshot:
            raise RuntimeError("Remote source registry does not match its cloud record")
        return {"manifest": plan, "instance_id": machine.id, "projects": {
            p["name"]: root + "/" + transfer_library().relative(p["destination"]) for p in plan["projects"]}}
    record = json.loads(path.read_text())
    if machine and record.get("instance_id", machine.id) != machine.id:
        raise RuntimeError("Local source registry belongs to another instance")
    return record


def sync_projects(provider: Provider, name: str, machine: Instance, manifest: str) -> None:
    lib = transfer_library()
    ensure_controller_ssh(provider, machine)
    if active_jobs(machine):
        raise RuntimeError("Wait for the running job before syncing another source snapshot")
    ready = subprocess.run(ssh_args(machine) + ["test -x /usr/local/bin/workspace-transfer"], capture_output=True)
    if ready.returncode:
        connect(provider, name)
    with scoped_lease(provider, machine, 900, f"op:sync-{uuid.uuid4().hex[:8]}"):
        with tempfile.TemporaryDirectory(prefix="transfer-", dir=STATE) as temporary:
            inputs = Path(temporary) / "input"
            plan = lib.pack(manifest, inputs)
            print(json.dumps(plan, indent=2), flush=True)
            remote(machine, ["mkdir", "-p", "/home/ubuntu/.cache/coder-transfers"])
            stage = remote(machine, ["mktemp", "-d", "/home/ubuntu/.cache/coder-transfers/input-XXXXXXXX"],
                           capture_output=True).stdout.strip()
            try:
                run(["rsync", "-azs", "-e", shlex.join(ssh_args(machine)[:-1]), str(inputs) + "/",
                     ssh_args(machine)[-1] + ":" + stage + "/"])
                directories = json.loads(remote(machine, ["/usr/local/bin/workspace-transfer", "prepare", stage],
                                                capture_output=True).stdout)
            finally:
                remote(machine, ["/usr/local/bin/workspace-transfer", "cleanup", stage])
        records = STATE / "manifests"
        records.mkdir(exist_ok=True)
        temp = records / (name + ".tmp")
        temp.write_text(json.dumps({"manifest": plan, "projects": directories, "instance_id": machine.id}, indent=2))
        os.replace(temp, records / (name + ".json"))
        provider.set_tags(machine, {
            "SourceSnapshot": plan["snapshot"],
            "SourceProjects": str(len(plan["projects"])),
        })
    print("Prepared projects: " + json.dumps(directories), flush=True)


def fetch_projects(provider: Provider, name: str, machine: Instance) -> None:
    record = project_record(name, machine)
    install_worker_helpers(provider, machine)
    stage = json.loads(remote(machine, ["/usr/local/bin/workspace-transfer", "collect",
                                        record["manifest"]["snapshot"]], capture_output=True).stdout)
    target = STATE / "results" / name / (record["manifest"]["snapshot"] + "-" + uuid.uuid4().hex[:8])
    target.mkdir(parents=True)
    run(["rsync", "-azs", "-e", shlex.join(ssh_args(machine)[:-1]),
         ssh_args(machine)[-1] + ":" + stage + "/", str(target) + "/"])
    transfer_library().restore(target)
    (target / "retrieved.json").write_text(json.dumps({
        "verified": True, "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat()}))
    remote(machine, ["/usr/local/bin/workspace-transfer", "cleanup", stage])
    print(f"Verified project metadata and artifacts retrieved into {target}", flush=True)


def _normalize_job_id(job_id: str | None) -> str | None:
    if not job_id:
        return None
    cleaned = job_id.removesuffix(".service")
    if not re.fullmatch(r"coder-job-[0-9a-f]{1,32}", cleaned):
        raise RuntimeError(f"Invalid job ID: {job_id!r} (expected coder-job-<hex>)")
    return cleaned


def read_job_logs(machine: Instance, *, job_id: str | None = None, lines: int = 80) -> tuple[dict | None, str]:
    job_id = _normalize_job_id(job_id)
    script = r'''
import json, pathlib, sys
job_id = sys.argv[1]
lines = int(sys.argv[2])
base = pathlib.Path.home() / ".local/state/coder-jobs"
if not base.exists():
    print(json.dumps({"record": None, "output": ""}))
    sys.exit(0)
if job_id:
    target = base / job_id
else:
    dirs = [p for p in base.iterdir() if p.is_dir()]
    if not dirs:
        print(json.dumps({"record": None, "output": ""}))
        sys.exit(0)
    target = max(dirs, key=lambda p: (p / "result.json").stat().st_mtime if (p / "result.json").exists() else p.stat().st_mtime)
record_path = target / "result.json"
record = json.loads(record_path.read_text()) if record_path.exists() else {"job": target.name}
log_path = target / "output.log"
output = ""
if log_path.exists():
    raw = log_path.read_text(errors="replace").splitlines()
    output = "\n".join(raw[-lines:]) if lines > 0 else "\n".join(raw)
print(json.dumps({"record": record, "output": output}))
'''
    res = remote(machine, ["python3", "-c", script, job_id or "", str(lines)], capture_output=True)
    data = json.loads(res.stdout)
    return data["record"], data["output"]


def wait_for_job(provider: Provider, name: str, machine: Instance, *,
                 job_id: str | None = None, poll: int = 10, max_wait: int = 0) -> int:
    job_id = _normalize_job_id(job_id)
    ensure_controller_ssh(provider, machine)
    start = time.monotonic()
    target_unit = (job_id + ".service") if job_id else None
    while True:
        running = active_jobs(machine)
        still_running = (target_unit in running) if target_unit else bool(running)
        if not still_running:
            break
        if max_wait > 0 and (time.monotonic() - start) >= max_wait:
            raise RuntimeError(f"Wait limit ({max_wait}s) reached while job {running[0]} is still active")
        time.sleep(max(1, poll))
    record, output = read_job_logs(machine, job_id=job_id, lines=40)
    if record is None:
        print(f"{name}: no job records found")
        return 0
    code = record.get("exit_status")
    status_label = "timed out" if record.get("timed_out") else f"exit {code}"
    print(f"{name}: job {record.get('job', '?')} finished ({status_label})", flush=True)
    if output:
        print(output, flush=True)
    return int(code) if isinstance(code, int) else 0


def cleanup_local_state(name: str, *, instance_id: str | None = None,
                        workspace_id: str | None = None, keep_log: bool = False) -> None:
    if instance_id:
        subprocess.run(["systemctl", "--user", "stop", bridge_unit(instance_id)], capture_output=True)
        known_hosts_path(instance_id).unlink(missing_ok=True)
    paths = [STATE / "manifests" / (name + ".json")]
    if not keep_log:
        paths.append(STATE / (name + "-build.log"))
    for path in paths:
        path.unlink(missing_ok=True)
    if workspace_id:
        save_bridge_records({}, remove=[workspace_id])


def delete_workspace(provider: Provider, name: str, *, force: bool = False, keep_log: bool = False) -> None:
    owner = api("users/me")["id"]
    workspace = None
    try:
        workspace = api("users/me/workspace/" + name)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    if workspace is not None and workspace.get("template_name") and workspace["template_name"] != provider.template_name:
        raise RuntimeError(
            f"Workspace {name} belongs to template {workspace['template_name']}, not {provider.template_name}; "
            f"use the matching provider CLI"
        )
    matches = [item for item in provider.list_instances(owner) if item.name == name]
    if len(matches) > 1 and not force:
        ids = ", ".join(m.id for m in matches)
        raise RuntimeError(f"Multiple {provider.name.upper()} instances match {name} ({ids}); pass --force to proceed")
    machine = matches[0] if len(matches) == 1 else None
    if machine and machine.state == "running" and not force:
        try:
            ensure_controller_ssh(provider, machine)
            running = active_jobs(machine)
            if running:
                raise RuntimeError(f"Job {running[0]} is still running; wait for it or pass --force")
        except subprocess.CalledProcessError as error:
            raise RuntimeError(
                f"Could not verify running jobs on {name} over SSH ({error}); retry or pass --force"
            ) from error
    if workspace is not None:
        run(["coder", "delete", name, "--yes"])
    elif matches:
        if not force:
            ids = ", ".join(m.id for m in matches)
            raise RuntimeError(
                f"Orphaned {provider.name.upper()} instance(s) for {name} ({ids}) have no Coder workspace; "
                f"pass --force to terminate them directly"
            )
        for orphan in matches:
            provider.terminate_instance(orphan)
            cleanup_local_state(name, instance_id=orphan.id, keep_log=keep_log)
        print(f"{name}: terminated orphaned {provider.name.upper()} instance(s)")
        return
    else:
        raise RuntimeError(f"Workspace {name} not found in Coder or {provider.name.upper()}")
    cleanup_local_state(name, instance_id=machine.id if machine else None,
                        workspace_id=workspace["id"] if workspace else None, keep_log=keep_log)
    print(f"{name}: deleted workspace, instance, and root disk")


def format_remaining(deadline: dt.datetime | None, now: dt.datetime) -> str:
    if deadline is None:
        return "-"
    delta = int((deadline - now).total_seconds())
    if delta <= 0:
        return "expired"
    minutes, secs = divmod(delta, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def list_workspaces(providers: list[Provider], *, as_json: bool = False) -> None:
    owner = api("users/me")["id"]
    coder_list = api("workspaces?q=owner%3Ame")["workspaces"]
    by_template = {p.template_name: p for p in providers}
    cloud_by_name: dict[tuple[str, str], Instance] = {}
    for prov in providers:
        try:
            for item in prov.list_instances(owner):
                cloud_by_name[(prov.name, item.name)] = item
        except Exception as error:
            print(f"warning: could not query {prov.name} instances: {error}", file=sys.stderr)
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    for ws in coder_list:
        prov = by_template.get(ws["template_name"])
        if not prov:
            continue
        inst = cloud_by_name.get((prov.name, ws["name"]))
        rows.append({
            "name": ws["name"],
            "provider": prov.name,
            "template": ws["template_name"],
            "coder_status": ws["latest_build"]["status"],
            "cloud_state": inst.state if inst else "missing",
            "size": inst.instance_type if inst else "-",
            "lease": format_remaining(inst.lease_expires_at, now) if inst and inst.state == "running" else "-",
            "keep": inst.keep_workspace if inst else False,
            "stopped_since": inst.tags.get("StoppedSince", "") if inst else "",
        })
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No managed workspaces.")
        return
    header = f"{'NAME':24} {'PROV':4} {'STATE':9} {'SIZE':14} {'LEASE':9} {'KEEP':4}"
    print(header)
    for row in rows:
        print(f"{row['name']:24} {row['provider']:4} {row['cloud_state']:9} {row['size']:14} "
              f"{row['lease']:9} {'yes' if row['keep'] else 'no':4}")


def gc_workspaces(providers: list[Provider], settings: dict, *,
                  older_than_hours: int | None = None, dry_run: bool = False,
                  owner_id: str | None = None, workspaces: list[dict] | None = None) -> dict:
    """Enforce expired leases on running workers and delete stale stopped workspaces."""
    owner = owner_id or api("users/me")["id"]
    if older_than_hours is not None and older_than_hours < 0:
        raise ValueError("--older-than-hours must be >= 0")
    explicit_cutoff = older_than_hours is not None
    cutoff_hours = int(settings["delete_after_stopped_hours"]) if older_than_hours is None else older_than_hours
    ws_list = workspaces if workspaces is not None else api("workspaces?q=owner%3Ame")["workspaces"]
    coder_by_key = {(ws.get("template_name", ""), ws["name"]): ws for ws in ws_list}
    now = dt.datetime.now(dt.timezone.utc)
    stopped: list[str] = []
    deleted: list[str] = []
    marked: list[str] = []
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    for prov in providers:
        try:
            instances = prov.list_instances(owner)
        except Exception as error:
            print(f"warning: gc could not list {prov.name} instances: {error}", file=sys.stderr)
            continue
        for inst in instances:
            if not inst.name:
                continue
            lock_path = STATE / (re.sub(r"[^A-Za-z0-9_.-]", "_", inst.name) + ".lock")
            with lock_path.open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    if not dry_run:
                        fresh = prov.get_instance(inst.id)
                        if isinstance(fresh, Instance):
                            inst = fresh
                    if inst.state == "running":
                        deadline = inst.lease_expires_at
                        if deadline is None or deadline <= now:
                            if not dry_run:
                                prov.stop_instance(inst)
                                subprocess.run(["systemctl", "--user", "stop", bridge_unit(inst.id)], capture_output=True)
                            stopped.append(f"{prov.name}:{inst.name}")
                    elif inst.state == "stopped":
                        if inst.keep_workspace or (not explicit_cutoff and cutoff_hours <= 0):
                            continue
                        since = inst.stopped_since
                        if since is None:
                            if not dry_run:
                                prov.set_tags(inst, {"StoppedSince": now.isoformat()})
                            marked.append(f"{prov.name}:{inst.name}")
                            continue
                        age_hours = (now - since).total_seconds() / 3600.0
                        if age_hours >= cutoff_hours:
                            ws = coder_by_key.get((prov.template_name, inst.name))
                            tag_ws_id = inst.tags.get("WorkspaceId", "")
                            if ws and (not tag_ws_id or tag_ws_id == ws["id"]):
                                if not dry_run:
                                    run(["coder", "delete", inst.name, "--yes"])
                                    cleanup_local_state(inst.name, instance_id=inst.id, workspace_id=ws["id"])
                                    notify("Auto-deleted stale workspace",
                                           f"Deleted {prov.name} workspace {inst.name} (stopped {age_hours:.1f}h >= {cutoff_hours}h)")
                                deleted.append(f"{prov.name}:{inst.name}")
                            else:
                                print(f"warning: stale orphaned {prov.name} instance {inst.name} ({inst.id}) "
                                      f"has no matching Coder workspace; delete with coder-{prov.name} delete {inst.name} --force",
                                      file=sys.stderr)
                except Exception as error:
                    print(f"warning: gc failed for {prov.name}:{inst.name} ({inst.id}): {error}", file=sys.stderr)
    return {"stopped": stopped, "marked": marked, "deleted": deleted, "dry_run": dry_run}


def bridge_state(record: dict) -> str:
    if not record.get("instance"):
        return "inactive"
    res = subprocess.run(
        ["systemctl", "--user", "is-active", bridge_unit(record["instance"])],
        capture_output=True, text=True,
    )
    if res.returncode == 0:
        return "active"
    if isinstance(res.stdout, str) and res.stdout.strip() == "activating":
        return "activating"
    return "inactive"


def bridge_running(record: dict) -> bool:
    return bridge_state(record) == "active"


def configured_providers(settings: dict) -> list[Provider]:
    providers: list[Provider] = [AwsProvider(settings)]
    oci_provider = OciProvider(settings, state_dir=STATE)
    if oci_provider.infra_path.exists():
        providers.append(oci_provider)
    return providers


def reconcile(settings: dict, providers: list[Provider] | None = None) -> None:
    """Connect each successful dashboard start once, enforce OCI leases, and prune stale stopped workers."""
    registry_path = STATE / "dashboard-builds.json"
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    active_providers = providers if providers is not None else configured_providers(settings)
    by_template = {p.template_name: p for p in active_providers}
    workspaces = api("workspaces?q=owner%3Ame")["workspaces"]
    owner = api("users/me")["id"]
    try:
        for workspace in workspaces:
            prov = by_template.get(workspace["template_name"])
            if not prov or workspace["name"].startswith("bake-"):
                continue
            previous = registry.get(workspace["id"], {})
            build = workspace["latest_build"]
            bridge_alive = bridge_running(previous)
            try:
                if should_connect(workspace, previous, bridge_alive, tuple(by_template.keys())):
                    with (STATE / (workspace["name"] + ".lock")).open("a") as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        latest = json.loads(registry_path.read_text()) if registry_path.exists() else {}
                        current = latest.get(workspace["id"], {})
                        is_new_build = current.get("build") != build["id"]
                        if not is_new_build and bridge_running(current):
                            continue
                        machine = prov.find_instance(workspace["name"], owner)
                        if machine.state != "running":
                            subprocess.run(["systemctl", "--user", "stop", bridge_unit(machine.id)], capture_output=True)
                            continue
                        try:
                            prov.assert_guard(instance_type=machine.tags.get("InstanceSize") or None)
                        except RuntimeError as guard_err:
                            print(f"warning: stopping guarded {prov.name} workspace {workspace['name']}: {guard_err}",
                                  file=sys.stderr)
                            prov.stop_instance(machine)
                            subprocess.run(["systemctl", "--user", "stop", bridge_unit(machine.id)], capture_output=True)
                            continue
                        if is_new_build:
                            connect(prov, workspace["name"], wait_timeout=15, restart_bridge=False)
                            save_bridge_records({workspace["id"]: {
                                "build": build["id"],
                                "instance": machine.id,
                                "provider": prov.name,
                                "public_ip": machine.public_ip,
                            }})
                        else:
                            reconnect_bridge(prov, machine)
                elif build["status"] in ("stopped", "deleted", "failed") and previous.get("instance"):
                    subprocess.run(["systemctl", "--user", "stop", bridge_unit(previous["instance"])],
                                   capture_output=True)
            except Exception as error:
                print(f"warning: reconcile failed for {workspace['name']}: {error}", file=sys.stderr)
    finally:
        # Always run periodic lease-stop + stale-workspace auto-delete regardless of bridge errors.
        stamp = STATE / "last-gc.ts"
        now_ts = time.time()
        last = float(stamp.read_text()) if stamp.exists() else 0.0
        if now_ts - last >= GC_INTERVAL_SECONDS:
            gc_workspaces(active_providers, settings, owner_id=owner, workspaces=workspaces)
            stamp.write_text(str(now_ts))


def build_parser(prog: str, default_provider: str, instance_types: tuple[str, ...], default_size: str):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Private Coder cloud workers with bounded leases and automatic stale cleanup. Default timeout: 300s.",
    )
    parser.add_argument("--provider", choices=("aws", "oci"), default=default_provider,
                        help="Cloud backend (default: " + default_provider + ")")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("setup")
    sub.add_parser("publish-template")
    sub.add_parser("reconcile")
    sub.add_parser("configure-auth")
    sub.add_parser("teardown-network")
    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--all", action="store_true", help="List workspaces across all configured providers")
    list_cmd.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    gc_cmd = sub.add_parser("gc")
    gc_cmd.add_argument("--dry-run", action="store_true")
    gc_cmd.add_argument("--older-than-hours", type=int, default=None)
    gc_cmd.add_argument("--all", action="store_true")
    images_cmd = sub.add_parser("images")
    images_cmd.add_argument("--all", action="store_true", help="List private images across all configured providers")
    images_cmd.add_argument("--json", action="store_true")
    bake_cmd = sub.add_parser("bake-image")
    bake_cmd.add_argument("--spec", required=True, help="Path to image spec JSON (e.g. .coder-images/unity.json)")
    bake_cmd.add_argument("--instance-type", choices=list(instance_types), help="Temporary builder size")
    bake_cmd.add_argument("--timeout", type=int, default=2400, help="Max bake setup seconds (default 2400)")
    bake_cmd.add_argument("--force", action="store_true", help="Rebuild even if matching hash already exists")
    del_img_cmd = sub.add_parser("delete-image")
    del_img_cmd.add_argument("image", help="Scoped image name or cloud image ID")
    github_login = sub.add_parser("github-login-setup")
    github_login.add_argument("--port", type=int, default=8787)
    github_setup = sub.add_parser("github-setup")
    github_setup.add_argument("--port", type=int, default=8786)
    plan_command = sub.add_parser("plan")
    plan_command.add_argument("--manifest", required=True)
    workspace_actions = (
        "authorize", "authorize-unity", "create", "sync", "link", "links", "resume", "connect",
        "stop", "delete", "keep", "upgrade", "status", "hold", "extend",
        "run", "wait", "logs", "fetch",
    )
    for action in workspace_actions:
        command = sub.add_parser(action)
        command.add_argument("workspace")
        if action == "authorize":
            command.add_argument("--repo", required=True)
            command.add_argument("--write", action="store_true", help="Grant repository contents write until token expiry")
        if action == "authorize-unity":
            mode_group = command.add_mutually_exclusive_group()
            mode_group.add_argument("--login", action="store_true", help="Sign into Unity CLI on the worker and cache auth locally")
            mode_group.add_argument("--return", dest="return_license", action="store_true", help="Return returnable Unity licenses and clear worker Unity session")
            mode_group.add_argument("--license", help="Enterprise/Industry offline Unity_lic.ulf path")
            mode_group.add_argument("--create-alf", action="store_true", help="Generate an offline .alf request (Enterprise/Industry)")
            command.add_argument("--alf-out", help="Output path when --create-alf is set")
        if action in ("hold", "extend", "run"):
            command.add_argument("--timeout", type=int, default=300, help="Finite timeout in seconds; default 300")
        if action == "create":
            command.add_argument("--manifest")
            command.add_argument("--instance-type", default=default_size, choices=list(instance_types))
            command.add_argument("--disk-gb", type=int, default=None, help="Root disk size in GiB (AWS: 30-100, OCI: 50-100)")
            command.add_argument("--image", help="Private baked image name or cloud ID (see images)")
            command.add_argument("--keep", action="store_true", help="Exempt from automatic stopped-workspace deletion")
        if action in ("create", "link"):
            command.add_argument("--thread-id", default=os.environ.get("T3_THREAD_ID"), required=action == "link")
            command.add_argument("--thread-url", help="Explicit HTTP(S) T3 thread link")
        if action == "run":
            command.add_argument("--project", help="Project name matching projects[].name in the manifest")
            command.add_argument("--wait", action="store_true", help="Wait for the job to finish and print its result")
            command.add_argument("--poll", type=int, default=10, help="Poll interval in seconds when --wait is set")
            command.description = "Run one detached job. Put job arguments after --. Default timeout: 300s."
        if action == "wait":
            command.add_argument("--job", help="Specific coder-job-<id> unit name")
            command.add_argument("--poll", type=int, default=10, help="Poll interval in seconds")
            command.add_argument("--timeout", type=int, default=0, help="Max seconds to wait (0 = until job ends)")
        if action == "logs":
            command.add_argument("--job", help="Specific coder-job-<id> unit name")
            command.add_argument("--lines", type=int, default=80, help="Tail line count (0 = full log)")
        if action == "delete":
            command.add_argument("--force", action="store_true", help="Delete even if a job is currently running")
        if action == "keep":
            command.add_argument("--off", action="store_true", help="Re-enable automatic deletion after stopping")
        if action == "sync":
            command.add_argument("--manifest", required=True)
    return parser


def main(default_provider: str = "aws", prog: str | None = None) -> int:
    os.umask(0o077)
    settings = load_settings()
    cli = sys.argv[1:]
    # Peek at --provider early so --instance-type choices match the selected backend.
    selected_provider = default_provider
    for idx, token in enumerate(cli):
        if token == "--":
            break
        if token == "--provider" and idx + 1 < len(cli):
            selected_provider = cli[idx + 1]
        elif token.startswith("--provider="):
            selected_provider = token.split("=", 1)[1]
    proto = AwsProvider if selected_provider == "aws" else OciProvider
    parser = build_parser(prog or f"coder-{selected_provider}", selected_provider,
                          proto.instance_types, proto.default_instance_type)
    separator = cli.index("--") if "--" in cli else len(cli)
    job_command = cli[separator + 1:]
    args = parser.parse_args(cli[:separator])

    if args.action in ("github-setup", "github-login-setup"):
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        setup_lock = (STATE / "github-setup.lock").open("a")
        try:
            fcntl.flock(setup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("GitHub setup is already running; use its existing setup URL")
        library("github").setup(
            STATE / ("github-login" if args.action == "github-login-setup" else "github"),
            args.port, owner=settings["github_owner"], app_name=settings["github_app_name"],
            login_url=settings["coder_url"] if args.action == "github-login-setup" else None,
        )
        return 0
    if args.action == "plan":
        print(json.dumps(transfer_library().inventory(args.manifest), indent=2))
        return 0
    if args.action in ("create", "sync") and args.manifest:
        print(json.dumps(transfer_library().inventory(args.manifest), indent=2), flush=True)
    if args.action == "run" and not job_command:
        parser.error("run requires a command after --")
    if hasattr(args, "workspace") and not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,30}[a-zA-Z0-9])?", args.workspace):
        parser.error("workspace names must be 1–32 letters, digits or internal hyphens")
    if args.action == "gc" and args.older_than_hours is not None and args.older_than_hours < 0:
        parser.error("--older-than-hours must be >= 0")
    if args.action == "bake-image" and args.timeout <= 0:
        parser.error("--timeout must be positive")
    origin = thread_origin(settings, args.thread_id, args.thread_url) if args.action in ("create", "link") else None
    if args.action == "create" and os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE") == "T3 Code" and not args.thread_id:
        parser.error("T3 agents must supply --thread-id from t3_thread_configure; CODEX_THREAD_ID is a different ID")

    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    if args.action == "reconcile":
        reconcile(settings)
        return 0

    provider = make_provider(args.provider, settings, bootstrap=args.action in ("setup", "configure-auth"))
    # Cloud-only or self-locking commands do not require local Coder or the global setup.lock.
    if args.action == "images":
        providers = configured_providers(settings) if args.all else [provider]
        list_images_cmd(providers, as_json=args.json)
        return 0
    if args.action == "delete-image":
        deleted = provider.delete_image(args.image)
        print(f"Deleted private {provider.name.upper()} image {deleted['name']} ({deleted['id']})")
        return 0
    if args.action == "configure-auth":
        if not isinstance(provider, AwsProvider):
            raise RuntimeError("configure-auth applies to AWS only; OCI uses ~/.oci/config")
        provider.configure_auth()
        return 0
    if args.action == "teardown-network":
        if not isinstance(provider, OciProvider):
            raise RuntimeError("teardown-network applies to OCI only")
        provider.teardown_network()
        return 0
    if args.action == "list":
        providers = configured_providers(settings) if args.all else [provider]
        list_workspaces(providers, as_json=args.json)
        return 0
    if args.action == "gc":
        providers = configured_providers(settings) if args.all else [provider]
        result = gc_workspaces(providers, settings, older_than_hours=args.older_than_hours, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0
    if args.action == "bake-image":
        bake_image(provider, settings, spec_path=args.spec, instance_type=args.instance_type,
                   timeout=args.timeout, force=args.force)
        return 0
    if args.action == "links":
        show_links(args.workspace, settings, provider.filter_hint)
        return 0
    if args.action in ("wait", "logs"):
        owner = api("users/me")["id"]
        machine = provider.find_instance(args.workspace, owner)
        if machine.state != "running":
            msg = ("Workspace is not running; resume it to inspect logs"
                   if args.action == "wait" else "Resume the workspace before reading logs")
            raise RuntimeError(msg)
        if args.action == "wait":
            return wait_for_job(provider, args.workspace, machine, job_id=args.job, poll=args.poll, max_wait=args.timeout)
        ensure_controller_ssh(provider, machine)
        record, output = read_job_logs(machine, job_id=args.job, lines=args.lines)
        if record is None:
            print(f"{args.workspace}: no job records found")
            return 0
        print(json.dumps(record, indent=2))
        if output:
            print("--- output ---")
            print(output)
        return 0

    lock = (STATE / (getattr(args, "workspace", "setup") + ".lock")).open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        return _run_locked_action(args, provider, settings, origin, job_command, parser, lock)
    finally:
        lock.close()


def _run_locked_action(args, provider: Provider, settings: dict, origin: dict | None,
                       job_command: list[str], parser: argparse.ArgumentParser, lock) -> int:
    if args.action in ("setup", "publish-template"):
        publish(provider, settings, refresh_guard=args.action == "setup")
        return 0
    owner = api("users/me")["id"]
    if args.action == "authorize":
        authorize_repository(provider, args.workspace, args.repo, write=args.write)
        return 0
    if args.action == "authorize-unity":
        authorize_unity(provider, args.workspace, license_path=args.license,
                        create_alf=args.create_alf, alf_out=args.alf_out,
                        login=args.login, return_license=args.return_license)
        return 0
    if args.action == "upgrade":
        upgrade(provider, args.workspace)
        return 0
    if args.action == "delete":
        delete_workspace(provider, args.workspace, force=args.force)
        return 0

    if args.action in ("create", "resume"):
        if args.action == "create":
            min_gb = 50 if provider.name == "oci" else 30
            resolved_image = ""
            image_min_gb = min_gb
            if args.image:
                image_info = provider.resolve_image_info(args.image)
                resolved_image = image_info["id"]
                size_arch = provider.image_arch_for_size(args.instance_type)
                if image_info.get("arch") and image_info["arch"] != size_arch:
                    raise RuntimeError(
                        f"Image {args.image} is {image_info['arch']}, incompatible with "
                        f"--instance-type {args.instance_type} ({size_arch})"
                    )
                if image_info.get("disk_gb"):
                    image_min_gb = max(min_gb, int(image_info["disk_gb"]))
            disk_gb = image_min_gb if args.disk_gb is None else args.disk_gb
            if not (min_gb <= disk_gb <= 100):
                parser.error(f"--disk-gb for {provider.name} must be between {min_gb} and 100")
            if disk_gb < image_min_gb:
                parser.error(f"--disk-gb ({disk_gb}) must be at least image size ({image_min_gb} GiB)")
            provider.assert_guard(instance_type=args.instance_type, check_create=True, disk_gb=disk_gb)
            command = [
                "coder", "create", args.workspace, "--template", provider.template_name, "--yes",
                "--automatic-updates", "always",
                "--parameter", "instance_type=" + args.instance_type,
                "--parameter", "disk_gb=" + str(disk_gb),
                "--parameter", "image_id=" + resolved_image,
                "--parameter", "keep_workspace=" + ("true" if args.keep else "false"),
            ]
            for key, value in origin.items():
                command.extend(["--parameter", key + "=" + value])
            log = STATE / (args.workspace + "-build.log")
            with log.open("w") as output:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
                try:
                    process.wait(timeout=420)
                except subprocess.TimeoutExpired:
                    _reap_process(process)
                    raise RuntimeError(f"Coder build timed out; see {log}")
            if process.returncode:
                tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:])
                raise RuntimeError(f"Coder build failed; see {log}\n{tail}")
            show_links(args.workspace, settings, provider.filter_hint)
            connect(provider, args.workspace)
            if args.manifest:
                sync_projects(provider, args.workspace, provider.find_instance(args.workspace, owner), args.manifest)
            return 0
        machine = provider.find_instance(args.workspace, owner)
        provider.assert_guard(instance_type=machine.tags.get("InstanceSize") or None, check_create=True, check_quota=False)
        if machine.state == "running":
            workspace = api("users/me/workspace/" + args.workspace)
            if workspace["latest_build"]["status"] == "failed":
                ensure_controller_ssh(provider, machine)
                if active_jobs(machine):
                    raise RuntimeError("Preserve the active job; reconcile Coder after it finishes")
                transition(args.workspace, "start")
            connect(provider, args.workspace)
            return 0
        renew(provider, machine, 900)
        workspace = api("users/me/workspace/" + args.workspace)
        if workspace["latest_build"]["status"] != "stopped":
            transition(args.workspace, "stop")
        transition(args.workspace, "start")
        connect(provider, args.workspace)
        return 0

    machine = provider.find_instance(args.workspace, owner)
    if args.action == "sync":
        if machine.state != "running":
            raise RuntimeError("Resume the workspace before syncing sources")
        sync_projects(provider, args.workspace, machine, args.manifest)
    elif args.action == "link":
        if machine.tags.get("T3ThreadId") and machine.tags["T3ThreadId"] != args.thread_id:
            raise RuntimeError("Workspace already has a different creating thread; refusing to replace its origin")
        if machine.state != "stopped":
            raise RuntimeError("Link an existing workspace while it is stopped, to preserve active jobs")
        transition(args.workspace, "stop", origin)
        show_links(args.workspace, settings, provider.filter_hint)
    elif args.action == "keep":
        value = "false" if args.off else "true"
        provider.set_tags(machine, {"KeepWorkspace": value})
        print(f"{args.workspace}: KeepWorkspace={value}")
    elif args.action == "status":
        print(json.dumps({
            "provider": provider.name,
            "instance": machine.id,
            "state": machine.state,
            "type": machine.instance_type,
            "public_ip": machine.public_ip,
            "keep": machine.keep_workspace,
            "tags": [{"Key": k, "Value": v} for k, v in sorted(machine.tags.items())],
        }, indent=2))
    elif args.action == "stop":
        transition(args.workspace, "stop")
        subprocess.run(["systemctl", "--user", "stop", bridge_unit(machine.id)], capture_output=True)
        try:
            provider.set_tags(machine, {"StoppedSince": dt.datetime.now(dt.timezone.utc).isoformat()})
        except Exception:
            pass
    elif args.action == "connect":
        connect(provider, args.workspace)
    elif args.action in ("hold", "extend", "run"):
        if args.timeout <= 0:
            parser.error("timeout must be positive and finite")
        if machine.state != "running":
            raise RuntimeError("Resume the workspace before running commands or holding leases")
        ensure_controller_ssh(provider, machine)
        if args.action == "hold":
            acquire_lease(provider, machine, args.timeout + 300, owner="hold")
        elif args.action == "extend":
            units = active_jobs(machine, include_deactivating=False)
            if len(units) != 1:
                raise RuntimeError("extend requires exactly one running job (use hold for idle/interactive work)")
            job = units[0].removesuffix(".service")
            lease_owner = f"job:{job}"
            acquire_lease(provider, machine, args.timeout + 300, owner=lease_owner)
            try:
                remote(machine, ["/usr/local/bin/workspace-job", "extend", job, str(args.timeout)])
            except BaseException:
                try:
                    if f"{job}.service" not in active_jobs(machine, include_deactivating=False):
                        release_lease(provider, machine, owner=lease_owner)
                except Exception:
                    pass
                raise
            if f"{job}.service" not in active_jobs(machine, include_deactivating=False):
                release_lease(provider, machine, owner=lease_owner)
                raise RuntimeError(f"Job {job} finished before extend completed")
        else:
            if active_jobs(machine):
                raise RuntimeError("A job is already running; this worker runs one job at a time")
            directory = project_record(args.workspace, machine)["projects"].get(args.project) if args.project else "/home/ubuntu/projects"
            if not directory:
                raise RuntimeError(f"Unknown manifest project: {args.project}")
            job = "coder-job-" + uuid.uuid4().hex[:12]
            lease_owner = f"job:{job}"
            acquire_lease(provider, machine, args.timeout + 300, owner=lease_owner)
            try:
                remote(machine, ["sudo", "systemd-run", "--unit", job, "--uid", "ubuntu", "--gid", "ubuntu",
                    "--property", "WorkingDirectory=" + directory,
                    "--property", f"ExecStopPost=/usr/local/bin/workspace-job finalize {job}",
                    "/usr/local/bin/workspace-job", "run", job, str(args.timeout)] + job_command)
            except BaseException:
                # Only release the job lease if the transient unit is confirmed not to have started.
                try:
                    if f"{job}.service" not in active_jobs(machine):
                        release_lease(provider, machine, owner=lease_owner)
                except Exception:
                    pass
                raise
            print(f"Detached job {job}; timeout {args.timeout}s; use coder-{provider.name} wait/fetch {args.workspace}", flush=True)
            if args.wait:
                lock.close()
                return wait_for_job(provider, args.workspace, machine, job_id=job, poll=args.poll)
    elif args.action == "fetch":
        if machine.state != "running":
            raise RuntimeError("Resume the stopped workspace before fetching its preserved results")
        ensure_controller_ssh(provider, machine)
        if active_jobs(machine):
            raise RuntimeError(f"Wait for the registered job before collecting (use coder-{provider.name} wait {args.workspace})")
        target = STATE / "results" / args.workspace
        target.mkdir(parents=True, exist_ok=True)
        with scoped_lease(provider, machine, 300, f"op:fetch-{uuid.uuid4().hex[:8]}"):
            logs = subprocess.run(ssh_args(machine) + ["test -d /home/ubuntu/.local/state/coder-jobs"], capture_output=True)
            if logs.returncode == 0:
                run(["rsync", "-azs", "-e", shlex.join(ssh_args(machine)[:-1]),
                     ssh_args(machine)[-1] + ":.local/state/coder-jobs/", str(target) + "/"])
            if (STATE / "manifests" / (args.workspace + ".json")).exists() or "SourceSnapshot" in machine.tags:
                fetch_projects(provider, args.workspace, machine)
        print(f"Results retrieved into {target}")
    return 0


def run_cli(default_provider: str = "aws", prog: str | None = None) -> None:
    label = prog or f"coder-{default_provider}"
    try:
        code = main(default_provider=default_provider, prog=label)
        if code:
            sys.exit(code)
    except subprocess.CalledProcessError as error:
        print(f"{label}: command failed ({error.returncode}): {shlex.join(error.cmd)}", file=sys.stderr)
        if error.stderr:
            err = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else error.stderr
            print(err.strip(), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        if isinstance(error, (RuntimeError, ValueError, urllib.error.URLError, FileNotFoundError)) or type(error).__name__ in ("ClientError", "ServiceError"):
            print(f"{label}: {error}", file=sys.stderr)
            sys.exit(1)
        raise
