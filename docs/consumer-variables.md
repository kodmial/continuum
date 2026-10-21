# Consumer contract

Continuum is configured by the **calling repository**. Generic engines do not
carry defaults copied from any existing consumer.

## Validation

`continuum-validation.yml` is the common validation engine. Every setting may
be supplied as a reusable-workflow input; an empty input falls through to the
repository variable shown below.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `CONTINUUM_RUNNER` | `ubuntu-latest` | Runner for generic validation. |
| `CONTINUUM_VALIDATION_TIMEOUT_MINUTES` | `25` | Validation timeout. |
| `CONTINUUM_PREPARE_COMMAND` | empty | Prepare toolchains/dependencies. |
| `CONTINUUM_BUILD_COMMAND` | empty | Consumer build hook. |
| `CONTINUUM_TEST_COMMAND` | empty | Consumer test hook. |
| `CONTINUUM_VALIDATION_COMMAND` | empty | Additional validation hook. |
| `CONTINUUM_PACKAGE_COMMAND` | empty | Consumer packaging hook. |
| `CONTINUUM_RELEASE_COMMAND` | empty | Consumer release hook. |
| `CONTINUUM_AUTO_DETECT` | `true` | Detect and validate Python/Node/PHP/Java/Go/Rust, Docker and shell. |
| `CONTINUUM_NODE_VERSION` | empty | Optional Node.js version setup for explicit consumer hooks. |
| `CONTINUUM_PHP_VERSION` | empty | Optional PHP version setup for explicit consumer hooks. |
| `CONTINUUM_PHP_EXTENSIONS` | empty | Comma-separated PHP extensions when PHP setup is requested. |
| `CONTINUUM_PHP_COVERAGE` | `none` | PHP coverage driver when PHP setup is requested. |
| `CONTINUUM_BUN_VERSION` | empty | Optional Bun version setup for explicit consumer hooks. |
| `CONTINUUM_ARTIFACT_PATHS` | empty | Newline-separated artifact paths to upload. |
| `CONTINUUM_ARTIFACT_NAME` | `continuum-validation` | Uploaded artifact name. |
| `CONTINUUM_STATUS_CONTEXT` | `continuum/validation` | Commit-status context. |
| `CONTINUUM_PUBLISH_STATUS` | `true` | Publish pending/final status. |
| `CONTINUUM_DISPATCH_REPAIR` | `true` | Dispatch repair after a PR validation result. |
| `CONTINUUM_REPAIR_WORKFLOW` | `continuum-opencode-repair.yml` | Repair caller file. |
| `CONTINUUM_SKIP_OPENCODE_PR` | `false` | Suppress duplicate pull_request validation for `opencode/*` branches. |

The reusable engine is **not** installed as a `continuum-validation.yml`
caller. Each consumer owns one primary `.github/workflows/ci.yml` whose
workflow `name:` is **CI** and calls
`kodmial/continuum/.github/workflows/continuum-validation.yml@main`.
That keeps trigger/toolchain differences project-owned without creating a second
generic CI state machine. Core repair/watchdog logic treats the stable `CI`
workflow name as the interface.

