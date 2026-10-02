# Continuum

Continuum is a **reusable GitHub Actions control plane**. It holds the actual
automation logic for CI, review, release, packaging, and parent/child delegated
execution as `workflow_call` workflows. Consumer repositories hold only thin
`continuum-*.yml` callers that call into this repository — they never copy the
engine.

## Repository layout

| Path | Purpose |
| --- | --- |
| `.github/workflows/continuum-*.yml` | Continuum-owned reusable (`on: workflow_call`) engine workflows. |
| `.github/workflows/{ci,opencode,automation}.yml` | Project-owned entry workflows used only to dogfood Continuum on this repository. |
| `.github/caller-stubs/*.yml` | Thin core callers installed by the default `core` set (task-domain). |
| `.github/caller-stubs/tech/*.yml` | Thin callers for the opt-in technology library (`tech` set). |
| `.github/caller-stubs/parent/*.yml` | Thin callers installed by the `parent` set. |
| `install.sh` | Installs one set into a consumer repository. |
| `src/continuum/` | Dependency-free Python engine (config + YAML subset). |
| `.github/scripts/` | Delegation resolver/runtime and the global instructions installer. |
| `scripts/` | Continuum contract tests. Product build/release scripts stay in consumers. |
| `docs/` | Design documentation (see [parent/child delegation](docs/parent-child-delegation.md)). |

## Reuse model

- Reusable engines are `on: workflow_call`. Lifecycle engines that need a
  consumer trigger have install-managed caller stubs; shared capability engines
  such as validation may be called directly from a project-owned entry point.
- Every installed caller carries the **`continuum-` prefix**. In a consumer
  repository, a `continuum-*.yml` file is Continuum-owned and is never hand-edited
  there; any other workflow belongs to the project.
- The consumer's primary `.github/workflows/ci.yml` is **project-owned** and is
  the only primary workflow named `CI`. It calls the shared
  `continuum-validation.yml` engine and supplies project-specific hooks/tooling.
  The core installer never creates a second CI workflow.
- Controllers and `workflow_run` triggers match on workflow `name:` values, so
  names are part of the interface and stay stable.
- Callers pin a revision. `install.sh` rewrites the `@main` in `uses:` and the
  `continuum_ref`/`engine_ref` inputs to the requested ref, so one value governs
  both the workflow and its fallback scripts. The rewrite covers a `uses:` line
  at either level — job-level (`call:` then `uses:`) and step-level (`- uses:`) —
  and keeps a trailing YAML comment. An unrelated `main` elsewhere in the file,
  a third-party action, and an already-pinned Continuum ref are all left alone.

### Fixed contracts

These names are the interface and never change per consumer:

- the `TAP_PAT` repository secret (classic PAT, `repo` + `workflow` scopes) —
  every set expects a secret with exactly this name;
- the `continuum-` prefix on every **Continuum-owned reusable workflow and installed caller stub**, and the
  `continuum-tech-<tech>-` prefix for opt-in library callers; project-owned entry workflows are deliberately outside this namespace;
- the installer sets (`core`, `tech`, and `parent`);
- the `CONTINUUM_*` repository-variable names.

Continuum is technology-neutral: `core` ships the task-domain controllers
(issue scheduling, PR creation/repair/recovery, PR review, PR analysis,
auto-merge, delegation) every project needs; `tech` is a separate, opt-in
library of technology-specific workflows — the `continuum-tech-<tech>-` prefix
(`continuum-tech-<tech>-<name>.yml`) marks them as a library that Continuum
itself never triggers; `parent` drives delegated execution for any technology.

Every **Continuum-owned reusable workflow** and every installed caller stub is named `continuum-<name>.yml`, and
the installer writes each stub's stored name verbatim — it adds no prefix of
its own. The technology library uses the longer
`continuum-tech-<tech>-<name>.yml` name, where the `continuum-tech-<tech>-`
prefix marks the opt-in layer. Project-owned workflow entry points are outside that ownership namespace.
In this repository, `.github/workflows/ci.yml`, `.github/workflows/opencode.yml`,
and `.github/workflows/automation.yml` are project-owned entry workflows forming
the minimal self-dogfooding ingress; they deliberately carry no `continuum-`
prefix and call the same reusable `continuum-*.yml` engines external consumers
use. External consumer integration and caller stubs are unchanged.

## Install

