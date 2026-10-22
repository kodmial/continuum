# Consumer configuration reference

Continuum is configured by the **calling repository**. This page covers the
settings an external consumer is expected to change. It is not a catalog of
internal-only inputs. See [Architecture and trust boundaries](architecture.md)
for what runs where, and [Upgrading and rollback](upgrading.md) for installing
and pinning revisions.

## How a value is resolved

For reusable-workflow settings the order is:

1. a non-empty `workflow_call` input;
2. the repository variable named in the workflow;
3. the workflow's literal default.

Installed caller stubs deliberately forward empty inputs. That is what lets a
repository variable take effect; a caller that pins a literal would override the
variable for every consumer. Settings documented as inputs only are not
repository variables.

## Validation (project-owned CI)

[`continuum-validation.yml`](../.github/workflows/continuum-validation.yml) is
the common validation engine. The consumer owns exactly one workflow named
`CI` (normally `.github/workflows/ci.yml`) that calls it. Validation is not an
installed caller and the installer does not create a second CI workflow.

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

Every hook is optional. With `CONTINUUM_AUTO_DETECT=false` and no hooks,
validation reports that it had nothing to execute. The `CI` workflow name is
part of the interface: review, repair, and merge controllers match it.

## Agent execution and model routing

[`continuum-opencode.yml`](../.github/workflows/continuum-opencode.yml) runs the
coding agent. PR-Agent review and delegated child jobs use the same routing
mechanism.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `AUTOMATION_OPENCODE_RUNNER` | `ubuntu-latest` | Runner used by OpenCode. |
| `AUTOMATION_OPENCODE_TIMEOUT_MINUTES` | `180` | OpenCode job timeout. |
| `CONTINUUM_AGENT_PREPARE_COMMAND` | empty | Consumer-owned toolchain/environment setup run after checkout. |
| `CONTINUUM_OPENCODE_VERSION` | `1.19.3` from [`toolchain.json`](../.github/actions/agent-toolchain/toolchain.json) | Exact SemVer override for the shared hosted OpenCode runtime. An invalid or unavailable pin fails closed; there is no moving `latest`. |
| `CONTINUUM_CI_REPAIR_LABEL` | `opencode-ci-repair` | In-flight lock label for automatic CI repair. |
| `CONTINUUM_ISSUE_BASE_REF` | `main` | Base branch for issue implementation. |
| `CONTINUUM_ISSUE_COMMIT_PREFIX` | `fix` | Prefix of the task commit message. |

Model routing is owned by
[`src/continuum/model_routing.py`](../src/continuum/model_routing.py) and its
workflow adapter. A route is qualified by running the verified OpenCode binary,
not by its name.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `OPENCODE_MODELS` | empty | Ordered, unique pool of 1–8 `provider/model` routes. This is the authoritative production policy. |
| `OPENCODE_MODEL` | empty | Backward-compatible single route, used only when `OPENCODE_MODELS` is unset. |
| `OPENCODE_MODEL_STRATEGY` | `sticky-failover` | The only supported strategy. |
| `OPENCODE_PROBE_TTL` | `3600` | Seconds before a qualified route expires. |
| `OPENCODE_MODEL_COOLDOWN` | `900` | Seconds before retrying an unsuccessful route. |
| `OPENCODE_PROBE_TIMEOUT` | `90` | Timeout per OpenCode probe, in seconds. |

There is **no built-in production model default**. Set `OPENCODE_MODELS`, or the
legacy `OPENCODE_MODEL`, or coding/review fails closed. The PR-Agent canary has
its own supported free route; that does not make a model available to normal
runs. Do not infer that a route is free from its name, and do not depend on a
free route being continuously available.

`CONTINUUM_IMAGE_DIGEST` is a **legacy native-image/profile digest** kept for
compatibility with the reusable `image_digest` inputs. It is still forwarded as
`inputs.image_digest || vars.CONTINUUM_IMAGE_DIGEST`. It is not a hosted-cache
pin: GitHub-hosted jobs restore the verified OpenCode runtime through
`agent-toolchain` + `actions/cache`, so this value cannot create a hosted warm
hit. A separately qualified native-image provider may still use it as profile
identity; those providers and macOS/Windows prepared images are not qualified by
the hosted tests.

