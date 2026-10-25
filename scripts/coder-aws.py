#!/usr/bin/env python3
"""Provision and operate private Coder AWS workers with independent stop leases."""
import argparse
import base64
import datetime as dt
import fcntl
import gzip
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
import zipfile
import importlib.util
import tempfile

import boto3
from botocore.exceptions import ClientError

from coder_workspace.settings import load

SETTINGS = load()
REGION = SETTINGS["region"]
MANAGER = SETTINGS["managed_by"]
GUARD = SETTINGS["guard_name"]
STATE = Path.home() / ".local/state/coder-workspaces"
CONFIG = Path.home() / ".config/coderv2"
AUTH_FILE = Path.home() / ".aws/coder-workspaces.credentials"


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def api(path, payload=None):
    url = (CONFIG / "url").read_text().strip()
    token = (CONFIG / "session").read_text().strip()
    request = urllib.request.Request(url + "/api/v2/" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Coder-Session-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def aws_session(bootstrap=False):
    # Export into memory only; neither credentials nor their values enter state/logs.
    environment = os.environ.copy()
    environment.pop("AWS_PROFILE", None)
    environment.pop("AWS_CONFIG_FILE", None)
    profile = "default"
    if AUTH_FILE.exists() and not bootstrap:
        environment["AWS_SHARED_CREDENTIALS_FILE"] = str(AUTH_FILE)
        environment["AWS_CONFIG_FILE"] = "/dev/null"
        profile = "coder-workspaces"
    credentials = json.loads(run(["aws", "--region", REGION, "configure", "export-credentials", "--profile", profile,
                                  "--format", "process"], env=environment, capture_output=True).stdout)
    return boto3.Session(region_name=REGION, aws_access_key_id=credentials["AccessKeyId"],
                         aws_secret_access_key=credentials["SecretAccessKey"],
                         aws_session_token=credentials.get("SessionToken"))


def controller_policy(account, subnets):
    prefix = f"arn:aws:ec2:{REGION}:{account}:"
    tagged = {"StringEquals": {"ec2:ResourceTag/ManagedBy": MANAGER}}
    resources = [prefix + kind + "/*" for kind in ("instance", "volume", "security-group", "key-pair")]
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "ec2:Describe*", "Resource": "*",
         "Condition": {"StringEquals": {"aws:RequestedRegion": REGION}}},
        {"Effect": "Allow", "Action": ["ec2:StartInstances", "ec2:StopInstances", "ec2:ModifyInstanceAttribute",
            "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress",
            "ec2:RevokeSecurityGroupEgress", "ec2:DeleteSecurityGroup", "ec2:DeleteKeyPair", "ec2:CreateTags"],
         "Resource": resources, "Condition": tagged},
        {"Effect": "Allow", "Action": ["ec2:CreateSecurityGroup", "ec2:ImportKeyPair"],
         "Resource": [prefix + "security-group/*", prefix + "key-pair/*"],
         "Condition": {"StringEquals": {"aws:RequestTag/ManagedBy": MANAGER}}},
        {"Effect": "Allow", "Action": "ec2:CreateSecurityGroup",
         "Resource": prefix + "vpc/" + subnets["vpc"]},
        {"Effect": "Allow", "Action": "ec2:RunInstances",
         "Resource": [prefix + "instance/*", prefix + "volume/*"],
         "Condition": {"StringEquals": {"aws:RequestTag/ManagedBy": MANAGER}}},
        {"Effect": "Allow", "Action": "ec2:RunInstances", "Resource": [
            f"arn:aws:ec2:{REGION}::image/{SETTINGS['ami_id']}", prefix + "network-interface/*",
            *[prefix + "subnet/" + subnet for subnet in subnets["ids"]]]},
        {"Effect": "Allow", "Action": "ec2:RunInstances", "Resource": [
            prefix + "security-group/*", prefix + "key-pair/*"], "Condition": tagged},
        {"Effect": "Allow", "Action": "ec2:CreateTags", "Resource": resources,
         "Condition": {"StringEquals": {"ec2:CreateAction": ["RunInstances", "CreateSecurityGroup", "ImportKeyPair"]}}},
        {"Effect": "Deny", "Action": "ec2:CreateTags", "Resource": resources,
         "Condition": {"ForAnyValue:StringEquals": {"aws:TagKeys": "ManagedBy"},
                       "StringNotEquals": {"aws:RequestTag/ManagedBy": MANAGER}}},
        {"Effect": "Allow", "Action": "freetier:GetAccountPlanState", "Resource": "*"},
        {"Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
        {"Effect": "Allow", "Action": "lambda:GetFunctionConfiguration",
         "Resource": f"arn:aws:lambda:{REGION}:{account}:function:{GUARD}"},
        {"Effect": "Allow", "Action": ["events:DescribeRule", "events:ListTargetsByRule"],
         "Resource": f"arn:aws:events:{REGION}:{account}:rule/{GUARD}"},
    ]}


