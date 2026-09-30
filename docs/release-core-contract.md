# The release core contract

The release core walks one release event through one fixed chain of stages,
records every transition, and refuses to do the same thing twice. It is the
part of Continuum that a new platform is written against.

It lives in `src/continuum/release/`:

| module | what it is |
| --- | --- |
| `contract.py` | the ports, the values, and the invariants they must satisfy |
| `state.py` | the chain, its keys, its outcomes, and the journal |
| `version.py` | how one release agrees on one version number |
| `core.py` | the walk: the stage order, the failure policy, the resume policy |
| `github.py` | the GitHub Release destination, as the reference implementation |
| `github_api.py` | that destination over the real GitHub Releases API, behind the same port |
| `provenance.py` | in-toto statements, and the checks that make one worth reading |

The core holds no adapter, opens no client, and reads no file except through a
component. That is what makes a plan possible for a release nobody has run, and
what makes a new platform an addition rather than a fork of the loop.

## The chain

```
eligibility -> version -> release-pr -> validation -> source
            -> build -> sign -> verify -> draft -> publish -> sync
```

The order is the argument, and each link is a place a release can be stopped:

- **eligibility** — may this event become a release at all?
- **version** — one number, agreed from independent sources.
- **release-pr** — make sure a release pull request exists and carries it.
- **validation** — every target has an adapter, before anything is built.
- **source** — pin the release to one commit.
- **build** — build every target from that commit.
- **sign** — sign what was built, and record who signed it.
- **verify** — check the artifacts against their digests and their pins.
- **draft** — create the release that will hold the assets, unpublished.
- **publish** — publish, but only once the whole asset set verifies.
- **sync** — bring downstream records up to date.

`sign` and `verify` are separate because they are separate claims: a signature
that verifies is not a signature from the identity a release pins, and a
release has to be able to fail *between* them — which is the moment where a
wrong signing identity is still a failed job rather than a published release.

A new destination is a new publisher inside an existing stage, not a new stage.
A stage that is not on the chain has no place to be enforced from, and the
enforcement is the reason for a chain rather than a script.

### Two kinds of nothing-to-do

`Stage.optional` and `Stage.terminal` are not the same flag, because "nothing to
do" means opposite things in two places.

- An **optional, non-terminal** stage is passed through. The release pull
  request is optional: a repository that cuts releases from a tag has no release
  pull request, and requiring one would mean that repository could never
  release.
- A **terminal** stage ends the release when it has nothing to do. Eligibility
  is terminal because an event that is not releasable is not a release that
  goes on to be released. The destination stages are terminal because a release
  that reached no destination, or that every destination already holds, has
  nothing left to publish.

Either way the run is green. A no-op is a decision the policy made, not a
failure, and it is recorded with a code and a reason so a reader can tell a
switched-off release from a duplicate one.

## Idempotency is a key, not a flag

Every transition computes a key from what it is acting on:

```
continuum-ck-<digest>            # a stage, or one target, or one destination
continuum-release/<repo>/<ver>   # the release's identity
continuum-release/<repo>/<chan>  # a concurrency group, not cancel-in-progress
```

The digest form is a SHA-256 over the identifying parts, joined with a
separator that cannot appear in a part — joining with a space would let
`("1.2", "3")` and `("1", "2.3")` produce one key, and two releases would share
an idempotency key and one of them would be silently skipped. A stage key is
derived from the release; a unit key adds the target or destination under it.

A key already recorded as complete is **not run again**. That is the whole
mechanism behind "a duplicate event cannot publish a duplicate version", and it
is deliberately not a check for "is this release already published" at the last
moment: by then the build has run, the assets have been uploaded, and the
damage is done. The key is computed at the start.

The key is scoped to the **release**, never to the run. A re-dispatch, a
re-run of a failed job, and a second workflow watching the same tag are three
runs and one release, and they must be unable to become three releases. That is
why the event key is derived from the repository, the event name, and the
commit — never from a delivery id, a run id, or a timestamp.

A stage that did nothing (a no-op or a block) is asked again, and asked again
for free, because it changed nothing the first time.

### What a plan may not do

A dry run writes to the journal like any other pass, so a job can report what
it would do. What it must not do is **count**. A plan's entries are kept for the
report and ignored for idempotency, because otherwise the first plan anybody ran
would silently disable releases for that version.

