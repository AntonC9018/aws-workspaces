"""AWS-side deadline enforcement; no dependency on Coder or the local PC."""
import datetime
import boto3
import os

MANAGER = os.environ.get("MANAGED_BY", "coder-workspaces")


def expired(instance, now):
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    if tags.get("ManagedBy") != MANAGER:
        return False
    try:
        deadline = datetime.datetime.fromisoformat(tags["LeaseExpiresAt"].replace("Z", "+00:00"))
        return deadline <= now
    except (KeyError, ValueError, TypeError):
        # An owned instance without a valid lease must not run indefinitely.
        return True


def handler(event, context):
    ec2 = boto3.client("ec2")
    now = datetime.datetime.now(datetime.timezone.utc)
    stopped = []
    pages = ec2.get_paginator("describe_instances").paginate(Filters=[
        {"Name": "tag:ManagedBy", "Values": [MANAGER]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    for page in pages:
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                if not expired(instance, now):
                    continue
                # Re-read before stopping, so a recently renewed lease wins.
                latest = ec2.describe_instances(InstanceIds=[instance["InstanceId"]])
                current = latest["Reservations"][0]["Instances"][0]
                if current["State"]["Name"] == "running" and expired(current, now):
                    ec2.stop_instances(InstanceIds=[current["InstanceId"]])
                    stopped.append(current["InstanceId"])
    print({"stopped": stopped})
    return {"stopped": stopped}
