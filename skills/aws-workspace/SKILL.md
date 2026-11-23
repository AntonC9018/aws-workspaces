---
name: aws-workspace
description: Offload builds and commands to Coder-managed AWS or Oracle Cloud workers with bounded jobs, persistent disks, explicit source/result transfer, and cleanup.
---

# Cloud workspaces (`coder-aws` / `coder-oci`)

Use `coder-aws` (AWS) or `coder-oci` (OCI) matching the worker's provider. If missing from PATH,
`realpath` this skill dir and go two dirs up for `nix run <dir>#coder-aws --` (`#coder-oci`) and
`docs/setup.md`. If local Coder refuses connection, run `coder-trial start`.

## Sizes

- `coder-aws` (x86_64): `t3.small` (2v/2G, default), `c7i-flex.large` (2v/4G),
  `m7i-flex.large` (2v/8G), `c7i-flex.xlarge` (4v/8G), `m7i-flex.xlarge` (4v/16G).
  `--disk-gb` 30–100 (default 30).
- `coder-oci` (`coder-oci setup` once): `a1-2c-12g` (Always Free ARM64 2c/12G, default),
  `a1-4c-24g` (ARM64), `e5-2c-8g` / `e5-4c-16g` / `e5-8c-32g` (trial x86_64 4–16v/8–32G; needs
  quota beyond Always Free). `--disk-gb` 50–100 (default 50).

Pick the smallest RAM-fitting size. Never expose dashboards or previews publicly. If a task needs
environment setup beyond syncing the repo and running its install script, use an existing private
image (`images` / `create --image`) or suggest adding a `.coder-images/<name>.json` spec and
baking it with `bake-image --spec`.

## Lifecycle and cleanup

```sh
coder-aws list --all
coder-aws create NAME [--instance-type SIZE] [--disk-gb N] [--image NAME_OR_ID] [--manifest FILE] [--thread-id ID] [--keep]
coder-aws run NAME [--project KEY] [--timeout 3600] [--wait] -- bash -c 'make build'
coder-aws fetch NAME && coder-aws delete NAME
```

Same commands on `coder-oci`: `status`, `plan --manifest`, `sync --manifest`, `wait`, `logs`,
`extend --timeout`, `hold --timeout`, `stop`, `resume`, `connect`, `keep [--off]`, `gc`, `links`,
`images [--all]`, `bake-image --spec FILE`, `delete-image IMG`,
`authorize-unity NAME [--login|--return]`.

- **Always `delete NAME` after fetching/verifying** unless asked to keep it (`stop` bills 30–50 GiB
  disks; stopped workers without `--keep` auto-delete after 24h).
- Default timeout is **300s**; pass finite `--timeout` for long builds. One detached systemd job
  runs per worker; wrap shell syntax in `bash -c '...'`. Use `--wait`, `wait`, or `logs` (not raw
  SSH polling). Finished jobs drop to a 5m idle lease.
- `extend NAME --timeout SEC` requires an active job; use `hold NAME --timeout SEC` for idle work
  or previews. `resume NAME` takes no `--timeout` (follow with `hold`). SSH/editors do not renew
  leases.
- `--project KEY` must match `projects[].name` in the manifest (omit for `/home/ubuntu/projects`).

## Manifest and binary transfer

```json
{"version":1,"projects":[{"name":"app","path":"../repo","revision":"HEAD","destination":"src/app","include_untracked":["a.txt"],"artifacts":["dist"]}],"files":[{"source":"c.txt","destination":"shared/c.txt"}]}
```

- Validate with `plan --manifest`; `sync` needs an idle worker and never overwrites snapshots.
  Separate projects for submodules; rejects LFS, symlinks, artifact globs,
  `.aws`/`.ssh`/`.gnupg`/`.git`.
- Run `fetch NAME` only after job exit; writes verified output to
  `~/.local/state/coder-workspaces/results/NAME/` without editing local worktrees.
- Never stream raw binaries over `coder ssh` stdout (PTY turns `\n` into `\r\n`). Use `fetch`
  artifacts, `scp`, or `coder ssh NAME -- 'base64 FILE' | tr -d '\r' | base64 -d > FILE`.

## T3 threads, Git, and secrets

- T3 tasks: pass `--thread-id` from `t3_thread_configure` (never `CODEX_THREAD_ID`). Include the
  printed Coder link in the reply; never use PR-linking tools; never overwrite a reused worker's
  thread.
- Keep AWS/OCI/GitHub keys local. Use `authorize NAME --repo OWNER/REPO` (1h, single-repo,
  read-only; `--write` only when asked to push). Snapshots lack `origin`; add HTTPS remote and push
  only on explicit request. Keep `Generated-by:` trailers per policy.
