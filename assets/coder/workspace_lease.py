#!/usr/bin/env python3
"""Guest-side leases and job results; external AWS guard remains authoritative."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

STATE = Path("/var/lib/coder-workspace/lease.json")
DEFAULT_TIMEOUT = 300


def update(action):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(STATE.read_text()) if STATE.exists() else {"until": 0}
        result = action(state)
        temp = STATE.with_suffix(".tmp")
        temp.write_text(json.dumps(state))
        os.replace(temp, STATE)
        return result


def main():
    parser = argparse.ArgumentParser(description="Workspace leases default to five minutes (300s).")
    parser.add_argument("action", choices=["hold", "tick", "complete", "status"])
    parser.add_argument("seconds", type=int, nargs="?", default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("timeout must be positive")
    if args.action == "hold":
        update(lambda s: s.update(until=max(s["until"], time.time() + args.seconds)))
    elif args.action == "complete":
        # Completion returns to the normal five-minute idle countdown.
        update(lambda s: s.update(until=time.time() + DEFAULT_TIMEOUT))
    elif args.action == "status":
        print(STATE.read_text())
    elif update(lambda s: s["until"] <= time.time()):
        subprocess.run(["systemctl", "poweroff"], check=True)


if __name__ == "__main__":
    main()
