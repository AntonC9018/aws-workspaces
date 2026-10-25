#!/usr/bin/env python3
"""Git credential helper: return only an unexpired token for the exact HTTPS repo."""
import datetime as dt
import json
import hashlib
from pathlib import Path
import re
import sys


def lookup(values, directory):
    if values.get("protocol") != "https" or values.get("host", "").lower() != "github.com":
        return None
    repo = values.get("path", "").removesuffix(".git").strip("/").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*/[a-z0-9_.-]+", repo):
        return None
    path = directory / (hashlib.sha256(repo.encode()).hexdigest() + ".json")
    try:
        credential = json.loads(path.read_text())
        expiry = dt.datetime.fromisoformat(credential["expires_at"].replace("Z", "+00:00"))
        if expiry <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30):
            return None
        return credential["token"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main():
    if sys.argv[1:] != ["get"]:
        return
    values = dict(line.rstrip("\n").split("=", 1) for line in sys.stdin if "=" in line)
    token = lookup(values, Path.home() / ".config/coder-github")
    if token:
        print("username=x-access-token\npassword=" + token)


if __name__ == "__main__":
    main()
