# Task snapshot contract (kodmial/continuum#295)

The issue title and body are **immutable for a single implementation
generation once admission starts**. This document defines the versioned
contract record, the entry points that enforce it, and the migration
behavior for legacy work.

## Canonical record

* **Encoding:** UTF-8. Titles/bodies normalize line endings (`\r\n`/`\r`
  to `\n`) and Unicode NFC. No trimming: trailing whitespace is part of
  the specification. `None` bodies read as `""`.
* **Digests:** SHA-256 hex over UTF-8 bytes, per field (`title_sha256`,
  `body_sha256`). The combined `spec_sha256` is SHA-256 over the ASCII
  string `"<title_hex>:<body_hex>"`, so no separator ambiguity can merge
  distinct pairs.
* **Identity:** `repo` (lowercased `owner/name`), `issue` (positive
  integer), `generation` (positive integer, default `1`).
* **Writer authority and creation time:** the snapshot comment is trusted
  only from repository-side authority (`OWNER`/`MEMBER`/`COLLABORATOR`
  association) or the workflow-owned `github-actions[bot]` login or the
  repository-owner login. `created_by` records the writer; `created_at`
  records creation time (authoritative GitHub comment timestamps order
  competing pins).
* **Durable form:** a bot-owned hidden-marker issue comment. The first
  line is the machine-readable marker
  `<!-- continuum-task-snapshot repo=… issue=… generation=… title-sha256=… body-sha256=… spec-sha256=… -->`
  followed by a fenced `json` block with the full record (recoverable
  exact title/body). Human prose in the comment is never parsed as state.
* **PR provenance:** implementation PRs carry the short reference
  `<!-- continuum-task-snapshot-ref repo=… issue=… generation=… … -->`
  next to the existing `continuum-task-context` marker. Reviews and
  merges validate the reference; the reference never replaces the issue
  snapshot.

The executable definition is `src/continuum/task_snapshot.py` (pure,
standard library only, no I/O). Workflow-embedded digest code mirrors it
byte-for-byte and cites it. Shell steps that run with the engine checked
out (delegated child worker/review) call
`.github/scripts/task_snapshot_admission.py` instead of duplicating the
rules.

## Admission and lifecycle gates

Admission is idempotent with stable generation identity: the earliest
trusted snapshot for `(repo, issue, generation)` wins and is never
overwritten. Simultaneous scheduler/agent/event wakeups converge on the
earliest pin; a loser whose live read differs stops with drift instead
of rebasing silently. Comment creation is atomic at the GitHub API
level: pin persistence failure fails closed and the agent must not
implement live mutable text.

## Fresh authoritative live reads (kodmial/continuum#309)

