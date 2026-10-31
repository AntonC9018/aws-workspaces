# Lifecycle and limits

Coder stores Terraform state in local PostgreSQL. The template creates an EC2
instance, encrypted 30 GiB gp3 root disk, security group and SSH key pair. Guest
shutdown stops the instance and retains its disk. Explicit workspace deletion
permanently terminates the instance and deletes its root disk, security group
and SSH key pair. Fetch valuable results before deleting a workspace.
Back up the Coder database before relying on valuable long-lived workspaces;
this project does not configure a remote database backup.

`run` registers a detached systemd job under the workspace user. It records
output, exit status and a mutable deadline. The default job timeout is five
minutes. The agent chooses a suitable finite timeout before a long build and
can extend it while running. One registered job runs per worker.

After completion or failure, the guest returns to a five-minute idle countdown.
An explicit `hold` allows quiet interactive work or a preview for a finite time.
A running server, open editor or SSH connection alone does not keep a worker
alive. Rate limits or a lost controller therefore leave a deadline in place.

The guest checks leases every 15 seconds. EventBridge invokes a Lambda every
minute to stop owned running instances whose `LeaseExpiresAt` tag is invalid
or expired. The worker has no credentials to change that external deadline.
The Lambda re-reads a lease before stopping, but EC2 tag updates and stopping
are not atomic. Extend leases before they are close to expiry. Shutdown timing
is approximate.

The default sizes are `t3.small`, `c7i-flex.large` and `m7i-flex.large`. Choose
sufficient RAM for the job and avoid idle capacity. Account eligibility is
checked against the active AWS Free plan. Service availability and free-credit
terms depend on the account; the controller refuses paid-plan accounts and
never upgrades a plan.

Stopping loses RAM and processes but keeps committed files and job records.
Storage and managed services may still consume credits. There is no retention
cleanup, automatic deletion, cost forecast, NixOS boot profile or KVM profile.
The current Ubuntu template does not prove that a NixOS configuration boots.

## Development

```sh
nix develop -c env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
nix build .#coder-aws .#coder-trial --max-jobs 1 --cores 1
```

Tests use temporary Git repositories and the configured Git identity. They need
no AWS resources. Coverage includes lease expiry, shutdown ownership, bridge
reconciliation, source/result recovery, path validation, token scope and JWT
signatures. Live smoke checks also covered stop/resume disk preservation,
private HTTPS cloning, agent commit identity, one-repository tokens and denial
for an unrelated private repository. Actual pushes were not performed.
