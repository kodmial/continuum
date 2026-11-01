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
| `CONTINUUM_CI_REPAIR_WORKFLOWS` | `CI` | Comma- or newline-separated exact workflow names whose completed pull-request runs may dispatch an OpenCode `ci-fix`. Only these exact names are admitted; the repair caller wakes broadly because a `workflow_run` filter cannot read repository variables, then skips the runner for an unapproved completed name. |
| `CONTINUUM_SKIP_OPENCODE_PR` | `false` | Suppress duplicate pull_request validation for `opencode/*` branches. |

### CI-repair wake-up admission

GitHub does not allow a repository variable in a `workflow_run` event filter, so
the installed repair caller still creates a controller run for every completed
workflow. The caller and the reusable controller therefore read
`CONTINUUM_CI_REPAIR_WORKFLOWS` in their job-level `if` and skip the runner
before any checkout, git fetch, or GitHub API call when the completed name
cannot be admitted. The check is a conservative substring superset of the
controller's exact, comma/newline-aware allow-list, so it can never skip an
approved name and the exact runtime admission remains authoritative.

A suppressed completion gets a unique concurrency group, so it cannot replace a
pending same-head run of an approved workflow. All approved workflow names,
including custom names added to the variable later, still share the exact-head
lock. The direct `workflow_dispatch` CI report and the `pull_request_target`
branch-sync path do not use this name admission and are unaffected.

This change does not remove the broad `workflow_run` trigger or introduce a
timer; workflow-run creation itself remains a GitHub limitation, and durable
all-event ingress/retirement stays with the paused #344/#345 successor work.

Upgrade: reinstall the `core` set so the installed caller also skips before the
reusable job is created. Existing installed callers already benefit from the
reusable controller admission because it is referenced at `@main`. Rollback:
restore the previous `continuum-opencode-repair.yml` caller and/or controller
revision; the event trigger and both independent dispatch paths are unchanged,
so no trigger migration is needed to roll back.

Issue mode uses the same repository-authoritative verification contract. Before
the primary coding session it resolves the consumer's literal
`prepare_command`, `build_command`, `test_command`, and `validation_command`
values from the primary `CI` caller (the file that calls
`continuum-validation.yml`), falling back to the matching
`CONTINUUM_*_COMMAND` repository variables. At least one of build, test, or
validation must resolve; otherwise the implementation fails closed with an
explicit unconfigured state instead of inventing a stack-specific check. A
non-literal GitHub expression other than `${{ vars.NAME }}` is also an explicit
unconfigured failure, never a silent fallback to a different command.

The resolved command set runs once before implementation to record the baseline,
then again after a fresh independent verifier/fix OpenCode session. The final
run reuses the baseline's exact commands and environment fingerprint. A failed
or unavailable post-change check never produces a successful result; a
pre-existing baseline failure is recorded separately and does not become green.
The workflow-owned `.continuum-verification.json` evidence is retained on a
429 checkpoint and removed before the implementation commit/PR.

The evidence file lives in the task worktree, so the engine never trusts it on
its own. The verification engine recognizes an optional internal HMAC-SHA256
attestation hook only when its process environment supplies the path
`CONTINUUM_VERIFICATION_ATTESTATION_KEY_FILE` to a private key file outside
the task worktree. The key value itself is never read from the environment: a
repository-controlled command can inspect the engine's inherited environment
through `/proc/$PPID/environ`, so setting
`CONTINUUM_VERIFICATION_ATTESTATION_KEY` in the engine environment fails
closed instead of being accepted. The engine reads the key file in every
verification CLI process and never passes the key value or its file path to a
repository-controlled command. Read-only subcommands (`summary`,
`needs-verifier`, `changes`, `record-verifier`) leave the file in place, so
separate sequential read-only processes verify the same evidence. A subcommand
that runs repository-controlled commands (`baseline`, `final`) reads the key
into memory and removes the on-disk copy before the first command starts, and
it never writes the copy back: a command can spawn a same-uid process that
outlives the engine and reads any file the engine can read, so restoring the
file at command end or at process exit, or leaving it after an interrupt,
would hand the key to that process. A later invocation that needs the key must
re-provision it explicitly from its authoritative source, for example
`printf '%s' "$KEY" | python -m continuum.verification provision-key --repo "$GITHUB_WORKSPACE"`,
which reads the key from standard input (never an argument or the environment),
writes atomically with mode `0600`, and refuses to replace a different key. If
the file is absent when it is needed, the engine fails closed with an explicit
"the verification attestation key file is unavailable" error instead of
continuing without an attestation. No shipped
Continuum workflow declares or passes this hook; it is not a consumer secret
and requires no consumer configuration. If supplied, it signs the recorded
contract, baseline, verifier result, and accepted final record, and a resumed
run reuses those records only when the signature verifies. Without the key, an
existing baseline is rebuilt and the final phase re-resolves the contract from
the task base commit instead of the worktree, so a coding agent cannot rewrite
its own verification commands or forge an accepted result. The key is read
from the private file, never from an argument or the environment, so it does
not appear in process listings.

