# Continuum target architecture

Status: **normative target architecture**  
Last reviewed: **2026-09-30**

This document defines the end-state architecture for repositories managed by Continuum.
Implementation issues and migration plans must conform to this document; they do not redefine it.
Existing MVP documents may describe transitional behavior. Where a transitional document differs
from this target, the transition remains valid only until the target capability is implemented.

The architectural decisions behind this model are recorded in
[ADR-0001](adr/0001-centralized-reusable-control-plane.md) and
[ADR-0002](adr/0002-consumer-controlled-immutable-releases.md).

## 1. Goal

A new repository should be able to adopt Continuum with one bootstrap operation, provide a small
amount of repository-specific configuration, explicitly select a Continuum release, and receive
the same automation control plane as every other consumer pinned to that release.

The desired operator experience is:

1. Create a repository.
2. Install Continuum.
3. Select an exact Continuum release, for example `v0.3.0`.
4. Set the few required secrets/variables or select a profile.
5. Commit product code.
6. Continuum owns scheduling, agent execution, repair, review, merge, CI orchestration,
   packaging orchestration, release orchestration, and recovery.

A consumer must never need to copy or independently maintain Continuum implementation logic.
A consumer also must never change behavior merely because Continuum `main` advanced or a newer
Continuum release was published.

## 2. Core architectural rule

**Continuum is the single implementation source for generic automation, while each consumer
explicitly selects the exact Continuum release it runs.**

Consumer repositories contain only:

- one generated thin GitHub Actions ingress workflow;
- one versioned Continuum configuration document;
- repository variables and secrets;
- product-specific scripts, manifests, tests, signing material references, and other domain data.

They do not contain independent implementations of generic controllers.

The generated ingress contains one external Continuum version pin, expressed as a literal
release-specific immutable reference such as:

```yaml
jobs:
  continuum:
    uses: kodmial/continuum/.github/workflows/consumer.yml@v0.3.0
    secrets: inherit
```

That exact `vX.Y.Z` release is the atomic implementation version for the whole Continuum control
plane used by that consumer. The version is intentionally not stored in `env`, `vars`, or an
expression because GitHub does not allow contexts or expressions in `jobs.<job_id>.uses`.

Production consumers must not use floating branches such as `@main` or floating compatibility
tags such as `@v0` / `@v0.3` as their normal version-selection mechanism.

## 3. Execution model

GitHub Actions events originate in the consumer repository. GitHub does not allow a reusable
workflow in another repository to subscribe directly to another repository's events, so a local
ingress workflow is required.

The local workflow is an event adapter and release selector only:

```text
consumer event
    |
    v
.github/workflows/continuum.yml       # generated thin ingress
    |
    |  uses: .../consumer.yml@v0.3.0
    v
Continuum release v0.3.0
    |
    +----> scheduler
    +----> agent
    +----> repair controller
    +----> review controller
    +----> merge controller
    +----> CI controller
    +----> packaging controller
    +----> release controller
```

The top-level `consumer.yml` in Continuum is the release entrypoint. It may route to nested
reusable workflows inside Continuum. Those internal calls use same-repository relative reusable
workflow references so GitHub resolves them from the same commit as the running `consumer.yml`.
Therefore the consumer's single `@vX.Y.Z` pin selects an internally consistent snapshot of the
entire workflow graph.

Although the implementation is loaded from Continuum, the run belongs to the consumer repository.
The called reusable workflow receives the caller's GitHub context and operates on the caller
repository. Checkout therefore checks out the consumer, not Continuum.

The consumer ingress may declare only facts GitHub requires to be local and static, such as:

- event subscriptions;
- workflow names GitHub must match statically (for example `workflow_run.workflows`);
- minimum permissions needed by the called workflow chain;
- the exact Continuum release reference.

It must not contain controller algorithms, retry logic, merge policy, review-provider parsing,
release state machines, or duplicated business rules.

## 4. Target consumer footprint

The default target is:

```text
.github/
  workflows/
    continuum.yml          # generated; thin; event adapter + exact Continuum release pin
  continuum.yml            # reviewed desired-state configuration

scripts/                   # optional product-owned commands
...                        # product code/tests/manifests
```

Multiple local Continuum callers are permitted only when a GitHub platform constraint makes a
single ingress materially clearer or safer. They are not the default architecture.

Generated ingress files are owned by Continuum tooling, but the selected Continuum release is
consumer-owned state. No background process may change that release pin automatically.

## 5. Responsibility boundary

### Continuum owns

