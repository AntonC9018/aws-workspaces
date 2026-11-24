#!/usr/bin/env python3
"""Guest-side named leases; external cloud guard remains authoritative."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time

STATE = Path("/var/lib/coder-workspace/lease.json")
DEFAULT_TIMEOUT = 300
BOOT_GRACE = 600
OWNER_RE = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")


def other_job_running():
    """Whether any coder job is starting or running; finalizing units are in 'deactivating'."""
    units = subprocess.run(
        ["systemctl", "list-units", "--state=activating,running", "--no-legend", "coder-job-*.service"],
        capture_output=True, text=True,
    )
    if units.returncode != 0:
        # Fail closed against shortening leases if systemd state cannot be inspected.
        return True
    return bool(units.stdout.strip())


def _load_state(raw, now):
    if not raw or (raw.get("until", 0) == 0 and raw.get("idle_until", 0) == 0 and not raw.get("leases")):
        return {"idle_until": 0.0, "leases": {}, "_uninitialized": True}
    if "idle_until" not in raw and "leases" not in raw:
        legacy_until = float(raw.get("until", 0))
        if legacy_until > now + DEFAULT_TIMEOUT:
            return {"idle_until": now + DEFAULT_TIMEOUT, "leases": {"legacy": legacy_until}, "_uninitialized": False}
        return {"idle_until": legacy_until, "leases": {}, "_uninitialized": False}
    idle_until = float(raw.get("idle_until", 0))
    leases = {}
    for owner, deadline in raw.get("leases", {}).items():
        if not isinstance(owner, str) or not isinstance(deadline, (int, float)):
            continue
        ts = float(deadline)
        if ts > now:
            leases[owner] = ts
        else:
            # Preserve expired timestamp floor so an exhausted lease never resets to uninitialized (0).
            idle_until = max(idle_until, ts)
    return {"idle_until": idle_until, "leases": leases, "_uninitialized": False}


def _effective_until(state):
    return max([state["idle_until"], *state["leases"].values()], default=0.0)


def _serialize(state):
    return {
        "until": _effective_until(state),
        "idle_until": state["idle_until"],
        "leases": dict(sorted(state["leases"].items())),
    }


def update(action):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = time.time()
        raw = json.loads(STATE.read_text()) if STATE.exists() else {}
        state = _load_state(raw, now)
        result = action(state, now)
        payload = _serialize(state)
        temp = STATE.with_suffix(".tmp")
        temp.write_text(json.dumps(payload))
        os.replace(temp, STATE)
        return result, payload, now


def _validate_owner(parser, owner):
    if not owner or not OWNER_RE.fullmatch(owner):
        parser.error(f"invalid lease owner: {owner!r}")
    return owner


def main():
    parser = argparse.ArgumentParser(description="Workspace leases default to five minutes (300s).")
    parser.add_argument("action", choices=["hold", "release", "tick", "complete", "status"])
    parser.add_argument("seconds", type=int, nargs="?", default=DEFAULT_TIMEOUT)
    parser.add_argument("--owner", help="Named lease scope (e.g. boot, hold, job:<id>, op:<id>)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print resulting lease state as JSON")
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("timeout must be positive")

    if args.action == "hold":
        owner = _validate_owner(parser, args.owner or "hold")
        _, payload, now = update(lambda s, t: s["leases"].update({owner: max(s["leases"].get(owner, 0.0), t + args.seconds)}))
    elif args.action == "release":
        owner = _validate_owner(parser, args.owner)
        def _release(s, t):
            s["leases"].pop(owner, None)
            s["idle_until"] = max(s["idle_until"], t + DEFAULT_TIMEOUT)
        _, payload, now = update(_release)
    elif args.action == "complete":
        if args.owner:
            owner = _validate_owner(parser, args.owner)
            def _complete_owned(s, t):
                s["leases"].pop(owner, None)
                s["idle_until"] = max(s["idle_until"], t + DEFAULT_TIMEOUT)
            _, payload, now = update(_complete_owned)
        else:
            # Legacy ownerless completion: check systemd under the lock and never drop explicit holds.
            def _complete_legacy(s, t):
                s["idle_until"] = max(s["idle_until"], t + DEFAULT_TIMEOUT)
                if not other_job_running():
                    for key in [k for k in s["leases"] if k == "legacy" or k.startswith("job:")]:
                        s["leases"].pop(key, None)
            _, payload, now = update(_complete_legacy)
    elif args.action == "status":
        now = time.time()
        raw = json.loads(STATE.read_text()) if STATE.exists() else {}
        payload = _serialize(_load_state(raw, now))
        print(json.dumps(payload | {"remaining": max(0.0, payload["until"] - now)}))
        return
    else:
        def _tick(s, t):
            if s["_uninitialized"]:
                s["idle_until"] = t + DEFAULT_TIMEOUT
                s["leases"]["boot"] = t + BOOT_GRACE
                return False
            return _effective_until(s) <= t
        expired, payload, now = update(_tick)
        if expired:
            subprocess.run(["systemctl", "poweroff"], check=True)
        return

    if args.as_json:
        print(json.dumps(payload | {"remaining": max(0.0, payload["until"] - now)}))


if __name__ == "__main__":
    main()
