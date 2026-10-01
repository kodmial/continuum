# Consumer configuration variables

Continuum's reusable workflows are **technology-oriented, not project-oriented**:
anything that names a specific product — the release version file, the app and
binary names, the signing identity — is read from a GitHub Actions **repository
variable** in the calling repository.

In a reusable (`workflow_call`) workflow the `vars` context resolves to the
**caller repository's** variables, so each consumer sets its own values and the
same Continuum engine serves every project. The engine carries no product
references in its logic — every product-specific name is read from a variable.
Variables that the release contract genuinely requires fail fast in the first
step of the job. Optional knobs instead carry a **default that reproduces the
previously hardcoded behaviour**, so a consumer that sets nothing keeps the
behaviour it had before the split.

The Swift release workflow's Homebrew tap and MacPorts canon tree are the
exception, and are **not** variables today: they are literals in
`.github/workflows/continuum-tech-swift-release.yml`, alongside the NanoDictate
file layout that workflow still assumes. See
[Not yet parameterised](#not-yet-parameterised) below.

| Variable | Meaning | Example for a Swift app consumer |
| --- | --- | --- |
| `CONTINUUM_VERSION_FILE` | Source file the release reads the version from. | `Sources/MyCore/Version.swift` |
| `CONTINUUM_APP_NAME` | Application name (`.app` bundle and artifacts). | `MyApp` |
| `CONTINUUM_CLI_NAME` | Command-line binary name. | `myapp` |
| `CONTINUUM_AGENT_NAME` | Agent binary name. | `MyAppAgent` |
| `CONTINUUM_BUNDLE_AGENT` | Agent bundle id / launchd label. | `com.example.agent` |
| `CONTINUUM_BUNDLE_CTL` | CLI bundle id. | `com.example.ctl` |
| `CONTINUUM_SIGNING_IDENTITY` | `codesign` identity. | `MyApp CI Signing` |
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
`continuum-issue-scheduler.yml` and `continuum-opencode.yml` for a caller that
wants per-run control; the input wins over the variable.

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
| `AUTOMATION_DISPATCH_MARKER` | `opencode.dispatch_marker` | `<!-- issue-scheduler-dispatch -->` | Dispatch marker `continuum-opencode.yml` matches and counts. |
| `AUTOMATION_IN_PROGRESS_LABEL` | `opencode.in_progress_label` | `automation:in-progress` | Reservation label. |
| `AUTOMATION_PAUSE_LABEL` | `opencode.pause_marker` | `automation:paused` | Pause label. |
| `AUTOMATION_REQUIRE_PRIORITY_LABEL` | `issue-scheduler.require_priority_label` | `false` | Dispatch only issues carrying a priority label. |
| `AUTOMATION_COMMAND_GRACE_MINUTES` | `issue-scheduler.command_grace_minutes` | `5` | Race window after a manual owner `/oc` during which the scheduler does not enqueue a second OpenCode run. |
| `CONTINUUM_CHILD_OWNED_MARKER` | `issue-scheduler.child_owned_marker` | `<!-- continuum-child-owned -->` | Body marker reserving an issue for a delegated child worker; the local scheduler never dispatches it. |
| `CONTINUUM_LEGACY_CHILD_OWNED_MARKER` | `issue-scheduler.legacy_child_owned_marker` | `<!-- runtime-worker-owned -->` | Legacy spelling of the child-owned marker, still accepted while a consumer migrates. |
| `CONTINUUM_OPENCODE_WORKFLOW_NAME` | `issue-scheduler.opencode_workflow_name` | `OpenCode agent` | `name:` of the local OpenCode caller whose in-flight runs count as active work. |
| `CONTINUUM_OPENCODE_WORKFLOW_PATH` | `issue-scheduler.opencode_workflow_path` | `.github/workflows/continuum-opencode.yml` | Path of the local OpenCode caller, for runs that predate `run-name:`. The default names the prefixed file the installer writes. |
| `AUTOMATION_WATCHDOG_MAX_RETRIES` | `continuum-opencode-watchdog.max_recovery_retries` | `1` | Automatic recovery retries before the issue is paused. |
| `AUTOMATION_WATCHDOG_RETRY_MARKER` | `continuum-opencode-watchdog.retry_marker` | `<!-- opencode-watchdog-retry -->` | Hidden marker the watchdog counts. |
| `AUTOMATION_WATCHDOG_TIMEOUT_MINUTES` | `continuum-opencode-watchdog.timeout_minutes` | `5` | Watchdog job timeout. |
| `CONTINUUM_REQUIRE_CODERABBIT` | `auto-merge.require_coderabbit` | `false` | Require a CodeRabbit approval before merging. |

### Optional integrations are off by default

`CONTINUUM_REQUIRE_CODERABBIT` and the `continuum-opencode-watchdog` caller are
optional integrations, so neither has a default that reproduces the behaviour a
consumer had before it adopted Continuum. Set the value in the repository
variable of the consumer that asked for the integration; every other consumer
keeps the disabled behaviour with no extra configuration.

- **`CONTINUUM_REQUIRE_CODERABBIT`** defaults to `false`. Only a consumer that
  actually uses CodeRabbit sets it to `true`. With it off,
  `continuum-auto-merge.yml` neither waits for a CodeRabbit approval nor
  dispatches
  `continuum-coderabbit-retry.yml`, so a repository without that caller cannot
  get a 404 dispatch or a merge that never lands. With it on, the previous
  behaviour is unchanged.
- **The watchdog** is opt-in in the sense that it only fires on
  `workflow_run: completed`. Install it when the consumer wants a failed
  `/oc` run recovered — Continuum's own `recover-scheduled-issue` job in
  `continuum-opencode.yml` only fires for scheduler dispatch comments, so a
  manual run has no recovery path without it. Its `watched_workflow` input must
  equal the `name:` of the consumer's OpenCode caller, because GitHub's
  `workflow_run.workflows` filter matches on the workflow `name:` and never on
  the file name.

The watchdog reads the same three markers and labels as `continuum-opencode.yml`
(`AUTOMATION_DISPATCH_MARKER`, `AUTOMATION_IN_PROGRESS_LABEL`,
`AUTOMATION_PAUSE_LABEL`), so a renamed label stays consistent across the
scheduler, the agent, and the recovery.

Setting `AUTOMATION_OPENCODE_RUNNER` to a non-macOS label (for example
`ubuntu-latest`) skips the Swift toolchain setup and capability probe, which only
apply to macOS runners.

`AUTOMATION_DISPATCH_MARKER`, `AUTOMATION_IN_PROGRESS_LABEL`, and
`AUTOMATION_PAUSE_LABEL` matter on the `issue_comment` path, where
`continuum-opencode.yml` is invoked by an event and cannot receive
`workflow_call` inputs.
A consumer that renames the scheduler's marker or labels must set the matching
`vars.*` knob, and pass the same value as the `continuum-issue-scheduler.yml`
input, so the release reservation `continuum-opencode.yml` reads on failure is
the one the scheduler wrote. `continuum-consumer-child-dispatcher.yml` reads
`AUTOMATION_PAUSE_LABEL`, and
`continuum-consumer-child-review.yml` reads `AUTOMATION_IN_PROGRESS_LABEL` and
`AUTOMATION_PAUSE_LABEL`, so a renamed label stays consistent across the core
and the parent/child pair.

### Required secrets

A secret belongs to the installer **set** that installs the workflow reading it.
The `core` set installs the controllers, the `parent` set the child wrappers,
and the opt-in `tech` set the release and CI workflows, so a consumer that
installs only `core` never needs the `parent` or `tech` secrets.

| Secret | Set | Read by | Fallback |
| --- | --- | --- | --- |
| `TAP_PAT` | `core` | The core controllers that call the GitHub API, and the installed `parent` stubs (which forward it to the child runtime as `CHILD_RUNTIME_TOKEN`). | `github.token` on steps that only need the built-in token. |
| `RENDER_API_KEY` | `core` | `continuum-render-executor.yml` only | **None.** The controller fails explicitly when it is unset. |
| `CHILD_RUNTIME_TOKEN` | `parent` | `continuum-consumer-child-dispatcher`, `-review`, `-worker` and `-pr-review` | **None.** Every installed parent stub fills it from the parent's own `TAP_PAT`, so the same credential serves both layers. |
| `CHILD_RUNTIME_REPOSITORIES` | `parent` | The same four child wrappers. | None — the input is optional (the pre-variables compatibility path described in `docs/parent-child-delegation.md`). |
| `NANODICTATE_SIGNING_P12` | `tech` | The tech release and packaging-smoke workflows. | None. |
| `NANODICTATE_SIGNING_PASSWORD` | `tech` | The tech release and packaging-smoke workflows. | None. |
| `RELEASE_PR_TOKEN` | `tech` | `continuum-tech-swift-release-pr.yml` | Resolved in this order: `RELEASE_PR_TOKEN`, then `TAP_PAT`, then the built-in `GITHUB_TOKEN`. |

Two qualifications the rows alone cannot carry:

- The `core` set's consumer-defined secrets are exactly the two in its rows, and
  they are **different credentials**: `TAP_PAT` and `RENDER_API_KEY` are never
  interchangeable. A GitHub token is not accepted by the Render API, and the
  Render key is useless against the GitHub API, so a repository that installs
  the render controller must define both. The four `continuum-consumer-child-*`
  workflows live in `.github/workflows/` next to the core ones but are installed
  only by the `parent` set, and read only the two child-runtime secrets listed
  above.
- GitHub mints `secrets.GITHUB_TOKEN` automatically, so it is **not** a
  credential a consumer defines and no row above lists it. Two workflows read
  it: `continuum-pr-agent.yml`, which still refuses to run because no paid
  provider key is supported (see [Model and provider keys](#model-and-provider-keys)),
  and `continuum-tech-swift-release-pr.yml`, as the last link of the
  `RELEASE_PR_TOKEN` chain above.

### Not yet parameterised

Two names in the Swift release contract are still literals in
`.github/workflows/continuum-tech-swift-release.yml` rather than repository
variables, so they are deliberately **absent** from the table above:

- the Homebrew tap repository, pushed by the `Update tap repo` step;
- the MacPorts canon tree repository, cloned by the `Sync MacPorts port tree`
  step and probed by the preflight installer-pin check.

A consumer cannot redirect either one today: the workflow also assumes the
NanoDictate file layout (`packaging/homebrew/nanodictate.rb`,
`packaging/macports/Portfile`, `audio/nanodictate/Portfile` inside the tree), so
parameterising the two repository names alone would not make the step portable.
Continuum therefore does not document variables that nothing reads. Until the
whole block is parameterised, a consumer who needs a different tap or tree must
fork the tech release workflow.

### Render execution controller

`continuum-render-executor.yml` drives one execution of a consumer's Render
lifecycle. Everything that fork of the engine used to hardcode is an input with
a `vars.` fallback, so a consumer that installs the workflow and sets nothing
keeps the behaviour it had.

`OPENCODE_MODEL` is the one exception: it has **no literal default anywhere in
the chain**. A Render worker started without a model executes nothing, so the
workflow fails the run explicitly instead of reporting a green no-op. Set the
variable (or pass the `model` input) if the consumer uses this controller.

| Variable | Default | Meaning |
| --- | --- | --- |
| `RENDER_REGION` | `oregon` | Region the ephemeral Render worker is created in. |
| `RENDER_STATE_FILE` | `/tmp/continuum-render-state.json` | Lifecycle state file the cleanup step reads. |
| `RENDER_RESULT_FILE` | `/tmp/continuum-render-result.json` | Render run result file. |
| `RENDER_MEMORY_SUMMARY_FILE` | `/tmp/continuum-render-memory-summary.json` | Memory summary file the classification step reads. |
| `RENDER_QUALIFICATION_RESULT_FILE` | `/tmp/continuum-render-qualification.json` | Qualification record written from the run evidence. |
| `MAX_RENDER_REPAIR_ATTEMPTS` | `10` | Automatic recovery issues per source issue before the controller stops creating repairs. |
| `RENDER_QUALIFICATION_LABEL` | `qualification:render` | Issue label marking a durable qualification run. An issue without it is classified `not-chain`. |
| `RENDER_QUALIFICATION_MARKER` | `<!-- continuum-render-qualification-result -->` | Hidden marker prefixed to the recorded result comment. |
| `RENDER_ARTIFACT_PREFIX` | `render-qualification` | Prefix of the uploaded evidence artifact name. |
| `RENDER_DISPATCH_REF` | `main` | Revision the controller checks out and dispatches subsequent workflows at. |
| `RENDER_JOB_SCRIPT` | `automation/render-job.sh` | Consumer script driving the lifecycle. |
| `RENDER_CLEANUP_SCRIPT` | `automation/render-cleanup.sh` | Consumer script deleting the ephemeral service. |
| `RENDER_QUALIFICATION_SCRIPT` | `automation/record_render_qualification.py` | Consumer script turning measured evidence into the qualification payload. |
| `AUTOMATION_REPAIR_LABEL` | `priority:p0` | Label applied to an automatic recovery issue. |
| `RENDER_E2E_BRANCH_PREFIX` | `opencode/issue` | Branch-head prefix the `e2e` mode requires of a result PR. |
| `RENDER_CHAIN_WORKFLOW` | *(empty)* | Optional follow-up workflow woken when a qualification needs one. Empty means the wake is skipped, not that a file is dispatched. |
| `RENDER_SCHEDULER_WORKFLOW` | `continuum-issue-scheduler.yml` | Scheduler woken after the controller resolves an issue. Keep this in sync with the installed scheduler file name, which carries the `continuum-` prefix. |
| `RENDER_CONCURRENCY_GROUP` | `continuum-render-single-service` | Serialises the controller's runs so one run's repair cannot tear down the next run's worker. |
| `AUTOMATION_RENDER_TIMEOUT_MINUTES` | `55` | Controller job timeout. |

The controller reads **two distinct repository secrets**, and they are never
interchangeable:

- **`RENDER_API_KEY`** — the Render API key, used for every Render API call:
  the execute step that creates the ephemeral worker and the mandatory cleanup
  step that deletes it. A GitHub token is **not** accepted by the Render API, so
  this key must be set as a repository secret before the capability can run.
- **`TAP_PAT`** — the classic GitHub PAT, used on the genuine GitHub-token path:
  the issue/label reads and writes in the classification step, with
  `github.token` as the fallback. The steps that only need the built-in token
  (`GH_TOKEN: ${{ github.token }}`) read no secret at all.

The controller **fails explicitly** when `RENDER_API_KEY` is unset — both in the
execute step and in the cleanup step, so a missing key can never leave an
ephemeral worker running. It does **not** fall back to `TAP_PAT`, to
`github.token`, or to any other credential; the failure names the exact secret to
set rather than reporting a green no-op.

### Docker qualification controller

`continuum-docker-qualification.yml` runs the artifact under test inside a
fixed no-swap memory ceiling and classifies the result as `pass`, `memory`,
`correctness` or `infrastructure`. The artifact store it pulls from, the binary
name inside the archive, the memory ceiling and the payload schema were all
literals in the fork; each is an input with a `vars.` fallback here.

`OPENCODE_MODEL` is the one value with **no literal default anywhere in the
chain**. A trial that runs no model leaves the task unchanged and would be
recorded as a correctness failure, blaming a configuration gap on the binary.
Set the variable (or pass the `model` input) if the consumer uses this
controller.

The controller reads the repository secret **`TAP_PAT`**, falling back to
`github.token`, for the artifact download and the issue/label writes. It never
reads any other secret name.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARTIFACT_REPOSITORY` | the consumer's own repository | Repository whose Actions artifact store holds the artifact under test. |
| `DOCKER_QUALIFICATION_BINARY_NAME` | `opencode-coding-linux-x64` | File name of the binary inside the archive, and of its `.sha256` sidecar. |
| `DOCKER_QUALIFICATION_IMAGE` | `ubuntu:22.04` | Container image the binary runs in. |
| `DOCKER_QUALIFICATION_MEMORY_MIB` | `512` | Memory ceiling in MiB, applied as **both** `--memory` and `--memory-swap` so a trial cannot swap. One value drives the flags and the classifier, so they cannot disagree. |
| `DOCKER_QUALIFICATION_MIN_HEADROOM_MIB` | `32` | Headroom the peak must leave below the ceiling. |
| `DOCKER_QUALIFICATION_TRIALS` | `2` | Independent coding trials. A `pass` requires every trial to pass. |
| `DOCKER_QUALIFICATION_RESULT_SCHEMA` | `continuum-qualification-result/v1` | Schema name written into the payload. |
| `DOCKER_QUALIFICATION_RESULT_KIND` | `docker` | Kind written into the payload. |
| `DOCKER_QUALIFICATION_RESULT_MARKER` | `<!-- continuum-docker-qualification-result -->` | Hidden marker prefixed to the recorded result comment. |
| `DOCKER_QUALIFICATION_RESULT_FILE` | `/tmp/continuum-docker-qualification-result.json` | Result file. |
| `DOCKER_QUALIFICATION_EVIDENCE_DIR` | `/tmp/continuum-docker-qualification-evidence` | Directory the collected evidence is written to. |
| `DOCKER_QUALIFICATION_ARTIFACT_PREFIX` | `docker-qualification` | Prefix of the uploaded evidence artifact name. |
| `DOCKER_QUALIFICATION_DISPATCH_REF` | `main` | Revision the controller checks out and dispatches the chain at. |
| `DOCKER_QUALIFICATION_CHAIN_WORKFLOW` | *(empty)* | Optional follow-up workflow. Empty means the wake is skipped, not that a file is dispatched. |
| `DOCKER_QUALIFICATION_CONCURRENCY_GROUP` | `continuum-docker-qualification` | Serialises runs, which share one evidence directory and one result file. |
| `AUTOMATION_DOCKER_QUALIFICATION_TIMEOUT_MINUTES` | `45` | Controller job timeout. |

Every result pauses the issue: `DOCKER_QUALIFICATION_RESULT_MARKER` decides
whether the recorded comment is machine-readable, and the label pair is the same
`AUTOMATION_IN_PROGRESS_LABEL` / `AUTOMATION_PAUSE_LABEL` pair the scheduler and
the render controller use.

### Scheduler guards

The scheduler reconciles more than the dispatch marker. Every guard below is
part of the core engine, so a consumer gets it without a fork:

- an **active OpenCode run** (`CONTINUUM_OPENCODE_WORKFLOW_NAME` /
  `CONTINUUM_OPENCODE_WORKFLOW_PATH`) counts as in-flight work, so a lease
  never expires while GitHub is still running or queueing the implementation;
- a **manual owner `/oc`** reserves the issue immediately, and
  `AUTOMATION_COMMAND_GRACE_MINUTES` suppresses a duplicate scheduler dispatch
  for that short window;
- a **closed-without-merge OpenCode PR** pauses the issue only when it has no
  open native blocker — blocked work is released, not paused;
- a **native open dependency** releases an existing reservation rather than
  letting an old lease hold WIP capacity;
- a **child-owned marker** in the issue body excludes it from local dispatch;
- **declared `<!-- automation-blocked-by: -->` markers** are honoured in both
  candidate selection and the just-in-time re-check before dispatch.

All six are consumer-visible knobs: the owner-command grace window, the two
child-owned markers, and the two OpenCode-caller identity inputs a consumer
needs when its OpenCode caller is renamed. The rest are unconditional engine
behaviour.

## Model and provider keys

The OpenCode agent runs on **free anonymous models** and needs **no API key**.
The default model is `opencode/muse-spark-1.3-contributor-free`; override it with
`vars.OPENCODE_MODEL`. No core workflow requires a paid provider token, and paid
providers are **not supported in the core**: no core workflow reads or forwards a
paid provider key.

`continuum-pr-agent.yml` is a legacy helper that upstream wires to the paid Groq
provider.
It is **outside the supported core**: Groq is not an accepted exception. With no
supported key the workflow fails explicitly (it never reports a silent green
no-op), and the core never reads `GROQ_API_KEY`. A consumer that wants PR-Agent
must remove the caller or wire the action to a free provider itself.

## Scheduler naming inputs

A consumer that renames the scheduler's labels or dispatch marker must pass the
same values to both workflows, because the marker is what the scheduler counts
as a dispatch attempt and what `continuum-opencode.yml` matches on.
`post_pause_comment`
is disabled only by the exact string `false`; an empty value keeps the comment
enabled:

| `continuum-issue-scheduler.yml` input | Default |
| --- | --- |
| `dispatch_marker` | `<!-- issue-scheduler-dispatch -->` |
| `in_progress_label` | `automation:in-progress` |
| `pause_marker` | `automation:paused` |
| `post_pause_comment` | `true` (only `false` disables) |
| `reset_markers` | `false` |
| `require_priority_label` | `false` |
| `command_grace_minutes` | `5` |
| `child_owned_marker` | `<!-- continuum-child-owned -->` |
| `legacy_child_owned_marker` | `<!-- runtime-worker-owned -->` |
| `opencode_workflow_name` | `OpenCode agent` |
| `opencode_workflow_path` | `.github/workflows/continuum-opencode.yml` |

### Downstream wake-ups and label-driven execution routes

Every workflow Continuum wakes after a transition belongs to the consumer, not
to Continuum: `knowledge-sync` has no core equivalent at all, and a repository
does not necessarily install the child dispatcher. So each list below is
**empty by default** and the enabling value lives in the consumer's repository
variable. Nothing is dispatched until it is set.

| Variable | Read by | Effect when empty |
| --- | --- | --- |
| `CONTINUUM_POST_MERGE_WAKEUPS` | `continuum-auto-merge.yml` | No workflow is woken after a merge |
| `CONTINUUM_POST_MERGE_WAKEUP_REF` | `continuum-auto-merge.yml` | Falls back to the repository's default branch |
| `CONTINUUM_EXECUTION_LABEL_ROUTES` | `continuum-issue-scheduler.yml` | No label-driven execution dispatch |
| `CONTINUUM_CHILD_DISPATCH_WORKFLOW` | `continuum-issue-scheduler.yml` | No downstream dispatcher is woken after a reconcile pass |
| `CONTINUUM_DISPATCH_REF` | `continuum-issue-scheduler.yml` | Falls back to `main` |
| `CONTINUUM_OPENCODE_DISPATCH` | `continuum-issue-scheduler.yml` | Falls back to `comment` |
| `CONTINUUM_ISSUE_BASE_REF` | `continuum-opencode.yml` | Falls back to `main` |
| `CONTINUUM_ISSUE_COMMIT_PREFIX` | `continuum-opencode.yml` | Falls back to `fix` |

- **`CONTINUUM_POST_MERGE_WAKEUPS`** is a comma-separated list of workflow file
  names, dispatched after a successful merge. Each wake-up is best-effort: one
  that cannot be delivered logs a warning and does not fail a run whose merge
  already landed.
- **`CONTINUUM_EXECUTION_LABEL_ROUTES`** is the label-driven dispatch table, one
  entry per line:

  ```text
  <issue label>|<workflow file name>|<input>=<value>,...
  execution:docker-qualify|continuum-docker-qualification.yml|
  execution:render-smoke|continuum-render-executor.yml|mode=smoke
  ```

  A line may be omitted or may stop after the workflow name. Lines starting
  with `#` and blank lines are ignored. The scheduler creates each label in the
  repository so it can be applied from the issue page, and always supplies
  `issue_number` to the route so no route has to spell it out. A selected issue
  carrying a route label is dispatched to that workflow **instead of**
  OpenCode, and never to both. A malformed line fails the run loudly rather than
  being dropped, because a silently ignored route looks reserved but is never
  executed.
- **`CONTINUUM_CHILD_DISPATCH_WORKFLOW`** names one workflow file woken after
  every reconcile pass. A parent that installs
  `.github/caller-stubs/parent/continuum-child-dispatcher.yml` sets it to that
  file's name.
- **`CONTINUUM_OPENCODE_DISPATCH`** chooses how a ready issue reaches OpenCode:
  `comment` (the default) posts the owner's `/oc` command, which needs no extra
  permission, and `workflow` dispatches the OpenCode caller's `issue` mode
  directly. Use `workflow` only when the OpenCode caller is installed without an
  `issue_comment` trigger. Any other value fails the run.

### OpenCode `issue` mode

`mode: issue` drives a bare issue number to a pull request: it branches from the
base ref, runs `opencode run --auto`, refuses to publish anything under
`.github/workflows/**`, and publishes the commit refs-first so an ordinary merge
conflict is never mistaken for a failed task. It skips a launch when the issue
already has an open PR whose head branch starts with `opencode/issue<N>-`, and a
burnt run releases the scheduler's reservation through the same
`recover-scheduled-issue` job an `issue_comment` run uses.

Two optional halves are **off by default** because Continuum ships neither the
protocol document nor the validator — both belong to the consumer:

- **`knowledge_protocol_path`** names the repository path of the knowledge
  handoff protocol the agent must read before changing code. Empty imposes no
  such requirement.
- **`knowledge_records_dir`** names the directory the agent must write its
  single run record into, as `issue-<number>-run-<run id>.md`. Empty disables
  the requirement; when set, a missing record fails the run closed.

`base_ref` (default `main`), `issue_commit_prefix` (default `fix`), and
`ci_workflow_id` (default empty, meaning CI is left to the consumer's own
machinery) complete the mode's inputs.

### Cancelling and duplicate suppression

- **`/oc-cancel`** in a comment suppresses the launch: neither the interactive
  agent nor branch recovery runs for it. It is a launch guard only. Continuum
  has no mechanism that cancels an already-running workflow run, so a comment
  arriving after the agent has started does not stop it.
- **Duplicate suppression** is automatic and needs no configuration: an
  `issue_comment` run and an `issue` mode dispatch both check for an open PR
  whose head branch starts with `opencode/issue<N>-` before the agent launches.

The OpenCode caller stub sets a `run-name`, so the watchdog and the scheduler
can find a run by issue number instead of by issue title, which is not unique
among open issues.

`continuum-opencode.yml` accepts the matching `dispatch_marker`,
`in_progress_label`, and
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