`CONTINUUM_RUNTIME_PRESET` (`agent-linux`) and `CONTINUUM_RUNTIME_PROVIDER`
(`github-hosted`) are read by
[`src/continuum/agent_runtime.py`](../src/continuum/agent_runtime.py), not by the
installed hosted workflows. Setting them alone provisions nothing. Strict
presets keep zero idle instances and one job per instance. The non-GitHub cloud
backend path is experimental; see [Prebuilt runtime](agent-runtime.md).

## Issue admission and lifecycle

The scheduler is
[`continuum-issue-scheduler.yml`](../.github/workflows/continuum-issue-scheduler.yml).
Most callers are optional; the defaults below are the installed-caller behavior.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `AUTOMATION_WIP_LIMIT` | `2` | Concurrent implementation reservations. |
| `AUTOMATION_LEASE_MINUTES` | `45` | Reservation lease before it can be recovered. |
| `AUTOMATION_MAX_DISPATCH_ATTEMPTS` | `2` | Dispatch attempts before configured failure handling. |
| `AUTOMATION_SCHEDULER_TIMEOUT_MINUTES` | `5` | Scheduler job timeout. |
| `AUTOMATION_AUTOMERGE_TIMEOUT_MINUTES` | `10` | Auto-merge reconciliation timeout. |
| `AUTOMATION_DISPATCH_TIMEOUT_MINUTES` | `5` | Internal dispatch timeout. |
| `AUTOMATION_DISPATCH_BACKOFF_SECONDS` | `120` | Dispatch retry backoff. |
| `AUTOMATION_COMMAND_GRACE_MINUTES` | `5` | Window after a manual owner command during which the scheduler does not enqueue a duplicate run. |
| `AUTOMATION_REQUIRE_PRIORITY_LABEL` | `false` | Dispatch only issues carrying a priority label. |
| `AUTOMATION_COUNT_OPEN_PRS_AS_WIP` | `true` | Count an open implementation PR against WIP. |
| `AUTOMATION_PAUSE_ON_FAILURE` | scheduler `false`; OpenCode issue path `true` | Enter a terminal pause after exhausted dispatches or a closed failed PR. Installed callers forward an empty value: the scheduler then falls back to `false`, while the OpenCode issue path falls back to `true`. A direct reusable scheduler call that omits the input gets its literal `true` default. |
| `AUTOMATION_READY_LABEL` | empty | Optional admission label for automatic implementation. Empty keeps normal behavior. Qualification dispatch and manual owner `/oc` never require it. |
| `AUTOMATION_DISPATCH_MARKER` | `<!-- issue-scheduler-dispatch -->` | Scheduler dispatch marker. Must match the OpenCode and watchdog callers. |
| `AUTOMATION_IN_PROGRESS_LABEL` | `automation:in-progress` | Reservation label. |
| `AUTOMATION_PAUSE_LABEL` | `automation:paused` | Pause label. |
| `AUTOMATION_QUALIFYING_LABEL` | `automation:qualifying` | Capabilities awaiting mandatory live qualification. |
| `AUTOMATION_BLOCKED_LABEL` | `automation:blocked` | Capabilities whose mandatory qualification failed. |
| `CONTINUUM_OPENCODE_DISPATCH` | `comment` | How a ready issue reaches OpenCode: `comment` or `workflow`; any other value fails. |
| `CONTINUUM_DISPATCH_REF` | `main` | Ref targeted by scheduler `workflow_dispatch` calls. |
| `CONTINUUM_OPENCODE_WORKFLOW_NAME` | `OpenCode agent` | Workflow `name:` whose active runs count as work. |
| `CONTINUUM_OPENCODE_WORKFLOW_PATH` | `.github/workflows/continuum-opencode.yml` | Caller path used for older runs without `run-name:`. |
| `CONTINUUM_EXECUTION_LABEL_ROUTES` | empty | Optional label-to-workflow execution routes, one per line. Empty routes nothing. |
| `CONTINUUM_CI_WORKFLOW_NAME` | `CI` | Exact workflow name that proves current-head CI. |

