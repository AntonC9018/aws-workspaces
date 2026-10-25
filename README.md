# AWS workspaces

Offload builds and agent jobs from your PC to AWS while using up free AWS
credits. Coder provides the dashboard. Jobs have deadlines, and idle machines
stop after five minutes even if your PC is off. Stopping keeps their disks.

## Usage

Requires Linux with systemd, Nix, and an AWS account on the active Free plan.
The default template uses Ubuntu 24.04 in Frankfurt.

```sh
nix run .#coder-trial -- start
nix run .#coder-trial -- login
nix run .#coder-aws -- setup
nix run .#coder-aws -- configure-auth

nix run .#coder-aws -- create build --manifest examples/workspace.json
nix run .#coder-aws -- run build --project app --timeout 3600 -- make build
nix run .#coder-aws -- fetch build
nix run .#coder-aws -- stop build
nix run .#coder-aws -- resume build
```

Create the dashboard account before CLI login. Edit the example manifest to
select your sources. `setup` needs AWS bootstrap credentials; `configure-auth`
creates a restricted local key for later operations. Neither command upgrades
your AWS plan.

`run` starts a detached job. Its default timeout is 300 seconds; choose a longer
finite timeout for builds. Use `extend` to extend a running job, or `hold` for a
timed preview. `fetch` returns commits, dirty files, logs and selected artifacts
to a separate local directory. It leaves your working tree untouched.

For private repositories, run `github-setup` once, install the App, then use
`authorize build --repo OWNER/REPO`. Tokens cover one repository and default to
read-only. Add `--write` when a job needs to push. Nothing pushes automatically.

## Architecture

- Local Coder and PostgreSQL hold workspace and Terraform state.
- The controller creates EC2 workers and connects them through private reverse
  SSH tunnels. Only the controller's IPv4 address can SSH into a worker.
- Worker systemd jobs enforce job deadlines and return to a five-minute idle
  lease after completion. An AWS Lambda checks lease tags every minute as a
  fallback. It keeps working when the controller is offline.
- Source manifests create isolated Git snapshots. Results stay on persistent
  EBS storage until you retrieve them. GitHub App signing keys stay local;
  workers receive short-lived repository tokens.

Stopped disks, public IPv4 addresses and managed AWS services can still consume
credits. There is no automatic deletion. NixOS boot testing and token renewal
while the controller is offline are not implemented.

[Setup](docs/setup.md) · [Sources and results](docs/sources.md) ·
[Lifecycle and limits](docs/architecture.md)