```sh
# Local working tree (default set: core)
bash install.sh /path/to/consumer

# Pin a specific revision (branch, tag, or full commit SHA)
bash install.sh /path/to/consumer <ref> core

# Opt into the consumer-neutral Swift validation profile
bash install.sh /path/to/consumer <ref> tech

# Add parent/child delegated execution to any repository
bash install.sh /path/to/consumer <ref> parent

# Also remove callers Continuum no longer ships, without a confirmation prompt
bash install.sh /path/to/consumer <ref> core --yes
```

The installer fetches caller templates from the requested `<ref>` and rewrites
their Continuum references consistently. Use `main` when consumers should track
the live shared implementation, or an explicit ref when a deployment requires
pinning.

| Set | Installs |
| --- | --- |
| `core` (default) | 14 callers covering OpenCode, issue scheduling, qualification, CodeRabbit, PR Agent, and auto-merge. Validation is a shared engine called by the project-owned `ci.yml`, not a second installed CI caller. |
| `tech` | 1 caller: the optional consumer-neutral Swift CI profile. |
| `parent` | 4 callers: `continuum-child-worker.yml`, `continuum-child-review.yml`, `continuum-child-pr-review.yml`, and `continuum-child-run-cleanup.yml`. The ordinary core `Issue scheduler` owns parent/child dispatch; the cleanup caller removes completed delegated public runs. |

The `parent` and `tech` sets add **only** their own callers; each preserves
the consumer's own CI, release, and scheduling workflows. The `core` set also
preserves `ci.yml`: validation is integrated by making that project-owned CI a
thin caller of `kodmial/continuum/.github/workflows/continuum-validation.yml@main`.
Any value other than `core`, `tech`, or `parent` is rejected.

### CI ownership

Do not hand-edit an installed `continuum-*.yml` caller. If a consumer needs a
project-specific trigger, toolchain setup, or hook, keep that behavior in a
project-owned file (for example `ci.yml`, `nanodictate-packaging-repair.yml`)
and call the reusable Continuum engine from there.

The primary CI contract is intentionally asymmetric:

1. the consumer owns exactly one `ci.yml` / workflow named `CI`;
2. that workflow calls the reusable `continuum-validation.yml@main` engine;
3. project-specific commands/tool versions are inputs or repository variables;
4. `install.sh` owns only the `continuum-*.yml` caller set and never creates
   another primary CI.

This makes a repeated core install idempotent: the installer converges its own
callers without replacing the consumer's CI or duplicating its triggers.

## Upgrading

The technology set was renamed from `swift` to `tech`. Replace

```sh
bash install.sh <path-to-consumer-repo> <ref> swift
```

with

```sh
bash install.sh <path-to-consumer-repo> <ref> tech
```

`core` is the new default set, so `bash install.sh <path-to-consumer-repo> <ref>`
now installs the core layer where the old command installed the technology
library. `core` and `tech` are separate installs: installing `core` does not
install `tech`, and a consumer that wants the technology library runs the `tech`
command as well. Passing the old `swift` value is rejected with
`invalid set: swift (the old 'swift' set is now 'tech')`.

Every core and parent workflow file now carries the `continuum-` prefix, so an
already installed layer does not update itself: the stubs in the consumer still
carry `uses:` lines pointing at the old unprefixed file names and would fail with
`workflow not found` after the merge. Before updating Continuum, a consumer must
reinstall every set it uses,

```sh
bash install.sh <path-to-consumer-repo> <ref> core
bash install.sh <path-to-consumer-repo> <ref> tech
bash install.sh <path-to-consumer-repo> <ref> parent
```

Reinstalling rewrites those stubs and also removes any caller that no layer ships
any more, so a renamed or dropped workflow cannot leave a stale caller running
beside its replacement. Because that is the only destructive thing the installer
does, it is fenced three ways:

1. **Ownership.** The prefix alone is not evidence. A file is only a candidate if
   it is named `continuum-*.yml` *and* its body references this repository. A
   consumer's own `continuum-experiment.yml` is left alone even with `--yes`.
2. **Confirmation.** At a terminal the exact list is printed and the install waits
   for a `y`. Non-interactively — CI, a pipe, `< /dev/null` — the default is to
   delete **nothing**: the install reports what it would remove and exits. A CI
   install can never silently drop a file.
3. **Self-install.** Installing into Continuum's own checkout is not a consumer
   install, so the prune is skipped there entirely.

To prune unattended, opt in explicitly:

```sh
bash install.sh <path> <ref> core --yes
CONTINUUM_INSTALL_ASSUME_YES=1 bash install.sh <path> <ref> core
```

`--yes` may appear in any position and is removed before the positional
arguments are parsed, so the documented argument shape stays `<path> <ref> <set>`.

