# Contributing to Continuum

Continuum is a reusable GitHub Actions control plane, not an application. It
ships `workflow_call` engines, thin caller templates, an installer, and a
dependency-free Python engine. You can work on all of it from a clone of this
repository; no consumer checkout, Swift package, or product-specific toolchain
is required.

Contribute by pull request to `kodmial/continuum`. Do not commit directly to
`main`.

## Repository boundaries

Continuum has one source of truth for each layer. Keep changes in the layer that
owns the behavior:

| Path | Owns |
| --- | --- |
| `.github/workflows/continuum-*.yml` | Reusable (`workflow_call`) engine workflows. |
| `.github/workflows/continuum-tech-*.yml` | The opt-in technology library. Continuum never triggers it itself. |
| `.github/caller-stubs/**` | Thin caller templates installed into consumers. The stored file name is the installed file name. |
| `install.sh` | The installer for the `core`, `tech`, and `parent` sets. |
| `src/continuum/` | The dependency-free Python engine. |
| `.github/scripts/` | Delegation runtime and helper scripts. |
| `scripts/` | Contract tests and `scripts/validate-continuum.sh`. |
| `docs/` and `README.md` | Consumer and design documentation. |

Caller stubs stay thin: behavior belongs in the reusable engine or the Python
module, not in an installed caller. A `continuum-*.yml` file in a consumer
repository is Continuum-owned only when its body has a `uses:` line for
`kodmial/continuum/.github/workflows/`; a file without that call is
consumer-owned. Project-specific behavior belongs in a project-owned workflow.

These are public interfaces and must not change silently:

- reusable-workflow file names, `name:` values, and `workflow_call` inputs,
  defaults, and secrets;
- the `continuum-` prefix on every Continuum workflow and caller stub, plus the
  `continuum-tech-<tech>-` prefix for the opt-in library;
- the installer sets (`core`, `tech`, `parent`) and its verbatim copy of each
  stored caller name;
- the `TAP_PAT` and `RENDER_API_KEY` secret names; and
- the `CONTINUUM_*` repository-variable names.

If a change touches one of these interfaces, update the caller templates in
`.github/caller-stubs/` and the contract tests in `scripts/test-continuum.rb` in
the same pull request. Keep workflow `name:` values stable because controllers
and `workflow_run` triggers match on them.

## Prerequisites

Install only what your change needs:

- Git and Bash;
- Ruby for the contract suite;
- Python 3 for the engine and delegation suites;
- `curl` and network access for `scripts/validate-continuum.sh`, which downloads
  actionlint 1.7.12 into `.opencode-tmp/` when it is not already available.

You do not need macOS, Xcode, Swift, NanoDictate, or any other consumer
repository.

## Validate your change

In the complete repository tree the canonical full check is:

```sh
bash scripts/validate-continuum.sh
```

It runs the Ruby contract suite, the trigger-manifest check, the Python engine
suite, the delegation runtime suite, and actionlint over every workflow and
caller stub. Temporary files stay under `.opencode-tmp/`; remove them when you
are finished with:

```sh
rm -rf .opencode-tmp
```

A split import that does not ship `scripts/` cannot run the canonical command:
it fails before executing any check. Use the focused checks below in that tree;
the canonical script and `scripts/test-continuum.rb` are part of the full tree.

For faster iteration, run the focused check that matches your change from the
repository root:

```sh
# Workflow, caller, installer, or interface contracts
ruby -E UTF-8 scripts/test-continuum.rb

# Installer syntax
bash -n install.sh

# Python engine suite, with fixtures inside the worktree
mkdir -p .opencode-tmp
export TMPDIR="$PWD/.opencode-tmp"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests

# Delegation runtime suite
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts -p 'test_delegation_runtime.py'

# One focused Python module while iterating
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_merge_lifecycle.py'

# actionlint directly, if version 1.7.12 or newer is installed
actionlint -shellcheck= -pyflakes= .github/workflows/*.yml .github/caller-stubs/*.yml .github/caller-stubs/tech/*.yml .github/caller-stubs/parent/*.yml
```

Documentation-only changes do not need a consumer checkout or a product build.
State which checks you ran in the pull request; do not claim a check you did not
run.

## Pull requests and review

- Branch from `main` and open a pull request targeting `main`.
- Keep the pull request scoped to one problem and describe what changed, why,
  compatibility impact, and how you verified it.
- A maintainer review is required before merge. Address review findings and keep
  the required checks green for the exact pull-request head being merged.
- Do not include secrets, tokens, or consumer data in code, logs, issues, or
  pull requests.
- Never edit a consumer repository as part of a Continuum pull request. Installer
  behavior is documented in [README.md](README.md#install).

## Issues and support

Use the GitHub issue forms:

- **Bug report** for a reproducible defect in a workflow, caller, installer,
  Python engine, or this repository's documentation. Include the exact ref,
  reproduction steps, and expected versus actual behavior.
- **Feature request** for a consumer-neutral improvement. Describe the problem,
  the proposed behavior, and any compatibility or migration impact.
- **Question / support** for usage and integration questions that are not a bug
  report or a feature request.
- **Security vulnerability**: do not open a public issue. Follow the
  [security policy](https://github.com/kodmial/continuum/security/policy); it is
  the source of truth for the current reporting status and instructions. Never
  include exploit details, credentials, or consumer data in a public issue, and
  do not send reports to a guessed email address.

The bug, feature, and question forms apply the `automation:blocked` admission
hold at creation time and never add `automation:ready`. This keeps a new report
out of the automatic implementation queue until a maintainer deliberately admits
it. The forms are an intake convenience, not a scheduler policy: maintainers and
API users can still create blank issues, and labels can be changed after
creation. Only the scheduler's configured admission rules decide whether work is
dispatched.
