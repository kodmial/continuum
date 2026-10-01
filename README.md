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
  both the workflow and its fallback scripts. The rewrite covers a `uses:` line
  at either level — job-level (`call:` then `uses:`) and step-level (`- uses:`) —
  and keeps a trailing YAML comment. An unrelated `main` elsewhere in the file,
  a third-party action, and an already-pinned Continuum ref are all left alone.

### Fixed contracts

These names are the interface and never change per consumer:

- the `TAP_PAT` repository secret (classic PAT, `repo` + `workflow` scopes) —
  every set expects a secret with exactly this name;
- the `continuum-` prefix on every workflow file and caller stub, and the
  `continuum-tech-<tech>-` prefix for opt-in library callers;
- the installer sets (`core`, `tech`, and `parent`);
- the `CONTINUUM_*` repository-variable names.

Continuum is technology-neutral: `core` ships the task-domain controllers
(issue scheduling, PR creation/repair/recovery, PR review, PR analysis,
auto-merge, delegation) every project needs; `tech` is a separate, opt-in
library of technology-specific workflows — the `continuum-tech-<tech>-` prefix
(`continuum-tech-<tech>-<name>.yml`) marks them as a library that Continuum
itself never triggers; `parent` drives delegated execution for any technology.

Every workflow file and every caller stub is named `continuum-<name>.yml`, and
the installer writes each stub's stored name verbatim — it adds no prefix of
its own. The technology library uses the longer
`continuum-tech-<tech>-<name>.yml` name, where the `continuum-tech-<tech>-`
prefix marks the opt-in layer. There is no unprefixed file and no exception
list.

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

# Also remove callers Continuum no longer ships, without a confirmation prompt
bash install.sh /path/to/consumer <ref> core --yes
```

Use a **full commit SHA** as `<ref>` in production. The installer fetches the
templates at that revision so the caller and its fallback scripts match, and it
copies fallback scripts into the consumer's `scripts/` without overwriting
existing files.

| Set | Installs |
| --- | --- |
| `core` (default) | 12 callers covering OpenCode, issue scheduling, CodeRabbit, PR Agent, and auto-merge. |
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
| `continuum-tech-swift-ci.yml` | Build, test, and classify whether a macOS build is required. (tech) | — |
| `continuum-tech-swift-release.yml` | Tag, GitHub Release, and manifest generation. (tech) | `version`, `dry_run` |
| `continuum-tech-swift-release-pr.yml` | Maintains the single automated Release PR (release-please). (tech) | — |
| `continuum-tech-swift-release-automation-merge.yml` | Merges trusted release-automation PRs. (tech) | — |
| `continuum-tech-swift-packaging-smoke.yml` | Homebrew/MacPorts install lifecycle smoke test. (tech) | `mode`, `version` |
| `continuum-opencode.yml` | OpenCode agent (`name: OpenCode agent`). | `mode`, `pr_number`, `head_ref`, `review_id`, `run_id` |
| `continuum-opencode-repair.yml` | OpenCode repair controller. | — |
| `continuum-opencode-unresolved.yml` | Retry OpenCode on unresolved CodeRabbit findings. | — |
| `continuum-opencode-watchdog.yml` | Recover a failed issue implementation run (`name: OpenCode watchdog`). | `watched_workflow`, `max_recovery_retries`, `retry_marker`, `in_progress_label`, `pause_marker`, `dispatch_marker`, `timeout_minutes` |
| `continuum-issue-scheduler.yml` | Scheduled issue dispatch. | — |
| `continuum-auto-merge.yml` | Auto-merge reviewed pull requests. | `require_coderabbit` |
| `continuum-pr-agent.yml` | Manual PR Agent (Groq). | — |
| `continuum-add-review-label.yml` | Mark a PR ready for CodeRabbit. | — |
| `continuum-remove-review-label.yml` | Remove the ready label on sync. | — |
| `continuum-coderabbit-retry.yml` | Retry CodeRabbit after a rate limit. | — |
| `continuum-coderabbit-unresolved.yml` | Retry unresolved CodeRabbit findings. | — |
| `continuum-bootstrap-runtime-secret.yml` | Fetch an Actions secrets public key for secret bootstrapping. | — |
| `continuum-consumer-child-dispatcher.yml` | Parent scheduler for child tasks/reviews. | `worker_workflow`, `review_workflow`, `manual_pr_review_workflow`, `engine_ref` |
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
| `TAP_PAT` | `core`, `parent`, `tech` | review, release, opencode, delegation | Classic PAT (`repo` + `workflow` scopes) for checkout/push/API. Also the child-runtime token the `parent` stubs forward. |
| `RENDER_API_KEY` | `core` | `continuum-render-executor.yml` | Render API key. A different credential from `TAP_PAT`, with **no** fallback: the controller fails explicitly when it is unset. |
| `CHILD_RUNTIME_TOKEN` | `parent` | `consumer-child-*` | Parent delegation token; the caller stubs map it from `TAP_PAT`. |
| `CHILD_RUNTIME_REPOSITORIES` | `parent` | `consumer-child-*` (optional) | Pre-variables compatibility path; see `docs/parent-child-delegation.md`. |
| `RELEASE_PR_TOKEN` | `tech` | `continuum-tech-swift-release-pr.yml` (optional) | Fine-grained PAT (`Contents: write`, `Pull requests: write`); resolved as `RELEASE_PR_TOKEN`, then `TAP_PAT`, then the built-in `GITHUB_TOKEN`. |
| `NANODICTATE_SIGNING_P12` | `tech` | tech release and packaging-smoke workflows | Base64 macOS signing certificate (`.p12`). |
| `NANODICTATE_SIGNING_PASSWORD` | `tech` | tech release and packaging-smoke workflows | Password for the signing certificate. |

No paid provider key is part of this contract: **no workflow reads
`OPENCODE_API_KEY` or `GROQ_API_KEY`.** The core runs on free anonymous models
and requires no API key; `continuum-pr-agent.yml` refuses to run rather than
read a paid provider secret. `secrets.GITHUB_TOKEN` is minted by GitHub
automatically, so it is not a credential a consumer defines.

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
