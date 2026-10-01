# Consumer configuration variables

Continuum's reusable workflows are **technology-oriented, not project-oriented**:
anything that names a specific product — the release version file, the app and
binary names, the signing identity, the Homebrew tap and the MacPorts tree — is
read from a GitHub Actions **repository variable** in the calling repository.

In a reusable (`workflow_call`) workflow the `vars` context resolves to the
**caller repository's** variables, so each consumer sets its own values and the
same Continuum engine serves every project. The engine carries no product
references in its logic — every product-specific name is read from a variable.
Variables that the release contract genuinely requires fail fast in the first
step of the job. Optional knobs instead carry a **default that reproduces the
previously hardcoded behaviour**, so a consumer that sets nothing keeps the
behaviour it had before the split.

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
| `CONTINUUM_RELEASE_MANIFEST_FILES` | JSON array of packaging manifest files trusted as release automation. | `["myapp.rb"]` |
| `OPENCODE_MODEL` | OpenCode agent model. Free anonymous models only; no API key required. | `opencode/muse-spark-1.3-contributor-free` |

Set the values in the consumer repository (example):

```sh
gh variable set CONTINUUM_VERSION_FILE -R owner/myapp \
  -b 'Sources/MyCore/Version.swift'
```

## Automation limits

Automation capacity and timing are consumer policy, not engine constants. Every
limit has a `vars.AUTOMATION_*` knob whose default reproduces the value the
engine hardcoded before the split, so a consumer that sets nothing keeps the
previous behaviour. The same knob is also exposed as a `workflow_call` input on
`issue-scheduler.yml` and `opencode.yml` for a caller that wants per-run control;
the input wins over the variable.

| Variable | Input | Default | Meaning |
| --- | --- | --- | --- |
| `AUTOMATION_WIP_LIMIT` | `issue-scheduler.wip_limit` | `2` | Concurrent in-progress issues the scheduler holds. |
| `AUTOMATION_LEASE_MINUTES` | `issue-scheduler.lease_minutes` | `45` | Reservation lease before it is considered expired. |
| `AUTOMATION_MAX_DISPATCH_ATTEMPTS` | `*.max_dispatch_attempts` | `2` | Dispatch attempts before an issue is paused. |
| `AUTOMATION_OPENCODE_RUNNER` | — | `macos-15` | Runner the OpenCode agent job executes on. |
| `AUTOMATION_OPENCODE_TIMEOUT_MINUTES` | — | `180` | OpenCode agent job timeout. |
| `AUTOMATION_SCHEDULER_TIMEOUT_MINUTES` | — | `5` | Issue scheduler job timeout. |
| `AUTOMATION_AUTOMERGE_TIMEOUT_MINUTES` | — | `10` | Auto-merge job timeout. |
| `AUTOMATION_CHILD_DISPATCH_TIMEOUT_MINUTES` | — | `8` | Child dispatcher job timeout. |
| `AUTOMATION_DISPATCH_TIMEOUT_MINUTES` | — | `5` | OpenCode internal dispatch job timeout. |
| `AUTOMATION_ATTEMPTS_TIMEOUT_MINUTES` | — | `10` | Failed-dispatch cleanup job timeout. |
| `AUTOMATION_DISPATCH_BACKOFF_SECONDS` | — | `120` | Backoff before the scheduler retries a dispatch. |
| `AUTOMATION_DISPATCH_MARKER` | `opencode.dispatch_marker` | `<!-- issue-scheduler-dispatch -->` | Dispatch marker `opencode.yml` matches and counts. |
| `AUTOMATION_IN_PROGRESS_LABEL` | `opencode.in_progress_label` | `automation:in-progress` | Reservation label. |
| `AUTOMATION_PAUSE_LABEL` | `opencode.pause_marker` | `automation:paused` | Pause label. |

Setting `AUTOMATION_OPENCODE_RUNNER` to a non-macOS label (for example
`ubuntu-latest`) skips the Swift toolchain setup and capability probe, which only
apply to macOS runners.

`AUTOMATION_DISPATCH_MARKER`, `AUTOMATION_IN_PROGRESS_LABEL`, and
`AUTOMATION_PAUSE_LABEL` matter on the `issue_comment` path, where
`opencode.yml` is invoked by an event and cannot receive `workflow_call` inputs.
A consumer that renames the scheduler's marker or labels must set the matching
`vars.*` knob, and pass the same value as the `issue-scheduler.yml` input, so the
release reservation `opencode.yml` reads on failure is the one the scheduler
wrote. `consumer-child-dispatcher.yml` reads `AUTOMATION_PAUSE_LABEL`, and
`consumer-child-review.yml` reads `AUTOMATION_IN_PROGRESS_LABEL` and
`AUTOMATION_PAUSE_LABEL`, so a renamed label stays consistent across the core
and the parent/child pair.

## Model and provider keys

The OpenCode agent runs on **free anonymous models** and needs **no API key**.
The default model is `opencode/muse-spark-1.3-contributor-free`; override it with
`vars.OPENCODE_MODEL`. No core workflow requires a paid provider token, and paid
providers are **not supported in the core**: no core workflow reads or forwards a
paid provider key.

`pr-agent.yml` is a legacy helper that upstream wires to the paid Groq provider.
It is **outside the supported core**: Groq is not an accepted exception. With no
supported key the workflow fails explicitly (it never reports a silent green
no-op), and the core never reads `GROQ_API_KEY`. A consumer that wants PR-Agent
must remove the caller or wire the action to a free provider itself.

## Scheduler naming inputs

A consumer that renames the scheduler's labels or dispatch marker must pass the
same values to both workflows, because the marker is what the scheduler counts
as a dispatch attempt and what `opencode.yml` matches on. `post_pause_comment`
is disabled only by the exact string `false`; an empty value keeps the comment
enabled:

| `issue-scheduler.yml` input | Default |
| --- | --- |
| `dispatch_marker` | `<!-- issue-scheduler-dispatch -->` |
| `in_progress_label` | `automation:in-progress` |
| `pause_marker` | `automation:paused` |
| `post_pause_comment` | `true` (only `false` disables) |
| `reset_markers` | `false` |
| `require_priority_label` | `false` |

`opencode.yml` accepts the matching `dispatch_marker`, `in_progress_label`, and
`pause_marker` inputs, plus `max_dispatch_attempts`, `issue_number` (the issue a
dispatcher names — empty falls back to `github.event.issue.number`),
`ci_workflow_id`, and `conflict_strategy` (a `merge`/`checkout` choice, `merge`
by default).

`CONTINUUM_RELEASE_MANIFEST_FILES` lists, as a JSON array, the packaging
manifest files a consumer trusts as release automation. It defaults to the
NanoDictate packaging layout; set it for other projects. Because this variable
expands the release-automation allowlist that bypasses review, a typo in its
value silently widens that trust — review changes to it with the same care as
the workflows themselves.
