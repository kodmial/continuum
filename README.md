# Continuum

Reusable GitHub automation and project-lifecycle platform.

Continuum is being extracted from automation that has already been exercised in
real repositories. The initial reference implementation is an exact snapshot of
the NanoDictate workflows at commit
`64e89a3bcbab671513a933855e7495a07fac56bb`.

## Architecture

The normative target architecture for Continuum consumers is documented in
[docs/architecture/README.md](docs/architecture/README.md). Architecturally significant decisions
are preserved as ADRs under [docs/architecture/adr/](docs/architecture/adr/).

## Bootstrap state

The repository currently dogfoods a deliberately small active control plane:

- dependency-aware issue scheduling and priority labels;
- OpenCode execution/repair plumbing derived from NanoDictate;
- generic bootstrap CI (unit tests + configuration validation);
- conflict/CI repair;
- a temporary review-free auto-merge controller;
- a self-healing review queue reconciler (provider action disabled while
  `review.provider: none`).

The complete NanoDictate workflow set is preserved verbatim under
`reference/nanodictate-workflows/`.

CodeRabbit and the consumer-facing release adapters are intentionally **not
active workflows** yet. Their implementations are preserved in the reference
snapshot and will be reintroduced as optional adapters after the
configuration/module boundaries are implemented.

