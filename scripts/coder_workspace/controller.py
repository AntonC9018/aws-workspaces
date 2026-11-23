"""Provider-agnostic Coder workspace controller with bounded leases and automatic cleanup."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import gzip
import importlib.util
import ipaddress
import json
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
    provider.assert_guard()
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds + 60)
    if preserve:
        latest = provider.get_instance(machine.id)
        existing = latest.lease_expires_at
        if existing is not None:
            deadline = max(deadline, existing)
    provider.set_tags(machine, {"LeaseExpiresAt": deadline.isoformat(), "StoppedSince": ""})


def ssh_args(machine: Instance) -> list[str]:
    if not machine.public_ip:
        raise RuntimeError(f"Workspace {machine.name} has no public IP address yet")
    return [
        "ssh", "-i", str(key_path()), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ForwardAgent=no",
        "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={STATE / 'known_hosts'}",
        "ubuntu@" + machine.public_ip,
    ]


def remote(machine: Instance, command: list[str], **kwargs):
    return run(ssh_args(machine) + [shlex.join(command)], **kwargs)


def active_jobs(machine: Instance) -> list[str]:
    active = remote(machine, ["systemctl", "list-units", "--state=running", "--no-legend",
                              "coder-job-*.service"], capture_output=True)
    return [line.split()[0] for line in active.stdout.splitlines() if line.strip()]


def repair_bootstrap(provider: Provider, machine: Instance) -> None:
    """Reapply declarative bootstrap after an interrupted cloud-init and upgrade runtime helpers."""
    raw = provider.read_userdata(machine)
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    config = json.loads(raw.decode().split("\n", 1)[1])
    repair = r'''
import json, os, pathlib, pwd, grp, subprocess, sys
config = json.load(sys.stdin)
user = pwd.getpwnam("ubuntu")
os.chown(user.pw_dir, user.pw_uid, user.pw_gid)
for item in config["write_files"]:
    path = pathlib.Path(item["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(item["content"])
    path.chmod(int(item.get("permissions", "0644"), 8))
    if "owner" in item:
        user, group = item["owner"].split(":")
        os.chown(path, pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid)
for command in config["runcmd"]:
    subprocess.run(command, shell=True, check=True)
'''
    remote(machine, ["sudo", "python3", "-c", repair], input=json.dumps(config))
    template_root = Path(os.environ[provider.template_env_var])
    for filename, target in (("workspace_lease.py", "workspace-lease"), ("workspace_job.py", "workspace-job"),
                             ("workspace_transfer.py", "workspace-transfer"),
                             ("github_credential.py", "workspace-github-credential")):
        source = template_root / filename
        remote(machine, ["sudo", "tee", "/usr/local/bin/" + target],
               input=source.read_text(), stdout=subprocess.DEVNULL)
        remote(machine, ["sudo", "chmod", "0755", "/usr/local/bin/" + target])


def ensure_controller_ssh(provider: Provider, machine: Instance) -> None:
    try:
        cidr = current_ipv4_cidr()
    except Exception:
        return
    if provider.ensure_ssh_access(machine, cidr):
        print(f"{machine.name}: updated SSH firewall rule for controller IP {cidr}", flush=True)


def connect(provider: Provider, name: str) -> Instance:
    owner = api("users/me")["id"]
    machine = provider.find_instance(name, owner)
    if machine.state != "running":
        raise RuntimeError(f"Workspace is {machine.state}; use coder-{provider.name} resume first")
    ensure_controller_ssh(provider, machine)
    renew(provider, machine, 600)
    unit = bridge_unit(machine.id)
    subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
    run(["systemd-run", "--user", "--quiet", "--collect", "--unit", unit,
         "--property", "Restart=on-failure", "--property", "RestartSec=5"] +
        ssh_args(machine)[:-1] + ["-NT", "-o", "ExitOnForwardFailure=yes", "-R",
                                  "127.0.0.1:7080:127.0.0.1:7080", ssh_args(machine)[-1]])
    deadline = time.monotonic() + 240
    for attempt in range(90):
        if time.monotonic() >= deadline:
            break
        readiness = ("if test -x /usr/local/bin/workspace-lease; then sudo /usr/local/bin/workspace-lease hold 600; fi; "
                     "test -f /var/lib/cloud/instance/boot-finished")
        ready = subprocess.run(ssh_args(machine) + [readiness],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ready.returncode == 0:
            if active_jobs(machine):
                remember_bridge(name, machine)
                print(f"{name}: bridge restored; preserving the active job without bootstrap changes")
                return machine
            repair_bootstrap(provider, machine)
            remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "complete"])
            renew(provider, machine, 300, preserve=False)
            remember_bridge(name, machine)
            print(f"{name}: private Coder bridge ready; default lease is five minutes")
            return machine
        if attempt % 3 == 2:
            state = provider.get_instance(machine.id).state
            if state != "running":
                subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
                raise RuntimeError("Workspace stopped while reconnecting; resume it before retrying")
        time.sleep(3)
    raise RuntimeError("SSH/bootstrap did not become ready within its bounded startup allowance")


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
        "build": workspace["latest_build"]["id"], "instance": machine.id, "provider": machine.provider}})


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
    renew(provider, machine, 300)
    remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "hold", "300"])
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
    renew(provider, machine, 900)
    remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "hold", "900"])
    ready = subprocess.run(ssh_args(machine) + ["test -x /usr/local/bin/workspace-transfer"], capture_output=True)
    if ready.returncode:
        connect(provider, name)
        renew(provider, machine, 900)
        remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "hold", "900"])
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
    remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "complete"])
    renew(provider, machine, 300, preserve=False)
    print("Prepared projects: " + json.dumps(directories), flush=True)


def fetch_projects(name: str, machine: Instance) -> None:
    record = project_record(name, machine)
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
    print(f"Verified project checkouts and artifacts retrieved into {target}", flush=True)


def read_job_logs(machine: Instance, *, job_id: str | None = None, lines: int = 80) -> tuple[dict | None, str]:
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


def cleanup_local_state(name: str, *, instance_id: str | None = None, workspace_id: str | None = None) -> None:
    if instance_id:
        subprocess.run(["systemctl", "--user", "stop", bridge_unit(instance_id)], capture_output=True)
    for path in (STATE / "manifests" / (name + ".json"), STATE / (name + "-build.log")):
        path.unlink(missing_ok=True)
    if workspace_id:
        save_bridge_records({}, remove=[workspace_id])


def delete_workspace(provider: Provider, name: str, *, force: bool = False) -> None:
    owner = api("users/me")["id"]
    workspace = None
    try:
        workspace = api("users/me/workspace/" + name)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    machine = None
    matches = [item for item in provider.list_instances(owner) if item.name == name]
    if len(matches) == 1:
        machine = matches[0]
    if machine and machine.state == "running" and not force:
        try:
            ensure_controller_ssh(provider, machine)
            running = active_jobs(machine)
            if running:
                raise RuntimeError(f"Job {running[0]} is still running; wait for it or pass --force")
        except subprocess.CalledProcessError:
            pass
    if workspace is not None:
        run(["coder", "delete", name, "--yes"])
    elif machine is None:
        raise RuntimeError(f"Workspace {name} not found in Coder or {provider.name.upper()}")
    cleanup_local_state(name, instance_id=machine.id if machine else None,
                        workspace_id=workspace["id"] if workspace else None)
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
    cutoff_hours = int(settings["delete_after_stopped_hours"]) if older_than_hours is None else older_than_hours
    ws_list = workspaces if workspaces is not None else api("workspaces?q=owner%3Ame")["workspaces"]
    coder_by_name = {ws["name"]: ws for ws in ws_list}
    now = dt.datetime.now(dt.timezone.utc)
    stopped: list[str] = []
    deleted: list[str] = []
    marked: list[str] = []
    for prov in providers:
        try:
            instances = prov.list_instances(owner)
        except Exception:
            continue
        for inst in instances:
            if inst.state == "running":
                deadline = inst.lease_expires_at
                if deadline is None or deadline <= now:
                    if not dry_run:
                        prov.stop_instance(inst)
                        subprocess.run(["systemctl", "--user", "stop", bridge_unit(inst.id)], capture_output=True)
                    stopped.append(f"{prov.name}:{inst.name}")
            elif inst.state == "stopped":
                if inst.keep_workspace or cutoff_hours <= 0:
                    continue
                since = inst.stopped_since
                if since is None:
                    if not dry_run:
                        prov.set_tags(inst, {"StoppedSince": now.isoformat()})
                    marked.append(f"{prov.name}:{inst.name}")
                    continue
                age_hours = (now - since).total_seconds() / 3600.0
                if age_hours >= cutoff_hours and inst.name in coder_by_name:
                    if not dry_run:
                        run(["coder", "delete", inst.name, "--yes"])
                        cleanup_local_state(inst.name, instance_id=inst.id,
                                            workspace_id=coder_by_name[inst.name]["id"])
                        notify("Auto-deleted stale workspace",
                               f"Deleted {prov.name} workspace {inst.name} (stopped {age_hours:.1f}h >= {cutoff_hours}h)")
                    deleted.append(f"{prov.name}:{inst.name}")
    return {"stopped": stopped, "marked": marked, "deleted": deleted, "dry_run": dry_run}


def bridge_running(record: dict) -> bool:
    return bool(record.get("instance")) and subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", bridge_unit(record["instance"])],
        capture_output=True).returncode == 0


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
    updates = {}
    active_providers = providers if providers is not None else configured_providers(settings)
    by_template = {p.template_name: p for p in active_providers}
    workspaces = api("workspaces?q=owner%3Ame")["workspaces"]
    owner = api("users/me")["id"]
    for workspace in workspaces:
        prov = by_template.get(workspace["template_name"])
        if not prov:
            continue
        previous = registry.get(workspace["id"], {})
        build = workspace["latest_build"]
        bridge_alive = bridge_running(previous)
        if should_connect(workspace, previous, bridge_alive, tuple(by_template.keys())):
            with (STATE / (workspace["name"] + ".lock")).open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                latest = json.loads(registry_path.read_text()) if registry_path.exists() else {}
                current = latest.get(workspace["id"], {})
                if current.get("build") == build["id"] and bridge_running(current):
                    continue
                machine = prov.find_instance(workspace["name"], owner)
                if machine.state != "running":
                    continue
                connect(prov, workspace["name"])
                updates[workspace["id"]] = {"build": build["id"], "instance": machine.id, "provider": prov.name}
        elif build["status"] in ("stopped", "deleted", "failed") and previous.get("instance"):
            subprocess.run(["systemctl", "--user", "stop", bridge_unit(previous["instance"])],
                           capture_output=True)
    if updates:
        save_bridge_records(updates)
    # Run lease-stop + stale-workspace auto-delete at most once every 5 minutes.
    stamp = STATE / "last-gc.ts"
    now_ts = time.time()
    last = float(stamp.read_text()) if stamp.exists() else 0.0
    if now_ts - last >= GC_INTERVAL_SECONDS:
        stamp.write_text(str(now_ts))
        gc_workspaces(active_providers, settings, owner_id=owner, workspaces=workspaces)


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
    github_login = sub.add_parser("github-login-setup")
    github_login.add_argument("--port", type=int, default=8787)
    github_setup = sub.add_parser("github-setup")
    github_setup.add_argument("--port", type=int, default=8786)
    plan_command = sub.add_parser("plan")
    plan_command.add_argument("--manifest", required=True)
    workspace_actions = (
        "authorize", "create", "sync", "link", "links", "resume", "connect",
        "stop", "delete", "keep", "upgrade", "status", "hold", "extend",
        "run", "wait", "logs", "fetch",
    )
    for action in workspace_actions:
        command = sub.add_parser(action)
        command.add_argument("workspace")
        if action == "authorize":
            command.add_argument("--repo", required=True)
            command.add_argument("--write", action="store_true", help="Grant repository contents write until token expiry")
        if action in ("hold", "extend", "run"):
            command.add_argument("--timeout", type=int, default=300, help="Finite timeout in seconds; default 300")
        if action == "create":
            command.add_argument("--manifest")
            command.add_argument("--instance-type", default=default_size, choices=list(instance_types))
            command.add_argument("--disk-gb", type=int, default=None, help="Root disk size in GiB (AWS: 30-100, OCI: 50-100)")
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
    if hasattr(args, "workspace") and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,31}", args.workspace):
        parser.error("workspace names must be 1–32 letters, digits or hyphens")
    origin = thread_origin(settings, args.thread_id, args.thread_url) if args.action in ("create", "link") else None
    if args.action == "create" and os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE") == "T3 Code" and not args.thread_id:
        parser.error("T3 agents must supply --thread-id from t3_thread_configure; CODEX_THREAD_ID is a different ID")

    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    if args.action == "reconcile":
        reconcile(settings)
        return 0

    lock = (STATE / (getattr(args, "workspace", "setup") + ".lock")).open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    owner = api("users/me")["id"]

    provider = make_provider(args.provider, settings, bootstrap=args.action in ("setup", "configure-auth"))
    if args.action == "links":
        show_links(args.workspace, settings, provider.filter_hint)
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
    if args.action in ("setup", "publish-template"):
        publish(provider, settings, refresh_guard=args.action == "setup")
        return 0
    if args.action == "authorize":
        authorize_repository(provider, args.workspace, args.repo, write=args.write)
        return 0
    if args.action == "upgrade":
        upgrade(provider, args.workspace)
        return 0
    if args.action == "delete":
        delete_workspace(provider, args.workspace, force=args.force)
        return 0

    if args.action in ("create", "resume"):
        if args.action == "create":
            provider.assert_guard(instance_type=args.instance_type, check_create=True, disk_gb=args.disk_gb)
            min_gb = 50 if provider.name == "oci" else 30
            disk_gb = min_gb if args.disk_gb is None else args.disk_gb
            if not (min_gb <= disk_gb <= 100):
                parser.error(f"--disk-gb for {provider.name} must be between {min_gb} and 100")
            command = [
                "coder", "create", args.workspace, "--template", provider.template_name, "--yes",
                "--automatic-updates", "always",
                "--parameter", "instance_type=" + args.instance_type,
                "--parameter", "disk_gb=" + str(disk_gb),
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
                    process.terminate()
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
        provider.assert_guard(instance_type=machine.tags.get("InstanceSize") or None, check_create=True)
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
    elif args.action == "wait":
        if machine.state != "running":
            raise RuntimeError("Workspace is not running; resume it to inspect logs")
        return wait_for_job(provider, args.workspace, machine, job_id=args.job, poll=args.poll, max_wait=args.timeout)
    elif args.action == "logs":
        if machine.state != "running":
            raise RuntimeError("Resume the workspace before reading logs")
        ensure_controller_ssh(provider, machine)
        record, output = read_job_logs(machine, job_id=args.job, lines=args.lines)
        if record is None:
            print(f"{args.workspace}: no job records found")
            return 0
        print(json.dumps(record, indent=2))
        if output:
            print("--- output ---")
            print(output)
    elif args.action in ("hold", "extend", "run"):
        if args.timeout <= 0:
            parser.error("timeout must be positive and finite")
        if machine.state != "running":
            raise RuntimeError("Resume the workspace before running commands or holding leases")
        ensure_controller_ssh(provider, machine)
        units = active_jobs(machine)
        if units and args.action == "run":
            raise RuntimeError("A job is already running; this worker runs one job at a time")
        if args.action == "extend":
            if len(units) != 1:
                raise RuntimeError("extend requires exactly one running job (use hold for idle/interactive work)")
            remote(machine, ["/usr/local/bin/workspace-job", "extend",
                             units[0].removesuffix(".service"), str(args.timeout)])
        renew(provider, machine, args.timeout + 300)
        remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "hold", str(args.timeout + 300)])
        if args.action == "run":
            directory = project_record(args.workspace, machine)["projects"].get(args.project) if args.project else "/home/ubuntu/projects"
            if not directory:
                raise RuntimeError(f"Unknown manifest project: {args.project}")
            job = "coder-job-" + uuid.uuid4().hex[:12]
            remote(machine, ["sudo", "systemd-run", "--unit", job, "--uid", "ubuntu", "--gid", "ubuntu",
                "--property", "WorkingDirectory=" + directory,
                "--property", f"ExecStopPost=/usr/local/bin/workspace-job finalize {job}",
                "/usr/local/bin/workspace-job", "run", job, str(args.timeout)] + job_command)
            print(f"Detached job {job}; timeout {args.timeout}s; use coder-{provider.name} wait/fetch {args.workspace}", flush=True)
            if args.wait:
                return wait_for_job(provider, args.workspace, machine, job_id=job, poll=args.poll)
    elif args.action == "fetch":
        if machine.state != "running":
            raise RuntimeError("Resume the stopped workspace before fetching its preserved results")
        ensure_controller_ssh(provider, machine)
        target = STATE / "results" / args.workspace
        target.mkdir(parents=True, exist_ok=True)
        renew(provider, machine, 300)
        remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "hold", "300"])
        if active_jobs(machine):
            raise RuntimeError(f"Wait for the registered job before collecting (use coder-{provider.name} wait {args.workspace})")
        logs = subprocess.run(ssh_args(machine) + ["test -d /home/ubuntu/.local/state/coder-jobs"], capture_output=True)
        if logs.returncode == 0:
            run(["rsync", "-azs", "-e", shlex.join(ssh_args(machine)[:-1]),
                 ssh_args(machine)[-1] + ":.local/state/coder-jobs/", str(target) + "/"])
        if (STATE / "manifests" / (args.workspace + ".json")).exists() or "SourceSnapshot" in machine.tags:
            fetch_projects(args.workspace, machine)
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
            print(error.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        if isinstance(error, (RuntimeError, ValueError, urllib.error.URLError, FileNotFoundError)) or type(error).__name__ in ("ClientError", "ServiceError"):
            print(f"{label}: {error}", file=sys.stderr)
            sys.exit(1)
        raise
