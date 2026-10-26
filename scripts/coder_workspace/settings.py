"""Non-secret controller settings, kept outside the source checkout."""
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
    if not re.fullmatch(r"[1-9][0-9]{0,5}h", settings["session_duration"]) or int(settings["session_duration"][:-1]) > 876000:
        raise ValueError("session_duration must be 1–876000 hours, for example 24h")
    for key in ("managed_by", "guard_name", "iam_user"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", settings[key]):
            raise ValueError(f"Invalid {key}")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", settings["region"]):
        raise ValueError("Invalid region")
    if not re.fullmatch(r"ami-[0-9a-f]+", settings["ami_id"]):
        raise ValueError("Invalid AMI ID")
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