Recovery settings for
[`continuum-opencode-watchdog.yml`](../.github/workflows/continuum-opencode-watchdog.yml):

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `AUTOMATION_WATCHDOG_MAX_RETRIES` | `1` | Automatic recovery retries before the issue is paused. |
| `AUTOMATION_WATCHDOG_MAX_429_RUNNER_RESTARTS` | `3` | Fresh-runner restarts after an explicit OpenCode 429 marker. This is a budget, not a recovery guarantee. |
| `AUTOMATION_WATCHDOG_TIMEOUT_MINUTES` | `5` | Watchdog job timeout. |
| `AUTOMATION_WATCHDOG_RETRY_MARKER` | `<!-- opencode-watchdog-retry -->` | Hidden marker counted as a consumed retry. |
| `AUTOMATION_ATTEMPTS_TIMEOUT_MINUTES` | `10` | Timeout for dispatch-attempt bookkeeping. |

## Review provider and merge

`CONTINUUM_REVIEW_PROVIDER` is the single authoritative selector. The generic
merge path lives in
[`continuum-auto-merge.yml`](../.github/workflows/continuum-auto-merge.yml); the
PR-Agent path is
[`continuum-pr-agent.yml`](../.github/workflows/continuum-pr-agent.yml), and the
CodeRabbit path is the retry/unresolved controller pair.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `CONTINUUM_REVIEW_PROVIDER` | `none` | `none`, `coderabbit`, or `pr-agent`. Invalid values fail explicitly. |
| `CONTINUUM_REQUIRED_WORKFLOW_GATE_LABEL` | empty | Optional PR label that activates an extra current-head workflow gate. Set it only together with the name. |
| `CONTINUUM_REQUIRED_WORKFLOW_GATE_NAME` | empty | Exact workflow name required when that label is present. |
| `CONTINUUM_POST_MERGE_WAKEUPS` | empty | Comma-separated workflows woken after a successful merge. Generic auto-merge wakes nothing when empty; the PR-Agent merge path defaults to `continuum-issue-scheduler.yml`. |
| `CONTINUUM_POST_MERGE_WAKEUP_REF` | default branch, then `main` | Ref for post-merge wake-ups. |
| `CONTINUUM_VERSION_FILE` | empty | Optional version file used only by the generic stale-release-run optimization. |

`coderabbit` keeps the CodeRabbit label/retry/unresolved stack. `pr-agent`
disables the generic merge path (it remains sync-only) and uses the PR-Agent
exact-head gate. `none` runs neither provider review nor its recovery stack.
Deprecated true/false provider switches are not part of this contract.

## PR-Agent over OpenCode

PR-Agent is selected by `CONTINUUM_REVIEW_PROVIDER=pr-agent`. It runs the pinned
Python release on the runner and reaches `opencode serve` through a loopback
bridge. See [PR-Agent over the OpenCode backend](pr-agent-opencode.md) for the
topology and failure modes.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `PR_AGENT_API_BASE` | `http://127.0.0.1:<bridge_port>/v1` | OpenAI-compatible backend base. |
| `PR_AGENT_MODEL` | `openai/continuum-review` | LiteLLM routing id (`openai/` selects the bridge path). |
| `PR_AGENT_MAX_TOKENS` | `128000` | Custom-model context cap used for prompt budgeting. |
| `PR_AGENT_BRIDGE_PORT` | `18000` | Loopback bridge port. |
| `PR_AGENT_OPENCODE_PORT` | `4096` | Loopback `opencode serve` port. |
| `PR_AGENT_VERSION` | `0.46.0` | Pinned upstream `pr-agent` release. |
| `AUTOMATION_PR_AGENT_TIMEOUT_MINUTES` | `60` | PR-Agent job timeout. |
| `PR_AGENT_RECOVERY_STALE_MINUTES` | `70` | Age before PR-Agent recovery considers an operation stale. |
| `PR_AGENT_RECOVERY_DISPATCH_GRACE_SECONDS` | `90` | Grace before treating a missing dispatch as failed. |
| `PR_AGENT_RECOVERY_MAX_EXECUTIONS` | `10` | Total executions per exact PR/HEAD/operation. |
| `CONTINUUM_PR_AGENT_TARGET_CHILD_ID` | empty | Parent-side delegated target when the caller does not pass one. |

