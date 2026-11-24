# Private project images and Unity licensing

## Custom machine images

When a task needs setup beyond syncing the repo and running its install script, use an existing
private image (`images` / `create --image`) or suggest adding `.coder-images/<name>.json` and
baking it with `bake-image --spec`.

```sh
coder-oci images --all
coder-oci bake-image --spec .coder-images/unity-6000.json
coder-oci create NAME --image PROJECT-NAME --instance-type SIZE --disk-gb 80
coder-oci delete-image PROJECT-NAME
```

Same on `coder-aws` (`amd64` only). Specs take `version: 1`, optional `name` (auto-scoped
`<project>-<name>`, multiple images per project), `arch` (`amd64`/`arm64`), `disk_gb`, `packages`,
`setup_script`, and `commands`. Never bake source checkouts or credentials. `create --image`
requires matching architecture and `--disk-gb` >= image `disk_gb` (auto-defaults to image size if
omitted). `bake-image` skips matching hashes and deletes superseded images automatically.

## Unity Personal activation

On workers with `/usr/local/bin/unity` (Unity CLI v1.0.1):

- `coder-oci authorize-unity NAME` activates Personal using cached local auth
  (`~/.local/state/coder-workspaces/unity/auth-bundle.tar`); pass `--login` on first use to print
  the browser sign-in URL and cache auth locally.
- `coder-oci authorize-unity NAME --return` returns returnable seats and clears worker Unity state
  before stopping/deleting. Free Personal seats cannot be returned remotely, so `--return` confirms
  only Personal entitlements remain before wiping worker auth files; other return failures raise and
  keep worker files for retry.
- Offline `.alf`/`.ulf` (`--create-alf`, `--license FILE`) is Enterprise/Industry only.

Image requirements and pitfalls:

- Include `unzip` in `packages`: `unity license activate` downloads the licensing client as a zip
  and otherwise fails with "Can't reach the Unity licensing client".
- Unity CLI 1.x stores Hub accounts in `~/.config/unityhub/accounts.db` (SQLite WAL) and prefers
  the RAM-only Linux kernel keyring (`keyutils`) unless `@keyring` syscalls return `EPERM`.
  `authorize-unity` runs Unity CLI via `sudo systemd-run` (requires systemd and passwordless sudo on
  workers) with `SystemCallErrorNumber=EPERM`, `SystemCallFilter=~@keyring`, `UMask=0077`,
  `UNITY_INSECURE_STORAGE=1`, and `UNITY_NO_AUTH_BROKER=1` so Unity uses
  `~/.local/share/unity-keyring/credentials.json`. The controller snapshots `accounts.db` via
  `sqlite3.backup()` into `auth-bundle.tar` and refreshes the bundle after replay, allowing
  cross-worker replay until Unity's OAuth refresh token expires. Manual `unity` CLI auth commands in
  user jobs need `UNITY_INSECURE_STORAGE=1 UNITY_NO_AUTH_BROKER=1` to read the replayed session.
- `coder-oci run` starts in `~/projects`; pass `--project NAME` to start in the synced checkout.
