# NanoDictate workflow snapshot

Exact snapshot of `kodmial/nanodictate/.github/workflows` at commit
`64e89a3bcbab671513a933855e7495a07fac56bb`.

These files are reference material only. They intentionally live outside
`.github/workflows`, so GitHub Actions cannot execute them in Continuum.

`shadow-bridge.yml` is the one file a consumer repository installs to run
Continuum's shadow validation plane against its own lifecycle events. It is
reference material for the same reason as the rest: installing it means copying
it into that repository's `.github/workflows/`, where it is executed there and
only there. The operational procedure is
[docs/shadow-validation.md](../../docs/shadow-validation.md).

The release and CodeRabbit implementations are preserved verbatim here for
later extraction into configurable modules.