## Agent execution

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `AUTOMATION_OPENCODE_RUNNER` | `ubuntu-latest` | Runner used by OpenCode. |
| `AUTOMATION_OPENCODE_TIMEOUT_MINUTES` | `180` | OpenCode timeout. |
| `CONTINUUM_AGENT_PREPARE_COMMAND` | empty | Consumer-owned toolchain/environment setup run after checkout. |
| `CONTINUUM_OPENCODE_VERSION` | `1.19.3` (action manifest default) | Exact SemVer override to the shared hosted OpenCode cache bootstrap; invalid or unavailable pins fail closed, no moving `latest`. Used by coder, PR-Agent review/repair and delegated Child agent jobs. When unset, `toolchain.json` is authoritative; PR-Agent separately validates its own pinned wheel digest. |
| `CONTINUUM_IMAGE_DIGEST` | empty | **Legacy native-image/profile digest, preserved for compatibility** with the reusable `image_digest` inputs (which still forward `inputs.image_digest || vars.CONTINUUM_IMAGE_DIGEST`). Before Work Lock #67, Linux GitHub-hosted OpenCode jobs treated this as a possible preinstalled-runtime stamp and otherwise ran an inline cold installer; this was a misleading warm-cache proxy on freshly provisioned hosted VMs. **After #67 migration**, the six active GitHub-hosted coding/review/repair/delegation workflows instead restore and verify OpenCode binaries using `agent-toolchain` + `actions/cache`, so setting this variable cannot create a hosted warm hit. Consumers with a real separately qualified native golden-image provider may still use the digest as a profile identity; those backends and macOS/Windows are not qualified by hosted cache tests. Legacy caller inputs remain accepted, but should not be interpreted as hosted cache pins. |
| `CONTINUUM_RUNTIME_PRESET` | `agent-linux` | Named ephemeral runtime preset (`agent-linux`, `agent-macos`, `agent-windows`, ...). Strict presets keep `idle_instances: 0` and `max_uses_per_instance: 1`; they cannot be retuned into persistent waiting runners. |
| `CONTINUUM_RUNTIME_PROVIDER` | `github-hosted` | Provider backend for ephemeral instances (`github-hosted`, `gce`, `ec2`, `azure`, `custom`). |

Strict ephemeral invariants (zero live idle instances, one job per
instance, per-job network lifecycle, reconciler-backed teardown) are
enforced by the Continuum provisioning path
(`src/continuum/agent_runtime.py`: `resolve_profile` /
`resolve_profile_from_env` plus `EphemeralController` and the
`sweep_orphans` reconciler) for strict profiles that keep
`idle_instances: 0` and `max_uses_per_instance: 1`; setting the variables
in this file alone provisions nothing. GitHub-hosted runners are already
fresh instances per job; the prepared runtime (golden image or its
content-addressed cache equivalent) is what makes them fast. There is no
pre-created live VM pool and no IP-uniqueness guarantee between jobs.

There is no implicit macOS or language setup in core.

## Optional Swift profile

The `tech` set installs `continuum-tech-swift-ci.yml`, a consumer-neutral
profile over the same validation engine.

| Repository variable | Default |
| --- | --- |
| `CONTINUUM_SWIFT_RUNNER` | `macos-latest` |
| `CONTINUUM_SWIFT_BUILD_COMMAND` | `swift build` |
| `CONTINUUM_SWIFT_TEST_COMMAND` | `swift test` |

Applications that need a particular macOS image select it explicitly in their
own workflow or configuration. Signing, package-manager manifests and release
publication are not part of the Swift profile.

## Lifecycle tuning

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `AUTOMATION_WIP_LIMIT` | `2` | Concurrent issue reservations. |
| `AUTOMATION_LEASE_MINUTES` | `45` | Reservation lease. |
| `AUTOMATION_MAX_DISPATCH_ATTEMPTS` | `2` | Dispatch attempts before configured failure handling. |
| `AUTOMATION_SCHEDULER_TIMEOUT_MINUTES` | `5` | Scheduler timeout. |
| `AUTOMATION_AUTOMERGE_TIMEOUT_MINUTES` | `10` | Auto-merge reconciliation timeout. |
| `AUTOMATION_DISPATCH_TIMEOUT_MINUTES` | `5` | Internal dispatch timeout. |
| `AUTOMATION_DISPATCH_BACKOFF_SECONDS` | `120` | Dispatch retry backoff. |
| `AUTOMATION_DISPATCH_MARKER` | `<!-- issue-scheduler-dispatch -->` | Scheduler dispatch marker. |
| `AUTOMATION_IN_PROGRESS_LABEL` | `automation:in-progress` | Reservation label. |
| `AUTOMATION_PAUSE_LABEL` | `automation:paused` | Retired compatibility value. No runtime decision reads or writes this label; a stale label is cleared. |
| `AUTOMATION_QUALIFYING_LABEL` | `automation:qualifying` | Implementation-merged capabilities waiting on mandatory live qualification. |
| `AUTOMATION_BLOCKED_LABEL` | `automation:blocked` | Capabilities whose mandatory qualification failed and need repair. |
| `AUTOMATION_READY_LABEL` | ignored | Deprecated compatibility variable. Admission is fixed to `automation:ready` for every new issue-coder attempt (scheduler, owner `/oc`, direct issue dispatch, watchdog/429 recovery, delegated child worker). Qualification dispatch uses its own lifecycle. |
| `CONTINUUM_CHILD_OWNED_MARKER` | `<!-- continuum-child-owned -->` | Current delegated-child ownership marker. |
| `CONTINUUM_LEGACY_CHILD_OWNED_MARKER` | empty | Optional legacy marker supplied by a migrating consumer. |
| `CONTINUUM_REVIEW_PROVIDER` | `none` | Authoritative review selector: `none`, `coderabbit`, or `pr-agent`. |
| `CONTINUUM_VERSION_FILE` | empty | Optional version file used only by the generic stale-release-run optimization. |