- dependency-aware scheduling and work-in-progress control;
- OpenCode/agent lifecycle, dispatch, retry, timeout, and recovery;
- conflict/CI/review repair orchestration;
- review-provider adapters and normalized review gates;
- merge eligibility and exact-head reconciliation;
- generic CI orchestration, toolchain/bootstrap/cache behavior, and gate normalization;
- generic packaging orchestration and artifact lifecycle;
- release planning, publication transaction, provenance, downstream publisher orchestration,
  recovery, and idempotency;
- trusted installation/bootstrap;
- consumer compatibility validation;
- publishing immutable, semantically versioned Continuum releases;
- rollback support and self-healing controllers;
- observability contracts and normalized state.

### The consumer owns

- the exact Continuum release selection used by the repository;
- product source code;
- product-specific build/test/package commands and assertions;
- product-specific manifests and generated inputs;
- desired platform/toolchain/profile selection;
- product-specific release metadata;
- signing material references and secrets;
- bundle/package identifiers, entitlements, distribution choices, and similar domain facts;
- repository-specific configuration values.

A product-specific command may live in the consumer; the decision of **when, why, with what retry
policy, and with what lifecycle semantics** to execute it belongs to the selected Continuum release.

## 6. Configuration model

Configuration has four layers, each with a distinct purpose.

### 6.1 Continuum release selection

The Continuum implementation version is selected in the generated ingress by a literal exact
release tag:

```yaml
uses: kodmial/continuum/.github/workflows/consumer.yml@v0.3.0
```

This is dependency selection, not runtime configuration. It must not be represented by an
environment variable, GitHub `var`, or `.github/continuum.yml` field because `uses:` does not
accept expressions.

Changing `v0.3.0` to `v0.3.1` is an explicit consumer upgrade. Until that line changes, the
consumer continues to execute the old release even if Continuum publishes newer releases.

### 6.2 Versioned desired state

`.github/continuum.yml` is the canonical, reviewable desired-state document for how the selected
Continuum release should behave in this repository.

It describes stable repository intent such as enabled capabilities, profile/platform/toolchain
selection, blocking gates, adapters, and release/packaging policy. The schema is versioned and
strictly validated. Defaults must be safe, deterministic, and documented.

The current v0.1 two-boolean contract is a transitional schema, not a reason to encode future
architecture into workflow YAML.

### 6.3 Repository / organization variables

GitHub `vars` hold non-sensitive operational tuning that is expected to vary without changing the
architecture, for example concurrency limits, retry budgets, timeouts, or model selection when
those are intentionally operator-tunable.

Defaults live in Continuum so a new repository requires as few variables as possible.

### 6.4 Secrets and product-owned executable facts

Secrets remain in the consumer repository/environment and are passed explicitly to reusable
workflows with least privilege. Secret values are never copied into Continuum and never placed in
versioned configuration.

Build, test, package, and product validation commands that are intrinsically repository-specific
live as scripts/manifests in the consumer. Continuum invokes them through typed contracts instead
of embedding product logic in its generic core.

Do not use workflow-level `env` as a cross-repository configuration or version-selection
protocol. Reusable workflows use typed inputs, `vars`, explicit secrets, outputs, and versioned
configuration.

## 7. Installation and onboarding

Installation is a trusted, idempotent Continuum operation.

Given a repository, the installer must:

1. inventory the repository and detect supported product/toolchain characteristics;
2. create or reconcile the generated ingress workflow;
3. create or reconcile the versioned configuration;
4. provision Continuum-owned labels/metadata;
5. validate required secret **presence** without reading secret values;
6. set an explicitly selected known-good Continuum release when installing for the first time;
7. run compatibility/preflight checks;
8. activate exactly one writer per responsibility;
9. record enough state to support rollback.

Re-running installation must reconcile desired state and normally be a no-op. It must not silently
move an existing consumer to a newer Continuum release.

The target onboarding UX is therefore: **install once, select a release, configure a few facts,
then operate normally**.

## 8. Release and upgrade model

Continuum is released as immutable exact SemVer releases:

```text
v0.3.0
v0.3.1
v0.4.0
v1.0.0
```

Before production use, release immutability must be enabled for the Continuum repository.
A release-specific tag such as `v0.3.0` is then locked to its published commit and cannot be
silently moved while the immutable release exists.

The release lifecycle is:

```text
Continuum development on main
    -> Continuum CI/contracts/integration tests
    -> prepare draft GitHub Release
    -> final release validation
    -> publish immutable release vX.Y.Z
    -> release tag permanently identifies that commit
```

Publishing `v0.3.1` has no effect on consumers pinned to `v0.3.0`.

Consumer upgrade is a separate, explicit operation:

```text
NanoDictate @v0.3.0
    -> explicitly change one pin to @v0.3.1
    -> run compatibility + consumer CI
    -> merge only if intentionally accepted
    -> NanoDictate now runs the complete v0.3.1 workflow graph
```