def configure_auth(session):
    user = SETTINGS["iam_user"]
    iam = session.client("iam")
    account = session.client("sts").get_caller_identity()["Account"]
    ec2 = session.client("ec2")
    vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]
    try:
        existing = iam.get_user(UserName=user)["User"]
        if {t["Key"]: t["Value"] for t in existing.get("Tags", [])}.get("ManagedBy") != MANAGER:
            raise RuntimeError("Refusing to change an IAM user not owned by this configuration")
    except iam.exceptions.NoSuchEntityException:
        iam.create_user(UserName=user, Tags=[{"Key": "ManagedBy", "Value": MANAGER}])
    policy = controller_policy(account, {"vpc": vpc, "ids": [s["SubnetId"] for s in subnets]})
    policy_arn = f"arn:aws:iam::{account}:policy/{user}"
    try:
        managed = iam.get_policy(PolicyArn=policy_arn)["Policy"]
        tags = iam.list_policy_tags(PolicyArn=policy_arn)["Tags"]
        if {t["Key"]: t["Value"] for t in tags}.get("ManagedBy") != MANAGER:
            raise RuntimeError("Refusing to change a policy not owned by this configuration")
        current = iam.get_policy_version(PolicyArn=policy_arn, VersionId=managed["DefaultVersionId"])["PolicyVersion"]["Document"]
        if current != policy:
            for version in iam.list_policy_versions(PolicyArn=policy_arn)["Versions"]:
                if not version["IsDefaultVersion"]:
                    iam.delete_policy_version(PolicyArn=policy_arn, VersionId=version["VersionId"])
            iam.create_policy_version(PolicyArn=policy_arn, PolicyDocument=json.dumps(policy), SetAsDefault=True)
    except iam.exceptions.NoSuchEntityException:
        iam.create_policy(PolicyName=user, PolicyDocument=json.dumps(policy),
                          Tags=[{"Key": "ManagedBy", "Value": MANAGER}])
    iam.attach_user_policy(UserName=user, PolicyArn=policy_arn)
    # Older workspaces predate key-pair ownership tags; tag only keys referenced
    # by our already tagged instances, before switching to restricted credentials.
    instances = ec2.describe_instances(Filters=[{"Name": "tag:ManagedBy", "Values": [MANAGER]}])
    for reservation in instances["Reservations"]:
        for machine in reservation["Instances"]:
            if machine.get("KeyName") and machine["State"]["Name"] != "terminated":
                pair = ec2.describe_key_pairs(KeyNames=[machine["KeyName"]])["KeyPairs"][0]
                ec2.create_tags(Resources=[pair["KeyPairId"]], Tags=[{"Key": "ManagedBy", "Value": MANAGER}])
    if AUTH_FILE.exists():
        print("Restricted controller policy updated; existing local key retained")
        return
    if iam.list_access_keys(UserName=user)["AccessKeyMetadata"]:
        raise RuntimeError("Controller already has a key absent locally; recover or revoke it before creating another")
    key = iam.create_access_key(UserName=user)["AccessKey"]
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with AUTH_FILE.open("x") as output:
            output.write(f"[coder-workspaces]\naws_access_key_id = {key['AccessKeyId']}\n"
                         f"aws_secret_access_key = {key['SecretAccessKey']}\n")
        AUTH_FILE.chmod(0o600)
    except OSError:
        iam.delete_access_key(UserName=user, AccessKeyId=key["AccessKeyId"])
        raise
    print("Restricted controller key saved privately; daily browser login is no longer needed for routine workspace use")


def check_plan(session):
    plan = session.client("freetier", region_name="us-east-1").get_account_plan_state()
    if plan["accountPlanType"] != "FREE" or plan["accountPlanStatus"] != "ACTIVE":
        raise RuntimeError("Expected an active AWS Free plan; no account changes were made")


