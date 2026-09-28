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

## Public-repository safety

During bootstrap, automatic issue execution is limited to issues opened by the
repository owner. The scheduler also refuses to reserve/dispatch work when the
workflow-capable `TAP_PAT` secret is not configured.

This prevents a public issue from becoming a privileged agent prompt.

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