There is **no automatic repinning** and no background mutation of consumer repositories when a new
Continuum release appears. Tooling may report that a newer release exists or perform validation,
but it may change the pin only as the result of an explicit consumer/operator upgrade action.

Rollback is the inverse explicit change: restore the previous exact release pin.

A full commit SHA remains a valid stricter supply-chain reference when required, but the standard
human-facing Continuum dependency contract is an exact immutable release tag `vX.Y.Z`.

## 9. Security model

- Production consumers never follow `@main` or another floating branch.
- Standard pins use exact SemVer tags backed by GitHub immutable releases; high-assurance consumers
  may pin the corresponding full commit SHA.
- The top-level Continuum release pin selects the whole nested workflow graph from one release
  commit.
- Permissions are least-privilege and can only stay equal or become more restrictive through a
  reusable-workflow call chain.
- Untrusted PR data is never treated as authority for refs, workflow identities, permissions, or
  release targets.
- Secrets remain consumer-owned and are passed explicitly.
- Privileged writers re-read live state immediately before side effects.
- Merge/publication operations bind to exact source SHAs.
- Installer privileges are separate from agent privileges.
- OpenCode or any coding agent never administers repository secrets or repository settings.
- Generated ingress changes are allow-listed and mechanically validated.
- No automated process may silently change a consumer's Continuum release selection.

## 10. Templates versus reusable workflows

Workflow templates may be used to bootstrap the first thin ingress, but templates are not the
implementation distribution mechanism. A copied template becomes consumer-owned text and can
drift.

Reusable workflows are the implementation distribution mechanism because GitHub executes the
central called workflow as part of the caller workflow while avoiding copied controller logic.

## 11. Migration rule

Legacy consumer workflows are classified as either:

- **generic orchestration** — move to Continuum;
- **product executable fact** — remain as a script/manifest or typed consumer input;
- **generated ingress** — reduce to a thin Continuum caller with one exact release pin;
- **migration-only** — remove after verified cutover.

A migration is not complete while a consumer still contains an independently maintained generic
controller or while moving to a new Continuum implementation requires copying workflow logic
between repositories.

## 12. Definition of Done for a managed consumer

A repository is fully Continuum-managed only when all of the following are true:

- generic automation has one implementation source: Continuum;
- local Continuum workflow YAML is generated and contains no generic controller logic;
- one explicit exact Continuum release pin controls the implementation version used by the
  consumer;
- CI/packaging/release orchestration is Continuum-owned even when product commands remain local;
- configuration is expressed through the versioned contract, `vars`, secrets, and product
  scripts/manifests according to the boundaries above;
- the selected release is an immutable exact SemVer release, or an explicitly chosen full SHA;
- publishing a newer Continuum release does not change consumer behavior;
- upgrade and rollback are explicit single-pin changes and are mechanically testable;
- no migration-only workflows remain;
- creating another supported repository requires installation/configuration, not workflow copying.

## 13. Current implementation status

NanoDictate is a transitional consumer. The consumer-selected immutable release model and the
single generated ingress are implemented: `.github/workflows/consumer.yml` is the release
entrypoint a consumer names, it routes the whole control plane through same-repository relative
references, and every engine checkout resolves from `job.workflow_sha` — the commit the consumer's
one pin already selected. The two generated fixtures
(`fixtures/consumer-repo/.github/workflows/continuum.yml` and
`fixtures/delegation-parent/.github/workflows/continuum.yml`) each hold exactly one literal
Continuum reference, and `.github/tests/test_release_selection.py` fails the build if that stops
being true.

Still to converge on this architecture: release and parts of CI/packaging orchestration, PR-Agent,
and the remaining migration-only workflows. `fixtures/consumer-repo/.github/workflows/continuum-release.yml`
is retained as a product fact rather than migrated: what a consumer builds, signs, and publishes
is its own, and Continuum decides only when to hand the merge commit over.

The previously planned automatic-repin model is no longer part of the target architecture.

Continuum holds parity claims against repositories it does not own, and those repositories keep
moving. `src/continuum/provenance/` is the plane that keeps the claims honest: a versioned ledger
records what was classified against which commit of which source
(`docs/provenance-ledger.json`), a weekly read-only audit asks whether those sources have moved
(`.github/workflows/parity-drift.yml`), and one deduplicated issue describes whatever has not been
classified yet. Nothing is copied from a source — a finding asks a human to classify a path, and the
classification is reviewed data in the ledger, not an automated import. The reading is not advisory:
`--provenance` is required by the cutover gate, so a promotion cannot assert that the preserved
workflows still match the code they were preserved from.

Issues describe implementation work. This document defines the destination.
