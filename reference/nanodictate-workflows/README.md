# NanoDictate workflow snapshot

Exact snapshot of `kodmial/nanodictate/.github/workflows` at commit
`64e89a3bcbab671513a933855e7495a07fac56bb`.

These files are reference material only. They intentionally live outside
`.github/workflows`, so GitHub Actions cannot execute them in Continuum.

The release and CodeRabbit implementations are preserved verbatim here for
later extraction into configurable modules.

## Continuum-authored material

`shadow-bridge.yml` and `shadow-observer.yml` are not part of that snapshot. They
are the two files Continuum asks a production repository to install, and they
live here under the same `reference/` rule so GitHub Actions cannot execute them
in Continuum. Install them as `.github/workflows/continuum-shadow-bridge.yml` and
`.github/workflows/continuum-shadow-observer.yml`.

Because they are not in the snapshot, they are not rows in
`docs/parity-ledger.json`: the ledger claims what the *consumer's* tree contains
at its audited head, and it claims that about the bridge alone because the bridge
alone has been installed there. The observer gains a row when it is installed,
not before.
