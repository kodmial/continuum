# ADR-0001: Centralized reusable control plane with thin consumer ingress

- **Status:** Accepted; version-selection and automatic-upgrade portions superseded by [ADR-0002](0002-consumer-controlled-immutable-releases.md)
- **Date:** 2026-09-30
- **Decision owners:** Continuum maintainers
- **Scope:** Consumer integration, configuration ownership, upgrades, and GitHub Actions execution

> **Lifecycle note:** ADR-0001 remains authoritative for the centralized reusable control plane,
> thin consumer ingress, and configuration boundaries. ADR-0002 supersedes only this ADR's
> version-selection and automatic-repin decisions. The historical text below is preserved as the
> original accepted decision.

## Context

Continuum exists to provide one uniform automation system to many repositories. The principal
failure mode to avoid is configuration and implementation drift: copying scheduler, repair, review,
merge, release, or other controller workflows into every consumer creates multiple implementations
that have to be patched and reviewed independently.

At the same time, GitHub Actions event subscriptions belong to the repository where the event
occurs. A remote repository cannot subscribe a reusable workflow directly to another repository's
pull requests, issues, schedules, or workflow completions.

We need an architecture where:

- Actions runs remain native to each consumer repository;
- generic implementation is maintained exactly once;
- adding a new consumer requires minimal setup;
- consumer-specific facts remain configurable;
- upgrades are deterministic and rollbackable;
- a Continuum change does not require manual copy/paste into every consumer.

## Decision drivers

1. Single source of truth for generic automation.
2. Minimal onboarding effort for new repositories.
3. No duplicated controller implementation in consumers.
4. Native GitHub Actions visibility and permissions in the consumer repository.
5. Deterministic supply-chain identity and rollback.
6. Least-privilege secret handling.
7. Product-specific flexibility without product names or branches in Continuum core.
8. Automatic evolution of existing consumers when Continuum changes.

## Decision

Use **cross-repository GitHub reusable workflows as the implementation plane**, called from a
**generated thin ingress workflow in each consumer repository**.

The consumer owns the GitHub event subscription because GitHub requires that trigger to be local.
The ingress contains no generic controller algorithms. It calls Continuum reusable workflows pinned
to a full immutable commit SHA.

The called workflow operates in the caller repository's GitHub context. Product checkout, issue/PR
state, Actions runs, repository variables, and explicitly passed secrets therefore refer to the
consumer.

The target consumer repository contains:

- one generated Continuum ingress workflow;
- one versioned Continuum desired-state document;
- consumer variables and secrets;
- product-specific scripts/manifests/data.

Continuum contains the generic scheduler, agent lifecycle, repair, review, merge, CI orchestration,
packaging orchestration, release orchestration, installer, updater/repinner, rollback, and
observability implementations.

Consumer-specific executable commands may stay with the product, but generic orchestration around
those commands does not.

Production references use a full Continuum commit SHA. Continuum automatically proposes and
validates repins; consumers never follow a floating `main`.

## Configuration decision

Use different channels for different kinds of state:

- versioned `.github/continuum.yml` for reviewed desired state;
- GitHub repository/organization/environment `vars` for non-sensitive operational tuning;
- GitHub secrets for credentials/signing material;
- product scripts/manifests for product-specific executable facts;
- typed reusable-workflow inputs only for bounded invocation context.

Workflow-level environment variables are not an inter-repository configuration protocol.

The configuration schema may evolve beyond the current MVP contract, but every expansion must
preserve a strict versioned schema and safe defaults.

## Installation decision

Continuum owns an idempotent installer/reconciler. Installation creates or updates the thin ingress,
configuration, labels/metadata, validates prerequisites, pins a known-good Continuum revision, and
records rollback state.

The same mechanism performs upgrades. Installation and upgrades are desired-state reconciliation,
not file-copy procedures.

## Consequences

### Positive

- Generic behavior is fixed once.
- Every consumer executes the same reviewed implementation.
- Actions runs remain visible and native in each consumer.
- New repositories have a small, repeatable onboarding surface.
- Consumer configuration is explicit without forking implementation.
- Immutable pins make a production run reproducible.
- Automatic repins combine central maintenance with controlled rollout.
- Rollback is a pin/config reversal rather than reconstruction of deleted workflows.

### Costs

- A small local ingress cannot be eliminated because GitHub events are repository-local.
- GitHub event names that must be static still have to appear in the ingress.
- Configuration contracts and reusable workflow interfaces become public APIs and require
  compatibility discipline.
- The installer/repinner is privileged infrastructure and needs stronger controls than coding
  agents.
- A single ingress can contain several conditional jobs; implementation must keep it generated and
  mechanically tested so readability does not degrade.

## Considered alternatives

### Copy/synchronize complete workflows into every consumer — Rejected

It creates independent copies, makes fixes multi-repository changes, permits drift, and turns a
new repository into a synchronization problem.

### Workflow templates as the primary distribution mechanism — Rejected

Templates are useful for bootstrap, but after creation their YAML is copied into the repository.
They do not provide centralized implementation updates.

### Reference Continuum `@main` — Rejected

It centralizes code but removes deterministic rollout: the behavior of an existing consumer changes
when Continuum main changes. Production consumers must use immutable full SHAs.

### Keep many handwritten thin callers permanently — Rejected as the default

Thin callers are acceptable during migration, but they still expose avoidable per-repository
surface area. The target is one generated ingress unless a GitHub platform constraint justifies a
split.

### Run automation entirely outside the consumer repository — Rejected as the default

It would make permissions, Actions observability, repository-native events, secrets, and checkout
semantics more complex without removing the need for GitHub integration. Continuum should use
GitHub's reusable-workflow execution model unless a capability genuinely cannot be expressed there.

## Industry alignment

This decision follows GitHub's reusable-workflow model: reuse avoids workflow duplication; a called
workflow from another repository runs in the caller's GitHub context; and GitHub recommends a
commit SHA as the safest reference for stability/security.

It also follows the ADR practice of recording an architecturally significant decision in version
control and preserving the accepted record. If this decision changes, create a new ADR and mark
this one **Superseded** rather than rewriting its history.

References:

- https://docs.github.com/en/actions/concepts/workflows-and-actions/reusing-workflow-configurations
- https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows
- https://docs.github.com/en/actions/reference/security/secure-use
- https://docs.github.com/en/actions/concepts/workflows-and-actions/variables
- https://docs.aws.amazon.com/prescriptive-guidance/latest/architectural-decision-records/adr-process.html

## Compliance

New Continuum implementation work and consumer migrations must be reviewed against
`docs/architecture/README.md`, this ADR, and ADR-0002.

Temporary departures must be named as migration debt with a removal condition. They are not new
architecture.