The inference route comes from `OPENCODE_MODELS`/`OPENCODE_MODEL` above. The
PR-Agent stack needs no paid-provider secret. An unavailable free route is an
explicit failure, not a silent green review.

## Optional controllers

### Render

Render execution requires `RENDER_API_KEY` and consumer-owned scripts. Continuum
supplies no repository-path defaults.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `RENDER_JOB_SCRIPT` | empty | Consumer script that drives the Render lifecycle. Required for Render execution. |
| `RENDER_CLEANUP_SCRIPT` | empty | Consumer script that deletes the ephemeral Render service. Required for Render execution. |
| `RENDER_QUALIFICATION_SCRIPT` | empty | Consumer qualification/classification script. Required only for qualification-labelled runs. |
| `RENDER_CAPACITY_WORKFLOW` | empty | Optional capacity/profile decision workflow. Empty holds the source safely. |
| `RENDER_REGION` | `oregon` | Render region passed to the consumer script. |
| `MAX_RENDER_REPAIR_ATTEMPTS` | `10` | Bounded Render repair attempts. |
| `AUTOMATION_RENDER_TIMEOUT_MINUTES` | `55` | Render job timeout. |

The same values are accepted as reusable-workflow inputs (`job_script`,
`cleanup_script`, `qualification_script`, `capacity_workflow`, and the matching
names). A missing required hook fails explicitly; core never guesses a consumer
layout. See
[`continuum-render-executor.yml`](../.github/workflows/continuum-render-executor.yml).

### Docker qualification

[`continuum-docker-qualification.yml`](../.github/workflows/continuum-docker-qualification.yml)
runs consumer-owned artifact qualification. These are the externally useful
variables; every one also has a matching input.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `ARTIFACT_REPOSITORY` | caller repository | Repository whose Actions artifacts hold the binary under test. |
| `DOCKER_QUALIFICATION_BINARY_NAME` | `opencode-coding-linux-x64` | Binary/checksum name inside the artifact. |
| `DOCKER_QUALIFICATION_IMAGE` | `ubuntu:22.04` | Container image. |
| `DOCKER_QUALIFICATION_MEMORY_MIB` | `512` | Hard container memory limit. |
| `DOCKER_QUALIFICATION_MIN_HEADROOM_MIB` | `32` | Required peak headroom below the limit. |
| `DOCKER_QUALIFICATION_TRIALS` | `2` | Independent coding trials. |
| `DOCKER_QUALIFICATION_RESULT_FILE` | `/tmp/continuum-docker-qualification-result.json` | Result payload path. |
| `AUTOMATION_DOCKER_QUALIFICATION_TIMEOUT_MINUTES` | `45` | Job timeout. |

A Docker qualification run is dispatched with an issue number; a run with no
target fails explicitly.

## Technology library (tech set)

`tech` installs
[`continuum-tech-swift-ci.yml`](../.github/workflows/continuum-tech-swift-ci.yml),
a consumer-neutral Swift profile over the validation engine. It disables generic
auto-detection and reuses the validation hooks, artifact, status, and repair
variables above. Signing, packaging, and release publication remain
consumer-owned.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `CONTINUUM_SWIFT_RUNNER` | `macos-latest` | Runner for the Swift profile. |
| `CONTINUUM_SWIFT_BUILD_COMMAND` | `swift build` | Swift build command. |
| `CONTINUUM_SWIFT_TEST_COMMAND` | `swift test` | Swift test command. |

## Parent/child delegation

Relationships live in repository variables, not tracked files. See
[Parent/child delegated execution](parent-child-delegation.md) for discovery and
verification.

| Repository variable | Default | Meaning |
| --- | --- | --- |
| `CONTINUUM_ROLE` | unset | `parent` or `child`. Unset keeps local scheduling behavior. |
| `CONTINUUM_CHILDREN` | empty | Parent-only JSON array of opaque child ids. |
| `CONTINUUM_CHILD_ID` | empty | Child-only id present in the parent's allow-list. |
| `CONTINUUM_PARENT` | empty | Child-only exact `owner/repository` of the parent. |
| `CONTINUUM_VALIDATION_SCRIPT` | empty | Child-only repository-relative `.sh` path run from the base branch. |
| `CONTINUUM_CHILD_WORKER_WORKFLOW` | `continuum-child-worker.yml` | Parent-side child worker caller. |
| `CONTINUUM_CHILD_REVIEW_WORKFLOW` | `continuum-child-review.yml` | Parent-side child task review caller. |
| `CONTINUUM_CHILD_PR_REVIEW_WORKFLOW` | `continuum-child-pr-review.yml` | Parent-side child PR review caller. |
| `CONTINUUM_CHILD_DISPATCH_WORKFLOW` | empty | Optional downstream workflow woken after a parent reconcile pass. |

