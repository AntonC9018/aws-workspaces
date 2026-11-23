# Manifests, binary transfer, and GitHub push

## Manifest JSON and sync/fetch

```json
{"version":1,"projects":[{"name":"app","path":"../repo","revision":"HEAD","destination":"src/app","include_untracked":["a.txt"],"artifacts":["dist"]}],"files":[{"source":"c.txt","destination":"shared/c.txt"}]}
```

- Validate with `plan --manifest FILE`. `--project KEY` in `run` matches `projects[].name`.
- `sync NAME --manifest FILE` needs an idle worker and never overwrites snapshots.
- Add submodules as separate `projects[]` entries; rejects LFS, symlinks, artifact globs, and
  `.aws`/`.ssh`/`.gnupg`/`.git`.
- Run `fetch NAME` after job exit; writes verified output to
  `~/.local/state/coder-workspaces/results/NAME/` without editing local worktrees.

## Binary transfer over SSH

`coder ssh` uses a PTY that corrupts raw bytes (`\n` -> `\r\n`). Use `fetch` artifacts, `scp`, or
`coder ssh` (not `coder-oci ssh`):

```sh
coder ssh NAME -- 'base64 /path/to/file' | tr -d '\r' | base64 -d > file
```

## GitHub authorization and push

- Keep GitHub keys local. Grant 1h single-repo HTTPS access via
  `coder-aws authorize NAME --repo OWNER/REPO` (or `coder-oci`; add `--write` only when asked to
  push).
- Snapshots lack `origin`; add the HTTPS remote via `coder ssh` and push only on explicit request.
  Keep `Generated-by:` trailers per policy.
