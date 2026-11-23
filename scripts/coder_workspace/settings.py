"""Non-secret controller settings, kept outside the source checkout."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

DEFAULTS = {
    "session_duration": "24h",
    "region": "eu-central-1",
    "ami_id": "ami-0c228cd2b65888e1b",
    "managed_by": "coder-workspaces",
    "guard_name": "coder-workspaces-stop-guard",
    "iam_user": "coder-workspaces-controller",
    "coder_url": "http://127.0.0.1:7080",
    "thread_base_url": "",
    "github_owner": "",
    "github_app_name": "AWS Workspaces",
    "delete_after_stopped_hours": "24",
    "oci_profile": "AWS_INFRA",
    "oci_region": "eu-turin-1",
    "oci_compartment_name": "coder-workspaces",
    "oci_x86_image_id": "ocid1.image.oc1.eu-turin-1.aaaaaaaahicxzwk6x3jahe3yqdcn2tgp46ixycm4hy37ot4rfoqi4gacgxxq",
    "oci_arm_image_id": "ocid1.image.oc1.eu-turin-1.aaaaaaaa7dxvj67m7psdu2vhp2xymsmy63yfodmrc275vzttjny2mp5gmaea",
    "oci_trial_expires": "2026-12-04",
    "oci_trial_spend_limit_usd": "250",
    "oci_subnet_id": "",
}


def load(path=None):
    path = Path(path or os.environ.get("CODER_WORKSPACES_CONFIG", str(Path.home() / ".config/coder-workspaces/config.json")))
    supplied = json.loads(path.read_text()) if path.exists() else {}
    unknown = set(supplied) - set(DEFAULTS)
    if unknown:
        raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))
    if any(not isinstance(value, str) for value in supplied.values()):
        raise ValueError("Controller settings must be strings")
    settings = DEFAULTS | supplied
    if settings["region"] != DEFAULTS["region"] and "ami_id" not in supplied:
        raise ValueError("Set ami_id when selecting a different AWS region")
    if settings["oci_region"] != DEFAULTS["oci_region"] and not ({"oci_x86_image_id", "oci_arm_image_id"} <= set(supplied)):
        raise ValueError("Set both oci_x86_image_id and oci_arm_image_id when selecting a different OCI region")
    if not re.fullmatch(r"[1-9][0-9]{0,5}h", settings["session_duration"]) or int(settings["session_duration"][:-1]) > 876000:
        raise ValueError("session_duration must be 1–876000 hours, for example 24h")
    for key in ("managed_by", "guard_name", "iam_user", "oci_compartment_name"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", settings[key]):
            raise ValueError(f"Invalid {key}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", settings["oci_profile"]):
        raise ValueError("Invalid oci_profile")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", settings["region"]):
        raise ValueError("Invalid region")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", settings["oci_region"]):
        raise ValueError("Invalid oci_region")
    if not re.fullmatch(r"ami-[0-9a-f]+", settings["ami_id"]):
        raise ValueError("Invalid AMI ID")
    for key in ("oci_x86_image_id", "oci_arm_image_id"):
        if not re.fullmatch(r"ocid1\.image\.oc1\.[a-z0-9-]+\.[a-z0-9]+", settings[key]):
            raise ValueError(f"Invalid {key}")
    if settings["oci_subnet_id"] and not re.fullmatch(r"ocid1\.subnet\.oc1\.[a-z0-9-]+\.[a-z0-9]+", settings["oci_subnet_id"]):
        raise ValueError("Invalid oci_subnet_id")
    if not re.fullmatch(r"[0-9]{1,3}", settings["delete_after_stopped_hours"]) or int(settings["delete_after_stopped_hours"]) > 720:
        raise ValueError("delete_after_stopped_hours must be 0–720")
    if settings["oci_trial_expires"]:
        try:
            dt.date.fromisoformat(settings["oci_trial_expires"])
        except ValueError as error:
            raise ValueError("oci_trial_expires must be YYYY-MM-DD or empty") from error
    if settings["oci_trial_spend_limit_usd"]:
        try:
            if float(settings["oci_trial_spend_limit_usd"]) <= 0:
                raise ValueError
        except ValueError as error:
            raise ValueError("oci_trial_spend_limit_usd must be a positive number or empty") from error
    for key in ("coder_url", "thread_base_url"):
        value = settings[key]
        if key == "thread_base_url" and not value:
            continue
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"{key} must be an HTTP(S) base URL without credentials, query or fragment")
        settings[key] = value.rstrip("/")
    return settings


if __name__ == "__main__":
    print(load()[sys.argv[1]])