def deploy_guard(session):
    iam = session.client("iam")
    account = session.client("sts").get_caller_identity()["Account"]
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {
        "Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        role = iam.get_role(RoleName=GUARD)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(RoleName=GUARD, AssumeRolePolicyDocument=json.dumps(trust),
                               Tags=[{"Key": "ManagedBy", "Value": MANAGER}])["Role"]
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "ec2:DescribeInstances", "Resource": "*"},
        {"Effect": "Allow", "Action": "ec2:StopInstances", "Resource":
         f"arn:aws:ec2:{REGION}:{account}:instance/*", "Condition": {"StringEquals": {
             "ec2:ResourceTag/ManagedBy": MANAGER}}},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:/aws/lambda/{GUARD}:*"},
    ]}
    iam.put_role_policy(RoleName=GUARD, PolicyName=GUARD, PolicyDocument=json.dumps(policy))
    logs = session.client("logs")
    try:
        logs.create_log_group(logGroupName="/aws/lambda/" + GUARD)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName="/aws/lambda/" + GUARD, retentionInDays=7)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("stop_guard.py", Path(os.environ["CODER_AWS_GUARD"]).read_bytes())
    functions = session.client("lambda")
    try:
        functions.get_function(FunctionName=GUARD)
    except functions.exceptions.ResourceNotFoundException:
        for attempt in range(12):
            try:
                functions.create_function(FunctionName=GUARD, Runtime="python3.13", Role=role["Arn"],
                    Handler="stop_guard.handler", Code={"ZipFile": archive.getvalue()},
                    Timeout=30, MemorySize=128, Tags={"ManagedBy": MANAGER},
                    Environment={"Variables": {"MANAGED_BY": MANAGER}})
                break
            except functions.exceptions.InvalidParameterValueException:
                if attempt == 11:
                    raise
                time.sleep(3)  # IAM propagation; bounded.
        functions.get_waiter("function_active_v2").wait(FunctionName=GUARD)
    else:
        functions.update_function_code(FunctionName=GUARD, ZipFile=archive.getvalue())
        functions.get_waiter("function_updated_v2").wait(FunctionName=GUARD)
        functions.update_function_configuration(FunctionName=GUARD,
            Environment={"Variables": {"MANAGED_BY": MANAGER}})
        functions.get_waiter("function_updated_v2").wait(FunctionName=GUARD)
    events = session.client("events")
    rule = events.put_rule(Name=GUARD, ScheduleExpression="rate(1 minute)", State="ENABLED",
                           Description="Stop only owned EC2 workspaces with expired leases")
    try:
        functions.add_permission(FunctionName=GUARD, StatementId=GUARD, Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com", SourceArn=rule["RuleArn"], SourceAccount=account)
    except functions.exceptions.ResourceConflictException:
        pass
    result = events.put_targets(Rule=GUARD, Targets=[{"Id": "stop-guard", "Arn":
        f"arn:aws:lambda:{REGION}:{account}:function:{GUARD}"}])
    if result["FailedEntryCount"]:
        raise RuntimeError(f"Could not attach the stop guard: {result['FailedEntries']}")
    assert_guard(session)
    print("AWS stop guard active; checks deadlines every minute")


def assert_guard(session):
    rule = session.client("events").describe_rule(Name=GUARD)
    targets = session.client("events").list_targets_by_rule(Rule=GUARD)["Targets"]
    function = session.client("lambda").get_function_configuration(FunctionName=GUARD)
    if (rule["State"] != "ENABLED" or rule.get("ScheduleExpression") != "rate(1 minute)"
            or not any(t["Arn"] == function["FunctionArn"] for t in targets)
            or function["State"] != "Active"):
        raise RuntimeError("AWS stop protection is not ready")


def key_path():
    return STATE / "id_ed25519"


def publish(session, refresh_guard=True):
    if refresh_guard:
        deploy_guard(session)
    else:
        assert_guard(session)
    key = key_path()
    if not key.exists():
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=15) as response:
        address = str(ipaddress.IPv4Address(response.read().decode().strip()))
    identity = run(["git", "var", "GIT_AUTHOR_IDENT"], capture_output=True).stdout
    name, rest = identity.split(" <", 1)
    email = rest.split(">", 1)[0]
    template = STATE / "template"
    template.mkdir(parents=True, exist_ok=True, mode=0o700)
    template.chmod(0o700)
    # Copy only packaged sources, leaving provider cache executables untouched.
    for source in Path(os.environ["CODER_AWS_TEMPLATE"]).iterdir():
        target = template / source.name
        shutil.copyfile(source, target)
        target.chmod(0o600)
    variables = STATE / "variables.json"
    variables.write_text(json.dumps({"ssh_public_key": key.with_suffix(".pub").read_text(),
        "ssh_cidr": address + "/32", "agent_name": name, "agent_email": email,
        "region": REGION, "ami_id": SETTINGS["ami_id"], "managed_by": MANAGER,
        "coder_url": SETTINGS["coder_url"]}))
    variables.chmod(0o600)
    run(["coder", "templates", "push", "aws-private", "--directory", str(template),
         "--variables-file", str(variables), "--yes"])
    # Guest and AWS leases are authoritative, so a Coder timer cannot cut off a long job.
    run(["coder", "templates", "edit", "aws-private", "--default-ttl", "0h", "--yes",
         "--display-name", "AWS private workspace", "--description",
         "Private SSH bridge; five-minute leases by default; persistent disk and independent AWS stop guard."])


