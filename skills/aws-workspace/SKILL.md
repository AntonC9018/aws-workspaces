---
name: aws-workspace
description: Offload builds and commands to Coder-managed AWS workers with bounded jobs, persistent disks and explicit source/result transfer.
---

# AWS workspaces

Use `coder-aws` and `coder-trial` from PATH, or `nix run <checkout>#coder-aws --`.
The Home Manager installation links into the full pinned repository: resolve
this skill directory with `realpath`, then go two directories up to find its
flake and `docs/setup.md`. This also supplies a checkout path for `nix run` when
the commands are not on PATH.
Credentials stay outside Git and the Nix store. Never copy controller AWS keys,
App signing keys or a broad personal GitHub token to a worker.

## Jobs and stopping

Job timeout defaults to **300 seconds**. Choose a longer finite timeout before a
long build. Open SSH sessions, editors and preview servers do not renew leases.
Completed jobs return to a five-minute idle countdown. Use an explicit finite
hold for quiet interactive work. Agent failure or rate limits must leave a
shutdown deadline in place.

```sh
coder-aws create build --manifest workspace.json
coder-aws run build --project app --timeout 7200 -- make build
coder-aws extend build --timeout 3600
coder-aws hold build --timeout 600
coder-aws fetch build
coder-aws stop build
coder-aws resume build
```

Jobs are detached systemd services. Use a shell explicitly for shell syntax.
One registered job runs per worker. Choose RAM for the actual work and avoid
unneeded capacity. Do not upgrade the AWS Free plan. Stopping preserves disks;
storage still consumes credits. Explicit dashboard Delete or `coder delete NAME`
permanently removes the instance and root disk. Fetch needed results first.
No automatic deletion is configured.

`connect` restores a bridge to a running worker; `resume` starts a stopped one.
Do not expose dashboards or previews publicly. Use the existing IPv4 networking;
IPv6-only GitHub access remains a recorded blocker. This Ubuntu template does
not validate NixOS boot or KVM availability.

## Sources and retrieval

Select needed repositories, revisions and extra files in a version-1 JSON
manifest. Preview with `plan --manifest FILE`, then use `create --manifest FILE`
or `sync NAME --manifest FILE` on an idle worker. See `docs/sources.md`.
Include untracked files explicitly. Never copy a whole home or credential store.
Existing remote snapshots are not overwritten. Submodules, LFS and symlinks
need separate handling; report unsupported inputs instead of omitting them.

Fetch only after the job finishes. It returns commits, dirty files, logs and
selected artifacts into separate local checkouts, preserving concurrent local
edits. Inspect results and merge or cherry-pick deliberately. If the controller
was off, fetch when the agent returns. There is no background retrieval or
implicit push.

## Threads and Git

For T3 tasks, obtain the app thread ID from `t3_thread_configuration` and pass
`--thread-id` at creation. Never use `CODEX_THREAD_ID`, which identifies the
provider session. Supply `--thread-url` if no thread base URL is configured.
Do not replace an existing machine's creating thread when reusing it.
Include the printed Coder link in the thread's reply. T3 has no generic
workspace toolbar API; do not misuse pull-request linking.

`authorize NAME --repo OWNER/REPO` issues a one-hour, one-repository, read-only
HTTPS credential. Add `--write` only when the agent requests push access.
Installation access can cover all repositories while worker tokens stay narrow.
Renew expired authorization with the controller online. Source snapshots have
no origin; add the intended HTTPS remote explicitly before pushing.

Remote authorship follows configured local Git identity at template publication.
Keep model attribution trailers where the user's policy requires them. Pushes
must be explicit agent actions, never completion or retrieval hooks.
