# Continuum

Continuum is a reusable **GitHub Actions control plane for autonomous,
gated issue-to-PR work**. It turns an admitted issue into a reviewed pull
request and merges it only when the current HEAD passes the configured gates:

```text
admitted issue -> scheduler -> OpenCode implementation -> pull request
      -> CI -> optional review -> repair / re-review -> exact-HEAD merge gate -> merge
```

Continuum is not a hosted service and not a replacement for GitHub Actions. It
is a set of reusable `workflow_call` workflows plus thin `continuum-*.yml`
callers that you install into a repository. The implementation lives in this
repository; consumer repositories keep only the caller files, their own
`ci.yml`, and their own project configuration.

**Who this is for:** maintainers of GitHub repositories who want autonomous
implementation with explicit human admission, pull-request-only writes, and
merge gates they can audit. You should be comfortable with GitHub Actions,
YAML, repository variables/secrets, and branch protection.

**Start with one repository.** A single-repository install is the basic
scenario. Parent/child delegated execution is an advanced, optional layer.

## Prerequisites

- A GitHub repository with Actions enabled and permission to add workflows.
- A runner. Core defaults to `ubuntu-latest`; Linux is the supported default.
  GitHub-hosted runner usage can consume GitHub Actions minutes depending on
  your plan.
- A **classic** personal access token stored as `TAP_PAT`, with `repo` and
  `workflow` scopes, for the lifecycle workflows that need authenticated
  GitHub writes.
- Branch protection (recommended and required for the stated safety model):
  no direct pushes to `main`, required pull requests, required current-HEAD
  checks, no routine bypass, and force-push/deletion blocked.
- Optional review provider: CodeRabbit, or Continuum's optional PR-Agent
  stack. No paid provider key is part of the core contract.
- Optional `RENDER_API_KEY` only if you enable the Render execution controller.

## Status and limitations

- This is open-source automation under active development. There is no uptime,
  latency, reliability, or cost SLA.
- Core workflows target free anonymous OpenCode models. Free routes can be
  rate-limited, unavailable, or changed by their provider; they are not a
  zero-cost or always-available guarantee.
- Linux is the qualified hosted-runner default. Do not assume macOS or Windows
  hosted-cache support.