def instance(session, name):
    owner = api("users/me")["id"]
    found = session.client("ec2").describe_instances(Filters=[
        {"Name": "tag:ManagedBy", "Values": [MANAGER]},
        {"Name": "tag:WorkspaceName", "Values": [name]},
        {"Name": "tag:OwnerId", "Values": [owner]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
    ])
    instances = [i for r in found["Reservations"] for i in r["Instances"]]
    if len(instances) != 1:
        raise RuntimeError(f"Expected one owned instance for {name}, found {len(instances)}")
    return instances[0]


def transition(name, action, parameters=None):
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


def renew(session, machine, seconds, preserve=True):
    assert_guard(session)
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds + 60)
    if preserve:
        latest = session.client("ec2").describe_instances(InstanceIds=[machine["InstanceId"]])
        tags = {item["Key"]: item["Value"] for item in latest["Reservations"][0]["Instances"][0].get("Tags", [])}
        try:
            deadline = max(deadline, dt.datetime.fromisoformat(tags["LeaseExpiresAt"].replace("Z", "+00:00")))
        except (KeyError, ValueError, TypeError):
            pass
    session.client("ec2").create_tags(Resources=[machine["InstanceId"]], Tags=[{
        "Key": "LeaseExpiresAt", "Value": deadline.isoformat()}])


def ssh_args(machine):
    return ["ssh", "-i", str(key_path()), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ForwardAgent=no",
        "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={STATE / 'known_hosts'}",
        "ubuntu@" + machine["PublicIpAddress"]]


def remote(machine, command, **kwargs):
    return run(ssh_args(machine) + [shlex.join(command)], **kwargs)


def repair_bootstrap(session, machine):
    """Reapply the recorded declarative bootstrap after an interrupted cloud-init."""
    encoded = session.client("ec2").describe_instance_attribute(
        InstanceId=machine["InstanceId"], Attribute="userData")["UserData"]["Value"]
    raw = base64.b64decode(encoded)
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
    # Upgrade runtime helpers from the pinned controller package on reconnect.
    for filename, target in (("workspace_lease.py", "workspace-lease"), ("workspace_job.py", "workspace-job"),
                             ("workspace_transfer.py", "workspace-transfer"),
                             ("github_credential.py", "workspace-github-credential")):
        source = Path(os.environ["CODER_AWS_TEMPLATE"]) / filename
        remote(machine, ["sudo", "tee", "/usr/local/bin/" + target],
               input=source.read_text(), stdout=subprocess.DEVNULL)
        remote(machine, ["sudo", "chmod", "0755", "/usr/local/bin/" + target])


def connect(session, name):
    machine = instance(session, name)
    if machine["State"]["Name"] != "running":
        raise RuntimeError("Workspace is stopped; use coder-aws resume first")
    renew(session, machine, 600)
    unit = "coder-bridge-" + machine["InstanceId"]
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
            active = remote(machine, ["systemctl", "list-units", "--state=running", "--no-legend",
                                      "coder-job-*.service"], capture_output=True)
            if active.stdout.strip():
                remember_bridge(name, machine)
                print(f"{name}: bridge restored; preserving the active job without bootstrap changes")
                return machine
            repair_bootstrap(session, machine)
            remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "complete"])
            renew(session, machine, 300, preserve=False)
            remember_bridge(name, machine)
            print(f"{name}: private Coder bridge ready; default lease is five minutes")
            return machine
        if attempt % 3 == 2:
            state = session.client("ec2").describe_instances(InstanceIds=[machine["InstanceId"]])["Reservations"][0]["Instances"][0]["State"]["Name"]
            if state != "running":
                subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
                raise RuntimeError("Workspace stopped while reconnecting; resume it before retrying")
        time.sleep(3)
    raise RuntimeError("SSH/bootstrap did not become ready within its bounded startup allowance")


