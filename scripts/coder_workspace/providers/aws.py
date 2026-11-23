"""AWS EC2 + Lambda stop-guard provider backend."""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

from .base import Instance, Provider

AUTH_FILE = Path.home() / ".aws/coder-workspaces.credentials"
AWS_GUARD_LOCKDOWN: Path | None = None
INSTANCE_TYPES = ("t3.small", "c7i-flex.large", "m7i-flex.large", "c7i-flex.xlarge", "m7i-flex.xlarge")
AMI_ID_RE = re.compile(r"^ami-[0-9a-f]+$")


def lockdown_path() -> Path:
    if AWS_GUARD_LOCKDOWN is not None:
        return AWS_GUARD_LOCKDOWN
    state_root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(state_root) / "aws-guard/lockdown.json"


def _run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def _normalize_state(raw_state: str) -> str:
    return "starting" if raw_state == "pending" else raw_state


def _to_instance(raw: dict) -> Instance:
    tags = {item["Key"]: item["Value"] for item in raw.get("Tags", [])}
    return Instance(
        id=raw["InstanceId"],
        name=tags.get("WorkspaceName", ""),
        state=_normalize_state(raw["State"]["Name"]),
        public_ip=raw.get("PublicIpAddress", ""),
        instance_type=raw.get("InstanceType", ""),
        provider="aws",
        tags=tags,
        raw=raw,
    )


def controller_policy(settings: dict, account: str, subnets: dict) -> dict:
    region = settings["region"]
    manager = settings["managed_by"]
    guard = settings["guard_name"]
    prefix = f"arn:aws:ec2:{region}:{account}:"
    tagged = {"StringEquals": {"ec2:ResourceTag/ManagedBy": manager}}
    resources = [prefix + kind + "/*" for kind in ("instance", "volume", "security-group", "key-pair")]
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "ec2:Describe*", "Resource": "*",
         "Condition": {"StringEquals": {"aws:RequestedRegion": region}}},
        {"Effect": "Allow", "Action": ["ec2:StartInstances", "ec2:StopInstances", "ec2:ModifyInstanceAttribute",
            "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress",
            "ec2:RevokeSecurityGroupEgress", "ec2:DeleteSecurityGroup", "ec2:DeleteKeyPair", "ec2:CreateTags", "ec2:DeleteTags"],
         "Resource": resources, "Condition": tagged},
        {"Effect": "Allow", "Action": "ec2:TerminateInstances",
         "Resource": prefix + "instance/*", "Condition": tagged},
        {"Effect": "Allow", "Action": ["ec2:CreateSecurityGroup", "ec2:ImportKeyPair"],
         "Resource": [prefix + "security-group/*", prefix + "key-pair/*"],
         "Condition": {"StringEquals": {"aws:RequestTag/ManagedBy": manager}}},
        {"Effect": "Allow", "Action": "ec2:CreateSecurityGroup",
         "Resource": prefix + "vpc/" + subnets["vpc"]},
        {"Effect": "Allow", "Action": "ec2:RunInstances",
         "Resource": [prefix + "instance/*", prefix + "volume/*"],
         "Condition": {"StringEquals": {"aws:RequestTag/ManagedBy": manager}}},
        {"Effect": "Allow", "Action": "ec2:RunInstances", "Resource": [
            f"arn:aws:ec2:{region}::image/{settings['ami_id']}", prefix + "network-interface/*",
            *[prefix + "subnet/" + subnet for subnet in subnets["ids"]]]},
        {"Effect": "Allow", "Action": "ec2:RunInstances",
         "Resource": f"arn:aws:ec2:{region}::image/*", "Condition": tagged},
        {"Effect": "Allow", "Action": "ec2:RunInstances", "Resource": [
            prefix + "security-group/*", prefix + "key-pair/*"], "Condition": tagged},
        {"Effect": "Allow", "Action": "ec2:CreateImage",
         "Resource": [prefix + "instance/*", f"arn:aws:ec2:{region}::image/*", f"arn:aws:ec2:{region}::snapshot/*"],
         "Condition": tagged},
        {"Effect": "Allow", "Action": "ec2:CreateImage",
         "Resource": [f"arn:aws:ec2:{region}::image/*", f"arn:aws:ec2:{region}::snapshot/*"],
         "Condition": {"StringEquals": {"aws:RequestTag/ManagedBy": manager}}},
        {"Effect": "Allow", "Action": "ec2:DeregisterImage",
         "Resource": f"arn:aws:ec2:{region}::image/*", "Condition": tagged},
        {"Effect": "Allow", "Action": "ec2:DeleteSnapshot",
         "Resource": f"arn:aws:ec2:{region}::snapshot/*", "Condition": tagged},
        {"Effect": "Allow", "Action": "ec2:CreateTags", "Resource": resources + [
            f"arn:aws:ec2:{region}::image/*", f"arn:aws:ec2:{region}::snapshot/*"],
         "Condition": {"StringEquals": {"ec2:CreateAction": ["RunInstances", "CreateSecurityGroup", "ImportKeyPair", "CreateImage"]}}},
        {"Effect": "Deny", "Action": "ec2:CreateTags", "Resource": resources,
         "Condition": {"ForAnyValue:StringEquals": {"aws:TagKeys": "ManagedBy"},
                       "StringNotEquals": {"aws:RequestTag/ManagedBy": manager}}},
        {"Effect": "Deny", "Action": "ec2:DeleteTags", "Resource": resources,
         "Condition": {"ForAnyValue:StringEquals": {"aws:TagKeys": "ManagedBy"}}},
        {"Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
        {"Effect": "Allow", "Action": "lambda:GetFunctionConfiguration",
         "Resource": f"arn:aws:lambda:{region}:{account}:function:{guard}"},
        {"Effect": "Allow", "Action": ["events:DescribeRule", "events:ListTargetsByRule"],
         "Resource": f"arn:aws:events:{region}:{account}:rule/{guard}"},
    ]}


