# Setup

The Nix flake supplies `coder-trial` and `coder-aws`. `nix develop` supplies test
and development tools. Run the commands from the checkout, or install those
packages with Home Manager.

## Controller

Run `coder-trial start`, visit `http://127.0.0.1:7080`, create your Coder account,
and run `coder-trial login`. The local server uses one Terraform provisioner,
a private PostgreSQL Unix socket, and memory limits. Telemetry is disabled.

Run `coder-aws setup` with an AWS bootstrap profile that can create IAM roles,
Lambda, EventBridge and EC2 resources. It deploys the stop guard and publishes
the `aws-private` Coder template. Then run `coder-aws configure-auth` to create
a restricted IAM controller key in `~/.aws/coder-workspaces.credentials`, mode
0600. Routine operations use that key; bootstrap still uses the default AWS
profile. The restricted policy can terminate only instances carrying this deployment's
ownership tag and cannot administer IAM.

Configuration lives in `~/.config/coder-workspaces/config.json`. It contains
non-secret settings only. See [the example](../examples/config.json). Defaults
select Frankfurt and a pinned Ubuntu image. Changing region also requires a
matching AMI. Choose your own resource namespace before first setup. Keep it
unchanged when reusing existing infrastructure.

For NixOS or Home Manager, import this flake's `homeModules.default`:

```nix
{
  imports = [ inputs.aws-workspaces.homeModules.default ];
  programs.aws-workspaces = {
    enable = true;
    settings = {
      coder_url = "https://controller.example.ts.net:8443";
      thread_base_url = "https://threads.example.com";
    };
  };
}
```

This installs the commands, settings and agent skill. Other setups can link
`skills/aws-workspace` into their agent's skills directory. Read the skill before
letting an agent provision machines.

## Oracle Cloud (OCI)

`coder-oci` uses `~/.oci/config` (default profile `AWS_INFRA`, region `eu-turin-1`). Run
`coder-oci setup` once to create:

- the isolated `coder-workspaces` compartment,
- one shared VCN (`10.42.0.0/16`), Internet Gateway, route table, egress-only security list, and
  public subnet (`10.42.1.0/24`),
- a compartment spend budget matching `oci_trial_spend_limit_usd`, and
- the `oci-private` Coder template.

OCI Free Trial allows only **one** Internet Gateway per tenancy. If another VCN already holds your
Internet Gateway, set `oci_subnet_id` in `config.json` to reuse that public subnet, or run
`coder-oci teardown-network` after deleting all OCI workspaces to free the gateway.

If your tenancy enforces an Always Free quota (for example `aws-infra/src/Oracle/quotas.ts`),
`a1-2c-12g` works out of the box at $0 cost while paid trial shapes (`e5-*`, `a1-4c-24g`) fail fast
during quota preflight until the quota opens those families. After `oci_trial_expires` (default
`2026-12-04`), paid shapes are blocked automatically.

## Private per-project machine images

Projects can define multiple image specs under `.coder-images/<name>.json` (with optional
`packages`, `setup_script`, `commands`, `arch`, and `disk_gb`). Image names are scoped
automatically by project (`<project>-<name>`). Both AWS AMIs and OCI custom images are private to
your account or compartment and never include source checkouts or credentials:

```sh
coder-oci bake-image --spec .coder-images/unity-6000.json
coder-oci images --all
coder-oci create dev --image colorshoot-unity-6000 --instance-type e5-4c-16g --disk-gb 80
coder-oci authorize-unity dev --login  # activates Unity Personal via Unity CLI (caches auth locally)
coder-oci authorize-unity dev --return # returns the seat and wipes worker Unity session
coder-oci delete-image colorshoot-unity-6000
```

Re-running `bake-image` skips work if the spec hash matches, or builds a fresh image and removes
the superseded one when the spec changes. On AWS, run `coder-aws configure-auth` once after
upgrading so the restricted controller policy allows private AMI creation and deletion.

## Dashboard login through GitHub

`coder-aws github-login-setup` prepares a private GitHub login App with read-only
email permission and no repository access. Open the printed local page and
approve App creation while signed into GitHub. This uses the App manifest flow
so generated client credentials are saved directly, without copying secrets.
It is separate from the repository-token App created by `github-setup`.