def save_bridge_records(updates):
    path = STATE / "dashboard-builds.json"
    with (STATE / "dashboard-builds.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = json.loads(path.read_text()) if path.exists() else {}
        records.update(updates)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(records))
        os.replace(temp, path)


def remember_bridge(name, machine):
    workspace = api("users/me/workspace/" + name)
    save_bridge_records({workspace["id"]: {
        "build": workspace["latest_build"]["id"], "instance": machine["InstanceId"]}})


def should_connect(workspace, previous, bridge_alive=True):
    build = workspace["latest_build"]
    return (workspace["template_name"] == "aws-private"
            and build["status"] == "running"
            and (previous.get("build") != build["id"] or not bridge_alive))


def thread_origin(thread_id, thread_url=None):
    if not thread_id:
        return {"t3_thread_id": "", "t3_thread_url": ""}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_%.-]{0,239}", thread_id):
        raise RuntimeError("Invalid T3 thread ID (use the app ID, not a provider session ID)")
    if not thread_url:
        home = Path(os.environ.get("T3CODE_HOME", str(Path.home() / ".t3")))
        environment = (home / "userdata/environment-id").read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9-]+", environment):
            raise RuntimeError("Invalid local T3 environment ID; specify --thread-url explicitly")
        base = SETTINGS["thread_base_url"]
        if not base:
            raise RuntimeError("Set thread_base_url or pass --thread-url explicitly")
        thread_url = base + "/" + environment + "/" + urllib.parse.quote(thread_id, safe="")
    parsed = urllib.parse.urlsplit(thread_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise RuntimeError("Thread link must be an HTTP(S) URL without credentials")
    if len(thread_url) > 256:
        raise RuntimeError("Thread URL exceeds the AWS tag-value limit of 256 characters")
    return {"t3_thread_id": thread_id, "t3_thread_url": thread_url}


def show_links(name):
    workspace = api("users/me/workspace/" + name)
    owner = urllib.parse.quote(workspace["owner_name"], safe="")
    url = SETTINGS["coder_url"] + "/@" + owner + "/" + urllib.parse.quote(name, safe="")
    print(f"[Open {name} in Coder]({url})", flush=True)
    print("AWS filter: tag:CreatedVia=Coder; Coder filter: template:aws-private", flush=True)


def library(name):
    filename = {"transfer": "transfer.py", "github": "github_app.py"}[name]
    source = os.environ.get("CODER_AWS_" + name.upper() + "_LIB",
                            str(Path(__file__).parent / "coder_workspace" / filename))
    spec = importlib.util.spec_from_file_location("workspace_" + name, source)
    library = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(library)
    return library


def transfer_library():
    return library("transfer")


def authorize_repository(session, name, repo, write=False):
    github = library("github")
    github.repository(repo)
    machine = instance(session, name)
    if machine["State"]["Name"] != "running":
        raise RuntimeError("Resume the worker before authorizing a repository")
    credential = github.mint(STATE / "github", repo, write=write)
    renew(session, machine, 300)
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


def project_record(name, machine=None):
    path = STATE / "manifests" / (name + ".json")
    if not path.exists():
        tags = {t["Key"]: t["Value"] for t in machine.get("Tags", [])} if machine else {}
        snapshot = tags.get("SourceSnapshot", "")
        if not re.fullmatch(r"[0-9a-f]{24}", snapshot):
            raise RuntimeError("No source snapshot registered; run coder-aws sync with a manifest first")
        root = "/home/ubuntu/projects/snapshots/" + snapshot
        plan = json.loads(remote(machine, ["cat", root + "/manifest.json"], capture_output=True).stdout)
        if plan["snapshot"] != snapshot:
            raise RuntimeError("Remote source registry does not match its AWS record")
        return {"manifest": plan, "instance_id": machine["InstanceId"], "projects": {
            p["name"]: root + "/" + transfer_library().relative(p["destination"]) for p in plan["projects"]}}
    record = json.loads(path.read_text())
    if machine and record.get("instance_id", machine["InstanceId"]) != machine["InstanceId"]:
        raise RuntimeError("Local source registry belongs to another instance")
    return record


def sync_projects(session, name, machine, manifest):
    library = transfer_library()
    active = remote(machine, ["systemctl", "list-units", "--state=running", "--no-legend", "coder-job-*.service"], capture_output=True)
    if active.stdout.strip():
        raise RuntimeError("Wait for the running job before syncing another source snapshot")
    renew(session, machine, 900)
    remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "hold", "900"])
    # Runtime helpers are declarative and upgraded on idle reconnect.
    ready = subprocess.run(ssh_args(machine) + ["test -x /usr/local/bin/workspace-transfer"], capture_output=True)
    if ready.returncode:
        connect(session, name)
        renew(session, machine, 900)
        remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "hold", "900"])
    with tempfile.TemporaryDirectory(prefix="transfer-", dir=STATE) as temporary:
        inputs = Path(temporary) / "input"
        plan = library.pack(manifest, inputs)
        print(json.dumps(plan, indent=2), flush=True)
        remote(machine, ["mkdir", "-p", "/home/ubuntu/.cache/coder-transfers"])
        stage = remote(machine, ["mktemp", "-d", "/home/ubuntu/.cache/coder-transfers/input-XXXXXXXX"], capture_output=True).stdout.strip()
        try:
            run(["rsync", "-azs", "-e", shlex.join(ssh_args(machine)[:-1]), str(inputs) + "/",
                 ssh_args(machine)[-1] + ":" + stage + "/"])
            directories = json.loads(remote(machine, ["/usr/local/bin/workspace-transfer", "prepare", stage], capture_output=True).stdout)
        finally:
            remote(machine, ["/usr/local/bin/workspace-transfer", "cleanup", stage])
    records = STATE / "manifests"
    records.mkdir(exist_ok=True)
    temporary = records / (name + ".tmp")
    temporary.write_text(json.dumps({"manifest": plan, "projects": directories, "instance_id": machine["InstanceId"]}, indent=2))
    os.replace(temporary, records / (name + ".json"))
    session.client("ec2").create_tags(Resources=[machine["InstanceId"]], Tags=[
        {"Key": "SourceSnapshot", "Value": plan["snapshot"]},
        {"Key": "SourceProjects", "Value": str(len(plan["projects"]))}])
    remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "complete"])
    renew(session, machine, 300, preserve=False)
    print("Prepared projects: " + json.dumps(directories), flush=True)