Stubs must never be patched by hand: every installed stub is a `continuum-*.yml`
file carrying a `uses:` line, is owned by Continuum, and hand-editing one in a
consumer repository is forbidden.

## Reusable workflows

Workflow names are identical to the reusable file names unless noted.

| Workflow | Purpose | Notable extra inputs |
| --- | --- | --- |
| `continuum-tech-swift-ci.yml` | Consumer-neutral Swift profile over `continuum-validation.yml`; defaults to `macos-latest`, `swift build`, and `swift test`. (tech) | runner/build/test/prepare/validation overrides |
| `continuum-opencode.yml` | OpenCode agent (`name: OpenCode agent`). | `mode`, `pr_number`, `head_ref`, `review_id`, `run_id` |
| `continuum-opencode-repair.yml` | OpenCode repair controller. | — |
| `continuum-opencode-unresolved.yml` | Retry OpenCode on unresolved CodeRabbit findings. | — |
| `continuum-opencode-watchdog.yml` | Recover a failed issue implementation run (`name: OpenCode watchdog`). | `watched_workflow`, `max_recovery_retries`, `retry_marker`, `in_progress_label`, `pause_marker`, `dispatch_marker`, `timeout_minutes` |
| `continuum-issue-scheduler.yml` | Scheduled issue dispatch. | — |
| `continuum-auto-merge.yml` | Auto-merge reviewed pull requests. | `require_coderabbit` |
| `continuum-pr-agent.yml` | Optional PR Agent review over the runner-local OpenCode backend. | `enabled`, `api_base`, `model`, `max_tokens`, `opencode_model` |
| `continuum-add-review-label.yml` | Mark a PR ready for CodeRabbit. | — |
| `continuum-remove-review-label.yml` | Remove the ready label on sync. | — |
| `continuum-coderabbit-retry.yml` | Retry CodeRabbit after a rate limit. | — |
| `continuum-coderabbit-unresolved.yml` | Retry unresolved CodeRabbit findings. | — |
| `continuum-bootstrap-runtime-secret.yml` | Fetch an Actions secrets public key for secret bootstrapping. | — |
| `continuum-consumer-child-dispatcher.yml` | Legacy compatibility callee for already-installed standalone dispatchers; parent-role runs are skipped. | `worker_workflow`, `review_workflow`, `manual_pr_review_workflow`, `engine_ref` |
| `continuum-consumer-child-worker.yml` | Execute one delegated child task. | `child_id`, `task_number`, `model`, `max_agent_passes`, `python_version`, `engine_ref` |
| `continuum-consumer-child-review.yml` | Independent review of a delegated child task. | `child_id`, `task_number`, `pr_number`, `model`, `max_review_passes`, `python_version`, `engine_ref` |
| `continuum-consumer-child-pr-review.yml` | Independent review of a child pull request. | `child_id`, `pr_number`, `model`, `max_review_passes`, `python_version`, `engine_ref` |

Every reusable workflow also accepts `continuum_ref` (default `main`), the
revision that supplies fallback scripts.

## Secrets and variables

Secrets a consumer must provide, under exactly these names (secret names are
part of the contract and never change per consumer):

| Secret | Set | Used by | Purpose |
| --- | --- | --- | --- |
| `TAP_PAT` | `core`, `parent` | review, opencode, delegation | Classic PAT (`repo` + `workflow` scopes) for checkout/push/API. Also the child-runtime token the `parent` stubs forward. |
| `RENDER_API_KEY` | `core` | `continuum-render-executor.yml` | Render API key. A different credential from `TAP_PAT`, with **no** fallback: the controller fails explicitly when it is unset. |
| `CHILD_RUNTIME_TOKEN` | `parent` | `consumer-child-*` | Parent delegation token; the caller stubs map it from `TAP_PAT`. |
| `CHILD_RUNTIME_REPOSITORIES` | `parent` | `consumer-child-*` (optional) | Pre-variables compatibility path; see `docs/parent-child-delegation.md`. |

No paid provider key is part of this contract: **no workflow reads
`OPENCODE_API_KEY` or `GROQ_API_KEY`.** The core runs on free anonymous models
and requires no API key; `continuum-pr-agent.yml` is an opt-in review provider
backed by the same free OpenCode route through a runner-local compatibility
bridge, and it stays disabled unless `CONTINUUM_PR_AGENT_ENABLED` is `true`.
`secrets.GITHUB_TOKEN` is minted by GitHub
automatically, so it is not a credential a consumer defines.

