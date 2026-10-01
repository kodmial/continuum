# Continuum

Continuum is a **reusable GitHub Actions control plane**. It holds the actual
automation logic for CI, review, release, packaging, and parent/child delegated
execution as `workflow_call` workflows. Consumer repositories hold only thin
`continuum-*.yml` callers that call into this repository — they never copy the
engine.

## Repository layout

| Path | Purpose |
| --- | --- |
| `.github/workflows/*.yml` | Reusable (`on: workflow_call`) workflows — the engine. |
| `.github/caller-stubs/*.yml` | Thin core callers installed by the default `core` set (task-domain). |
| `.github/caller-stubs/tech/*.yml` | Thin callers for the opt-in technology library (`tech` set). |
| `.github/caller-stubs/parent/*.yml` | Thin callers installed by the `parent` set. |
| `install.sh` | Installs one set into a consumer repository. |
| `src/continuum/` | Dependency-free Python engine (config + YAML subset). |
| `.github/scripts/` | Delegation resolver/runtime and the global instructions installer. |
| `scripts/` | Contract tests, release policy, and packaging-smoke fallback scripts. |
| `docs/` | Design documentation (see [parent/child delegation](docs/parent-child-delegation.md)). |

## Reuse model

- Every reusable workflow is `on: workflow_call`; the same file name is reused by
  its caller stub.
- Every installed caller carries the **`continuum-` prefix**. In a consumer
  repository, a `continuum-*.yml` file is Continuum-owned and is never hand-edited
  there; any other workflow belongs to the project.
- Controllers and `workflow_run` triggers match on workflow `name:` values, so
  names are part of the interface and stay stable.
- Callers pin a revision. `install.sh` rewrites the `@main` in `uses:` and the
  `continuum_ref`/`engine_ref` inputs to the requested ref, so one value governs
  both the workflow and its fallback scripts.

### Fixed contracts

These names are the interface and never change per consumer:

- the `TAP_PAT` repository secret (classic PAT, `repo` + `workflow` scopes) —
  every set expects a secret with exactly this name;
- the `continuum-` prefix on installed callers, and the `continuum-tech-<tech>-`
  prefix for opt-in library callers;
- the installer sets (`core`, `tech`, and `parent`);
- the `CONTINUUM_*` repository-variable names.

Continuum is technology-neutral: `core` ships the task-domain controllers
(issue scheduling, PR creation/repair/recovery, PR review, PR analysis,
auto-merge, delegation) every project needs; `tech` is a separate, opt-in
library of technology-specific workflows — the `continuum-tech-<tech>-` prefix
(`continuum-tech-<tech>-<name>.yml`) marks them as a library that Continuum
itself never triggers; `parent` drives delegated execution for any technology.

New core workflow files should be named `continuum-<name>.yml`. The core files
shipped today keep their historical unprefixed names (`opencode.yml`,
`pr-agent.yml`, …), which are listed explicitly in `scripts/test-continuum.rb`.

## Install

```sh
# Local working tree (default set: core)
bash install.sh /path/to/consumer

# Pin a specific revision (branch, tag, or full commit SHA)
bash install.sh /path/to/consumer <ref> core

# Opt into the technology library (Swift build/release/packaging)
bash install.sh /path/to/consumer <ref> tech

# Add parent/child delegated execution to any repository
bash install.sh /path/to/consumer <ref> parent
```

Use a **full commit SHA** as `<ref>` in production. The installer fetches the
templates at that revision so the caller and its fallback scripts match, and it
copies fallback scripts into the consumer's `scripts/` without overwriting
existing files.

| Set | Installs |
| --- | --- |
| `core` (default) | 11 callers covering OpenCode, issue scheduling, CodeRabbit, PR Agent, and auto-merge. |
| `tech` | 5 callers in the opt-in technology library: CI, release, release PR, release-automation merge, and packaging smoke. |
| `parent` | 4 callers: `continuum-child-dispatcher.yml`, `continuum-child-worker.yml`, `continuum-child-review.yml`, `continuum-child-pr-review.yml`. |

The `parent` and `tech` sets add **only** their own callers; each preserves
the consumer's own CI, release, and scheduling workflows. Any value other than
`core`, `tech`, or `parent` is rejected.

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

## Reusable workflows

Workflow names are identical to the reusable file names unless noted.