def fetch_projects(session, name, machine):
    record = project_record(name, machine)
    stage = json.loads(remote(machine, ["/usr/local/bin/workspace-transfer", "collect", record["manifest"]["snapshot"]], capture_output=True).stdout)
    target = STATE / "results" / name / (record["manifest"]["snapshot"] + "-" + uuid.uuid4().hex[:8])
    target.mkdir(parents=True)
    run(["rsync", "-azs", "-e", shlex.join(ssh_args(machine)[:-1]),
         ssh_args(machine)[-1] + ":" + stage + "/", str(target) + "/"])
    transfer_library().restore(target)
    (target / "retrieved.json").write_text(json.dumps({"verified": True, "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat()}))
    remote(machine, ["/usr/local/bin/workspace-transfer", "cleanup", stage])
    print(f"Verified project checkouts and artifacts retrieved into {target}", flush=True)


def bridge_running(record):
    return bool(record.get("instance")) and subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "coder-bridge-" + record["instance"]],
        capture_output=True).returncode == 0


def reconcile():
    """Connect each successful dashboard start once, without renewing idle work."""
    registry_path = STATE / "dashboard-builds.json"
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    updates = {}
    workspaces = api("workspaces?q=owner%3Ame")["workspaces"]
    for workspace in workspaces:
        if workspace["template_name"] != "aws-private":
            continue
        previous = registry.get(workspace["id"], {})
        build = workspace["latest_build"]
        bridge_alive = bridge_running(previous)
        if should_connect(workspace, previous, bridge_alive):
            # Use the same workspace lock and startup checks as explicit CLI use.
            # A CLI start may own the lock; skip rather than queue duplicate work.
            with (STATE / (workspace["name"] + ".lock")).open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                latest = json.loads(registry_path.read_text()) if registry_path.exists() else {}
                current = latest.get(workspace["id"], {})
                if current.get("build") == build["id"] and bridge_running(current):
                    continue
                session = aws_session()
                check_plan(session)
                machine = instance(session, workspace["name"])
                if machine["State"]["Name"] != "running":
                    # Coder can still say running after the independent stop guard.
                    # Never restart an idle-stopped instance from this reconciler.
                    continue
                connect(session, workspace["name"])
                updates[workspace["id"]] = {"build": build["id"], "instance": machine["InstanceId"]}
        elif build["status"] in ("stopped", "deleted", "failed") and previous.get("instance"):
            subprocess.run(["systemctl", "--user", "stop", "coder-bridge-" + previous["instance"]],
                           capture_output=True)
    if updates:
        save_bridge_records(updates)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="coder-aws", description="Private Coder AWS workers. Default timeout: five minutes (300s).")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("setup")
    sub.add_parser("publish-template")
    sub.add_parser("reconcile")
    sub.add_parser("configure-auth")
    github_setup = sub.add_parser("github-setup")
    github_setup.add_argument("--port", type=int, default=8786)
    plan_command = sub.add_parser("plan")
    plan_command.add_argument("--manifest", required=True)
    for action in ("authorize", "create", "sync", "link", "links", "resume", "connect", "stop", "status", "hold", "extend", "run", "fetch"):
        command = sub.add_parser(action)
        command.add_argument("workspace")
        if action == "authorize":
            command.add_argument("--repo", required=True)
            command.add_argument("--write", action="store_true", help="Explicitly grant repository contents write until token expiry")
        if action in ("hold", "extend", "run"):
            command.add_argument("--timeout", type=int, default=300, help="Finite timeout in seconds; default 300 (five minutes)")
        if action == "create":
            command.add_argument("--manifest")
            command.add_argument("--instance-type", default="t3.small",
                choices=["t3.small", "c7i-flex.large", "m7i-flex.large"])
        if action in ("create", "link"):
            command.add_argument("--thread-id", default=os.environ.get("T3_THREAD_ID"), required=action == "link")
            command.add_argument("--thread-url", help="Explicit HTTP(S) T3 thread link; otherwise use this T3 environment")
        if action == "run":
            command.add_argument("--project", help="Project name from the registered source manifest")
            command.description = "Run one detached job. Put job arguments after --. Default timeout: 300s."
        if action == "sync":
            command.add_argument("--manifest", required=True)
    cli = sys.argv[1:]
    separator = cli.index("--") if "--" in cli else len(cli)
    job_command = cli[separator + 1:]
    args = parser.parse_args(cli[:separator])
    if args.action == "github-setup":
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        setup_lock = (STATE / "github-setup.lock").open("a")
        try:
            fcntl.flock(setup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("GitHub setup is already running; use its existing setup URL")
        library("github").setup(STATE / "github", args.port, owner=SETTINGS["github_owner"],
                                app_name=SETTINGS["github_app_name"])
        return
    if args.action == "plan":
        print(json.dumps(transfer_library().inventory(args.manifest), indent=2))
        return
    if args.action in ("create", "sync") and args.manifest:
        # Validate and show the inventory before AWS access/provisioning.
        print(json.dumps(transfer_library().inventory(args.manifest), indent=2), flush=True)
    if args.action == "run" and not job_command:
        parser.error("run requires a command after --")
    if hasattr(args, "workspace") and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,31}", args.workspace):
        parser.error("workspace names must be 1–32 letters, digits or hyphens")
    origin = thread_origin(args.thread_id, args.thread_url) if args.action in ("create", "link") else None
    if args.action == "create" and os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE") == "T3 Code" and not args.thread_id:
        parser.error("T3 agents must supply --thread-id from t3_thread_configuration; CODEX_THREAD_ID is a different ID")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    if args.action == "reconcile":
        reconcile()
        return
    # Serialize controller mutations for a workspace, including concurrent job launches.
    lock = (STATE / (getattr(args, "workspace", "setup") + ".lock")).open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    api("users/me")  # Fail before AWS writes if local Coder is not authenticated.
    if args.action == "links":
        show_links(args.workspace)
        return
    session = aws_session(bootstrap=args.action in ("setup", "configure-auth"))
    check_plan(session)
    if args.action == "authorize":
        authorize_repository(session, args.workspace, args.repo, write=args.write)
        return
    if args.action == "configure-auth":
        configure_auth(session)
        return
    if args.action in ("setup", "publish-template"):
        publish(session, refresh_guard=args.action == "setup")
        return
    if args.action in ("create", "resume"):
        assert_guard(session)
        if args.action == "create":
            command = ["coder", "create", args.workspace, "--template", "aws-private", "--yes",
                       "--automatic-updates", "always", "--parameter", "instance_type=" + args.instance_type]
            for key, value in origin.items():
                command.extend(["--parameter", key + "=" + value])
        else:
            machine = instance(session, args.workspace)
            if machine["State"]["Name"] == "running":
                workspace = api("users/me/workspace/" + args.workspace)
                if workspace["latest_build"]["status"] == "failed":
                    active = remote(machine, ["systemctl", "list-units", "--state=running", "--no-legend",
                                              "coder-job-*.service"], capture_output=True)
                    if active.stdout.strip():
                        raise RuntimeError("Preserve the active job; reconcile Coder after it finishes")
                    transition(args.workspace, "start")
                connect(session, args.workspace)
                return
            renew(session, machine, 900)
            workspace = api("users/me/workspace/" + args.workspace)
            if workspace["latest_build"]["status"] != "stopped":
                transition(args.workspace, "stop")
            transition(args.workspace, "start")
            connect(session, args.workspace)
            return
        log = STATE / (args.workspace + "-build.log")
        with log.open("w") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
            try:
                process.wait(timeout=420)
            except subprocess.TimeoutExpired:
                process.terminate()
                raise RuntimeError(f"Coder build timed out; see {log}")
        if process.returncode:
            raise RuntimeError(f"Coder build failed; see {log}")
        show_links(args.workspace)
        connect(session, args.workspace)
        if args.manifest:
            sync_projects(session, args.workspace, instance(session, args.workspace), args.manifest)
        return
    machine = instance(session, args.workspace)
    if args.action == "sync":
        if machine["State"]["Name"] != "running":
            raise RuntimeError("Resume the workspace before syncing sources")
        sync_projects(session, args.workspace, machine, args.manifest)
    elif args.action == "link":
        tags = {t["Key"]: t["Value"] for t in machine.get("Tags", [])}
        if tags.get("T3ThreadId") and tags["T3ThreadId"] != args.thread_id:
            raise RuntimeError("Workspace already has a different creating thread; refusing to replace its origin")
        if machine["State"]["Name"] != "stopped":
            raise RuntimeError("Link an existing workspace while it is stopped, to preserve active jobs")
        transition(args.workspace, "stop", origin)
        show_links(args.workspace)
    elif args.action == "status":
        print(json.dumps({"instance": machine["InstanceId"], "state": machine["State"]["Name"],
            "type": machine["InstanceType"], "tags": machine.get("Tags", [])}, indent=2))
    elif args.action == "stop":
        transition(args.workspace, "stop")
        subprocess.run(["systemctl", "--user", "stop", "coder-bridge-" + machine["InstanceId"]], capture_output=True)
    elif args.action == "connect":
        connect(session, args.workspace)
    elif args.action in ("hold", "extend", "run"):
        if args.timeout <= 0:
            parser.error("timeout must be positive and finite")
        active = remote(machine, ["systemctl", "list-units", "--state=running", "--no-legend", "coder-job-*.service"], capture_output=True)
        if active.stdout.strip() and args.action == "run":
            raise RuntimeError("A job is already running; this small worker runs one job at a time")
        if args.action == "extend":
            units = [line.split()[0] for line in active.stdout.splitlines() if line.strip()]
            if len(units) != 1:
                raise RuntimeError("extend requires exactly one running job")
            remote(machine, ["/usr/local/bin/workspace-job", "extend",
                             units[0].removesuffix(".service"), str(args.timeout)])
        renew(session, machine, args.timeout + 300)
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
            print(f"Detached job {job}; timeout {args.timeout}s; use coder-aws fetch {args.workspace} for results")
    elif args.action == "fetch":
        if machine["State"]["Name"] != "running":
            raise RuntimeError("Resume the stopped workspace before fetching its preserved results")
        target = STATE / "results" / args.workspace
        target.mkdir(parents=True, exist_ok=True)
        renew(session, machine, 300)
        remote(machine, ["sudo", "/usr/local/bin/workspace-lease", "hold", "300"])
        active = remote(machine, ["systemctl", "list-units", "--state=running", "--no-legend", "coder-job-*.service"], capture_output=True)
        if active.stdout.strip():
            raise RuntimeError("Wait for the registered job before collecting a consistent result")
        logs = subprocess.run(ssh_args(machine) + ["test -d /home/ubuntu/.local/state/coder-jobs"], capture_output=True)
        if logs.returncode == 0:
            run(["rsync", "-azs", "-e", shlex.join(ssh_args(machine)[:-1]),
                 ssh_args(machine)[-1] + ":.local/state/coder-jobs/", str(target) + "/"])
        if (STATE / "manifests" / (args.workspace + ".json")).exists() or any(t["Key"] == "SourceSnapshot" for t in machine.get("Tags", [])):
            fetch_projects(session, args.workspace, machine)
        print(f"Results retrieved into {target}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(f"coder-aws: command failed ({error.returncode}): {shlex.join(error.cmd)}", file=sys.stderr)
        if error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError, ClientError, urllib.error.URLError, FileNotFoundError) as error:
        print(f"coder-aws: {error}", file=sys.stderr)
        sys.exit(1)
