# Architecture and trust boundaries

This page maps the implementation that exists in this repository. It is not a
roadmap. Configuration values live in
[Consumer configuration](consumer-variables.md); installing, pinning, and
rolling back live in [Upgrading and rollback](upgrading.md).

Status labels used here:

- **Implemented** — the code exists and deterministic contract/unit tests cover
  it. This does not prove a particular consumer's live run.
- **Live-tested** — an executable path produced evidence in a real GitHub
  Actions run. A successful test suite alone is not live evidence.
- **Experimental** — implemented, but not qualified for the default hosted path
  and not recommended as a production dependency yet.
- **Planned** — not implemented. Nothing in this document relies on it.

| Area | Status | Confidence note |
| --- | --- | --- |
| Caller boundary, installer, validation, scheduler, OpenCode, CodeRabbit, PR-Agent, merge, recovery, parent/child | Implemented | Deterministic suites cover the contracts; each consumer's live environment still governs credentials, quotas, and branch protection. |
| This repository's `CI` and Contract qualification | Live-tested per run | A run is evidence only for the exact SHA it tested; an older green run is stale. |
| PR-Agent | Implemented; live-tested through opt-in canary evidence | Not globally enabled. A canary result does not promise that a free route stays available. |
| Non-GitHub prepared runtimes | Experimental | The Python engine exists, but the installed hosted path uses `github-hosted` and does not qualify cloud/macOS/Windows images. |
| Roadmap features | Planned: none documented | No current caller depends on a plan. |

## Caller and engine boundary

```text
consumer repository                              Continuum repository
───────────────────                              ────────────────────
project-owned .github/workflows/ci.yml  ───────▶ continuum-validation.yml
  (name: CI)                                       exact-HEAD validation

installed continuum-*.yml callers       ───────▶ reusable continuum-*.yml
  scheduler, OpenCode, review,                     engines and controllers
  repair, recovery, merge                          │
                                                   ├─ src/continuum/ (Python)
GitHub labels, comments, statuses,  ◀─────────────┤  .github/scripts/ (runtime)
branches, and pull requests                       └─ .github/actions/ (toolchain)
```

Continuum owns the reusable `workflow_call` workflows named
`continuum-*.yml`, the thin caller templates under `.github/caller-stubs/`,
the installer, and the shared engine code. A consumer owns its triggers,
`.github/workflows/ci.yml`, toolchain commands, variables, secrets, and product
release/signing policy. The installer writes each stored caller name verbatim;
an installed `continuum-*.yml` is not hand-edited in a consumer repository.

Two boundaries are deliberate:

- **Validation is project-owned CI.** The consumer keeps one workflow named
  `CI` that calls
  [`continuum-validation.yml`](../.github/workflows/continuum-validation.yml).
  The installer does not create a second CI workflow.
- **Every installed caller pins one Continuum revision.** A caller's `uses:`
  line and its `continuum_ref`/`engine_ref` input name the same ref, so the
  workflow and its fallback scripts move together. `main` is a live branch; a
  commit SHA is reproducible. See [Upgrading and rollback](upgrading.md).

The layers are installed independently: `core` (task-domain lifecycle), `tech`
(opt-in technology profiles, currently the Swift CI caller), and `parent`
(delegated child execution callers). Installing one layer does not install
another.

## Issue admission and coding

The core scheduler
([`continuum-issue-scheduler.yml`](../.github/workflows/continuum-issue-scheduler.yml))
is an **implemented** reconciler. It runs from scheduled and event-driven
caller triggers, reads issues and pull requests through the GitHub API, and
applies repository role, labels, reservations/leases, open work, priority, WIP,
and dispatch-attempt state. It does not execute issue code and does not create
a pull request itself.

Admission is configured by repository variables, not by editing the caller.
`automation:ready` is an optional admission gate; when unset, the scheduler
keeps its normal behavior. Manual owner `/oc` and `/opencode` commands and
mandatory qualification dispatch do not require the ready label. The default
automatic hand-off is an owner-style `/oc` comment; a consumer that installs
its OpenCode caller without an `issue_comment` trigger can select direct
workflow dispatch instead.

[`continuum-opencode.yml`](../.github/workflows/continuum-opencode.yml) is the
**implemented** coding executor. It handles issue implementation, CodeRabbit
follow-up, CI repair, conflict resolution, and mandatory qualification. It
creates a task branch/pull request with `TAP_PAT`, and it selects a model from
the caller's trusted `OPENCODE_MODELS` pool (or the legacy single
`OPENCODE_MODEL`) after verifying the route. This is a live path in normal
consumer use, but model availability, quota, and response time are external
services and are not guaranteed.

## Exact-head CI, review, and merge

1. The project-owned `CI` workflow calls validation. Validation resolves the
   exact pull-request head SHA and publishes its status context against that
   SHA. On a later push the old SHA is no longer the current head.