The parent's `TAP_PAT` needs access to the child repositories and their Actions
variables. Setting a role variable adds or removes no workflow files.

## Secrets

Secret names are part of the contract and never change per consumer.

| Secret | Required when | Purpose |
| --- | --- | --- |
| `TAP_PAT` | Generic workflows need authenticated GitHub writes | Classic PAT with `repo` + `workflow` scopes. The `workflow` scope lets OpenCode task branches include `.github/workflows/**` changes; publication still goes through a PR and never writes directly to `main`. |
| `RENDER_API_KEY` | Render execution is enabled | Render API credential. |
| `CHILD_RUNTIME_TOKEN` | Direct parent reusable workflow invocation | Delegated child access; installed parent callers map their configured parent credential. |
| `CHILD_RUNTIME_REPOSITORIES` | Legacy parent configuration only | Optional compatibility input for child repository relationships. |

Every installed parent execution stub fills `CHILD_RUNTIME_TOKEN` from the parent's own `TAP_PAT`; metadata-only cleanup uses the caller `GITHUB_TOKEN` and receives no child credential. Direct reusable-workflow callers may supply `CHILD_RUNTIME_TOKEN` themselves.

`RENDER_API_KEY` is not `TAP_PAT` and has no fallback: a GitHub token is not
accepted by the Render API. `secrets.GITHUB_TOKEN` is minted automatically and
is not a credential a consumer defines. The optional Swift profile defines no
signing or release secrets; those belong to the consumer.

Installed callers declare the GitHub token permissions their jobs need (for
example `actions: write`, `contents: write`, `issues: write`,
`pull-requests: write`, and `statuses: write` for the relevant controllers).
Reducing them can break dispatch, label, status, or merge behavior. Mutation
paths that must fan out use `TAP_PAT`; read-only controller paths use the
run-scoped `GITHUB_TOKEN`. The declarations live in each installed caller under
[`.github/caller-stubs/`](../.github/caller-stubs/) and are consumed by the
reusable workflows under [`.github/workflows/`](../.github/workflows/).

## Minimal setup

1. Install `core` at the chosen revision.
2. Keep one project-owned `.github/workflows/ci.yml` named `CI` that calls
   `kodmial/continuum/.github/workflows/continuum-validation.yml@<ref>`.
3. Supply project build/test/toolchain behavior through validation inputs or
   `CONTINUUM_*` variables; do not edit an installed caller.
4. Add `TAP_PAT` for authenticated writes and configure a model route
   (`OPENCODE_MODELS`, or legacy `OPENCODE_MODEL`) for coding/review.
5. Select `CONTINUUM_REVIEW_PROVIDER` (`none`, `coderabbit`, or `pr-agent`) and
   configure branch protection separately if a hard merge gate is required.
6. Install `tech` only for an optional technology profile and `parent` only for
   delegated execution. Re-running the same install is idempotent.

No consumer source path, product name, release repository, or signing secret is
part of Continuum.

## Deprecated settings

| Setting | Status |
| --- | --- |
| `CONTINUUM_IMAGE_DIGEST` | Accepted legacy profile digest; not a hosted-cache or warm-runner pin. Use the verified `agent-toolchain` cache path. |
| `require_coderabbit` input / old provider booleans | Compatibility no-ops. `CONTINUUM_REVIEW_PROVIDER` is authoritative. |
| `child_owned_marker`, `legacy_child_owned_marker` scheduler inputs | Deprecated compatibility inputs; issue-body markers no longer affect admission. Use `CONTINUUM_ROLE` and the parent/child variables. |
| Set name `swift` | Rejected. Use `tech`. Bare install now defaults to `core`. |
