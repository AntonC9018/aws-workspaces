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
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
DEFAULT_TIMEOUT = 300
BOOT_GRACE = 600
OWNER_RE = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")


def current_boot_id():
    """Return the kernel boot UUID so leases never leak across VM stop/start cycles."""
    try:
        return BOOT_ID_PATH.read_text().strip()
    except OSError:
        return ""


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


def _effective_until(state):
    return max([state["idle_until"], *state["leases"].values()], default=0.0)


def _load_state(raw, now, boot_id=""):
    if not raw or (raw.get("until", 0) == 0 and raw.get("idle_until", 0) == 0 and not raw.get("leases")):
        return {"idle_until": 0.0, "leases": {}, "_uninitialized": True}
    stored_boot = raw.get("boot_id", "")
    if boot_id and stored_boot and stored_boot != boot_id:
        # New kernel boot: discard pre-stop leases and expired timestamps so resume starts fresh.
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
    state = {"idle_until": idle_until, "leases": leases, "_uninitialized": False}
    # Preserve a higher scalar 'until' written by an older helper on another controller host.
    raw_until = raw.get("until", 0)
    if isinstance(raw_until, (int, float)) and float(raw_until) > _effective_until(state) and float(raw_until) > now:
        state["leases"]["legacy"] = max(state["leases"].get("legacy", 0.0), float(raw_until))
    return state


def _serialize(state, boot_id=""):
    payload = {
        "until": _effective_until(state),
        "idle_until": state["idle_until"],
        "leases": dict(sorted(state["leases"].items())),
    }
    if boot_id:
        payload["boot_id"] = boot_id
    return payload


def _release_owner(state, now, owner):
    state["leases"].pop(owner, None)
    state["idle_until"] = max(state["idle_until"], now + DEFAULT_TIMEOUT)
    if owner.startswith("job:") and "legacy" in state["leases"] and not other_job_running():
        state["leases"].pop("legacy", None)
    elif owner == "boot" and "legacy" in state["leases"] and state["leases"]["legacy"] <= now + BOOT_GRACE:
        state["leases"].pop("legacy", None)


def update(action):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = time.time()
        boot_id = current_boot_id()
        raw = json.loads(STATE.read_text()) if STATE.exists() else {}
        state = _load_state(raw, now, boot_id)
        result = action(state, now)
        payload = _serialize(state, boot_id)
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
        # Ownerless hold maps to 'legacy' so unpatched boot/job scripts never masquerade as explicit user holds.
        owner = _validate_owner(parser, args.owner or "legacy")
        _, payload, now = update(lambda s, t: s["leases"].update({owner: max(s["leases"].get(owner, 0.0), t + args.seconds)}))
    elif args.action == "release":
        owner = _validate_owner(parser, args.owner)
        _, payload, now = update(lambda s, t: _release_owner(s, t, owner))
    elif args.action == "complete":
        if args.owner:
            owner = _validate_owner(parser, args.owner)
            _, payload, now = update(lambda s, t: _release_owner(s, t, owner))
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
        boot_id = current_boot_id()
        raw = json.loads(STATE.read_text()) if STATE.exists() else {}
        payload = _serialize(_load_state(raw, now, boot_id), boot_id)
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
