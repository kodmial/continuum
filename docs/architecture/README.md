# Continuum target architecture

Status: **normative target architecture**  
Last reviewed: **2026-09-30**

This document defines the end-state architecture for repositories managed by Continuum.
Implementation issues and migration plans must conform to this document; they do not redefine it.
Existing MVP documents may describe transitional behavior. Where a transitional document differs
from this target, the transition remains valid only until the target capability is implemented.

The architectural decision behind this model is recorded in
[ADR-0001](adr/0001-centralized-reusable-control-plane.md).

## 1. Goal

A new repository should be able to adopt Continuum with one bootstrap operation, provide a small
amount of repository-specific configuration, and immediately receive the same automation control
plane as every other Continuum consumer.

The desired operator experience is:

1. Create a repository.
2. Install Continuum.
3. Set the few required secrets/variables or select a profile.
4. Commit product code.
5. Continuum owns scheduling, agent execution, repair, review, merge, CI orchestration,
   packaging orchestration, release orchestration, recovery, and upgrades.

A consumer must never need to copy or independently maintain Continuum implementation logic.

## 2. Core architectural rule

**Continuum is the single implementation source for generic automation.**

Consumer repositories contain only:

- one generated thin GitHub Actions ingress workflow;
- one versioned Continuum configuration document;
- repository variables and secrets;
- product-specific scripts, manifests, tests, signing material references, and other domain data.

They do not contain independent implementations of generic controllers.

All reusable controller logic lives in `kodmial/continuum` and is invoked by the consumer at an
immutable Continuum commit SHA.

## 3. Execution model

GitHub Actions events originate in the consumer repository. GitHub does not allow a reusable
workflow in another repository to subscribe directly to another repository's events, so a local
ingress workflow is required.

The local workflow is an event adapter only:

```text
consumer event
    |
    v
.github/workflows/continuum.yml       # generated thin ingress
    |
    +----> Continuum reusable scheduler
    +----> Continuum reusable agent
    +----> Continuum reusable repair controller
    +----> Continuum reusable review controller
    +----> Continuum reusable merge controller
    +----> Continuum reusable CI/packaging/release controllers
```

Although the implementation is loaded from Continuum, the run belongs to the consumer repository.
The called reusable workflow receives the caller's GitHub context and operates on the caller
repository. Checkout therefore checks out the consumer, not Continuum.

The consumer ingress may declare only facts GitHub requires to be local and static, such as:

- event subscriptions;
- workflow names GitHub must match statically (for example `workflow_run.workflows`);
- minimum permissions needed by each called workflow;
- the immutable Continuum revision.

It must not contain controller algorithms, retry logic, merge policy, review-provider parsing,
release state machines, or duplicated business rules.

## 4. Target consumer footprint

The default target is:

```text
.github/
  workflows/
    continuum.yml          # generated; thin; no generic implementation logic
  continuum.yml            # reviewed desired-state configuration

scripts/                   # optional product-owned commands
...                        # product code/tests/manifests
```

Multiple local Continuum callers are permitted only when a GitHub platform constraint makes a
single ingress materially clearer or safer. They are not the default architecture.

Generated ingress files are owned by Continuum. Product changes should normally modify
`.github/continuum.yml`, repository variables/secrets, or product scripts instead.

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
- immutable version selection and automatic repinning;
- rollback and self-healing controllers;
- observability contracts and normalized state.

### The consumer owns

- product source code;
- product-specific build/test/package commands and assertions;
- product-specific manifests and generated inputs;
- desired platform/toolchain/profile selection;
- product-specific release metadata;
- signing material references and secrets;
- bundle/package identifiers, entitlements, distribution choices, and similar domain facts;
- repository-specific configuration values.

A product-specific command may live in the consumer; the decision of **when, why, with what retry
policy, and with what lifecycle semantics** to execute it belongs to Continuum.

## 6. Configuration model

Configuration has four layers, each with a distinct purpose.

### 6.1 Versioned desired state

`.github/continuum.yml` is the canonical, reviewable desired-state document.

It describes stable repository intent such as enabled capabilities, profile/platform/toolchain
selection, blocking gates, adapters, and release/packaging policy. The schema is versioned and
strictly validated. Defaults must be safe, deterministic, and documented.