## Issue admission, STOP and recovery

`automation:ready` is mandatory for a **new** implementation attempt. It is a
fixed core label, not an input or repository variable. Removing it prevents new
starts, including automatic scheduler dispatch, manual owner `/oc`, direct
`issue` workflow dispatch, watchdog/429 recovery and delegated child work. It
does **not** cancel an already-running Actions job.

An owner comment whose first significant line is exactly `/oc-cancel` is a
real operator STOP. It:

1. revokes `automation:ready`;
2. persists a durable intent/fence and the shared terminal generation-stop
   record for `repository + issue + generation`;
3. cancels only Actions runs whose workflow path and issue/generation
   provenance match (a private Child STOP fences the Child issue and cancels
   the Parent's delegated `SubTask #<task>` worker run);
4. observes the real terminal outcome (`STOP_REQUESTED`, `CANCEL_PENDING`,
   `STOPPED`, `PARTIAL`, `FAILED`) and blocks late code/PR/merge publication.

Repeated STOP is idempotent. Restoring `automation:ready` never re-arms a
stopped generation. An explicit owner recovery marker
(`continuum-task-generation-recover`) re-arms it, after which a new owner
command or scheduler pass may start exactly one new attempt. STOP and recovery
are separate operations: STOP is the only action that cancels a live run,
while recovery only clears the fence for a future generation.

Authorization is the repository owner's exact command. Prose, quoted examples,
machine snapshots, bot comments and non-owner comments never grant STOP or
recovery authority. Removing `automation:ready` manually is not a STOP: it
prevents new starts but never cancels an in-flight job and does not persist the
durable stop fence.

A private Child may allocate a control-plane runner only for an owner comment
that the shared parser classifies as an exact `/oc-cancel`; it never allocates
a child coder or automatic scheduler runner.
That STOP fences the Child issue and cancels the Parent's delegated
`SubTask #<task>` run through the Parent PAT. Parent cron remains the
authoritative discovery/recovery path, so child Actions availability is never
required for ordinary scheduling.

Migration and rollback are operator-controlled. Legacy compatibility inputs
(`ready_label`, `pause_marker`, `pause_on_failure`, `post_pause_comment`) are
still accepted by `workflow_call` but are inert: they never widen admission,
write `automation:paused`, or suppress cancellation. To return a consumer to
manual-only operation, remove `automation:ready` and stop dispatching new work;
do not reintroduce pause labels as a substitute for STOP. Recovery after a STOP
requires the explicit recovery marker and a fresh owner command.

Automatic errors (no diff, 429, infrastructure, transient API failures) use
bounded lease/backoff retry, not a terminal `automation:paused` label.

## Optional PR-Agent review provider

PR-Agent is the optional review stack selected with
`CONTINUUM_REVIEW_PROVIDER=pr-agent` and backed by the runner-local OpenCode
bridge. Provider selection does not use legacy true/false switches. Consumers
selecting `coderabbit` keep the CodeRabbit stack; no paid-provider secret is
required for PR-Agent. See `docs/pr-agent-opencode.md` for the architecture
and failure modes.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `PR_AGENT_API_BASE` | `http://127.0.0.1:<bridge_port>/v1` | OpenAI-compatible backend base PR-Agent talks to. |
| `PR_AGENT_MODEL` | `openai/continuum-review` | LiteLLM routing id (`openai/` prefix selects the bridge path). |
| `PR_AGENT_MAX_TOKENS` | `128000` | Custom-model context cap used by PR-Agent prompt budgeting. |
| `OPENCODE_MODEL` | `opencode/muse-spark-1.3-contributor-free` | OpenCode model used by the PR-Agent bridge. |
| `PR_AGENT_BRIDGE_PORT` | `18000` | Loopback port of the compatibility bridge. |
| `PR_AGENT_OPENCODE_PORT` | `4096` | Loopback port of `opencode serve`. |
| `PR_AGENT_VERSION` | `0.46.0` | Pinned `pr-agent` release installed on the runner. |
| `AUTOMATION_PR_AGENT_TIMEOUT_MINUTES` | `60` | PR-Agent job timeout. |

The inference model is the same `OPENCODE_MODEL` mechanism the agent
execution section documents (default: the free anonymous route).

## Optional controllers

Render and Docker qualification remain generic execution envelopes. Their
consumer-owned commands/files are already hooks/inputs; no product repository
is assumed.

Render consumers provide `RENDER_API_KEY` and explicitly configure their
consumer-owned hooks. Continuum deliberately supplies no repository-path defaults:

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `RENDER_JOB_SCRIPT` | empty | Consumer-owned script that drives the Render lifecycle. Required for Render execution. |
| `RENDER_CLEANUP_SCRIPT` | empty | Consumer-owned script that deletes the ephemeral Render service. Required for Render execution. |
| `RENDER_QUALIFICATION_SCRIPT` | empty | Consumer-owned qualification/classification script. Required only for qualification-labelled runs. |
| `RENDER_CAPACITY_WORKFLOW` | empty | Dedicated optimization/profile/capacity decision workflow woken for capacity-classified failures. Empty holds the source safely. |

The same values may be passed as the reusable-workflow inputs `job_script`,
`cleanup_script`, `qualification_script`, and `capacity_workflow`. A missing required hook fails
explicitly; core never guesses a consumer repository layout. Docker qualification
consumers configure their artifact repository, binary name, image, memory/trial
limits and result paths.

## Secrets

| Secret | Required when | Purpose |
| --- | --- | --- |
| `TAP_PAT` | Generic workflows need authenticated GitHub writes | Classic PAT with `repo` + `workflow` scopes. The `workflow` scope lets OpenCode task branches include `.github/workflows/**` changes; publication still goes through a PR and never writes directly to `main`. |
| `RENDER_API_KEY` | Render execution is enabled | Render API credential. |
| `CHILD_RUNTIME_TOKEN` | Direct parent reusable workflow invocation | Delegated child access; installed parent callers map their configured parent credential. |
| `CHILD_RUNTIME_REPOSITORIES` | Legacy parent configuration only | Optional compatibility input for child repository relationships. |

The optional Swift profile defines **no signing or release secrets**. Product
signing/release credentials belong to the consumer.

Every installed parent execution stub fills `CHILD_RUNTIME_TOKEN` from the parent's own `TAP_PAT`; metadata-only cleanup uses the caller `GITHUB_TOKEN` and receives no child credential. Direct reusable-workflow callers may supply `CHILD_RUNTIME_TOKEN` themselves.

## New repository example

1. Install `core` from Continuum `main`.
2. Add one project-owned `.github/workflows/ci.yml` named `CI` that calls
   `kodmial/continuum/.github/workflows/continuum-validation.yml@main`.
3. Supply project-specific build/test/toolchain semantics through validation
   inputs or `CONTINUUM_*` variables; do not edit an installed
   `continuum-*.yml` caller.
4. Configure `TAP_PAT` if the lifecycle needs authenticated writes.
5. Set `CONTINUUM_RUNNER` only when `ubuntu-latest` is not suitable.
6. Install `tech` only for an optional technology profile and `parent` only
   for delegated execution.
7. Re-running the same install is expected to be idempotent: project-owned
   workflows are preserved and the install-managed caller topology is unchanged.

No consumer source path, product name, release repository or signing secret is
added to Continuum.
