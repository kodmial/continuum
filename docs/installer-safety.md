# Installer safety

`install.sh` installs one caller set (`core`, `tech`, or `parent`) and keeps the
existing argument shape `[path] [ref] [set]`. Two optional flags may appear in
any position:

- `--dry-run` prints the exact proposed `add`, `update`, `keep`,
  `ownership-conflict`, and `prune` entries and changes nothing. A preview of an
  explicit ref fetches templates from that ref, so it needs network access but
  never credentials.
- `--yes` (or `CONTINUUM_INSTALL_ASSUME_YES=1`) preserves the existing
  superseded-caller prune confirmation semantics.

## Preflight and apply

Every requested template is fetched, ref-rewritten, and checked for a non-empty
`jobs` block and a Continuum `uses:` call before the target is touched. Templates
and backups live in a per-run staging directory outside the live target files.

Writes land in a temporary file next to the target and are then renamed into
place. Before the first write, every file that will be overwritten or pruned is
backed up. If a controlled download, write, backup, or prune step fails, the
installer restores the pre-install bytes, including pruned files, removes files
it added, and exits non-zero. Re-running after an interrupted install converges
safely.

Atomic replacement of the whole multi-file caller directory across a process
kill or host crash is not promised. The guarantee is per-file replacement plus
rollback for errors the process can observe.

## Ownership

An installed caller is Continuum-owned when its name carries the `continuum-`
prefix and its body has a `uses:` line for `kodmial/continuum/.github/workflows/`.
The pinned revision does not affect ownership, so callers installed at an older
ref remain upgradable.

A target path that is a directory, a symlink, or a consumer-owned file without
that call is an ownership conflict. The installer lists the conflict, refuses to
write or prune anything, and asks the operator to move or rename the file. It
never overwrites an unrelated consumer file because of the name alone.

A `continuum-*.yml` file without the Continuum call is not a prune candidate and
is preserved, even with `--yes`.
