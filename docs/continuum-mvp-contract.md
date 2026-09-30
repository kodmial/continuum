# Continuum v0.1 consumer contract

Status: **normative**. This document defines the complete user-facing
configuration surface for Continuum v0.1 and the resolution rules that every
Continuum entrypoint must apply.

Scope: this contract is intentionally the smallest thing that can be called a
contract. It is not a configuration framework, and it is not a schema
registry. Adding a key is a contract change, not a feature request.

## 1. The entire configuration

A consumer declares its configuration in exactly one file:

```
.github/continuum.yml
```

The complete contents of that file are:

```yaml
review: false
release: false
```

That is the whole user-facing feature configuration for v0.1. There are no
other keys, no sections, no profiles, and no inheritance.

## 2. Resolution rules

Let `R` be the resolved configuration. Then:

| Situation | `R.review` | `R.release` |
| --- | --- | --- |
| File absent | `false` | `false` |
| File present but empty | `false` | `false` |
| File present, key omitted | `false` | `false` |
| File present, key present | that boolean | that boolean |

Equivalently: **a missing configuration is `review: false, release: false`.**
Omission and explicit `false` are indistinguishable after resolution, and that
is the intent — a consumer can adopt Continuum by doing nothing.

A default may never be `true`. A toggle that is on must have been asked for
explicitly.

## 3. Validation rules

1. **Booleans only.** Each of the two keys accepts exactly the bare tokens
   `true` and `false`. Quoted forms (`"true"`), YAML 1.1 truthy spellings
   (`yes`, `no`, `on`, `off`, `1`, `0`), and empty values are **rejected**.
2. **Unknown keys are rejected.** A key other than `review` or `release` is a
   hard error, and the error names the permitted keys. This is a deliberate
   choice over silent ignoring: a typo such as `relase: true` must not resolve
   to a successful run with release quietly disabled.
3. **Duplicate keys are rejected.**
4. **Keys are case-sensitive.** `Review: true` is an unknown key, not `review`.
5. **The document is a flat top-level mapping.** Indented content, nested
   mappings, sequences, and flow style are rejected. Comments — full-line and
   trailing — are allowed and ignored.
6. **A contract violation fails the entrypoint loudly.** Resolution never
   degrades to defaults on invalid input; the difference between "absent" and
   "wrong" is the difference between a supported default and a typo. For the
   same reason a configuration path that exists but is not a readable regular
   file is an error, not an absence — silently defaulting there would be
   indistinguishable from a consumer that deliberately turned both toggles off.

## 4. Meaning of `review`

### `review: false`

Continuum does not wait for, request, or evaluate any review gate. A pull
request may merge once the consumer's own current-head CI and
conflict/mergeability gates succeed. This is the current behaviour of a
repository that declares neither toggle.

### `review: true`

Continuum requires a **normalized review gate** to be satisfied before it
will merge.

The provider is deliberately **not** a user-facing setting in v0.1. `true`
activates the single review implementation Continuum ships, and the repository
never names, selects, or configures it. Continuum is not asking a consumer to
assemble a reviewer before it can merge.

What ships in v0.1 is the review adapter, the finding normalizer, and the
bounded repair loop:

```
provider finding -> normalized findings on an exact HEAD
                  -> bounded agent repair on the existing branch
                  -> push -> CI
                  -> provider re-check of the original findings
                  -> merge on a green current-HEAD gate
```

Three properties of that loop are contract, not implementation detail:

- **The gate is exact-head.** A review of one commit decides that commit. A new
  push invalidates it, and a green gate is never carried across HEADs.
- **Repair is bounded and fails closed.** Attempts are counted per HEAD and per
  finding, an unchanged repair is asked about rather than retried indefinitely,
  a full re-review happens at most once, and the budget then pauses the loop
  rather than merging.
- **Repair never re-executes the source task.** It receives the normalized
  findings for one commit and nothing else.

The consequence of the portability guarantee is unchanged: the **merge core is
provider-neutral**. It consumes one normalized signal and never parses
vendor-specific reviews, comments, labels, or check names. The provider sits
behind an adapter inside Continuum, so adding or swapping one is a change to
that adapter and never a change to this file or to the two toggles.

## 5. Meaning of `release`

### `release: false`

Continuum performs no release orchestration after a merge and generates zero
release traffic — no tags, no releases, no package publications, no
registrations. This is the current behaviour of a repository that declares
neither toggle.

### `release: true`

Continuum invokes the consumer's thin release entrypoint after the normal merge
lifecycle completes.