The live title/body compared against the pinned snapshot must come
from a **fresh authoritative GitHub issue API response fetched inside
the exact verification step**. Title/body text carried through prior
GitHub Actions step outputs or environment variables is not an
authoritative live source: long bodies (~55,021 characters, as in the
AA #315 incident) can be altered, truncated, or interpolated in
transit and read back as false contract drift, stopping a generation
whose live issue is byte-identical to its pinned snapshot.

* `continuum-opencode.yml` → Pin and verify task specification
  snapshot re-fetches the issue with the repository installation token
  (TAP_PAT only as a bounded 401/403/429 liveness fallback) and hashes
  that response. The previous step's `task_title`/`task_body` outputs
  are not consumed by admission at all.
* The delegated child worker already re-reads the live issue
  immediately before `task_snapshot_admission.py decide`, and re-reads
  it again after pinning so a concurrent edit is observed.
* Every digest uses the same normalized source (UTF-8, NFC, LF) for
  the pinned record, the live comparison, and the exact agent input in
  `RUNNER_TEMP/continuum-task-snapshot.json`. A hermetic ~55k
  regression fixture (Cyrillic, Markdown tables, JSON, newlines,
  XML-like tags) proves an identical long body admits exactly once
  with zero false drift while a one-codepoint edit fails closed.

## Terminal generation-stop state (kodmial/continuum#309)

A truly drifted or tampered generation stops **exactly once** and is
never redispatched. The stop is a deterministic typed issue comment:

`<!-- continuum-task-generation-stop repo=… issue=… generation=… pinned-spec=… live-spec=… reason=drift|tampered -->`

* **Key:** `(repo, issue, generation, pinned/live hashes, reason)`.
  Digests only — never title/body text — so the record cannot leak
  prompt or user content.
* **Publisher:** the first run that observes true drift/tampering
  (same-repo admission, pre-agent and pre-publication revalidation,
  delegated child worker) publishes the single stop comment, then
  fails/holds. Later runs see the stop and publish nothing further:
  no endless identical `/oc`/drift pairs, no repeated worker
  dispatches, no ghost WIP leases.
* **Gates:** the parent delegated child queue, the legacy child
  dispatcher queue, the same-repo scheduler (candidate selection and
  just-in-time pre-dispatch), event-wake/cron/retry/lease-reclaim
  paths, the delegated child worker and review, and manual `/oc`
  admission all check the stop before dispatching, with the auditable
  skip reason `terminal generation stop: …`. Stopped generations never
  consume WIP and never fail the pass; unrelated tasks keep flowing.
* **Recovery (bounded, deterministic):** a successor issue with an
  explicit dependency, or an explicit new generation number, is a
  different key and stays schedulable without touching the stopped
  record. The only in-place re-arm is an explicit trusted owner/bot
  recovery comment posted strictly after the stop:
  `<!-- continuum-task-generation-recover repo=… issue=… generation=… -->`.
  No speculative auto-repair, no silent rebaseline, no reuse of a stale
  body across generations. `automation:blocked` dependency semantics
  are unchanged: the stop is a dedicated identity, never a way to
  bypass authorized dependent work.

| Gate | Location | Behavior |
|---|---|---|
| Admission pin/verify | `continuum-opencode.yml` → Pin and verify task specification snapshot | Pins before any agent work; writes the exact record to `RUNNER_TEMP` for the job; `setFailed` on drift/tamper |
| Agent input freeze | `continuum-opencode.yml` → Implement issue | Fails closed unless verified; prompts OpenCode with the snapshot file content, not a later live body |

The pin deliberately precedes the Definition-of-Ready and duplicate-PR
gates: per invariant 1 the freeze starts at admission (scheduler
reservation or manual `/oc` execution), not at agent start, so even a
readiness-gated trigger freezes its generation. A later edit is drift
with the same follow-up-issue remedy, never a silent rebaseline.
| Pre-PR revalidation | `continuum-opencode.yml` → Implement issue (before `gh pr create`) | Re-reads the live issue; drift posts a diagnostic and stops publication |
| PR provenance | `continuum-opencode.yml` → Implement issue / Recover agent-managed issue branch | Embeds the snapshot reference in every created PR body |
| Repair guards | `continuum-opencode.yml` → CodeRabbit fix, conflict resolve, CI fix | Re-reads live vs PR reference; drift stops repair (no spurious "fixed"); legacy PRs proceed explicitly unprotected |
| Repair dispatch hold | `continuum-opencode-repair.yml` → both CI dispatch paths | Drift or unverifiable contract holds the `ci-fix` dispatch with a deduplicated diagnostic; no blind retry loop |
| Pre-merge revalidation | `continuum-auto-merge.yml` → before `pulls.merge` | Re-reads live vs PR reference; drift or failed verification holds the merge with a diagnostic comment |
| Child admission | `continuum-consumer-child-worker.yml` → Execute child task | Pins/verifies with the canonical engine; drift holds before any agent work; PR carries the reference |
| Child review | `continuum-consumer-child-review.yml` | Reviews the frozen record; drift holds the review with no verdict |

Labels, comments and open/closed status never affect verification: only
title/body digests are compared, so label churn is not drift. The
automation never rewrites or reverts the user's issue body. Post-admission
changes belong in a **new follow-up issue** with an explicit dependency;
the working issue and its active PR are left untouched.

## Scope notes

* Scheduler-dispatched and owner-triggered `/oc` flows share one
  contract: both enter through the same admission pin (first writer
  wins), so no per-trigger variant can drift.
* Zero-diff and interruption retries reuse the same generation and the
  same snapshot; no re-pin, no rebaseline.
* Review paths that consume only review threads (not the issue spec),
  such as PR-Agent finding repair and CodeRabbit verification replies,
  are backstopped by the pre-merge revalidation: drifted work can be
  touched by finding-scoped repair but can never merge.
* Stale-generation retries (a PR reference for another generation) stop
  instead of repairing across generations.

## Migration for legacy tasks

Work that started before this guard has no snapshot comment and no PR
reference. It is classified `legacy_unpinned` and is **never falsely
declared protected**:

* In-flight legacy PRs may finish their current generation: repair
  guards log an explicit notice and proceed without drift protection,
  and the merge gate applies only the pre-existing gates.
* Every fresh issue-mode admission pins first: there is no path that
  starts new agent work on live mutable text.
* Adopting protection for a legacy issue is explicit: the next admission
  for a new generation pins the then-current title/body.

## Recovery metadata

The per-run exact record at `RUNNER_TEMP/continuum-task-snapshot.json`
is job-scoped (never committed, never part of the task diff). The
durable contract is always the GitHub snapshot comment, which survives
fresh-VM recovery: resumed runs re-verify live content against it and
rewrite the job-scoped file.
