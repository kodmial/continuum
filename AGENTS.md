# Continuum project instructions

Continuum is a reusable GitHub Actions control plane, not an application. It ships
`workflow_call` workflows under `.github/workflows/`, thin caller templates under
`.github/caller-stubs/`, an installer (`install.sh`), and a dependency-free Python
engine under `src/continuum/` and `.github/scripts/`. Consumers install the thin
callers; they never copy engine code into their repository.

## Source of truth

`CONTRIBUTING.md` owns build, test, and release mechanics for this repository and
is authoritative. When this file and `CONTRIBUTING.md` disagree, follow
`CONTRIBUTING.md`.

## Interfaces are contracts

These are public interfaces and must not change silently:

- the reusable-workflow file names and their `workflow_call` inputs, defaults, and
  secrets;
- the `continuum-` prefix on every installed caller (a `continuum-*.yml` file in a
  consumer repository is Continuum-owned and is never hand-edited there);
- the `TAP_PAT` repository secret name (classic PAT, `repo` + `workflow` scopes)
  expected by every profile;
- the installer profiles (`swift`, and its `nanodictate` alias, plus `parent`)
  and the `install.sh` argument shape;
- the parent/child repository-variable names (`CONTINUUM_ROLE`,
  `CONTINUUM_CHILDREN`, `CONTINUUM_CHILD_ID`, `CONTINUUM_PARENT`,
  `CONTINUUM_VALIDATION_SCRIPT`).

Changing any of them requires updating the caller templates in
`.github/caller-stubs/` and the contract tests in `scripts/test-continuum.rb` in
the same change. Keep workflow `name:` values stable: controllers and
`workflow_run` triggers match on them.

## Task and scope

- The GitHub issue or pull request request is the task specification. Complete
  every applicable acceptance criterion and keep the change scoped to that task;
  do not introduce unrelated refactors.

## Verification

There is no Swift package here. Do not run `swift build`, `swift test`, or
`swift run` in this repository; those commands apply to a NanoDictate checkout,
not to Continuum.

Run the checks that match your change from the repository root:

```sh
ruby -E UTF-8 scripts/test-continuum.rb   # caller/workflow/install contracts
bash -n install.sh                        # installer syntax
actionlint -shellcheck= -pyflakes= .github/workflows/*.yml .github/caller-stubs/*.yml .github/caller-stubs/parent/*.yml
```

The Python engine and delegation suites (keep fixtures inside the worktree):

```sh
mkdir -p .opencode-tmp
export TMPDIR="$PWD/.opencode-tmp"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts -p 'test_delegation_runtime.py'
```

Use `actionlint` 1.7.12 or newer. New or changed behavior requires focused
automated tests; never weaken or delete an existing test or coverage check to make
validation pass. If a required check cannot run here, report the exact limitation
instead of substituting an unrelated check or claiming success.

## Release machinery

`release-please-config.json`, `.release-please-manifest.json`, and
`scripts/release-policy.sh` describe the NanoDictate release contract that the
release workflows enforce inside a consumer repository. Do not hand-edit versions
or these files outside a task that explicitly concerns release automation.

## Git lifecycle

The invoking workflow owns the Git lifecycle: do not create or switch branches and
do not open another pull request unless the task explicitly requires it. A repair
task that explicitly requires commit/push updates only the current pull-request
branch.

## Execution environment

Runs are headless: never request interactive approval and never wait for user
input. Keep agent-created temporary files and fixtures inside the worktree under
`.opencode-tmp/`, remove them before finishing, and never commit them. Do not use
`/tmp`, `/var/tmp`, the runner home directory, or other paths outside the worktree.

## Language

Code comments, commit messages, pull-request text, and agent-authored repository
documentation are in English unless the task explicitly requires another language.