class AwsProvider(Provider):
    name = "aws"
    template_name = "aws-private"
    display_name = "AWS private workspace"
    template_description = "Private SSH bridge; five-minute leases by default; persistent disk and independent AWS stop guard."
    default_instance_type = "t3.small"
    instance_types = INSTANCE_TYPES
    filter_hint = "AWS filter: tag:CreatedVia=Coder; Coder filter: template:aws-private"
    template_env_var = "CODER_AWS_TEMPLATE"

    def __init__(self, settings: dict, *, session=None, bootstrap: bool = False):
        self.settings = settings
        self.region = settings["region"]
        self.manager = settings["managed_by"]
        self.guard = settings["guard_name"]
        self._session = session
        self._bootstrap = bootstrap

    @property
    def session(self):
        if self._session is None:
            self._session = self._create_session(bootstrap=self._bootstrap)
        return self._session

    def _create_session(self, *, bootstrap: bool = False):
        import boto3
        environment = os.environ.copy()
        environment.pop("AWS_PROFILE", None)
        environment.pop("AWS_CONFIG_FILE", None)
        profile = "default"
        if AUTH_FILE.exists() and not bootstrap:
            environment["AWS_SHARED_CREDENTIALS_FILE"] = str(AUTH_FILE)
            environment["AWS_CONFIG_FILE"] = "/dev/null"
            profile = "coder-workspaces"
        credentials = json.loads(_run(
            ["aws", "--region", self.region, "configure", "export-credentials",
             "--profile", profile, "--format", "process"],
            env=environment, capture_output=True).stdout)
        return boto3.Session(
            region_name=self.region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials.get("SessionToken"),
        )

    def configure_auth(self) -> None:
        session = self.session
        user = self.settings["iam_user"]
        iam = session.client("iam")
        account = session.client("sts").get_caller_identity()["Account"]
        ec2 = session.client("ec2")
        vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
        subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]
        try:
            existing = iam.get_user(UserName=user)["User"]
            if {t["Key"]: t["Value"] for t in existing.get("Tags", [])}.get("ManagedBy") != self.manager:
                raise RuntimeError("Refusing to change an IAM user not owned by this configuration")
        except iam.exceptions.NoSuchEntityException:
            iam.create_user(UserName=user, Tags=[{"Key": "ManagedBy", "Value": self.manager}])
        policy = controller_policy(self.settings, account, {"vpc": vpc, "ids": [s["SubnetId"] for s in subnets]})
        policy_arn = f"arn:aws:iam::{account}:policy/{user}"
        try:
            managed = iam.get_policy(PolicyArn=policy_arn)["Policy"]
            tags = iam.list_policy_tags(PolicyArn=policy_arn)["Tags"]
            if {t["Key"]: t["Value"] for t in tags}.get("ManagedBy") != self.manager:
                raise RuntimeError("Refusing to change a policy not owned by this configuration")
            current = iam.get_policy_version(PolicyArn=policy_arn, VersionId=managed["DefaultVersionId"])["PolicyVersion"]["Document"]
            if current != policy:
                for version in iam.list_policy_versions(PolicyArn=policy_arn)["Versions"]:
                    if not version["IsDefaultVersion"]:
                        iam.delete_policy_version(PolicyArn=policy_arn, VersionId=version["VersionId"])
                iam.create_policy_version(PolicyArn=policy_arn, PolicyDocument=json.dumps(policy), SetAsDefault=True)
        except iam.exceptions.NoSuchEntityException:
            iam.create_policy(PolicyName=user, PolicyDocument=json.dumps(policy),
                              Tags=[{"Key": "ManagedBy", "Value": self.manager}])
        iam.attach_user_policy(UserName=user, PolicyArn=policy_arn)
        instances = ec2.describe_instances(Filters=[{"Name": "tag:ManagedBy", "Values": [self.manager]}])
        for reservation in instances["Reservations"]:
            for machine in reservation["Instances"]:
                if machine.get("KeyName") and machine["State"]["Name"] != "terminated":
                    pair = ec2.describe_key_pairs(KeyNames=[machine["KeyName"]])["KeyPairs"][0]
                    ec2.create_tags(Resources=[pair["KeyPairId"]], Tags=[{"Key": "ManagedBy", "Value": self.manager}])
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

    def deploy_guard(self) -> None:
        session = self.session
        iam = session.client("iam")
        account = session.client("sts").get_caller_identity()["Account"]
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {
            "Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
        try:
            role = iam.get_role(RoleName=self.guard)["Role"]
        except iam.exceptions.NoSuchEntityException:
            role = iam.create_role(RoleName=self.guard, AssumeRolePolicyDocument=json.dumps(trust),
                                   Tags=[{"Key": "ManagedBy", "Value": self.manager}])["Role"]
        policy = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "ec2:DescribeInstances", "Resource": "*"},
            {"Effect": "Allow", "Action": "ec2:StopInstances", "Resource":
             f"arn:aws:ec2:{self.region}:{account}:instance/*", "Condition": {"StringEquals": {
                 "ec2:ResourceTag/ManagedBy": self.manager}}},
            {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{self.region}:{account}:log-group:/aws/lambda/{self.guard}:*"},
        ]}
        iam.put_role_policy(RoleName=self.guard, PolicyName=self.guard, PolicyDocument=json.dumps(policy))
        logs = session.client("logs")
        try:
            logs.create_log_group(logGroupName="/aws/lambda/" + self.guard)
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        logs.put_retention_policy(logGroupName="/aws/lambda/" + self.guard, retentionInDays=7)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("stop_guard.py", Path(os.environ["CODER_AWS_GUARD"]).read_bytes())
        functions = session.client("lambda")
        try:
            functions.get_function(FunctionName=self.guard)
        except functions.exceptions.ResourceNotFoundException:
            for attempt in range(12):
                try:
                    functions.create_function(
                        FunctionName=self.guard, Runtime="python3.13", Role=role["Arn"],
                        Handler="stop_guard.handler", Code={"ZipFile": archive.getvalue()},
                        Timeout=30, MemorySize=128, Tags={"ManagedBy": self.manager},
                        Environment={"Variables": {"MANAGED_BY": self.manager}})
                    break
                except functions.exceptions.InvalidParameterValueException:
                    if attempt == 11:
                        raise
                    time.sleep(3)
            functions.get_waiter("function_active_v2").wait(FunctionName=self.guard)
        else:
            functions.update_function_code(FunctionName=self.guard, ZipFile=archive.getvalue())
            functions.get_waiter("function_updated_v2").wait(FunctionName=self.guard)
            functions.update_function_configuration(
                FunctionName=self.guard, Environment={"Variables": {"MANAGED_BY": self.manager}})
            functions.get_waiter("function_updated_v2").wait(FunctionName=self.guard)
        events = session.client("events")
        rule = events.put_rule(Name=self.guard, ScheduleExpression="rate(1 minute)", State="ENABLED",
                               Description="Stop only owned EC2 workspaces with expired leases")
        try:
            functions.add_permission(FunctionName=self.guard, StatementId=self.guard, Action="lambda:InvokeFunction",
                Principal="events.amazonaws.com", SourceArn=rule["RuleArn"], SourceAccount=account)
        except functions.exceptions.ResourceConflictException:
            pass
        result = events.put_targets(Rule=self.guard, Targets=[{"Id": "stop-guard", "Arn":
            f"arn:aws:lambda:{self.region}:{account}:function:{self.guard}"}])
        if result["FailedEntryCount"]:
            raise RuntimeError(f"Could not attach the stop guard: {result['FailedEntries']}")
        self.assert_guard()
        print("AWS stop guard active; checks deadlines every minute")

    def setup(self, *, bootstrap: bool = False) -> None:
        self.deploy_guard()

    def assert_guard(self, *, instance_type: str | None = None, check_create: bool = False,
                     check_quota: bool = True, disk_gb: int | None = None) -> None:
        lockdown = lockdown_path()
        if lockdown.exists():
            raise RuntimeError(f"AWS account is in aws-guard lockdown ({lockdown}); unlock before running or renewing workers")
        rule = self.session.client("events").describe_rule(Name=self.guard)
        targets = self.session.client("events").list_targets_by_rule(Rule=self.guard)["Targets"]
        function = self.session.client("lambda").get_function_configuration(FunctionName=self.guard)
        if (rule["State"] != "ENABLED" or rule.get("ScheduleExpression") != "rate(1 minute)"
                or not any(t["Arn"] == function["FunctionArn"] for t in targets)
                or function["State"] != "Active"):
            raise RuntimeError("AWS stop protection is not ready")

    def publish_variables(self, *, ssh_public_key: str, ssh_cidr: str,
                          agent_name: str, agent_email: str) -> dict[str, str]:
        return {
            "ssh_public_key": ssh_public_key,
            "ssh_cidr": ssh_cidr,
            "agent_name": agent_name,
            "agent_email": agent_email,
            "region": self.region,
            "ami_id": self.settings["ami_id"],
            "managed_by": self.manager,
            "coder_url": self.settings["coder_url"],
        }

    def list_instances(self, owner_id: str | None = None) -> list[Instance]:
        filters = [
            {"Name": "tag:ManagedBy", "Values": [self.manager]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ]
        if owner_id:
            filters.append({"Name": "tag:OwnerId", "Values": [owner_id]})
        ec2 = self.session.client("ec2")
        if hasattr(ec2, "get_paginator"):
            pages = ec2.get_paginator("describe_instances").paginate(Filters=filters)
        else:
            pages = [ec2.describe_instances(Filters=filters)]
        return [_to_instance(item) for page in pages for res in page.get("Reservations", []) for item in res.get("Instances", [])]

    def get_instance(self, instance_id: str) -> Instance:
        found = self.session.client("ec2").describe_instances(InstanceIds=[instance_id])
        return _to_instance(found["Reservations"][0]["Instances"][0])

    def set_tags(self, machine: Instance, tags: dict[str, str]) -> None:
        payload = [{"Key": key, "Value": value} for key, value in tags.items()]
        self.session.client("ec2").create_tags(Resources=[machine.id], Tags=payload)
        machine.tags.update(tags)

    def ensure_ssh_access(self, machine: Instance, ssh_cidr: str) -> bool:
        """Update the owned security group's TCP/22 rule if the controller's IPv4 changed or stale rules exist."""
        groups = [g["GroupId"] for g in machine.raw.get("SecurityGroups", []) if g.get("GroupId")]
        if not groups:
            return False
        ec2 = self.session.client("ec2")
        described = ec2.describe_security_groups(GroupIds=groups)["SecurityGroups"]
        owned = [sg for sg in described
                 if {t["Key"]: t["Value"] for t in sg.get("Tags", [])}.get("ManagedBy") == self.manager]
        if not owned:
            return False
        sg = owned[0]
        sg_id = sg["GroupId"]
        current_cidrs = []
        for rule in sg.get("IpPermissions", []):
            if rule.get("IpProtocol") == "tcp" and rule.get("FromPort") == 22 and rule.get("ToPort") == 22:
                current_cidrs.extend(r["CidrIp"] for r in rule.get("IpRanges", []) if r.get("CidrIp"))
        stale = [c for c in current_cidrs if c != ssh_cidr]
        if ssh_cidr in current_cidrs and not stale:
            return False
        if ssh_cidr not in current_cidrs:
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                                "IpRanges": [{"CidrIp": ssh_cidr, "Description": "SSH from the controller only"}]}],
            )
        if stale:
            ec2.revoke_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                                "IpRanges": [{"CidrIp": c} for c in stale]}],
            )
        return True

    def stop_instance(self, machine: Instance) -> None:
        self.session.client("ec2").stop_instances(InstanceIds=[machine.id])

    def terminate_instance(self, machine: Instance) -> None:
        self.session.client("ec2").terminate_instances(InstanceIds=[machine.id])

    def read_userdata(self, machine: Instance) -> bytes:
        encoded = self.session.client("ec2").describe_instance_attribute(
            InstanceId=machine.id, Attribute="userData")["UserData"]["Value"]
        return base64.b64decode(encoded)

    def default_builder_type(self, arch: str) -> str:
        if arch != "amd64":
            raise RuntimeError("AWS template supports amd64 images only (use coder-oci for arm64)")
        return "m7i-flex.xlarge"

    def base_image_id(self, arch: str) -> str:
        return self.settings["ami_id"]

    def list_images(self, *, include_pending: bool = False) -> list[dict]:
        ec2 = self.session.client("ec2")
        found = ec2.describe_images(
            Owners=["self"],
            Filters=[{"Name": "tag:ManagedBy", "Values": [self.manager]}],
        )["Images"]
        results = []
        for img in found:
            state = img.get("State", "")
            if not include_pending and state != "available":
                continue
            tags = {t["Key"]: t["Value"] for t in img.get("Tags", [])}
            snaps = [b["Ebs"]["SnapshotId"] for b in img.get("BlockDeviceMappings", [])
                     if b.get("Ebs", {}).get("SnapshotId")]
            vol_sizes = [b["Ebs"]["VolumeSize"] for b in img.get("BlockDeviceMappings", [])
                         if b.get("Ebs", {}).get("VolumeSize")]
            disk_gb = int(tags["ImageDiskGb"]) if tags.get("ImageDiskGb", "").isdigit() else (vol_sizes[0] if vol_sizes else None)
            results.append({
                "id": img["ImageId"],
                "name": tags.get("ImageName", img.get("Name", "")),
                "hash": tags.get("ImageHash", ""),
                "arch": tags.get("ImageArch", "amd64"),
                "disk_gb": disk_gb,
                "state": state,
                "created": img.get("CreationDate", ""),
                "provider": "aws",
                "snapshots": snaps,
            })
        return sorted(results, key=lambda r: r["created"], reverse=True)

    def resolve_image_info(self, ref: str) -> dict:
        if ref.startswith("ami-"):
            if not AMI_ID_RE.fullmatch(ref):
                raise RuntimeError(f"Invalid AWS AMI ID: {ref}")
            owned = [img for img in self.list_images() if img["id"] == ref]
            return owned[0] if owned else {"id": ref, "name": ref, "arch": "amd64", "disk_gb": None, "provider": "aws"}
        matches = [img for img in self.list_images() if img["name"] == ref]
        if not matches:
            raise RuntimeError(f"Private AWS image not found: {ref} (run coder-aws images)")
        return matches[0]

    def create_image(self, machine: Instance, *, image_name: str, image_hash: str,
                     arch: str, disk_gb: int = 30) -> dict:
        ec2 = self.session.client("ec2")
        ami_name = f"coder-{image_name}-{image_hash[:8]}-{int(time.time())}"
        tags = [
            {"Key": "ManagedBy", "Value": self.manager},
            {"Key": "ImageName", "Value": image_name},
            {"Key": "ImageHash", "Value": image_hash},
            {"Key": "ImageArch", "Value": arch},
            {"Key": "ImageDiskGb", "Value": str(disk_gb)},
        ]
        response = ec2.create_image(
            InstanceId=machine.id,
            Name=ami_name,
            Description=f"Private Coder workspace image {image_name} ({image_hash})",
            TagSpecifications=[
                {"ResourceType": "image", "Tags": tags},
                {"ResourceType": "snapshot", "Tags": tags},
            ],
        )
        image_id = response["ImageId"]
        waiter = ec2.get_waiter("image_available")
        waiter.wait(ImageIds=[image_id], WaiterConfig={"Delay": 15, "MaxAttempts": 100})
        return {"id": image_id, "name": image_name, "hash": image_hash, "arch": arch,
                "disk_gb": disk_gb, "provider": "aws"}

    def delete_image(self, ref: str) -> dict:
        ec2 = self.session.client("ec2")
        matches = [img for img in self.list_images(include_pending=True) if img["id"] == ref or img["name"] == ref]
        if not matches:
            raise RuntimeError(f"Owned AWS image not found: {ref}")
        target = matches[0]
        ec2.deregister_image(ImageId=target["id"])
        failed_snaps = []
        for snap in target.get("snapshots", []):
            try:
                ec2.delete_snapshot(SnapshotId=snap)
            except Exception as error:
                failed_snaps.append(f"{snap} ({error})")
        if failed_snaps:
            print(f"warning: deregistered {target['id']} but failed to delete snapshots: {', '.join(failed_snaps)}",
                  file=sys.stderr)
        return target
