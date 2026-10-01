# Consumer configuration variables

Continuum's reusable workflows are **technology-oriented, not project-oriented**:
anything that names a specific product — the release version file, the app and
binary names, the signing identity, the Homebrew tap and the MacPorts tree — is
read from a GitHub Actions **repository variable** in the calling repository.

In a reusable (`workflow_call`) workflow the `vars` context resolves to the
**caller repository's** variables, so each consumer sets its own values and the
same Continuum engine serves every project. The engine carries **no fallback
product names and no product references** — a consumer that misses a required
variable fails fast in the first step of the job.

| Variable | Meaning | Example for a Swift app consumer |
| --- | --- | --- |
| `CONTINUUM_VERSION_FILE` | Source file the release reads the version from. | `Sources/MyCore/Version.swift` |
| `CONTINUUM_APP_NAME` | Application name (`.app` bundle and artifacts). | `MyApp` |
| `CONTINUUM_CLI_NAME` | Command-line binary name. | `myapp` |
| `CONTINUUM_AGENT_NAME` | Agent binary name. | `MyAppAgent` |
| `CONTINUUM_BUNDLE_AGENT` | Agent bundle id / launchd label. | `com.example.agent` |
| `CONTINUUM_BUNDLE_CTL` | CLI bundle id. | `com.example.ctl` |
| `CONTINUUM_SIGNING_IDENTITY` | `codesign` identity. | `MyApp CI Signing` |
| `CONTINUUM_HOMEBREW_TAP` | Homebrew tap repository. | `owner/homebrew-myapp` |
| `CONTINUUM_MACPORTS_TREE` | MacPorts canon tree repository. | `owner/macports-myapp` |
| `CONTINUUM_CORE_TEST_TARGET` | Swift test-runner product for CI coverage. | `MyAppCoreTests` |
| `CONTINUUM_CORE_IGNORE_SOURCES` | Extra consumer source dirs excluded from the coverage denominator, pipe-separated. | `Sources/AudioEngineGuard/` |

Set the values in the consumer repository (example):

```sh
gh variable set CONTINUUM_VERSION_FILE -R owner/myapp \
  -b 'Sources/MyCore/Version.swift'
```