For v0.1 the release implementation stays consumer-owned. Continuum defines
when the hook is invoked and what it hands over; it does not implement the
consumer's release internals. Generic release extraction is post-MVP work.

The same adapter boundary applies as for review: publication targets, artifact
formats, and signing are the consumer's business in v0.1, reached through its
own release entrypoint.

## 6. Explicitly not configurable in v0.1

None of the following is a v0.1 setting, and no entrypoint may read one:

- platform, language, or build system;
- Apple signing mode, entitlements, or notarization;
- package managers and distribution channels;
- Android/JVM toolchains;
- review provider selection;
- artifact formats;
- runner OS or runner image;
- WIP limits, lease durations, and retry counts.

These differences are already expressed in consumer-owned local CI, consumer
adapters, and existing repository variables. Continuum reads the existing
repository variables for scheduling behaviour; it does not introduce a second
place to declare them.

### 6.1 Release signing is an adapter concern, not a toggle

Signing deserves an explicit note because it was raised as a schema question.
The requirement it expresses is real — Apple signing must be a typed strategy
(`self-signed-stable` as the first-class path, `adhoc` as an explicit degraded
fallback, `developer-id` as an optional paid profile), and a stable
self-signed identity must be sufficient on its own so that Apple Developer
Program membership and notarization are never a prerequisite.

In v0.1 that strategy belongs to the consumer's release entrypoint, reached
through `release: true`, for the same reason the review provider does: the
platform belongs to the consumer. Adding `signing:` to this file would make
v0.1 a configuration framework, which this contract exists to avoid. The
strategy is specified and implemented where the code lives — the Apple/macOS
release adapter (#18 for the stable self-signed MVP, #26 for the optional paid
profile) — and a consumer migrating under `release: true` uses
`self-signed-stable` alone.

The consequence to hold onto: Continuum must never require a paid signing mode
for its schema or its MVP to be valid.

## 7. Observability

Every entrypoint that resolves the configuration must publish the resolved
values to the job summary, including the config path, whether the file was
present, and which defaults were applied. Resolved values must never be
readable only from a debug log.

Reference implementation: `.github/scripts/continuum_config.py`. It exposes a
library entrypoint and a CLI that writes both `$GITHUB_OUTPUT` and
`$GITHUB_STEP_SUMMARY` when those files are provided.

## 8. Consumer-agnostic requirement

Continuum code must contain no repository, project, product, provider, or
vendor name in any branch of control flow. One unchanged copy of Continuum
resolves every consumer's configuration, including consumers that do not exist
yet. This is enforced by a test that scans the contract core for such names.

## 9. Adopting the contract

The configuration above is what a consumer declares; this section is how it
becomes reachable. A reusable workflow cannot subscribe to a consuming
repository's own pull request, CI, or schedule events, so the consumer necessarily
writes the entry workflows itself. That split is the reason the two toggles are
the *whole* of what a consumer configures: everything else a consumer needs to
state is either a property of its own repository or an input of a shared
controller, never a second copy of a decision.

### 9.1 Reusable surfaces

| Surface | Called by the consumer to |
| --- | --- |
| `.github/workflows/consumer-opencode.yml` | run the agent in one of four modes: `issue`, `resolve-conflict`, `ci-fix`, `review-fix` |
| `.github/workflows/consumer-scheduler.yml` | dispatch dependency-aware work on the consumer's own schedule |
| `.github/workflows/consumer-review-gate.yml` | close the review loop: observe, repair, re-check |
| `.github/workflows/consumer-repair.yml` | repair a merge conflict or a failing gate on the existing branch |
| `.github/workflows/consumer-auto-merge.yml` | reconcile merge eligibility on every wake-up |
| `.github/workflows/pr-agent.yml` | run the review provider itself: provider request, normalized findings, verdict, blocking commit status |

Every call pins an immutable Continuum commit. A branch ref would make a
consumer's security model a function of whatever `main` holds when the workflow
next fires, which is precisely the property a reviewer cannot check by reading
the file. `pr-agent.yml` goes one step further and needs no second pin at all:
it resolves its engine from `job.workflow_sha`, the commit of the workflow file
itself, so the one reference the consumer writes selects an atomic snapshot of
the whole provider and cannot drift from it.

`pr-agent.yml` is the one surface a consumer reaches *directly* rather than
through a toggle, because selecting a review provider is configuration rather
than a boolean: `.continuum.yml` names the provider and the repository variables
and secrets it is reached through, and the consumer's entry workflow maps those
names onto the reusable inputs. `fixtures/provider-consumer/` is that
repository, and it is the whole cost of adoption — a configuration file and a
dispatch entry, with no provider image, model routing, checkout, or gate in it.

