# ADR-0002: Consumer-controlled immutable Continuum releases

- **Status:** Accepted
- **Date:** 2026-09-30
- **Decision owners:** Continuum maintainers
- **Scope:** Continuum release identity, consumer version selection, upgrades, rollback, and nested reusable workflows
- **Supersedes:** The version-selection and automatic-upgrade portions of ADR-0001

## Context

ADR-0001 established the correct implementation boundary: generic automation lives in Continuum
and consumer repositories keep only a thin local ingress plus repository-specific configuration and
product facts.

Its initial upgrade model used full-SHA pins combined with automatic validated repinning. That model
does not satisfy the stronger consumer-control requirement now adopted for Continuum: a consumer
must continue to run exactly the Continuum version it explicitly selected until that consumer is
deliberately upgraded.

Publishing a new Continuum commit or release must never silently change an existing consumer.

GitHub Actions imposes an important constraint on the version selector. A reusable workflow is
called through `jobs.<job_id>.uses`, and GitHub does not allow contexts or expressions in that
keyword. Therefore a version such as `CONTINUUM_VERSION=v0.3.0` cannot be placed in `env` or
`vars` and interpolated into `uses:`.

GitHub does allow cross-repository reusable workflows to be referenced by release tag or commit
SHA. GitHub immutable releases lock their associated release tag to its published commit. GitHub
also resolves same-repository relative reusable-workflow calls from the same commit as the caller
workflow. Together, these properties allow one literal consumer pin to select an atomic snapshot
of the whole Continuum workflow graph.

## Decision drivers

1. Consumer repositories control exactly when their automation implementation changes.
2. A Continuum release must be a stable, reproducible dependency version.
3. A single version selection must cover CI, packaging, release, scheduler, agent, repair, review,
   merge, PR-Agent, and every other Continuum-managed workflow.
4. Upgrading Continuum in a consumer should require one intentional change, not synchronized edits
   across many workflow files.
5. Publishing a newer Continuum release must not modify existing consumers.
6. The dependency reference should be understandable to humans while retaining immutable
   supply-chain identity.
7. Rollback must be one explicit dependency change.

## Decision

Continuum will publish exact SemVer GitHub Releases such as `v0.3.0`, `v0.3.1`, and `v1.0.0`.

Release immutability must be enabled for the Continuum repository before these release tags are
used as the standard production dependency contract. Once published as an immutable release, the
release-specific tag identifies one commit and cannot be silently moved.

Each consumer selects Continuum with exactly one literal external reusable-workflow reference in
its generated ingress, for example:

```yaml
jobs:
  continuum:
    uses: kodmial/continuum/.github/workflows/consumer.yml@v0.3.0
    secrets: inherit
```

The release reference is consumer-owned dependency state. It is not runtime configuration and is
not stored in `env`, GitHub `vars`, or `.github/continuum.yml`.

The top-level `consumer.yml` is the Continuum release entrypoint. It routes to nested reusable
workflows inside Continuum using same-repository relative references. Those nested workflows are
therefore resolved from the same commit as the selected `consumer.yml`, so the external
`@v0.3.0` pin selects one internally consistent release snapshot.

"Exactly one pin" means exactly one *version*, not one textual occurrence. A consumer ingress that
calls several surfaces repeats the identical literal reference, once per job, because each job
carries its own `permissions` block and a reusable workflow can only narrow the caller's grant.
Collapsing those calls into a single job to save a duplicated string would force every surface to
share one grant, which is the larger regression. The rule this ADR enforces is therefore: every
external Continuum reference in a consumer's repository targets `consumer.yml`, and all of them
name the same exact release. `continuum_engine.py assert` is the check, and it fails on a second
version, on a directly pinned surface, and on any floating reference.

Exact patch-level release tags are the standard human-facing pin. Floating references such as
`@main`, `@v1`, or `@v1.2` are not the standard production version-selection mechanism because
they can change consumer behavior without an explicit consumer version edit.

A full commit SHA remains an allowed stricter reference for consumers that require SHA pinning.
When used, the selected SHA must correspond to an accepted Continuum release or another explicitly
approved immutable revision.

## Continuum release lifecycle

A production Continuum release follows this lifecycle:

```text
development on main
    -> CI / contract / integration validation
    -> draft GitHub Release
    -> final release validation
    -> publish immutable release vX.Y.Z
    -> release tag permanently identifies the release commit
```

The release is the unit of distribution for the entire Continuum automation implementation.
Individual reusable workflows are not independently versioned for a consumer.

## Consumer upgrade lifecycle

Publishing a new Continuum release performs no write in any consumer repository.

A consumer upgrade is explicit:

```text
consumer @v0.3.0
    -> operator/tool explicitly requests upgrade to v0.3.1
    -> change the single ingress pin
    -> run compatibility and consumer CI
    -> merge intentionally
    -> consumer now runs v0.3.1
```