A plan's entry is also ignored when it sits *on top of* a real record, rather
than replacing it. `Journal.trusted` is the last non-plan entry per key:

```python
journal.trusted           # {key: the last entry a real run wrote}
journal.completed_keys    # the keys a later run will refuse to repeat
journal.resume_point()    # the stage a resumed run should start from
```

Dropping the *last* entry instead of the last non-plan one would mean a plan of
a release that had already been published would persuade the next run to
publish it again — the exact harm the dry-run rule exists to prevent.

## Resuming

```python
outcome = core.execute(request, journal=previous.journal, manifests=previous.manifests)
```

A resumed run is given the artifacts a previous run built. If the journal
records a target as built and the run has no manifest for it, the run fails with
`unresumable-manifest` rather than publishing from a description. Guessing is
not an option: a manifest is a claim about bytes, and a claim is only worth
anything if it came from the run that made it.

Retryable means "running this again could succeed and cannot double anything",
and it is the component's call, not the core's. An upload that failed half way
can be resumed; a published tag pointing at the wrong commit cannot.

Failures that a component raised as an exception are classified the same way as
failures it returned, per stage and per unit, because a stack trace in a job log
would be read as a fault in Continuum rather than as a failure at a destination.

## One release, one version, one commit

The version is agreed from independent sources and the disagreement is fatal:

```python
agreement = agree(explicit.propose(ctx), project_file.propose(ctx))
version = require_agreement(agreement)   # raises rather than voting
```

A `VersionAgreement` with a conflict carries **no** version, so a caller that
checks nothing but `.version` cannot publish whichever number happened to sort
first.

The tag is the release's identity, so a version bound to one commit stays bound:

```
1.4.0 is already bound to aaaa…, but this event is bbbb…
```

Two events that propose the same version from different commits are not two
releases. They are one release with a contradiction in it, and the only safe
reading is to refuse.

## Ports

A component is anything that satisfies one of the surfaces in
`contract.PORT_SURFACES`. `conform()` refuses a component that cannot honour
its surface, and a component that does not name itself, at construction rather
than at the first side effect.

| port | what it must be able to do |
| --- | --- |
| `eligibility` | say whether an event may become a release, and whether it is blocked |
| `version` | propose a number from an event, and say what it read |
| `target-adapter` | build, sign, and verify one target, and return manifests |
| `notes` | write the release notes for a version |
| `release-pr` | make sure a release pull request exists and carries the version |
| `publisher` | draft, publish, and report an immutable identity |
| `downstream-sync` | record what was published |
| `provenance` | attest an artifact so a consumer can check who built it |
| `release-repository` | find, create, and promote a release, and hold its assets |

Every component declares `SUPPORTS_DRY_RUN`. One that does not support a dry run
cannot be planned for, and is refused when it is wired in rather than when a
plan discovers it half way through.

### Manifests

A manifest is the release's own record of what it built: the target, the source
commit, the version, and every artifact with its size, digest, signing status,
and verification status. Three properties are load-bearing:

- **A plan's artifacts are `declared`,** with the digest of no bytes. Nothing
  downstream can mistake a described artifact for a shipped one.
- **A publishable manifest is one whose artifacts were verified**, and
  `mark_verified()` records *who* verified them. "Verified" without an identity
  is a claim, not evidence.
- **The source and version are assertions, not fields.** `assert_publishable()`
  is called both by the state machine and, again, by the publisher, because it
  is the last code between a build and the outside world and a check that
  exists only further up is a check a future caller can reach past.

### Asset names are a flat namespace

A release addresses assets by name, not by target, so two targets that both
produce `app.tar.gz` are not two assets — they are one asset and a choice about
which one the release holds. That choice belongs to the adapter, and it is
refused rather than resolved:

```
artifact 'app.tar.gz' is claimed by 'fixture/1.4.0' and 'other/1.4.0'
```

The check is a free function, `assert_distinct_artifact_names()`, because the
collision is a property of the release's asset set rather than of any
destination: a release with two colliding targets is ambiguous whether or not a
destination is configured, so it is refused at the build rather than at the
first upload that happens to notice.

## The GitHub destination

`github.GitHubReleasePublisher` is the reference implementation and the baseline
the contract is written against:

- **A draft is never published incomplete.** Before promoting a release, the
  attached asset set is compared against every manifest artifact; a missing
  asset is named, and the release stays a draft.
- **An attached asset is never replaced.** The destination will not take a
  second asset under a name it already holds, and replacing one would
  invalidate every manifest that already records the published digest.
- **A checksum file is checked against the manifest as well as against its
  bytes**, because a checksum file is the one asset a consumer trusts without
  reading the manifest.
- **A release never points at a commit it was not approved for**, and neither
  does its draft. Re-uploading assets cannot change which commit a tag names.
- **A plan writes nothing and looks nothing up.** It describes the work, and is
  exempt from the publishability gate — its artifacts are declared, so the
  invariants a real run insists on are exactly the ones it is reporting are not
  yet true.
- **Every destination call is classified.** A port that raises its own
  `PortFailure` keeps its code; anything else becomes a retryable failure with
  the name of the call that failed.

### Wiring a declared target

`continuum release run --target <id>` is the whole integration for a target
declared in `.continuum.yml`. The command reads the configuration, builds the
`AppleAdapter`, hands the chain to `ReleaseCore`, and prints the outcome:

```
continuum release run --target macos --tag v1.4.0 --sha "$GITHUB_SHA"
```

Three things about that command are worth stating, because each is a way the
integration could have been less honest:

- **It names no platform.** There is no Apple branch in it. The target's own
  configuration supplies the bundle, the binaries, the architectures, the
  artifact formats, and the signing identity; the adapter rehydrates them from
  `TargetSpec.options`, which `ReleaseRequest.from_config()` fills in by
  describing each configured target.
- **A dry run walks the same chain.** `release run --dry-run` executes no tool
  and writes no file — not even a checksum file — and declares the same
  artifacts, by name, that the real run records. A plan that described a
  different release than the one that ran would not be a plan.
- **Publication is not wired here.** With no publisher configured, a completed
  release ends as a verified no-op. That is the contract's answer for "nothing
  to publish to", not a success that skipped a step.

The checkout is checked before anything is built. The adapter is given a
revision reader, and a real run refuses both a checkout on a different commit
(`source-mismatch`) and a directory with no readable commit at all
(`source-unreadable`) — a tarball or a vendored copy cannot promise which commit
it is, and releasing it would build bytes nobody approved.

## The release plane

The core is the contract; the release plane is the three jobs that run it.

| job | token | what it does |
| --- | --- | --- |
| `release resolve` | none | reads the policy, writes the matrix every other job reads |
| `release target` | none | builds, signs, and verifies one target; writes one fragment |
| `release transaction` | the only one | merges every fragment, publishes once, reports |

`.github/workflows/release.yml` is that split as a `workflow_call`-only reusable
workflow. It has no trigger of its own on purpose: a workflow that both offers a
release and triggers one cannot say which code was under review when the token
was in scope.

The privilege boundary is enforced by the wiring rather than by convention.
`commands.build_components()` takes no publisher argument at all, so the job that
runs a caller's build has nothing to construct a destination with even if the
build asks for one; `commands.transaction_components()` is the only function that
reads `GITHUB_TOKEN`, and it refuses without one. Both facts are asserted in
`tests/test_release_plane_cli.py`.

### The matrix is the only thing the jobs agree on

`resolve` writes `runs/matrix.json` and every other job reads that file rather
than re-deriving it. A build job that decided for itself what it was building
would be a second source of truth, and a disagreement between it and the
transaction would be discovered only at the merge.

The matrix is also what decides where a build runs: each row carries the runner
label from the static entrypoint table, and the workflow uses
`runs-on: ${{ matrix.runner }}`. A caller cannot name a runner, so a release
cannot be built somewhere the table never described.

### Secrets are scoped by adapter

`entrypoints.ENTRYPOINTS` names the secrets each adapter reads. The workflow
declares exactly those names and passes each one to the build job only when the
row's adapter is the one that reads it:

```yaml
CONTINUUM_APPLE_P12: ${{ matrix.adapter == 'apple' && secrets.CONTINUUM_APPLE_P12 || '' }}
```

The gate is visible in the workflow rather than assembled in a loop, and
`.github/tests/test_workflow_hardening.py` asserts that the workflow's `secrets:`
block and the entrypoint table name the same set — a key read by one and not
passed by the other is a release that cannot be signed.