Continuum does, however, publish releases *of itself* — see
[Publishing a Continuum release](#publishing-a-continuum-release). That is a
different thing: the consumer's own artifacts are built by the consumer's own
release hook, while this repository's release is the control plane a consumer
pins with one reference.

## Consumer contract (v0.1)

The complete user-facing configuration for a Continuum consumer is two
booleans in `.github/continuum.yml`:

```yaml
review: false
release: false
```

A missing configuration resolves to exactly those values, so adopting
Continuum requires declaring nothing. `review` controls whether merging waits
for a normalized, consumer-adapter-provided review gate; `release` controls
whether Continuum invokes the consumer's release entrypoint after merge. The
review provider, platform, signing, and publication details are deliberately
not settings — they stay in consumer-owned adapters and existing repository
variables.

The normative rules are in
[docs/continuum-mvp-contract.md](docs/continuum-mvp-contract.md), and the
reference resolver plus its test suite live in `.github/scripts/`.

That contract file is distinct from the operational `.continuum.yml` this
repository validates with `continuum cli config-check`; the sections below
describe the implementation that sits behind the two toggles.

## Agent execution and build gates

Continuum owns the OpenCode lifecycle around the model: reservation, checkout,
branch, commit, push, pull request, retries, and recovery. The model is allowed
to edit the working tree; it is not the authority that decides which branch is
published. A normal coding task has a bounded 180-minute default window, and a
failed, cancelled, or timed-out issue run releases its reservation immediately
and re-enters the scheduler up to the configured attempt limit. The lease timer
is a missed-event backstop, not the primary progress mechanism.

Consumer repositories may use anonymous OpenCode models; an API key is optional
rather than a prerequisite. The upstream installer is retried with bounded
backoff and the resulting executable is verified before work starts.

Every run also loads a global OpenCode instructions file that Continuum owns
(`.github/agents/AGENTS.md`) and installs before the model starts. It holds only
policy that is true in every managed repository: how the task is scoped, how
results are reported honestly, who owns the Git lifecycle, and that the
project's own `AGENTS.md` is the source of truth for its build, test, and
release mechanics. A consumer repository's project instructions are never
modified, replaced, or shadowed. See `docs/agent-instructions.md`.

`CI` is the required merge gate. Consumers can also name optional blocking
workflows through `additional_blocking_workflows` on the reusable merge and
repair controllers. These are conditional by presence: if a path-filtered build
or packaging workflow did not run for the exact PR head, it is not applicable;
if it did run, the same exact-head run must finish successfully before merge.
A failed applicable gate enters the same one-repair-per-head controller as CI.

GitHub requires `workflow_run.workflows` to be declared statically in the thin
consumer entry workflow. Therefore a consumer that has, for example, a build or
packaging smoke workflow lists that workflow in its caller trigger and passes
the same name in `additional_blocking_workflows`; the reusable Continuum core
contains no product-specific workflow names.

## Review gate

The first reusable review adapter (`review.provider: pr-agent`) is, as of the
P0 issue #25, now **the blocking review signal** for the merge controller. The
imported NanoDictate reviewer is extracted into a provider-agnostic Continuum
adapter:

- One normalized result document (`continuum.review-gate/v1`) and one commit
  status contract: `verdict=<V> state=<S> provider=<P> head=<sha>` in the
  context named by `review.status_context` (default `continuum/review`).
- `auto-merge.yml` reads **only** that contract; it refuses to merge a PR whose
  current HEAD lacks a green, current-HEAD gate. `review.provider: none` is a
  deliberate zero-traffic opt-out and a free pass.
- The adapter never hard-codes an endpoint, model, or credential: `.continuum.yml`
  references repository **variable/secret names** only, and workflows request
  exactly those credentials (`pr-agent.yml` and `pr-agent-comment.yml`). Manual
  `/review` and `/verify <finding-id>` commands follow the trusted-actor contract
  (repository owner only) and parse via the Continuum module, never a shell or
  a GitHub expression.
- CodeRabbit is a sibling adapter behind the same normalized gate
  (`review.provider: coderabbit`). Its semantics live in the adapter, never in
  merge logic, because three CodeRabbit surfaces disagree often enough to
  deadlock a naive gate:
  - **The commit status is operational state, never authorization.** A head is
    mergeable on a durable exact-head `APPROVED` review. `Review skipped`,
    pending, and rate-limited statuses block a head that was never approved, but
    can never revoke an approval GitHub already stores for that exact head.
  - **Reviews bind to the head SHA they were submitted against.** An approval
    from an earlier commit does not authorize a new one, and no CI-versus-review
    timestamp ordering is needed because both signals already name the same SHA.
    Advisory nitpicks are superseded by a newer decisive verdict on that head;
    nitpicks published after it still block.
  - **A settled finding is stated in prose, then normalized.** When the newest
    CodeRabbit reply in a CodeRabbit-authored thread explicitly resolves it,
    the adapter resolves the thread in GitHub's own state so the repair stops
    being re-requested. An explicit `UNRESOLVED`, a failed normalization, and an
    unknown thread state all stay blocking: an unverifiable thread may
    over-report a finding but may never silently drop one.

To enable in another repository, validate a `.continuum.yml`, then call the
reusable workflow — see `fixtures/consumer-repo/` for the reference consumer.

## Adopting Continuum

A consuming repository declares two booleans and nothing else, in
`.github/continuum.yml`:

```yaml
review: false
release: false
```

A missing file is the same as both being `false`, so adopting Continuum costs
nothing until a repository opts in. `review: true` activates the review loop
above without the repository naming a reviewer or holding a reviewer credential;
`release: false` forbids the merge controller from dispatching anything.

```yaml
review: true
release: true
```

The full rules are in [`docs/continuum-mvp-contract.md`](docs/continuum-mvp-contract.md).
The wiring is:

| You write | To |
| --- | --- |
| `.github/workflows/continuum.yml` | declare the events Continuum should wake up on, and pin the one exact Continuum release you run |
| `.github/workflows/continuum-release.yml` | your own release, given the merge commit |

`continuum.yml` is the only file that names Continuum, and it names it once:

```yaml
jobs:
  continuum:
    uses: kodmial/continuum/.github/workflows/consumer.yml@v0.1.0
    secrets: inherit
```

That one line selects the scheduler, the agent, CI, packaging, review, repair,
merge, and release controllers as a single coherent snapshot — the entrypoint
reaches each of them through same-repository relative references, so nothing
inside the selected release can resolve a revision of its own. An upgrade and a
rollback are both that one line, and publishing a newer Continuum release
changes nothing here.

You own the event set, your agent's runner, your language toolchain, which of
your own checks block a merge, and your release; Continuum owns every decision.
You do not pass `review` or `release` to a controller — a second place to
declare them is a second thing that can disagree with the file you just
committed.

`fixtures/consumer-repo/` is a working instance of both files, and
`tests/test_fixture_consumer.py` runs the engine against that repository's own
configuration to prove both opt-in and opt-out behave as documented.

## Publishing a Continuum release

A release of Continuum is a commit, not an archive. The tag names that commit,
and everything a consumer executes comes out of it through the relative
references in `consumer.yml`, so a published release is complete by
construction — or it is refused.

`.github/workflows/release.yml` is dispatched by hand with the version to
publish:

```text
gh workflow run release.yml --repo kodmial/continuum \
  -f version=v0.1.0 -f commit=<full sha on main>
```

`commit` is optional and defaults to the commit the run was dispatched on. The
workflow refuses a version that is not an exact SemVer tag, and a commit that is
not already on `main`.

What a dispatch does, in order:

1. **Resolves the identity.** The exact tag, the full commit, and the previous
   release. The commit is checked out by SHA and shown to be merged.
2. **Proves the commit.** `.github/scripts/release_validation.sh` runs the same
   obligations CI runs — workflow syntax and actionlint, both unit suites,
   configuration validation, the two-toggle contract, the consumer-agnostic
   contract core, and the fixture workflows — against the release commit rather
   than the head of a branch.
3. **Derives the release.** `continuum release publish` resolves the whole graph
   reachable from `consumer.yml` at that commit, hashes every file in it, and
   computes the release notes — configuration-schema changes and compatibility
   included — from what actually changed. The result is one graph digest and a
   `release-manifest.json`.
4. **Decides, then publishes.** The publish job observes the repository — GitHub
   immutable releases, the published releases, the existing tags — and refuses
   unless immutability is already enabled, the tag does not already exist, and
   the version is not older than a published one. It then recomputes the
   candidate, refuses unless the digest matches what validation produced, and
   creates the tag at exactly that commit before creating the release. Afterwards
   it reads the release back and proves the tag still names the released commit,
   that the release is immutable, and that the manifest shipped is the manifest
   that was validated.

Two things it will never do: move an existing tag, and change a consumer.
Publishing `v0.1.1` is not a statement about `v0.1.0`; a consumer moves by
changing its own one-line reference.

Before the first release, enable GitHub's immutable releases for this
repository — Settings → Releases → Enable release immutability, or
`PUT /repos/kodmial/continuum/immutable-releases` with an admin token. The
workflow reads that setting and refuses rather than turning it on itself,
because enabling it is an administrative decision about the repository rather
than a property of any one release.

The rules are in
[ADR-0002](docs/architecture/adr/0002-consumer-controlled-immutable-releases.md);
the offline decision logic is `src/continuum/publication.py`, asserted by
`tests/test_publication.py` and audited as wiring by
`.github/tests/test_release_publication.py`.


## Parent/child delegated execution

Continuum also supports explicit cross-repository execution. A repository may
declare `delegation.role: parent` and allowlist opaque child ids, or declare
`delegation.role: child` with its id and exact parent. Repositories that omit
the block have role `none` and do not participate.

The parent keeps the actual child repository bindings in the
`CONTINUUM_CHILD_REPOSITORIES` secret rather than in public git. Every
dispatcher/worker/review run then verifies the child's own `.continuum.yml`
points back to the calling parent before doing any work. Repository visibility
is never used to discover or authorize a child.

Delegated issues opt in with `<!-- continuum-child-owned -->`; the local
consumer scheduler ignores them so delegated and local execution cannot race.
The legacy `runtime-worker-owned` marker is accepted for migration.

See [docs/parent-child-delegation.md](docs/parent-child-delegation.md) and the
`fixtures/delegation-parent/` / `fixtures/delegation-child/` examples.

## Review queue

A provider is a shared, rate-limited resource, so at most one review request may
be in flight per repository. The queue decides who gets the next slot.

**Events are wake-ups, not decisions.** Any state change that can change *who the
next eligible candidate is* starts a reconciliation, and every reconciliation
recomputes the whole queue from live GitHub state
(`continuum review queue`, `src/continuum/review/queue.py`). Nothing trusts an
event payload, so duplicate and out-of-order wake-ups are harmless, and a
candidate that is closed, merged, converted to draft, paused, reprioritized, or
whose CI finishes immediately releases its slot on the next wake-up instead of
after a timeout. The schedule is a recovery backstop for missed events, not the
progress mechanism.

- **Wake-ups** (`review-queue.yml`): every `pull_request_target` action
  including `closed` and `converted_to_draft`, submitted/dismissed reviews,
  provider comments, source-issue label changes, commit status, check runs and
  suites, workflow runs, manual dispatch, and a twice-hourly schedule.
- **Ranking** is deterministic: the first matching `review.queue.priority_labels`
  entry, then `review.queue.tie_breakers` (`source_issue`, `pr_number`,
  `created_at`, `head_sha`). A pull request inherits its source issue's priority
  label when it has none of its own.
- **The in-flight slot is keyed to `PR number + HEAD SHA`**, recorded in an
  issue comment (`<!-- continuum-review-request -->`,
  `continuum.review-request/v1`) written *before* the provider request. A moved
  HEAD, a closed owner, or a lock past `in_flight_timeout_minutes` with no
  provider response frees it; a failed provider command deletes the record.
- **The cooldown is global** and applies after ranking, so a replacement
  candidate cannot spend a slot the provider is still rate-limiting. A published
  rate-limit window is honoured even without a cooldown, and `safety_margin_seconds`
  covers the gap between the response and the next request.
- **Provider-independent**: the provider is a single configured action
  (`coderabbit` comment, `pr-agent` workflow dispatch) and `review.provider: none`
  is a zero-traffic opt-out. Eligibility, ordering, locking, and cooldown are
  decided before any provider is chosen.

Every run explains itself: candidates leaving the queue with a reason, the
selected candidate, cooldown or retry waits, released slots, and the single
scheduled action. `queue reconcile --no-apply` prints the same plan without
acting on it.

A consumer repository declares the event set in its single generated ingress and
reaches the shared reconciler through the release entrypoint it pins — see
`fixtures/consumer-repo/.github/workflows/continuum.yml`.
`tests/test_queue_workflows.py` fails if the two event sets ever drift apart.

## Release

For standalone Bun-built tools, `.github/workflows/release-bun-binary.yml`
provides a reusable immutable binary publisher. The simple contract remains
`package_dir + build_script`; projects that need an exact compiler/build
invocation can instead pass `build_argv_json` plus non-secret
`build_env_json`. The argv is executed directly with Python `subprocess`,
never through a shell, so flags and environment values remain data rather than
code.

Every published binary carries the exact source SHA, release tag, asset digest,
binary version, size, and build mode in its sidecar metadata. When a build is
expected to reproduce an already-qualified artifact, the publisher can also
require the exact SHA-256 and byte size and fails before upload on any mismatch.
A consumer using
`consumer-opencode.yml` may select a release by prefix for convenience, or
pin an exact immutable tag. Exact selection requires the expected SHA-256 and
can additionally require the binary version and source SHA; Continuum verifies
the downloaded asset, checksum file, and metadata sidecar before adding the
binary to `PATH`. This is the portable form of runtime-lab's exact-artifact
lesson: a qualified binary identity is never silently replaced by a newer
"latest" artifact.

A release target is a typed declaration in `.continuum.yml`, not a workflow.
`continuum release plan` turns it into a plan — an ordered list of argument
vectors, each one stating what it is for — and `continuum release sign` runs
it. The plan is the review surface: it is a value, so it can be printed,
diffed, and asserted on without a Mac anywhere in sight.

Only `apple` on `macos` with `swiftpm` is executable today. `ios`,
`xcode-archive`, and `app-store` are valid configuration that no adapter builds
yet, and asking for one fails with a refusal rather than a validation error, so
a repository can declare the intent before the adapter exists.

### Signing

The MVP profile is `self-signed-stable`: a self-signed code-signing certificate
held in repository secrets, imported into a keychain that exists only for the
job. It is the smallest thing that produces an upgrade users do not have to
re-authorize.

macOS keys microphone and accessibility permissions to the *signing identity*,
not to the bundle identifier. So the plan asserts the configured identity is
present in everything it signed:

- binaries are signed first, then the bundle is sealed over them with the same
  identity, so the nested code is not re-signed by a different key;
- the release version is written into the bundle's `Info.plist` *before* the
  seal, in both `CFBundleVersion` and `CFBundleShortVersionString` — the build
  number and the version Finder shows — because the signature covers the plist
  and because a bundle that reports the old version is not an upgrade;
- each product is checked for `Authority=<identity>`, and the job fails if it
  is absent — a signature from the wrong certificate is self-consistent and
  would otherwise pass;
- the result is then verified for integrity, which is a different claim;
- the keychain is deleted in teardown, and teardown runs even when signing
  failed, because a half-finished release has usually already put a private key
  on disk.

`developer-id` and notarization are schema-valid and unimplemented. They are
not on the MVP path and do not block it.

### Ad-hoc fallback

A pull request from a fork never receives the secrets, so a stable target has
nothing to sign with. There are two honest responses and one dishonest one.

Declaring `allow_adhoc_fallback: true` permits an ad-hoc build, which is signed
by nobody. The plan is then marked `degraded`, records why, and every field
that would imply a stable identity is false. macOS will not preserve TCC
permissions across such an upgrade, and the job says so in its output rather
than only in a JSON field nobody reads.

Passing `--signing-material present` turns a missing secret into a failed job
instead. That is the right setting for a release job: silently downgrading to
ad-hoc looks like success and costs every user their permissions.

### What the runner guarantees

`src/continuum/release/` is split so the platform lives in one file. `plan.py`
and `run.py` know what an argument vector is and nothing about what signs
anything — `tests/test_release_run.py` fails if either names `codesign`,
`keychain`, or `macos` in code.

- **No shell, ever.** There is no `shell=True` and no interpolation. A bundle
  identifier taken from a repository is data, not a language.
- **Secrets never printed.** Values arrive through a `${secret:NAME}` slot,
  are substituted into the argument vector, and are redacted back out of
  anything the runner records — a tool that echoes its own argv would otherwise
  publish the password into the job log.
- **Teardown always runs.** A failing job leaves nothing behind.
- **Materialization is a step, not a redirection.** Writing key material to
  disk is something the runner does deliberately and can clean up.

`continuum release sign` checks for the toolchain and for the values the plan
needs *before* the first step runs, so a job on the wrong runner fails with
"this runner has no `codesign`" instead of after creating a keychain.

### Downstream package publishers

Homebrew and MacPorts are not release adapters. They run *after* a release
exists, when they are handed the release's identity, the immutable URLs its
assets were published at, and the digests those URLs must serve. A publisher
turns those into the package manager's own description of the release; it never
builds, re-signs, or uploads anything, and it never asks the release pipeline
for a favour.

They live in `src/continuum/release/publishers/`, deliberately separate from
`release/plan.py` and `release/run.py`. Those two own the release state machine
and must not learn the name of a package manager; the publishers own everything
a package manager needs and know nothing about how the release was cut. The
seam is `contract.PublishRequest` and `contract.PublishResult` — immutable
value types that describe the *generic* contract — plus a `PackageRepository`
for the cross-repository write and an `AssetFetcher` for verification. A caller
assembles those and hands them to a publisher; the publisher returns a result
and nothing else happens outside it. `tests/test_release_run.py` fails if the
release core names a package manager, and `tests/test_release_publishers.py`
fails the other way if a publisher imports the release state machine.

A publisher is reached through `publishers.get(name)` rather than imported by
name, so adding a package manager is adding a module and a registry entry.
`homebrew` and `macports` are registered today:

- **Homebrew** renders a formula and/or a cask from the release's assets, with
  per-architecture blocks when the manifest carries them, then validates the
  Ruby syntax before writing. A cask that clears quarantine is refused for a
  signed or notarized artifact, and otherwise must carry evidence that is
  audited in the generated file.
- **MacPorts** writes the `Portfile`, its tree, and a separate installer script
  whose pinned revision is reconciled to the tree HEAD, so a moved pin is
  repaired rather than duplicated.

Every publisher is idempotent: a re-run against the same release with a
destination already at the new content returns `already-current` instead of
writing again, and a destination that moved under it is reconciled against the
new head. Updates either open a pull request or push the configured branch,
chosen by the destination's mode; secrets are checked for presence but never
read. Project data and templates are consumer-owned; the publisher only knows
the generic shape.

No *consumer-artifact* release workflow is active in this repository, and
`.github/workflows/ci.yml` enforces an allowlist of workflow names. The module
and the CLI are the deliverable; the macOS job is a follow-up. Publishing a
release of Continuum itself is a separate, already-active workflow — see
[Publishing a Continuum release](#publishing-a-continuum-release).

### The release core

The plan adapters above answer "what would this machine run". The release core
answers the question above that one: *what does a release have to do, in what
order, and what must it refuse to do twice*.

`src/continuum/release/core.py` walks one event through one fixed chain —
`eligibility → version → release-pr → validation → source → build → sign →
verify → draft → publish → sync` — and records every transition in a journal
keyed to the release rather than to the run. So a re-dispatch, a re-run of a
failed job, and a second workflow watching the same tag are three runs and one
release, and a key already recorded as complete is not run again. The key is
computed at the start, because by the time anyone could ask "is this already
published" the assets have been uploaded.

The core knows nothing about platforms. Everything platform-specific arrives as
a component satisfying a port in `contract.py` — an eligibility policy, a
version strategy, a target adapter, a notes writer, a publisher, a downstream
sync, a provenance attestor — so a new platform is an addition rather than a
fork of the loop, and a release for a platform nobody has written yet can still
be planned:

```python
outcome = core.plan(request)     # walks the whole chain, writes nothing
outcome.status                    # "planned"
outcome.planned                   # True — and nothing was published
```

A plan records itself in the journal like any other pass so a job can report
what it would do, and is ignored for idempotency so that the first plan anybody
runs cannot disable releases. `github.py` is the reference destination: a draft
that is never published with a missing asset, an attached asset that is never
replaced, a checksum file checked against the manifest, and a release that never
points at a commit it was not approved for.

The contract, the ports, and the reasoning behind each rule are in
[docs/release-core-contract.md](docs/release-core-contract.md). The plan
adapters and the release core are separate surfaces on purpose: the core is what
a new platform should be written against, and the plan adapters keep working
for the platforms that already have one.

## Public-repository safety

This repository is public, so every input the automation reads is attacker-supplyable:
anyone can open an issue, comment on one, open a pull request from a fork, and
forge a `workflow_dispatch` payload. The rules below are enforced by
[`.github/scripts/trust_policy.py`](.github/scripts/trust_policy.py) and audited by
[`.github/tests/`](.github/tests), which CI runs on every pull request.

**Who is trusted.** The repository owner, plus anyone listed in the
`AUTOMATION_TRUSTED_ACTORS` repository variable. Collaborators and bots are *not*
trusted by default — `author_association: COLLABORATOR` is a self-declared field on
an issue, so it is not evidence of identity. A `GITHUB_TOKEN` acts as
`github-actions[bot]`, whose comments the agent workflow refuses.

**What starts work.** Only a newly *created* comment whose first non-empty line is
`/oc` or `/opencode`, on a non-pull-request issue that a trusted actor opened.
Editing an existing comment cannot trigger execution.

**What gets executed.** Only same-repository pull requests against the base branch,
on an `opencode/issue<N>-<slug>` branch, at a full commit SHA. The privileged job
checks out `${{ needs.authorize.outputs.checkout_ref }}` — the base branch, or a
SHA the policy verified — never the pull request's own code.

**Separation of powers.** Each automation is split so that deciding is never the
same privilege as acting:

| Workflow | Decides (read-only) | Acts (write) |
| --- | --- | --- |
| `opencode.yml` | `authorize` | `opencode` |
| `opencode-repair.yml` | trust gate + decision step in each job | label/dispatch step |
| `auto-merge.yml` | `plan` | `reconcile` |
| `issue-scheduler.yml` | `authorize` | `schedule` |

A verification step never holds a write-capable credential, with one deliberate
exception: `check-token` must carry the PAT, because no read-only token can report
which account a credential speaks for. It performs a single `GET /user` and no
write, and the audit asserts that.

**Human review is never optional for the trust boundary.** Any change to
`.github/workflows/`, `.github/scripts/`, `.github/actions/`, `CODEOWNERS`, or
`action.yml` is refused automatic merge and automatic repair; those pull requests
get `opencode-human-review-required` instead.

**Untrusted text stays data.** Issue bodies, comments, and pull-request titles are
neutralized (terminal escapes, forged `::` workflow commands, bidirectional
overrides, zero-width characters, control bytes) and fenced before reaching a model
prompt, and titles are sanitized before becoming commit messages.

The policy fails closed throughout: a missing field, an absent API response, a
failed check, or an unrecognised shape is a denial, never a fallback to trusting
the input.

### Configuration

| Name | Kind | Default | Purpose |
| --- | --- | --- | --- |
| `TAP_PAT` | secret | — | Required. Write-capable credential for the agent, repair, merge, and scheduler steps. |
| `AUTOMATION_TRUSTED_ACTORS` | variable | repository owner | Comma-separated logins trusted in addition to the owner. |
| `AUTOMATION_WIP_LIMIT` | variable | `4` | Concurrent scheduled issues. |
| `AUTOMATION_LEASE_MINUTES` | variable | `45` | Reservation lease before an issue is retried. |
| `AUTOMATION_MAX_DISPATCH_ATTEMPTS` | variable | `2` | Dispatches before an issue is paused for a human. |
| `AUTOMATION_TRUSTED_ACTORS` must not include bots: a bot-authored comment or dispatch can never satisfy the author gate. |


## Roadmap

GitHub Issues are the execution plan. Native `blocked by` relationships define
the dependency DAG, while `priority:p0`, `priority:p1`, and `priority:p2`
define scheduling priority.

The first phase creates feature flags and security boundaries. The next phase
performs an evidence-based industry benchmark. Reusable workflows, authentication
hardening, reliability, CodeRabbit, release adapters, fault injection,
observability, versioning, and migration tooling follow from that baseline.

## Reference snapshot

See [reference/nanodictate-workflows](reference/nanodictate-workflows/README.md).