2. Review is selected by `CONTINUUM_REVIEW_PROVIDER`:
   - `none` — no provider review stack.
   - `coderabbit` — the label, retry, and unresolved-finding stack.
   - `pr-agent` — the runner-local OpenCode-backed PR-Agent stack.
3. The merge path re-reads the current PR head and its checks immediately
   before writing. The generic reconciler uses an atomic `sha` guard; the
   PR-Agent merge gate accepts only a complete persistent review for the same
   head. PR-Agent mode leaves the generic reconciler in sync-only mode.
4. Exact-head evidence protects against merging a stale commit, but it is not
   a substitute for server-side branch protection. Continuum does not configure
   GitHub rulesets. A consumer that needs a hard no-direct-push or
   required-check guarantee must enable branch protection itself. See
   [Contract qualification](contract-qualification.md) for this repository's
   own gate.

The **implemented** review providers are:

- **CodeRabbit** — an external GitHub app. Continuum manages readiness labels,
  rate-limit retries, and UNRESOLVED batches; it does not install the app.
- **PR-Agent** — a pinned PyPI release installed on the runner, talking to
  `opencode serve` through a loopback compatibility bridge. It needs a trusted
  OpenCode route and no paid-provider key. It is **live-tested only through its
  opt-in canary**; enabling it for normal PRs is a consumer choice, and the canary
  does not promise that a free model will always be available.

No core workflow requires, reads as input, or forwards a paid provider key.
The PR-Agent canary reads the presence of known paid-key variables only to
refuse the run.

## Recovery

Recovery is **implemented** but bounded:

- The scheduler reconciles leases and dispatch attempts; exhausted work can be
  paused or retried according to configuration.
- [`continuum-opencode-watchdog.yml`](../.github/workflows/continuum-opencode-watchdog.yml)
  recovers failed OpenCode issue runs. An explicit OpenCode 429 marker can start
  the same workflow again on fresh hosted runners within a configured restart
  budget. This is a bounded retry, not a guarantee that a provider-wide quota
  outage recovers.
- [`continuum-pr-agent-recovery.yml`](../.github/workflows/continuum-pr-agent-recovery.yml)
  reconciles PR-Agent work for the exact PR head.
- A project-owned global PR watchdog in this repository is a liveness wake-up
  only; it owns no review, merge, or retry decision.

Cron and scheduled events are best-effort. GitHub can delay or drop scheduled
delivery, so normal event triggers remain the primary wake-ups and cron is a
backstop.

## Optional parent/child execution

Parent/child is **implemented** optional behavior driven by repository
variables, never by tracked relationship files:

- A parent has `CONTINUUM_ROLE=parent` and `CONTINUUM_CHILDREN`.
- A child has `CONTINUUM_ROLE=child`, `CONTINUUM_CHILD_ID`, and
  `CONTINUUM_PARENT`; the parent verifies the relationship in both directions.
- The parent scheduler discovers verified child work and dispatches child
  worker/review callers. A child keeps the core caller but does not schedule
  local automatic work; manual owner commands still use the local OpenCode
  path.
- With PR-Agent selected, the parent is the only automatic PR-Agent execution
  plane for delegated child pull requests. Otherwise the independent child
  review path remains.

See [Parent/child delegated execution](parent-child-delegation.md) for the
variables, verification, and merge rules.

## Runtime and trust limits

- **Trusted revision.** Controllers check out the Continuum revision named by
  the caller, not the pull-request head. `pull_request_target` callers in this
  design read metadata and trusted workflows; they must not be changed to
  execute untrusted head code. The OpenCode coding path is different: it
  intentionally checks out and modifies the task branch with a write-capable
  token.
- **`TAP_PAT`.** A classic PAT with `repo` and `workflow` scopes. It can push
  branches, change workflow files on those branches, and drive merges. It is
  the main credential boundary: protect it as a repository secret and keep
  branch protection enabled. `RENDER_API_KEY` is a separate credential and is
  never replaced by `TAP_PAT`.
- **Untrusted issue and pull-request text.** Issue titles, bodies, comments,
  diffs, and review text are untrusted input. They are shown to coding/review
  models and can influence generated changes. Treat model output like an
  untrusted contributor's pull request: exact-head CI, review, and branch
  protection are the controls.
- **Runners and networks.** GitHub-hosted runners are fresh per job. There is no
  pre-created live VM pool and no guarantee of a unique public IP between jobs.
  A provider may reuse an address.
- **Prepared cloud runtimes.** The Python runtime engine supports strict
  ephemeral profiles and multiple provider backends, but the installed GitHub
  workflows use `github-hosted` runners. The non-GitHub cloud provisioning path
  is **experimental** and is not qualified by the hosted cache tests; macOS and
  Windows prepared images are likewise outside those tests.
- **No planned dependency.** No current caller depends on a planned component.
  If a feature is not implemented in the workflows or engine above, it is not
  part of this architecture.
