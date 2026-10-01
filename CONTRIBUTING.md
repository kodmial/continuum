# Contributing to Continuum

Continuum provides reusable GitHub Actions workflows and parent/child delegated execution. It ships two layers: a core layer of task-domain workflows every project needs, and an opt-in technology library (Swift build/release/packaging) that Continuum never triggers itself. The two layers are distinguished by file name — new core files are `continuum-<name>.yml`, the technology library is `continuum-tech-<tech>-<name>.yml` (the `continuum-tech-<tech>-` prefix marks the library). Keep `.github/caller-stubs/` compatible with the workflow interfaces and preserve workflow names used by controllers and `workflow_run` triggers.

## Verification

Run from the repository root:

```sh
ruby -E UTF-8 scripts/test-continuum.rb
bash -n install.sh
actionlint -shellcheck= -pyflakes= .github/workflows/*.yml .github/caller-stubs/*.yml .github/caller-stubs/tech/*.yml .github/caller-stubs/parent/*.yml
```

Use actionlint 1.7.12 or newer. The validation workflow runs the contract tests and actionlint on pushes and pull requests. Contract tests use temporary directories under `.opencode-tmp/` and remove their own fixtures.

When changing shared release policy or application workflows, also verify in the NanoDictate checkout:

```sh
bash scripts/test-release-policy.sh
swift build
swift run NanoDictateCoreTests
```

Continuum does not contain a Swift package. Do not use `swift test` for NanoDictate.

## Installing callers

Run `bash /path/to/continuum/install.sh /path/to/consumer` to use local templates. Supplying a second argument downloads templates at that branch, tag, or commit and pins both workflow calls and fallback scripts to it:

```sh
bash /path/to/continuum/install.sh /path/to/consumer v1.0.0
```

Fallback scripts are copied into the consumer's `scripts/` directory without overwriting existing files, so packaging helpers resolve templates relative to the consumer checkout.

## Parent and child delegation

See [Parent/child delegated execution](docs/parent-child-delegation.md) for the
repository variables, opt-in task markers, validation gate, and `parent`
installer set. Parent installation adds only child execution workflows;
project CI and release workflows remain owned by the consumer.

Run the Python suites with temporary files inside the worktree:

```sh
mkdir -p .opencode-tmp
export TMPDIR="$PWD/.opencode-tmp"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts -p 'test_delegation_runtime.py'
```