### 9.2 What a consumer's entry workflows may state

The consumer owns the event set, and with it everything that is a fact about its
own repository:

- which of its own workflows runs (its agent workflow, its release entrypoint),
- which of its own checks block a merge, as exact-head workflow names,
- where its contract file lives,
- the runner and language toolchain its agent needs,
- its scheduling policy — WIP limit, lease duration, attempt budget — as inputs
  of the shared scheduler rather than as configuration Continuum would have to
  look up.

### 9.3 What a consumer's entry workflows may not state

- **`review` or `release`.** These are read from `.github/continuum.yml` on the
  base branch, on every run. A second place to declare them is a second thing
  that can disagree with the file a reviewer reads. The shared controllers
  therefore expose no such inputs, and `.github/workflows/ci.yml` actionlint-checks
  that they do not appear.
- **A reviewer, a reviewer credential, a release target, or a platform** — in the
  entry workflows of a consumer that adopted the two-boolean contract. `review:
  true` activates the single review implementation Continuum ships. Which reviewer
  that is is Continuum's business, and adding a selector to the consumer's file
  would make the "two booleans, nothing else" guarantee false. The exception is
  the provider surface, which is not selected by a boolean at all: a consumer
  that adopts `review.provider` in `.continuum.yml` *does* name the provider,
  because that is what selecting one means. Its entry workflow may therefore
  name the provider credential, and may do nothing else — no provider image, no
  model routing, no checkout, no diff, and no gate. `fixtures/provider-consumer/`
  is that entry, and `tests/test_fixture_consumer.py` fails if any of the
  forbidden machinery appears in it.
- **Anything derived from an event payload that the pull-request author
  controls**, other than a candidate pull-request number. A ref or a commit
  taken from an untrusted payload is an instruction, and every controller
  re-derives the live state it acts on instead of trusting the value it was
  handed.

### 9.4 Reference consumer

`fixtures/consumer-repo/` is a complete, working instance of this section: the
contract file, and five entry workflows covering the merge, review, repair,
schedule, and release surfaces. It is asserted to be a valid one by
`tests/test_fixture_consumer.py`, which drives the engine with that
repository's own contract file and its own workflow names — so if adopting the
contract ever required a reviewer name, a platform, or a caller-supplied
boolean, the reference consumer would stop satisfying the contract rather than
quietly diverging from it.

`fixtures/provider-consumer/` is the instance for §9.1's provider surface, and
is held to the same standard from the other side: the caller passes exactly the
inputs and secrets the reusable surface declares, omits only the ones it
already defaults, declares exactly the wake-up the review queue dispatches, and
contains no review implementation of any kind.

## 10. Acceptance proof

The proof that one unchanged implementation satisfies both consumer postures is
twofold: the configuration resolves identically for both, and both postures are
reachable through the wiring above.

### 10.1 Both configurations

The fixture pair under `.github/fixtures/continuum-config/`, exercised by
`.github/scripts/test_continuum_config.py`:

| Fixture | Configuration | Expected resolution |
| --- | --- | --- |
| `enabled.yml` | `review: true`, `release: true` | both enabled |
| `declaring-nothing.yml` | no toggle declared | both disabled |

The declaring-nothing posture is the stronger statement, and the suite asserts
it both ways: the fixture file declares nothing, and a repository with no
`.github/continuum.yml` at all resolves identically. The fixtures are named for
the posture they prove rather than for the repository that first needed them, so
that deleting a consumer never leaves a fixture -- or a hard-code -- behind.
Nothing in `.github/scripts/continuum_config.py` branches on either fixture.

### 10.2 Both behaviours, end to end

`tests/test_fixture_consumer.py` runs the engine's review reconciler against the
fixture consumer's own contract file, dispatching into the fixture consumer's
own agent workflow name, and asserts the two outcomes that matter:

| Posture | Observed |
| --- | --- |
| `review: false` | no provider read, no status written, no comment posted, no dispatch — the opt-out costs nothing |
| `review: true` | exactly one bounded dispatch, in the generic `review-fix` mode, naming no reviewer |

## 11. Versioning this contract

v0.1 exposes two keys. Adding a third key changes the guarantee that "this is
the complete configuration" and therefore requires a minor-version decision
made deliberately, with a stated migration story for existing consumers —
specifically, a consumer that never adds the new key must keep resolving to a
defined, documented value. Until that decision is made, entrypoints must
reject unknown keys (rule 3.2) so that a consumer that has read a newer
document is told immediately rather than silently getting a default.