### Declared targets are named, not hidden

A target the policy lists but no adapter can build is refused at resolve time by
default. `--include-declared` asks for the other reading: release the buildable
targets and report the declared ones as not shipped. Then the gap appears in
three places, because it has three readers:

- the release body, which is where a consumer looks;
- the job summary, which is where an operator looks;
- the `declared` output, which is where a downstream workflow looks.

A release smaller than its policy is defensible. A release that is smaller
without saying so is not.

### Two kinds of release event

`tag-push` takes its identity from the tag that was pushed, and the version
input is cross-checked against it — two independent sources that must agree.

`workflow-dispatch` has no tag, because nobody pushed one. The version input is
then the only source of the version, which is why the publishing job sits behind
a required `environment` input: that approval is what stands in for the second
source. `load_event()` refuses to synthesise `v<version>` for a dispatch, because
a fabricated tag would reach the journal, the release body, and the publisher's
identity without anything in the repository backing it.

### What is not expressible yet

`.continuum.yml` still declares `adapter: apple` and still requires a target to
name a binary or an `app_bundle`, because that schema is the MVP consumer
contract and widening it is a consumer-facing change rather than a detail of
this one. Apple now has both its signing plan and a release adapter, so a declared
Apple target can enter the generic release chain without a platform branch in the
core. A repository that wants to declare a non-Apple target in configuration
still needs that enum widened; the core needs nothing.

## Permissions

`state.RELEASE_SCOPES` and `state.PR_AUTOMATION_SCOPES` are asserted disjoint
at construction, by `scopes_are_isolated()`. The failure that prevents is a
release job quietly holding the token that can label, approve, and merge pull
requests: a release that can merge is a release that can publish something a
human never reviewed.

## Reading the outcome

`ReleaseOutcome` is a value, so it can be printed, diffed, and asserted on. It
separates questions that are usually conflated:

```python
outcome.ok          # did the job go green?
outcome.released    # is a destination holding this release right now?
outcome.planned     # was this a dry run?
outcome.no_op       # was there correctly nothing to do, and why?
outcome.failed      # which stage, and is it resumable?
outcome.resumable   # could running this again succeed without doubling anything?
```

`ok` and `published` are different claims: a plan is green, a release with no
destination configured is green, and neither has published anything.

## Tests

- `tests/test_release_state.py` — the chain, the keys, the journal.
- `tests/test_release_contract.py` — the values and their invariants.
- `tests/test_release_version.py` — the strategies and the agreement.
- `tests/test_release_core.py` — the walk, end to end, over a fixture adapter
  that is deliberately not a platform.
- `tests/test_release_github.py` — the destination, against a repository that is
  a dict with the shape of the API.
- `tests/test_release_github_api.py` — the same destination over the real API's
  wire shape: classification, re-reads, and the immutable-release refusals.
- `tests/test_release_provenance.py` — statements, and every way one can be made
  to say something other than what it was built from.
- `tests/test_release_entrypoints.py` — the static table, the matrix it produces,
  and the anti-tamper checks a matrix has to survive between two jobs.
- `tests/test_release_transaction.py` — fragments, the merge, target convergence,
  and the failure taxonomy.
- `tests/test_release_plane.py` — eligibility and the generated release notes.
- `tests/test_release_plane_cli.py` — the three commands, as separate processes
  sharing only files, which is how the workflow runs them.
- `.github/tests/test_workflow_hardening.py` — the release workflow's own
  boundaries: who holds a token, who runs a checkout, and what a secret reaches.

- `tests/test_release_apple.py` — the Apple plans: which arguments, in which
  order, with which identity.
- `tests/test_release_apple_walk.py` — the same adapter walked end to end, with
  `tests/apple_toolchain_support.py` standing in for `swift`, `codesign`,
  `security`, and `lipo`. The plans, the runner, the manifest building, and the
  packaging are the real ones; only the process boundary is replaced.

`tests/release_core_support.py` holds the shared fixture: an adapter that writes
a text file and counts every side effect, an in-memory release repository, and
in-memory provenance and sync components. The counters are the point — an
idempotent release that is only idempotent because nothing happened is not
idempotent, so every side effect the contract can cause is counted and a second
run over the same event has to leave all of them unchanged.