| Workflow | Purpose | Notable extra inputs |
| --- | --- | --- |
| `continuum-tech-swift-ci.yml` | Build, test, and classify whether a macOS build is required. (tech) | — |
| `continuum-tech-swift-release.yml` | Tag, GitHub Release, and manifest generation. (tech) | `version`, `dry_run` |
| `continuum-tech-swift-release-pr.yml` | Maintains the single automated Release PR (release-please). (tech) | — |
| `continuum-tech-swift-release-automation-merge.yml` | Merges trusted release-automation PRs. (tech) | — |
| `continuum-tech-swift-packaging-smoke.yml` | Homebrew/MacPorts install lifecycle smoke test. (tech) | `mode`, `version` |
| `opencode.yml` | OpenCode agent (`name: OpenCode agent`). | `mode`, `pr_number`, `head_ref`, `review_id`, `run_id` |
| `opencode-repair.yml` | OpenCode repair controller. | — |
| `opencode-unresolved.yml` | Retry OpenCode on unresolved CodeRabbit findings. | — |
| `issue-scheduler.yml` | Scheduled issue dispatch. | — |
| `auto-merge.yml` | Auto-merge reviewed pull requests. | — |
| `pr-agent.yml` | Manual PR Agent (Groq). | — |
| `add-review-label.yml` | Mark a PR ready for CodeRabbit. | — |
| `remove-review-label.yml` | Remove the ready label on sync. | — |
| `coderabbit-retry.yml` | Retry CodeRabbit after a rate limit. | — |
| `coderabbit-unresolved.yml` | Retry unresolved CodeRabbit findings. | — |
| `bootstrap-runtime-secret.yml` | Fetch an Actions secrets public key for secret bootstrapping. | — |
| `consumer-child-dispatcher.yml` | Parent scheduler for child tasks/reviews. | `worker_workflow`, `review_workflow`, `manual_pr_review_workflow`, `engine_ref` |
| `consumer-child-worker.yml` | Execute one delegated child task. | `child_id`, `task_number`, `model`, `max_agent_passes`, `python_version`, `engine_ref` |
| `consumer-child-review.yml` | Independent review of a delegated child task. | `child_id`, `task_number`, `pr_number`, `model`, `max_review_passes`, `python_version`, `engine_ref` |
| `consumer-child-pr-review.yml` | Independent review of a child pull request. | `child_id`, `pr_number`, `model`, `max_review_passes`, `python_version`, `engine_ref` |

Every reusable workflow also accepts `continuum_ref` (default `main`), the
revision that supplies fallback scripts.

## Secrets and variables

Secrets a consumer must provide, under exactly these names (secret names are
part of the contract and never change per consumer):

| Secret | Used by | Purpose |
| --- | --- | --- |
| `TAP_PAT` | review, release, opencode, delegation | Classic PAT (`repo` + `workflow` scopes) for checkout/push/API. Also the child-runtime token for the `parent` set. |
| `OPENCODE_API_KEY` | OpenCode workflows | Model provider access. |
| `RELEASE_PR_TOKEN` | `continuum-tech-swift-release-pr.yml` (optional) | Fine-grained PAT (`Contents: write`, `Pull requests: write`); falls back to `TAP_PAT`. |
| `NANODICTATE_SIGNING_P12` | tech release and packaging-smoke workflows | Base64 macOS signing certificate (`.p12`). |
| `NANODICTATE_SIGNING_PASSWORD` | tech release and packaging-smoke workflows | Password for the signing certificate. |
| `GROQ_API_KEY` | `pr-agent.yml` | PR Agent model provider. |
| `CHILD_RUNTIME_TOKEN` | `consumer-child-*` | Parent delegation token; caller stubs map it from `TAP_PAT`. |

Repository variables:

| Variable | Kind | Purpose |
| --- | --- | --- |
| `OPENCODE_MODEL` | optional | Default OpenCode model. |
| `MAINTAINERS` | optional | Maintainer allow-list for controllers. |
| `AUTOMATION_LEASE_MINUTES`, `AUTOMATION_MAX_DISPATCH_ATTEMPTS`, `AUTOMATION_WIP_LIMIT` | optional | Scheduler tuning. |
| `REVISION` | optional | Packaging revision override. |

Delegation relationships are stored as repository variables — see below.

Product-identity variables (version file, app and binary names, signing identity,
Homebrew tap, MacPorts tree) are listed in
[Consumer configuration variables](docs/consumer-variables.md).

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
- Provide the signing and model secrets the installed sets use
  (see the secrets table above), plus the `CONTINUUM_*` repository variables
  listed in [Consumer configuration variables](docs/consumer-variables.md).
- The consumer keeps its own `release-please-config.json`,
  `.release-please-manifest.json`, and `CHANGELOG.md`; release-please runs **in
  the consumer**, not here.

### Parent (delegation control plane, one example per child id)

```sh
bash install.sh /path/to/parent-plane <sha> parent
```

- Repository variables: `CONTINUUM_ROLE=parent`,
  `CONTINUUM_CHILDREN=["<child-id>", ...]` (a JSON array of opaque child ids).
- Provide a runtime PAT secret with access to the child repositories and to their
  Actions variables.
- The parent dispatcher wakes on main pushes, every ten minutes, manual dispatch,
  and completion of child task/review workflows.

### Child (a delegated repository, one id per child)

- A neutral `.continuum.yml` (`version: 1`).
- Repository variables: `CONTINUUM_ROLE=child`, `CONTINUUM_CHILD_ID=<id>` (must
  match an id in the parent's `CONTINUUM_CHILDREN`), `CONTINUUM_PARENT=<owner>/<repo>`
  (must equal the calling parent exactly), and optionally
  `CONTINUUM_VALIDATION_SCRIPT=<repo-relative .sh path>`.
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
