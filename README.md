# Continuum

Continuum is a reusable GitHub Actions control plane for repository automation.
It owns generic task/PR/recovery lifecycle decisions and a generic validation
contract. Consumer repositories own their product, language, packaging and
release policy.

## Ownership boundary

- `.github/workflows/continuum-*.yml` contains reusable engines.
- `.github/caller-stubs/continuum-*.yml` contains thin core callers.
- `.github/caller-stubs/tech/` contains optional technology profiles.
- `.github/caller-stubs/parent/` contains optional parent/child delegation callers.
- Installed `continuum-*.yml` callers are Continuum-owned and should not be
  hand-edited in a consumer.
- Product workflows may call Continuum engines and pass configuration/hooks, but
  product policy stays in the consumer.

Continuum core contains no consumer repository names, source paths, signing
secret names, packaging manifests, release repositories or product-specific
runner labels. CI enforces that boundary with `scripts/check-project-agnostic.rb`.

## Minimal connection

From a checkout of Continuum:

```sh
# Install the generic lifecycle + CI callers.
bash install.sh /path/to/consumer main core

# Optional: install the generic Swift/macOS validation profile.
bash install.sh /path/to/consumer main tech

# Optional: make the repository a delegation parent.
bash install.sh /path/to/consumer main parent
```

The managed consumers use Continuum from `main`; caller stubs invoke reusable
workflows with `@main` and `continuum_ref: main`.

For the generic lifecycle, define `TAP_PAT` when workflows need authenticated
GitHub writes. Define `RENDER_API_KEY` only when using the Render controller.
Parent delegation uses the parent repository's configured child relationship and
token contract described in
[docs/parent-child-delegation.md](docs/parent-child-delegation.md).

A new repository can use the default CI contract without any product-specific
workflow logic. The installed `continuum-validation.yml` workflow appears as
**CI**, runs on `ubuntu-latest`, auto-detects common stacks, publishes the
validation result and routes a PR failure to the shared repair controller.

## Unified consumer validation contract

The reusable `continuum-validation.yml` workflow is the common build/test
boundary. Configuration is read from the calling repository.

| Setting | Repository variable | Default |
| --- | --- | --- |
| Runner | `CONTINUUM_RUNNER` | `ubuntu-latest` |
| Prepare hook | `CONTINUUM_PREPARE_COMMAND` | empty |
| Build hook | `CONTINUUM_BUILD_COMMAND` | empty |
| Test hook | `CONTINUUM_TEST_COMMAND` | empty |
| Validation hook | `CONTINUUM_VALIDATION_COMMAND` | empty |
| Package hook | `CONTINUUM_PACKAGE_COMMAND` | empty |
| Release hook | `CONTINUUM_RELEASE_COMMAND` | empty |
| Auto-detect standard stacks | `CONTINUUM_AUTO_DETECT` | `true` |
| Artifact paths | `CONTINUUM_ARTIFACT_PATHS` | empty |
| Artifact name | `CONTINUUM_ARTIFACT_NAME` | `continuum-validation` |
| Status context | `CONTINUUM_STATUS_CONTEXT` | `continuum/validation` |
| Publish status | `CONTINUUM_PUBLISH_STATUS` | `true` |
| Dispatch repair | `CONTINUUM_DISPATCH_REPAIR` | `true` |
| Repair workflow | `CONTINUUM_REPAIR_WORKFLOW` | `continuum-opencode-repair.yml` |

When auto-detection is enabled the shared workflow detects Python, Node, Java,
Go and Rust and runs their standard build/test commands. It also builds a
Dockerfile when present and syntax-checks shell scripts. Consumers can disable
auto-detection and supply one or more hooks instead.

The generic required-check contract is the top-level workflow named **CI**.
Product-specific checks can remain additional consumer workflows; they do not
belong in Continuum merely to make repositories textually identical.

Artifacts are opt-in through `CONTINUUM_ARTIFACT_PATHS`. Packaging and release
hooks are orchestration points, not built-in product release systems.

See [docs/consumer-variables.md](docs/consumer-variables.md) for the complete
configuration contract.

## Technology profiles

Technology profiles are optional adapters over the same validation contract.
The current `tech` set contains one profile:

- `continuum-tech-swift-ci.yml` — generic Swift validation. It defaults to
  `macos-latest`, `swift build`, and `swift test`, and accepts runner and
  hook overrides. It contains no application name, source layout, packaging,
  signing, Homebrew/MacPorts, or release policy.

A consumer that needs a fixed macOS image explicitly selects it in its own
configuration/workflow. Continuum core never defaults to `macos-15`.

## Generic lifecycle

The core set owns the shared state machine:

1. issue admission / WIP / lease / dispatch;
2. OpenCode execution;
3. PR observation and review;
4. optional CodeRabbit handling;
5. CI result and repair routing;
6. merge reconciliation;
7. watchdog / stale-run recovery;
8. completion reconciliation.

Consumer callers may configure supported inputs and hooks, but they do not copy
these lifecycle decisions.

OpenCode itself defaults to `ubuntu-latest` through
`AUTOMATION_OPENCODE_RUNNER`. A consumer that needs language/toolchain setup
uses `CONTINUUM_AGENT_PREPARE_COMMAND`; core contains no built-in Swift/Xcode
setup.

## Installer sets

| Set | Purpose |
| --- | --- |
| `core` | Generic lifecycle, validation, qualification and execution callers. |
| `tech` | Optional consumer-neutral technology profiles (currently Swift validation). |
| `parent` | Parent/child dispatcher, worker and review callers. |

Reinstalling a set updates its owned callers and can prune obsolete Continuum
callers. Use `--yes` only when unattended pruning is intentional.

## Development validation

Run the same checks as CI:

```sh
ruby -E UTF-8 scripts/check-project-agnostic.rb
ruby -E UTF-8 scripts/test-continuum.rb
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m unittest discover -s .github/scripts -p 'test_delegation_runtime.py'
```

The GitHub `Validate Continuum` workflow also runs actionlint across reusable
workflows and caller templates.