Because a consumer may spread one dependency over several ingress files — a provider entrypoint and
a dispatch target are separate files in the same repository — the upgrade is one command over the
repository rather than one command per file:

```bash
# Read the release the repository currently selects, across every referencing file.
.github/scripts/continuum_engine.py pin --repo .

# Upgrade. Only the release references change; the diff is reviewable as the pin itself.
.github/scripts/continuum_engine.py rewrite --repo . --pin v0.3.1

# Rollback is the same command with the previous release.
.github/scripts/continuum_engine.py rewrite --repo . --pin v0.3.0
```

`pin --repo` resolves the value across every referencing file at once and fails if they disagree.
That check is what makes the upgrade safe to script: a repository left half-upgraded by an
interrupted change is reported with both releases named, and `rewrite --repo` refuses to touch it
rather than guessing which half the operator meant. An ambiguous invocation — neither `--repo` nor
`--ingress`, or both — is refused rather than resolved by preferring a flag.

Tooling may discover or report newer versions, compare compatibility, or prepare an upgrade only
when explicitly invoked. There is no background automatic repin and no automatic merge caused by
the existence of a newer Continuum release.

Rollback is the inverse operation: restore the previous exact release pin and validate. It is the
same command with the previous release, and it is byte-identical — a rewrite touches the release
references and nothing else.

## Configuration boundary

The consumer release pin answers:

> **Which Continuum implementation does this repository execute?**

`.github/continuum.yml`, repository variables, secrets, and product scripts answer:

> **How should that selected Continuum implementation behave for this repository?**

These concerns must remain separate.

## Consequences

### Positive

- Existing consumers never change because Continuum `main` or a newer release changed.
- One exact version controls the entire nested workflow graph.
- Human-readable SemVer expresses the dependency clearly.
- Immutable release tags provide stable release identity.
- Upgrade and rollback are one-line dependency changes.
- Consumer testing happens before that consumer adopts a new Continuum version.
- Version selection is visible in code review and repository history.

### Costs

- Consumers do not automatically receive fixes; they must intentionally upgrade.
- Release discipline becomes part of Continuum's compatibility contract.
- Exact SemVer releases must be created before consumers can adopt new behavior.
- The generated ingress contains a literal ref because GitHub does not permit dynamic expressions
  in reusable-workflow `uses:`.
- If a high-assurance environment requires SHA-only references, human-readable release metadata
  must be mapped to the corresponding full SHA.

## Considered alternatives

### Automatic validated repinning — Rejected

Even if tests gate the update, an automatic repin changes consumer dependency state without an
explicit consumer version-selection action. This violates consumer-controlled versioning.

### Pin Continuum through an environment variable or GitHub `var` — Rejected

GitHub does not allow contexts or expressions in `jobs.<job_id>.uses`, so this cannot directly
select the reusable-workflow ref.

### Follow `@main` — Rejected

A consumer's behavior would change whenever Continuum main changes.

### Floating major/minor tags such as `@v1` or `@v1.2` — Rejected as the standard

These are convenient compatibility channels but can move to newer releases. They do not satisfy
the requirement that a consumer stay on one exact version until its pin is explicitly changed.

### Independent version pins for every Continuum workflow — Rejected

This permits mixed-version workflow graphs and turns upgrades into multi-file dependency
coordination. One release entrypoint must define the whole control-plane version.

### Copy released workflows into the consumer — Rejected

This recreates implementation duplication and configuration drift. The consumer should reference a
released Continuum implementation rather than vendor it.

## Industry alignment

This decision follows GitHub's supported reusable-workflow model:

- cross-repository reusable workflows may be referenced by a release tag or full commit SHA;
- `jobs.<job_id>.uses` does not accept contexts or expressions;
- nested same-repository reusable workflows can resolve from the same commit as their caller;
- GitHub immutable releases lock a published release tag to its commit;
- full commit SHA remains GitHub's strictest stability/security reference.

References:

- https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows
- https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations
- https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases
- https://docs.github.com/en/actions/how-tos/create-and-publish-actions/using-immutable-releases-and-tags-to-manage-your-actions-releases

## Compliance

New Continuum release tooling and consumer migrations must satisfy all of the following:

- the consumer has one explicit exact Continuum dependency pin;
- the selected exact release is immutable, or the consumer uses an approved full SHA;
- nested Continuum workflow calls resolve within the same selected release commit;
- publishing a newer Continuum release does not mutate consumers;
- upgrades and rollbacks occur only through an explicit consumer action;
- no automatic-repin controller is part of the target architecture.

ADR-0001 remains in force for the centralized reusable-control-plane and thin-ingress decisions.
Where ADR-0001 describes automatic repinning or Continuum-owned automatic version advancement,
this ADR supersedes those portions.
