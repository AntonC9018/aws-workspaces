# Cloud workspaces (`coder-aws` / `coder-oci`)

Offload builds and agent jobs from your PC to AWS EC2 or Oracle Cloud (OCI) while burning free
cloud credits safely. Coder provides the dashboard. Jobs run with finite deadlines, idle machines
stop after five minutes even if your PC is off, and stopped workspaces auto-delete after 24 hours
unless kept explicitly.

## Usage

Requires Linux with systemd, Nix, and an AWS or Oracle Cloud account. Both templates boot pinned
Canonical Ubuntu 24.04 images (`eu-central-1` on AWS, `eu-turin-1` on OCI).

```sh
nix run .#coder-trial -- start
nix run .#coder-trial -- login

# AWS setup (once)
nix run .#coder-aws -- setup
nix run .#coder-aws -- configure-auth

# Oracle Cloud setup (once; uses ~/.oci/config profile AWS_INFRA)
nix run .#coder-oci -- setup

# Daily workflow (identical commands for coder-aws and coder-oci)
nix run .#coder-aws -- create build --manifest examples/workspace.json
nix run .#coder-aws -- run build --project app --timeout 3600 --wait -- make build
nix run .#coder-aws -- fetch build
nix run .#coder-aws -- delete build
```

Create the dashboard account before CLI login. Edit the example manifest to select sources.
`coder-aws setup` needs AWS bootstrap credentials; `configure-auth` creates a restricted local key
for routine use. `coder-oci setup` creates an isolated `coder-workspaces` compartment, a single
shared VCN/IGW/subnet (fitting Oracle's 1-IGW Free Trial limit), a compartment budget, and the
`oci-private` template.

`run` starts a detached job with a 300-second default timeout; choose a finite `--timeout` for
builds and pass `--wait` (or follow with `wait` / `logs`). Use `extend` while a job is running,
or `hold` for quiet interactive work. `fetch` retrieves commits, dirty files, logs, and declared
artifacts into a separate local directory without modifying your worktree. Run `delete` once you
have verified the results.

For private repositories, run `github-setup` once, install the App, then use
`authorize build --repo OWNER/REPO`. Tokens cover one repository and default to read-only. Add
`--write` when a job needs to push. Nothing pushes automatically.

## Safeguards and architecture

- Local Coder and PostgreSQL hold workspace and Terraform state.
- Workers connect through private reverse SSH tunnels. Only the controller's IPv4 address can SSH
  into a worker; `connect` updates the security group / NSG automatically if your IPv4 changes.
- Worker systemd jobs enforce deadlines and drop to a five-minute idle lease after completion.
  On AWS, an EventBridge-triggered Lambda checks `LeaseExpiresAt` tags every minute independently
  of the laptop. On OCI, guest leases run inside the VM, `reconcile` enforces `LeaseExpiresAt`
  every 5 minutes, and preflight checks enforce OCI quotas, `oci_trial_expires`, and
  `oci_trial_spend_limit_usd` before creating paid shapes.
- On AWS, `create` and `resume` also fail closed when `aws-guard` lockdown is active.
- Stopped workspaces without `--keep` (`KeepWorkspace=true`) are automatically deleted by
  `reconcile` / `gc` after `delete_after_stopped_hours` (default 24h) so abandoned disks do not
  keep billing.

[Setup](docs/setup.md) · [Sources and results](docs/sources.md) ·
[Lifecycle and limits](docs/architecture.md)