- Review and coding routes are not all equally proven. See
  [what has been live-qualified](#what-has-been-live-qualified) before relying
  on a route for production work.
- Continuum cannot protect `main` by itself. Server-side branch protection and
  repository access controls remain your responsibility.
- The first-run walkthrough is a reproducible procedure, not a claim that an
  external sandbox run was performed in this repository generation.

## Safety model

1. **Human admission is explicit.** Set `AUTOMATION_READY_LABEL` (for example
   to `automation:ready`) before opening issues. When the variable is set, the
   scheduler auto-dispatches only issues carrying that label. When it is empty,
   the scheduler keeps its historical behavior and may enqueue any eligible open
   issue. Qualification dispatch and an owner's manual `/oc` command do not
   require the label.
2. **Automation writes through pull requests.** Installed workflows create
   branches and PRs; they do not push directly to `main`.
3. **Every merge is exact-HEAD.** CI and review evidence must belong to the
   current PR HEAD. Movement invalidates stale evidence.
4. **Repairs are bounded.** A failing CI or unresolved review can trigger a
   bounded repair path, followed by fresh CI and review. No path guarantees
   that a review always approves or that a repair always succeeds.
5. **Credentials stay named and scoped.** `TAP_PAT` is the lifecycle credential;
   `RENDER_API_KEY` is a separate Render-only credential. Never add a paid
   provider key to core.
6. **Use an isolated test repository first.** Start with a disposable
   repository and no valuable open issues, especially while tuning labels and
   runner settings.

### Owner-armed recovery after a prerequisite

An issue that was paused by an infrastructure failure can be released by one
owner-authored marker line on that issue:

```html
<!-- continuum-resume-after:v1 predecessor=374 -->
```

The line must be the first line of a comment from the repository owner as
reported by the GitHub API; a bot, member, model, or prose-only mention cannot
arm it. The marker is metadata, never an OpenCode command. It authorizes only
its own paused+ready target, and only after the named predecessor issue is
closed as completed through a bound implementation pull request that was
actually merged to the default branch with exact-HEAD `CI`, Contract Gate, and
independent clean-review evidence. The binding is the canonical
`opencode/issue<N>-` branch, or — for an owner-authored normal fix branch — the
exact `continuum-task-context` and `continuum-task-snapshot-ref` markers in the
PR body verified against the durable snapshot comment on the predecessor issue;
titles are never used. Continuum then removes only that target's
`automation:paused` label and lets the existing checkpoint/watchdog recovery
path continue; it does not edit the task snapshot, start a new generation, or
resume any other paused issue. While the predecessor is incomplete the marker
is a `WAIT`: the pause label stays in place even when an old scheduler lease
expires. A post-marker recovery attempt is a bounded fence, so a failed
automatic recovery stays paused instead of looping.

## What this adds beyond plain GitHub Actions

Plain workflow YAML gives you runners and triggers. You still have to assemble
the lifecycle. Continuum supplies:

- a scheduler with admission labels, leases, WIP limits, blockers, and
  fail-closed reconciliation;
- issue implementation and repair through OpenCode, with branch/PR publication
  instead of direct writes;
- one project-owned `CI` contract backed by reusable validation, with a
  deterministic repair hand-off;
- optional CodeRabbit or PR-Agent review, exact-HEAD review evidence, and a
  provider-neutral merge gate;
- thin, named callers installed by set, with pinning and safe superseded-caller
  pruning;
- optional parent/child delegation that keeps concrete child relationships in
  repository variables rather than tracked files.

## Architecture

```text
                    GitHub repository
  issue (admitted)        branch + PR        CI / review / gates
        |                     ^                      |
        v                     |                      v
  Issue scheduler  -->  OpenCode agent  -->  CI  -->  review?
        |                     ^                      |
        |                     |                      v
        +---- repair / re-review loop <------ repair controller
                                                     |
                                                     v
                                          exact-HEAD merge gate -> merge
```

Continuum's reusable workflows run from `kodmial/continuum` at the revision you
pin. The consumer repository owns only the caller stubs, its own `ci.yml`, and
its configuration. The detailed lifecycle routes are documented in
[docs/contract-qualification.md](docs/contract-qualification.md) and
[docs/provider-lifecycle-ownership.md](docs/provider-lifecycle-ownership.md).

## Start here

- **[Quick Start](docs/quickstart.md)** — a safe, copy-paste walkthrough for a
  fresh isolated test consumer, including the negative admission check.
- **[Example minimal consumer](examples/minimal-consumer/)** — a real sample
  project and its project-owned `ci.yml`.
- [Consumer configuration variables](docs/consumer-variables.md) — the complete
  variable contract.
- [Parent/child delegated execution](docs/parent-child-delegation.md) — the
  advanced multi-repository scenario.
- [PR-Agent review](docs/pr-agent-opencode.md) — the optional review stack.
- [Contract qualification](docs/contract-qualification.md) — the exact-HEAD gate
  model.

## Single-repository setup (basic)

1. Create a **fresh isolated test repository** and copy the sample files from
   `examples/minimal-consumer/`, including its `.github/workflows/ci.yml`.
2. Inspect `install.sh` and pin Continuum to a reviewed branch, tag, or full
   commit SHA.
3. Install the core callers.
4. Set `AUTOMATION_READY_LABEL=automation:ready` **before opening any issue**.
5. Add `TAP_PAT`; add `RENDER_API_KEY` only if you use Render execution.
6. Configure branch protection for `main`.
7. Open an ordinary issue without the ready label and confirm it is not
   dispatched. Then open a harmless issue with the label and follow it through
   the run, PR, CI, optional review, and merge.

The exact commands and expected observations are in
[docs/quickstart.md](docs/quickstart.md).

## Install

```sh
# Default set (core) from a local checkout
bash install.sh /path/to/consumer

# Pin a branch, tag, or full commit SHA
bash install.sh /path/to/consumer <ref> core

# Opt into the consumer-neutral technology library
bash install.sh /path/to/consumer <ref> tech

# Add parent/child delegated execution
bash install.sh /path/to/consumer <ref> parent

# Also prune superseded callers without a terminal confirmation
bash install.sh /path/to/consumer <ref> core --yes
```

The installer fetches caller templates from the requested revision and rewrites
their Continuum references consistently. `--yes` may appear in any position.
`CONTINUUM_INSTALL_ASSUME_YES=1` is the equivalent non-interactive opt-in.
Installing into Continuum's own checkout is refused.

| Set | Installs |
| --- | --- |
| `core` (default) | The task-domain callers installed by the `CORE_STUBS` array in `install.sh`: scheduling, implementation/repair, optional review controllers, validation hand-off, and merge. One legacy compatibility stub in `.github/caller-stubs/` (`continuum-opencode-unresolved.yml`) is intentionally not installed. |
| `tech` | The caller under `.github/caller-stubs/tech/`. This is an opt-in technology library that Continuum never triggers itself; it currently contains a consumer-neutral Swift validation profile. |
| `parent` | The callers under `.github/caller-stubs/parent/`. These are the delegated child worker/review/pr-review/cleanup wrappers. |

The set arrays in `install.sh` are the authoritative inventory. This README does
not duplicate a hand-maintained workflow list or a caller count.

### CI ownership

The consumer owns exactly one primary `.github/workflows/ci.yml` whose workflow
`name:` is `CI`. That project-owned workflow calls the shared
`continuum-validation.yml` engine and supplies project-specific hooks, tools, and
versions. The installer never creates a second CI workflow and never replaces
`ci.yml`.

Example:

```yaml
name: CI
on:
  push:
  pull_request:
  workflow_dispatch:
permissions:
  actions: write
  contents: read
  pull-requests: read
  statuses: write
jobs:
  validate:
    uses: kodmial/continuum/.github/workflows/continuum-validation.yml@main
    with:
      continuum_ref: main
```

A complete runnable example is in
[`examples/minimal-consumer/.github/workflows/ci.yml`](examples/minimal-consumer/.github/workflows/ci.yml).

### Upgrading and safe pruning

Re-run the installer for every set you use. That rewrites the stored stubs and
can remove callers Continuum no longer ships. Deletion is fenced:

1. a file is a candidate only if it is named `continuum-*.yml` **and** contains
   a real `uses:` call to this repository;
2. the exact list is confirmed at a terminal;
3. non-interactive installs delete nothing and only report what they would
   remove;
4. `--yes` or `CONTINUUM_INSTALL_ASSUME_YES=1` is the explicit opt-out;
5. installing into Continuum's own checkout is refused before any write.

Do not hand-edit an installed `continuum-*.yml` caller. Put project-specific
behavior in a project-owned workflow instead.

### Optional external periodic wake

Native GitHub `schedule` needs no extra setup. If you want a manual
cron-job.org backstop for the installed Issue scheduler, see
[docs/cron-job-org.md](docs/cron-job-org.md). It is an optional owner step after
merge, not an installer feature or a new credential requirement.

## Advanced: delegated parent/child execution (optional)

In a parent repository, install `core` plus `parent`, then set
`CONTINUUM_ROLE=parent`, `CONTINUUM_CHILDREN`, and a runtime PAT with access to
the child repositories. In each child, install `core`, set
`CONTINUUM_ROLE=child`, `CONTINUUM_CHILD_ID`, and `CONTINUUM_PARENT`, and keep
the relationship values in repository variables, not tracked files.

The ordinary core `Issue scheduler` owns parent/child dispatch. The `parent` set
adds only child execution wrappers. A child with `CONTINUUM_ROLE=child` never
schedules local work; it verifies the relationship and wakes the parent. See
[docs/parent-child-delegation.md](docs/parent-child-delegation.md) for the full
contract, discovery rules, and failure semantics.

## Secrets and variables

Secret names are part of the interface and never change per consumer.

| Secret | Required when | Purpose |
| --- | --- | --- |
| `TAP_PAT` | Lifecycle workflows need authenticated GitHub writes | Classic PAT with `repo` + `workflow` scopes. The `workflow` scope lets automation branches include `.github/workflows/**` changes; publication still goes through a PR and never directly to `main`. |
| `RENDER_API_KEY` | Render execution is enabled | Render API credential, read only by the render executor. It is a different credential from `TAP_PAT`, with no fallback; the controller fails explicitly when it is unset. |
| `CHILD_RUNTIME_TOKEN` | Direct parent reusable-workflow invocation | Delegated child access. Installed parent callers map it from the parent's own `TAP_PAT`. |
| `CHILD_RUNTIME_REPOSITORIES` | Legacy parent configuration only | Optional compatibility path for pre-variable child relationships. |

No paid provider key is part of the contract: **no workflow reads
`OPENCODE_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, or `GROQ.KEY`.** Core
runs on free anonymous models. `secrets.GITHUB_TOKEN` is minted by GitHub
automatically and is not a credential you define.

Repository variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUTOMATION_READY_LABEL` | empty | Optional admission label. When set, automatic implementation dispatches only issues carrying it. **When empty, any eligible open issue may be enqueued.** |
| `CONTINUUM_REVIEW_PROVIDER` | `none` | Authoritative review selector: `none`, `coderabbit`, or `pr-agent`. |
| `CONTINUUM_CI_REPAIR_WORKFLOWS` | `CI` | Comma- or newline-separated exact workflow names whose completed pull-request runs may dispatch an OpenCode `ci-fix`; the repair caller wakes broadly because a `workflow_run` filter cannot read repository variables. |
| `OPENCODE_MODEL` | free anonymous default | Optional model override for OpenCode. |
| `MAINTAINERS` | empty | Optional maintainer allow-list for controllers. |
| `AUTOMATION_WIP_LIMIT`, `AUTOMATION_LEASE_MINUTES`, `AUTOMATION_MAX_DISPATCH_ATTEMPTS` | scheduler defaults | Optional scheduler tuning. |

See [Consumer configuration variables](docs/consumer-variables.md) for the
complete validation, runner, lifecycle, delegation, review, Render, and
Swift-profile variable list.

## Failure diagnosis

| Symptom | Check first |
| --- | --- |
| An issue is not dispatched | `AUTOMATION_READY_LABEL` is set and the issue carries it; no pause/qualification/blocked label; no open blocker; WIP limit not exhausted; the scheduler run log shows the exact skip reason. |
| The scheduler ran but did nothing | That is expected for an unadmitted issue when `AUTOMATION_READY_LABEL` is configured. Check for `automation:in-progress`, the dispatch comment, and an `OpenCode agent` run to distinguish "skipped" from "dispatched". |
| No PR appears after dispatch | Missing or under-scoped `TAP_PAT`; Actions permissions; model/rate-limit failure in the OpenCode run; the issue has no actionable acceptance criteria. |
| CI never starts | The project workflow is not named `CI`, is not on the default branch, Actions is disabled, or the PR is from a fork without the required secrets. |
| Review never starts | `CONTINUUM_REVIEW_PROVIDER` is `none` or the selected provider is not configured. Optional review is not guaranteed to run or approve. |
| A green PR does not merge | Branch protection, required checks, exact-HEAD evidence, conflict state, or a required gate label/name. Read the auto-merge run log. |
| `workflow not found` after a Continuum upgrade | Reinstall every set you use, then review the prune output. |
| Repeated model or rate-limit failures | The free anonymous route can be unavailable. An explicit OpenCode 429 marker starts a bounded fresh-runner recovery checkpoint through a repository-local `workflow_dispatch` caller; a bounded timer catches up when `workflow_run` delivery is late or lost. This is not a Continuum SLA. |

## What has been live-qualified

This section deliberately separates deterministic coverage from live evidence.

| Route | Honest status |
| --- | --- |
| Continuum's own contract suite, `CI`, Contract Gate, and Contract qualification | Live on this repository's pull requests. They check the exact PR HEAD. |
| Hosted Linux OpenCode/PR-Agent toolchain | Cold/warm restore and a small anonymous-model inference were live-verified. This is not a complete real-consumer coding or review cycle; see [docs/agent-toolchain-cache.md](docs/agent-toolchain-cache.md). |
| OpenCode issue implementation through an installed `core` set | Implemented and contract-tested. The Quick Start end-to-end walkthrough is **not live-verified for an external consumer in this repository generation**. |
| CodeRabbit review route | Integrated and tested. No general claim that every review succeeds or approves. |
| PR-Agent review route | Optional and disabled by default. The canary harness and offline checks exist; a live canary rerun is maintainer opt-in and is not asserted here. See [docs/pr-agent-canary.md](docs/pr-agent-canary.md). |
| Parent/child delegated execution | Contract-tested. External live parent/child operation is not part of this Quick Start and is not asserted here. |

The default-unadmitted behavior is covered by a deterministic fixture in
[`examples/minimal-consumer/verification/`](examples/minimal-consumer/verification/).
No consumer repository was created or changed to produce this documentation.

## Repository layout

| Path | Purpose |
| --- | --- |
| `.github/workflows/continuum-*.yml` | Continuum-owned reusable (`on: workflow_call`) engine workflows. The directory is the authoritative workflow inventory. |
| `.github/workflows/{ci,opencode,automation}.yml` and other project-owned entries | Workflows used only to dogfood Continuum on this repository. They intentionally carry no `continuum-` prefix. |
| `.github/caller-stubs/*.yml` | Thin caller templates for the default `core` set; the `CORE_STUBS` array in `install.sh` is authoritative, and one legacy compatibility stub is intentionally not installed. |
| `.github/caller-stubs/tech/*.yml` | Thin callers for the opt-in technology library. |
| `.github/caller-stubs/parent/*.yml` | Thin callers installed by the `parent` set. |
| `install.sh` | Installs one set into a consumer repository. |
| `src/continuum/` | Dependency-free Python engine. |
| `.github/scripts/` | Delegation runtime and shared PR lifecycle scripts. |
| `scripts/` | Continuum contract tests. |
| `docs/` | Design documentation and the Quick Start. |
| `examples/minimal-consumer/` | Runnable sample consumer project, CI workflow, and admission fixture. |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full list. Quick version:

```sh
ruby -E UTF-8 scripts/test-continuum.rb
bash -n install.sh
actionlint -shellcheck= -pyflakes= .github/workflows/*.yml .github/caller-stubs/*.yml .github/caller-stubs/tech/*.yml .github/caller-stubs/parent/*.yml examples/minimal-consumer/.github/workflows/*.yml

mkdir -p .opencode-tmp
export TMPDIR="$PWD/.opencode-tmp"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts -p 'test_delegation_runtime.py'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s examples/minimal-consumer/verification
```

Use `actionlint` 1.7.12 or newer.