After registration, restart `coder-trial.service`. It reads the private
`~/.local/state/coder-workspaces/github-login/login.env`, disables Coder's shared
login provider and device flow, and uses browser redirects to the configured
`coder_url`. New-user signups remain disabled; existing users can sign in.
The repository-token App and saved CLI sessions are unchanged. Worker bootstrap
continues to use its loopback reverse tunnel rather than the browser URL.

## Browser sessions

Browser sessions default to 24 hours and renew with activity. Set
`session_duration` to a duration in whole hours, such as `8760h` for a year,
then restart `coder-trial.service`. The maximum supported setting is `876000h`.
This is a long expiry, not an immortal credential: logout, revocation and clearing
browser cookies still end the session. Existing short-lived sessions may need
one new login to receive the new lifetime. Use the same dashboard hostname;
localhost and a tailnet hostname have separate browser cookies.

## Network and threads

`coder-trial tailnet` exposes the dashboard privately through Tailscale Serve
on port 8443. Set `coder_url` to its HTTPS address so workspace links point
there. The backend remains on loopback. No public dashboard or monitoring VM is
needed. Workers use an ephemeral public IPv4 address for SSH and outbound access.
No NAT gateway is provisioned.

The IPv6-only option is blocked by direct GitHub clone/download endpoints that
lack IPv6 service. IPv4-mapped addresses do not provide translation. Recheck
endpoint support before changing this; NAT64 would add infrastructure and cost.

For T3 work, pass `create NAME --thread-id APP_THREAD_ID`. This is the T3 app
thread ID, not the provider session ID. Set `thread_base_url` for generated
links, or pass `--thread-url` explicitly. EC2 tags record the creating thread
and Coder URL. Include the printed Coder link in the agent's reply. Filter EC2
by `CreatedVia=Coder`, or Coder by `template:aws-private`.

If your controller public IPv4 address changes, `connect` and remote actions automatically update
the workspace's AWS security group or OCI NSG TCP/22 rule; run `publish-template` so newly created
workspaces also receive the new address immediately. A dashboard start restores its bridge once;
open SSH sessions do not renew idle leases indefinitely.

## GitHub

Run `coder-aws github-setup` and open the printed local URL within 15 minutes.
Create the private App in the account that owns your repositories. Choose all
repositories if agents should be able to request access to any current or
future repo, or select a smaller set. `github_owner` can restrict the expected
installation account. GitHub requires the account owner's installation consent.

The App key stays in `~/.local/state/coder-workspaces/github`, outside Git and
the Nix store. No webhooks or user OAuth are needed. Each `authorize NAME --repo
OWNER/REPO` request narrows the installation's access to one repository with
contents read-only. `--write` requests contents write. Tokens expire after one
hour; renewal currently needs the controller online. Workflow-file changes may
need additional App permissions beyond contents write.

Workers use an HTTPS Git credential helper. Tokens never enter clone URLs or
command arguments. Commit identity comes from local configured Git identity at
template publication, independently of push authentication. No personal access
token is copied to a worker.

References: [App manifests](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest),
[installation tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).

## Upgrading existing workspaces for permanent deletion

After upgrading the controller, run `coder-aws configure-auth` to update its
owned-instance termination permission, then `coder-aws publish-template`.
New workspaces delete their root disk when terminated.

Existing workspaces must apply the new template before deletion. For a stopped
workspace, run `coder-aws upgrade NAME`. This
updates the disk's deletion flag in place without starting the worker. For a
running workspace, finish or fetch active work before updating it. Verify the
root block device has `DeleteOnTermination=true` in EC2 before deleting.

Use `coder-aws delete NAME`, `coder-oci delete NAME`, the dashboard Delete action, or
`coder delete NAME --yes`. Stopping keeps the disk; deleting permanently removes it. Do not use
`--orphan` for cleanup. Stopped workspaces without `--keep` are deleted automatically by
`reconcile` / `gc` after `delete_after_stopped_hours` (default `24`).
