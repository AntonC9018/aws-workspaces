# Lifecycle and limits

Coder stores Terraform state in local PostgreSQL. On AWS, the `aws-private` template creates an EC2
instance, an encrypted 30 GiB gp3 root disk (`DeleteOnTermination=true`), a security group, and an
SSH key pair. On Oracle Cloud, the `oci-private` template creates a compute instance inside the
`coder-workspaces` compartment, a 50 GiB boot volume (`preserve_boot_volume = false`), and a
per-workspace NSG attached to the shared subnet.

Guest shutdown stops the instance and retains its disk. Explicit workspace deletion
(`coder-aws delete NAME`, `coder-oci delete NAME`, or dashboard Delete) permanently terminates the
instance and deletes its root disk, firewall rules, and local controller state. Fetch valuable
results before deleting a workspace. Back up the Coder database before relying on long-lived
workspaces.

## Jobs, leases, and waiting

`run` registers a detached systemd job under `ubuntu`. It writes output and exit status to
`~/.local/state/coder-jobs/<job-id>/` and reads a mutable deadline file. The default timeout is
300 seconds. Pass a finite `--timeout` before long builds, and use `--wait`, `wait NAME`, or
`logs NAME` instead of raw SSH polling. One registered job runs per worker.

After completion or failure, the guest drops back to a five-minute idle countdown. Use `extend`
only while a job is running; use `hold` for quiet interactive work or previews. A running server,
open editor, or SSH session alone does not renew leases. Rate limits or a lost controller leave a
shutdown deadline in place.

## Cloud safeguards and automatic cleanup

- **Guest lease timer (AWS and OCI):** checks `/var/lib/coder-workspace/lease.json` every 15
  seconds and runs `systemctl poweroff` when expired. Works even when the laptop is off.
- **AWS stop guard:** EventBridge invokes `stop_guard.py` Lambda every minute to stop owned
  running instances whose `LeaseExpiresAt` tag is missing, invalid, or expired. `create` and
  `resume` also fail closed if `aws-guard` (`~/.local/state/aws-guard/lockdown.json`) is active.
- **OCI safeguards:** `coder-oci` isolates all workers in the `coder-workspaces` compartment and
  shares one VCN/IGW/subnet (Free Trial allows one Internet Gateway). Before creating or resuming
  paid shapes (`e5-*`, `a1-4c-24g`), it checks:
  1. `oci_trial_expires` (default `2026-12-04`, one day before the 30-day trial ends),
  2. cumulative monthly Usage API cost against `oci_trial_spend_limit_usd` (default `$250`), and
  3. effective OCI tenancy quotas via the Limits API.
  Always Free `a1-2c-12g` remains allowed as long as Always Free quota is available.
- **Automatic stale-workspace deletion:** stopped disks still consume credits. Both `reconcile`
  (periodic) and `gc` tag stopped instances with `StoppedSince` and delete them cleanly via
  `coder delete NAME --yes` once they stay stopped longer than `delete_after_stopped_hours`
  (default `24`, set `"0"` to disable), unless the workspace is marked with `--keep` or
  `keep NAME` (`KeepWorkspace=true`).
- **Automatic SSH firewall update:** if the laptop's public IPv4 address changes, `connect` and
  remote operations update the workspace's AWS security group or OCI NSG TCP/22 rule automatically.

## Sizes

- AWS (`aws-private`, x86_64): `t3.small` (2 vCPU / 2 GiB), `c7i-flex.large` (2 vCPU / 4 GiB),
  `m7i-flex.large` (2 vCPU / 8 GiB), `c7i-flex.xlarge` (4 vCPU / 8 GiB), `m7i-flex.xlarge`
  (4 vCPU / 16 GiB). Root disk `--disk-gb` 30–100 GiB (default 30).
- OCI (`oci-private`): `a1-2c-12g` (Always Free ARM64 2 OCPU / 12 GiB, default), `a1-4c-24g`
  (ARM64 4 OCPU / 24 GiB), `e5-2c-8g` (x86_64 2 OCPU / 4 vCPU / 8 GiB), `e5-4c-16g` (x86_64 4 OCPU
  / 8 vCPU / 16 GiB), `e5-8c-32g` (x86_64 8 OCPU / 16 vCPU / 32 GiB). Boot disk `--disk-gb`
  50–100 GiB (default 50).

The Ubuntu templates do not prove that a NixOS configuration boots and do not provide KVM.

## Development

```sh
nix develop -c env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
nix build .#coder-aws .#coder-oci .#coder-trial --max-jobs 1 --cores 1
```
