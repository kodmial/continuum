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
conflict/mergeability gates succeed. This is the current behaviour of
`kodmai`.

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
registrations. This is the current behaviour of `kodmai`.

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

## 9. Acceptance proof

The proof that one unchanged implementation satisfies both consumer postures is
the fixture pair under `.github/fixtures/continuum-config/`, exercised by
`.github/scripts/test_continuum_config.py`:

| Fixture | Configuration | Expected resolution |
| --- | --- | --- |
| `nanodictate.yml` | `review: true`, `release: true` | both enabled |
| `kodmai.yml` | no toggle declared | both disabled |

`kodmai`'s posture is the stronger statement, and the suite asserts it both
ways: the fixture file declares nothing, and a repository with no
`.github/continuum.yml` at all resolves identically. Nothing in
`.github/scripts/continuum_config.py` branches on either repository.

## 10. Versioning this contract

v0.1 exposes two keys. Adding a third key changes the guarantee that "this is
the complete configuration" and therefore requires a minor-version decision
made deliberately, with a stated migration story for existing consumers —
specifically, a consumer that never adds the new key must keep resolving to a
defined, documented value. Until that decision is made, entrypoints must
reject unknown keys (rule 3.2) so that a consumer that has read a newer
document is told immediately rather than silently getting a default.
