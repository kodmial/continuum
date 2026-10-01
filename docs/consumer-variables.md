# Consumer configuration variables

Continuum's reusable workflows are **technology-oriented, not project-oriented**:
anything that names a specific product — the release version file, the app and
binary names, the signing identity, the Homebrew tap and the MacPorts tree — is
read from a GitHub Actions **repository variable** in the calling repository.

In a reusable (`workflow_call`) workflow the `vars` context resolves to the
**caller repository's** variables, so each consumer sets its own values and the
same Continuum engine serves every project. Every variable has a default that
matches the current NanoDictate values, so an unset variable preserves today's
behavior.

| Variable | Default | Meaning | State |
| --- | --- | --- | --- |
| `CONTINUUM_VERSION_FILE` | `Sources/NanoDictateCore/Version.swift` | Source file the release reads the version from. | wired |
| `CONTINUUM_APP_NAME` | `NanoDictate` | Application name (`.app` bundle and artifacts). | wired |
| `CONTINUUM_CLI_NAME` | `nanodictate` | Command-line binary name. | wired |
| `CONTINUUM_AGENT_NAME` | `NanoDictateAgent` | Agent binary name. | wired |
| `CONTINUUM_BUNDLE_AGENT` | `com.nanodictate.agent` | Agent bundle id / launchd label. | wired |
| `CONTINUUM_BUNDLE_CTL` | `com.nanodictate.ctl` | CLI bundle id. | wired |
| `CONTINUUM_SIGNING_IDENTITY` | `NanoDictate CI Signing` | `codesign` identity. | wired |
| `CONTINUUM_HOMEBREW_TAP` | `kodmial/homebrew-nanodictate` | Homebrew tap repository. | planned |
| `CONTINUUM_MACPORTS_TREE` | `kodmial/macports-nanodictate` | MacPorts canon tree repository. | planned |

`wired` is currently scoped to `packaging-smoke.yml` and
`scripts/packaging-smoke/common.sh`. The same variables are being threaded
through `release.yml`, `release-pr.yml`, `ci.yml`, and
`release-automation-merge.yml`; `planned` marks names reserved for those files.

Set a value in a consumer repository:

```sh
gh variable set CONTINUUM_VERSION_FILE -R kodmial/nanodictate \
  -b 'Sources/NanoDictateCore/Version.swift'
```

A consumer that omits a variable keeps the default, so an existing NanoDictate
install needs no changes to keep working.
