---
name: aws-workspace
description: Offload builds and commands to Coder-managed AWS or Oracle Cloud workers with bounded jobs, persistent disks, explicit source/result transfer, and cleanup.
---

# Cloud workspaces (`coder-aws` / `coder-oci`)

Match CLI to worker provider (`coder-aws` / `coder-oci`). If not on PATH, `realpath` this skill
dir, go two dirs up, and run `nix run <dir>#coder-aws --` (`#coder-oci`). If local Coder refuses
connection, run `coder-trial start`.

## Sizes (smallest RAM-fitting; never expose publicly)

- AWS (`x86_64`, `--disk-gb` 30–100, default 30): `t3.small` (2v/2G, default),
  `c7i-flex.large` (2v/4G), `m7i-flex.large` (2v/8G), `c7i-flex.xlarge` (4v/8G),
  `m7i-flex.xlarge` (4v/16G).
- OCI (`--disk-gb` 50–100, default 50): `a1-2c-12g` (Always Free ARM64, default), `a1-4c-24g`,
  `e5-2c-8g` / `e5-4c-16g` / `e5-8c-32g` (trial x86_64 4–16v/8–32G).

## Lifecycle and rules

```sh
coder-aws list --all
coder-aws create NAME [--instance-type SIZE] [--disk-gb N] [--image IMG] [--manifest FILE] [--thread-id ID] [--keep]
coder-aws run NAME [--project KEY] [--timeout 3600] [--wait] -- bash -c 'make build'
coder-aws fetch NAME && coder-aws delete NAME
```

Same on `coder-oci`: `status`, `wait`, `logs`, `extend`, `hold`, `stop`, `resume`, `connect`,
`keep [--off]`, `gc`, `links`.

- **Always `delete NAME`** after fetching/verifying unless asked to keep (`stop` bills disks;
  unkept stopped workers auto-delete after 24h).
- Default timeout **300s**; pass finite `--timeout`. One detached job/worker; wrap shell syntax in
  `bash -c '...'`; use `--wait`/`wait`/`logs` (not SSH polling). Finished jobs drop to a 5m lease.
- `extend --timeout SEC` needs an active job; use `hold --timeout SEC` for idle/previews; `resume`
  takes no `--timeout` (follow with `hold`). SSH/editors do not renew leases.
- T3 tasks: pass `--thread-id` from `t3_thread_configure` (never `CODEX_THREAD_ID`); include
  printed Coder link in reply; never use PR-linking tools; never overwrite a reused worker's thread.
- Keep AWS/OCI/GitHub/Unity keys local.

## References (load only when needed)

- `references/manifests-and-transfer.md`: manifest JSON, `plan`/`sync`/`fetch`, submodules, binary
  SSH transfer, GitHub `authorize`/push.
- `references/images-and-unity.md`: custom images (`images`, `bake-image`, `delete-image`,
  `.coder-images/*.json`) and Unity Personal (`authorize-unity [--login|--return]`).
