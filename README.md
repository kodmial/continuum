# Continuum

Reusable GitHub automation and project-lifecycle platform.

Continuum is being extracted from automation that has already been exercised in
real repositories. The initial reference implementation is an exact snapshot of
the NanoDictate workflows at commit
`64e89a3bcbab671513a933855e7495a07fac56bb`.

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

CodeRabbit and release automation are intentionally **not active workflows** yet.
Their implementations are preserved in the reference snapshot and will be
reintroduced as optional adapters after the configuration/module boundaries are
implemented.

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
  (`review.provider: coderabbit`).

To enable in another repository, validate a `.continuum.yml`, then call the
reusable workflow — see `fixtures/consumer-repo/` for the reference consumer.

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

A consumer repository declares the event set in a thin entry workflow and calls
the shared reconciler — see `fixtures/consumer-repo/.github/workflows/review-queue.yml`.
`tests/test_queue_workflows.py` fails if the two event sets ever drift apart.

## Release

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

No release workflow is active in this repository, and `.github/workflows/ci.yml`
enforces an allowlist of workflow names. The module and the CLI are the
deliverable; the macOS job is a follow-up.

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
