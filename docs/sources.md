# Sources and results

A version-1 JSON manifest selects Git repositories, untracked inputs, extra files
and output paths. Local paths can be absolute or relative to the manifest.
Destinations are relative to a new remote snapshot directory. Project names
are unique; destinations must not overlap. See [the example](../examples/workspace.json).

```sh
coder-aws plan --manifest workspace.json
coder-aws sync build --manifest workspace.json
coder-aws run build --project app --timeout 3600 -- make build
coder-aws fetch build
```

`plan` needs neither AWS nor Coder access. `create --manifest ...` also prepares
sources after provisioning. Each inventory records resolved commits, staged and
unstaged tracked changes relative to HEAD, selected untracked files, hashes,
permissions, and artifact paths. Selecting another revision applies the local
patch to that revision and fails if incompatible.

Git bundles transfer history without local Git configuration, remotes or
credentials. Packing uses one thread and bounded window memory. Remote sources
live at `/home/ubuntu/projects/snapshots/<id>`. Existing snapshots are never
overwritten, so repeated sync cannot replace remote edits. Snapshot IDs are
recorded locally and on the instance for recovery if the local registry is lost.

Wait for the registered job before fetching. Results go under
`~/.local/state/coder-workspaces/results/<workspace>/<snapshot>-<suffix>`.
`checkouts/<project>` contains reconstructed commits and dirty files;
`<project>/artifacts` contains declared outputs; `extra` contains extra files.
Hashes are checked before writing `retrieved.json`. Missing build outputs are
recorded rather than preventing recovery of source changes from a failed job.

Fetch leaves original local worktrees untouched. Inspect the result, then merge
or cherry-pick deliberately. If the PC was off, resume the worker and fetch
when the agent returns. There is no background retrieval service or automatic
push. Keep important results locally before AWS credits or account access end.

The first version rejects submodules and selected symlinks. Declare submodules
as separate projects. Git LFS needs manual materialization and checking; root
LFS rules are rejected. Copy Nix store outputs into a regular artifact directory
instead of selecting a `result` symlink. Artifact paths do not support globs.
Do not transfer homes, credential stores or unneeded ignored files.