Repository variables:

| Variable | Kind | Purpose |
| --- | --- | --- |
| `OPENCODE_MODEL` | optional | Default OpenCode model. |
| `MAINTAINERS` | optional | Maintainer allow-list for controllers. |
| `AUTOMATION_LEASE_MINUTES`, `AUTOMATION_MAX_DISPATCH_ATTEMPTS`, `AUTOMATION_WIP_LIMIT` | optional | Scheduler tuning. |

Delegation relationships are stored as repository variables — see below.

The complete project-agnostic validation, runner, lifecycle, delegation, and
optional Swift-profile variables are listed in
[Consumer configuration variables](docs/consumer-variables.md).

## Unified consumer contract

A new repository can install `core` and run the generic `CI` contract without
copying lifecycle logic. Core defaults to `ubuntu-latest` and auto-detects common
stacks; consumers can override runner plus prepare/build/test/validation/package/
release hooks, artifacts, status publication, and repair dispatch with
`CONTINUUM_*` variables. OpenCode also defaults to `ubuntu-latest`; projects
that require a different agent environment set `AUTOMATION_OPENCODE_RUNNER`
and `CONTINUUM_AGENT_PREPARE_COMMAND`.

Technology-specific behavior is opt-in. The `tech` set currently contains only
a generic Swift CI profile. Product release, signing, Homebrew/MacPorts or other
distribution policy is deliberately outside Continuum.

## Wiring the consumers

Concrete parent/child relationships are **not** stored in tracked files; they
live in GitHub Actions **repository variables**. See
[docs/parent-child-delegation.md](docs/parent-child-delegation.md) for the full
contract.

### Swift application consumer (example: any Swift app repository)

```sh
bash install.sh /path/to/myapp <sha> core
bash install.sh /path/to/myapp <sha> tech
```

- Keep the consumer's own `.continuum.yml` (the neutral `version: 1` file is
  sufficient when there is no tracked relationship).
- Configure only the runner/hooks the project needs; the Swift profile itself
  contains no application identity, signing, packaging, or release policy.
- Keep signing, package-manager manifests, publishing, release policy, and any
  product-specific CI in the consumer repository.

### Parent (delegation control plane, one example per child id)

```sh
bash install.sh /path/to/parent-plane <sha> core
bash install.sh /path/to/parent-plane <sha> parent
```

The `core` install supplies the one ordinary `Issue scheduler`; the
`parent` install adds only the child worker and review entry points.

- Repository variables: `CONTINUUM_ROLE=parent`,
  `CONTINUUM_CHILDREN=["<child-id>", ...]` (a JSON array of opaque child ids).
- Provide a runtime PAT secret with access to the child repositories and to their
  Actions variables.
- The ordinary `Issue scheduler` considers the parent's own issues and all
  verified child issues in one priority queue and one WIP budget. It wakes on
  its normal schedule/events and on completion of child task/review workflows.

### Child (a delegated repository, one id per child)

- A neutral `.continuum.yml` (`version: 1`).
- Repository variables: `CONTINUUM_ROLE=child`, `CONTINUUM_CHILD_ID=<id>` (must
  match an id in the parent's `CONTINUUM_CHILDREN`), `CONTINUUM_PARENT=<owner>/<repo>`
  (must equal the calling parent exactly), and optionally
  `CONTINUUM_VALIDATION_SCRIPT=<repo-relative .sh path>`.
- Every open issue is parent-routed automatically; priority labels are optional
  ordering hints, not admission requirements, and issue-body ownership markers
  are not required.
- The local issue scheduler remains installed but is fail-safe skipped while
  `CONTINUUM_ROLE=child`. Manual owner `/oc` and `/opencode` commands still
  use the existing local OpenCode workflow.
- A trusted validation script on the **base** branch is executed against the
  candidate worktree with a minimal `env -i` environment.
- Discovery is fail-closed: zero or multiple repositories matching the
  relationship abort the run. Visibility (public/private) is never a routing
  signal.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full list. Quick version:

```sh
ruby -E UTF-8 scripts/test-continuum.rb
bash -n install.sh
actionlint -shellcheck= -pyflakes= .github/workflows/*.yml .github/caller-stubs/*.yml .github/caller-stubs/tech/*.yml .github/caller-stubs/parent/*.yml

mkdir -p .opencode-tmp
export TMPDIR="$PWD/.opencode-tmp"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts -p 'test_delegation_runtime.py'
```

Use `actionlint` 1.7.12 or newer.