The reusable engine is **not** installed as a `continuum-validation.yml`
caller. Each consumer owns one primary `.github/workflows/ci.yml` whose
workflow `name:` is **CI** and calls
`kodmial/continuum/.github/workflows/continuum-validation.yml@main`.
That keeps trigger/toolchain differences project-owned without creating a second
generic CI state machine. Core repair/watchdog logic treats the stable `CI`
workflow name as the interface, and `CONTINUUM_CI_REPAIR_WORKFLOWS` extends
that interface only with the extra exact workflow names a consumer explicitly
approves (for example a product-test workflow named `Windows host`). The
repair lock is shared across those workflows per PR head, so one green workflow
never clears a lock another approved workflow still needs.

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
| `OPENCODE_MODELS` | empty | Ordered, comma-separated pool of 1–8 unique `provider/model` identifiers for failover routing. Empty disables pool routing; the single-model `OPENCODE_MODEL` fallback applies instead. Malformed, duplicate, or excessive entries fail closed. |
| `OPENCODE_MODEL_STRATEGY` | `sticky-failover` | Pool selection strategy. `sticky-failover` is the only supported value; any other value fails closed. |
| `OPENCODE_PROBE_TTL` | `3600` | Seconds a successful route probe remains fresh. Integer in `[60, 86400]`. |
| `OPENCODE_MODEL_COOLDOWN` | `900` | Seconds a failed or burned route stays out of selection. Integer in `[60, 86400]`. |
| `OPENCODE_PROBE_TIMEOUT` | `90` | Per-probe timeout in seconds. Integer in `[15, 240]`. |

`OPENCODE_MODEL` is only the legacy single-model fallback: it is used when
`OPENCODE_MODELS` is empty. A per-run explicit pin must be one of the pool
routes. The full selection, qualification, and state contract is in
`docs/opencode-model-routing.md`.

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
| `AUTOMATION_PAUSE_LABEL` | `automation:paused` | Pause label. |
| `AUTOMATION_QUALIFYING_LABEL` | `automation:qualifying` | Implementation-merged capabilities waiting on mandatory live qualification. |
| `AUTOMATION_BLOCKED_LABEL` | `automation:blocked` | Capabilities whose mandatory qualification failed and need repair. |
| `AUTOMATION_READY_LABEL` | empty | Optional admission label for automatic implementation. When set (for example `automation:ready`), the scheduler auto-dispatches only issues carrying that label; empty keeps the current behavior. Qualification dispatch and manual owner `/oc` never require it. |
| `CONTINUUM_CHILD_OWNED_MARKER` | `<!-- continuum-child-owned -->` | Current delegated-child ownership marker. |
| `CONTINUUM_LEGACY_CHILD_OWNED_MARKER` | empty | Optional legacy marker supplied by a migrating consumer. |
| `CONTINUUM_REVIEW_PROVIDER` | `none` | Authoritative review selector: `none`, `coderabbit`, or `pr-agent`. |
| `CONTINUUM_CONTRACT_GATE_WORKFLOW_NAME` | `Continuum Contract Gate` | Workflow name for the owner-armed resume Contract Gate. At the default, the exact-HEAD gate is required only in `kodmial/continuum`; a non-default name applies the same gate in any consumer repository. |
| `CONTINUUM_VERSION_FILE` | empty | Optional version file used only by the generic stale-release-run optimization. |

## Optional CodeRabbit dispatch targets

The CodeRabbit controllers and the auto-merge reconciler dispatch sibling
workflows by file name. Each target is overridable for a consumer whose
dispatchable entry workflow uses a different file name; an empty input or
variable keeps the legacy Continuum name, so existing installations are
unchanged. These are bare file names, not paths: the engines reject a value
containing `/`, `\`, or `..`, or one that does not end in `.yml`, and fall back
to the legacy name.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `CONTINUUM_CODERABBIT_RETRY_WORKFLOW` | `continuum-coderabbit-retry.yml` | Workflow dispatched when a current head still needs its CodeRabbit review. |
| `CONTINUUM_CODERABBIT_UNRESOLVED_WORKFLOW` | `continuum-coderabbit-unresolved.yml` | Workflow dispatched to reconcile a CodeRabbit unresolved-thread verdict. |
| `CONTINUUM_AUTO_MERGE_WORKFLOW` | `continuum-auto-merge.yml` | Workflow dispatched to reconcile auto-merge after a successful CI run. |

The reusable-workflow inputs `coderabbit_retry_workflow`,
`coderabbit_unresolved_workflow`, and `auto_merge_workflow` accept the same
values. Installed callers forward them empty, so the repository variable remains
the source of truth.

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