The current v0.1 two-boolean contract is a transitional schema, not a reason to encode future
architecture into workflow YAML.

### 6.2 Repository / organization variables

GitHub `vars` hold non-sensitive operational tuning that is expected to vary without changing the
architecture, for example concurrency limits, retry budgets, timeouts, or model selection when
those are intentionally operator-tunable.

Defaults live in Continuum so a new repository requires as few variables as possible.

### 6.3 Secrets

Secrets remain in the consumer repository/environment and are passed explicitly to reusable
workflows with least privilege. Secret values are never copied into Continuum and never placed in
versioned configuration.

### 6.4 Product-owned executable facts

Build, test, package, and product validation commands that are intrinsically repository-specific
live as scripts/manifests in the consumer. Continuum invokes them through typed contracts instead
of embedding product logic in its generic core.

Do not use workflow-level `env` as a cross-repository configuration protocol. Reusable workflows
use typed inputs, `vars`, explicit secrets, outputs, and versioned configuration.

## 7. Installation and onboarding

Installation is a trusted, idempotent Continuum operation.

Given a repository, the installer must:

1. inventory the repository and detect supported product/toolchain characteristics;
2. create or reconcile the generated ingress workflow;
3. create or reconcile the versioned configuration;
4. provision Continuum-owned labels/metadata;
5. validate required secret **presence** without reading secret values;
6. select an immutable validated Continuum revision;
7. run compatibility/preflight checks;
8. activate exactly one writer per responsibility;
9. record the previous known-good state for rollback.

Re-running installation must reconcile desired state and normally be a no-op. It must not create a
second installation.

The target onboarding UX is therefore: **install once, configure a few facts, then operate normally**.

## 8. Versioning and automatic upgrades

Production consumers never use a floating Continuum branch or mutable tag.

Every reusable workflow reference is pinned to a full immutable Continuum commit SHA.

Continuum owns an automated repin controller:

```text
Continuum revision
    -> Continuum CI/contracts
    -> consumer compatibility validation
    -> generated pin-update PR
    -> consumer exact-head gates
    -> automatic merge
    -> post-update verification
```

A failed candidate never advances the consumer. A newer candidate supersedes an older pending
candidate safely. The previous pin is retained as the immediate rollback target.

Therefore generic automation changes are implemented once in Continuum and propagated
automatically without copying implementation changes into consumers.

## 9. Security model

- Reusable workflows are pinned to full commit SHAs.
- Permissions are least-privilege and can only stay equal or become more restrictive through a
  reusable-workflow call chain.
- Untrusted PR data is never treated as authority for refs, workflow identities, permissions, or
  release targets.
- Secrets remain consumer-owned and are passed explicitly.
- Privileged writers re-read live state immediately before side effects.
- Merge/publication operations bind to exact source SHAs.
- Installer/updater privileges are separate from agent privileges.
- OpenCode or any coding agent never administers repository secrets or repository settings.
- Generated ingress changes are allow-listed and mechanically validated.

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
- **generated ingress** — reduce to a thin Continuum caller;
- **migration-only** — remove after verified cutover.

A migration is not complete while a consumer still contains an independently maintained generic
controller or while a Continuum update requires a person to synchronize workflow implementation
between repositories.

## 12. Definition of Done for a managed consumer

A repository is fully Continuum-managed only when all of the following are true:

- generic automation has one implementation source: Continuum;
- local Continuum workflow YAML is generated and contains no generic controller logic;
- CI/packaging/release orchestration is Continuum-owned even when product commands remain local;
- configuration is expressed through the versioned contract, `vars`, secrets, and product
  scripts/manifests according to the boundaries above;
- all Continuum calls use immutable full SHAs;
- automatic validated repinning is active;
- rollback to the previous pin is mechanically tested;
- no migration-only workflows remain;
- creating another supported repository requires installation/configuration, not workflow copying.

## 13. Current implementation status

NanoDictate is a transitional consumer. Scheduler, agent, repair, review, and merge already call
Continuum reusable workflows, but the target architecture is not yet complete: release, parts of
CI/packaging orchestration, PR-Agent, migration-only workflows, a single generated ingress, and
automatic repinning still need to converge on this architecture.

Issues describe that implementation work. This document defines the destination.
