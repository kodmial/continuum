# Continuum contract qualification (kodmial/continuum#286)

One merge-blocking qualification contract with three layers. The
deterministic rules live in `src/continuum/contract_qualification.py`;
the executable gate is the reusable workflow
`continuum-contract-qualification.yml` (surfaced as the **Contract
qualification** check); the merge enforcement lives in both merge
reconcilers.

## Layer 1 — deterministic regression gate (every PR)

- The `Contract qualification` workflow checks out the exact PR HEAD and
  runs the full Continuum CI/contract suite on it: `scripts/test-continuum.rb`,
  the Python engine suite (`tests/`), and the delegation runtime suite.
- Gate evidence must belong to the exact current PR HEAD. A run for any
  other SHA is stale and never satisfies the gate.
- Fail closed: absent, stale, or red evidence blocks every merge path,
  including generic auto-merge and PR-Agent merge. There is no bypass.

## Layer 2 — lifecycle integration qualification (lifecycle changes)

Exercises the real state-machine path end to end:

`issue -> scheduler/admission -> worker -> implementation PR -> CI ->
review -> repair/re-review if needed -> merge -> source issue
reconciliation/close`

The probe explicitly verifies:

- `automation:in-progress` implies an attributable live run/PR/lease,
  never a dead label;
- stale leases are released/recovered without human intervention;
- completed/failed/cancelled workers cannot strand an issue;
- duplicate wakeups coalesce instead of duplicating work;
- WAIT/backpressure is distinguishable from ERROR;
- the watchdog moves stalled work forward within a bounded budget and
  otherwise fails loudly;
- exact-HEAD evidence is required before merge;
- both generic auto-merge and PR-Agent merge obey the same contract
  (both consult the exact-HEAD `Contract qualification` run immediately
  before any `pulls.merge` call).

## Layer 3 — live canary (scheduled)

- The installed `continuum-contract-qualification.yml` caller also runs on
  a daily schedule (`23 5 * * *`) plus `workflow_dispatch` with
  `mode: canary`.
- The canary probe checks forward progress against bounded per-stage
  lifecycle deadlines (see `CANARY_STAGE_DEADLINES`).
- A stage past its deadline records the exact canary/issue/PR/run IDs and
  the stalled stage, triggers bounded autonomous recovery where safe
  (`MAX_CANARY_RECOVERY_ATTEMPTS`), and otherwise fails loudly.

## Main-branch safety (server-side, required)

Code cannot protect `main` by itself. Configure GitHub branch protection
(a ruleset) on `main` with all of the following:

- Require a pull request before merging; no direct pushes.
- Require status checks to pass before merging, including at minimum:
  - `CI` (the primary regression suite), and
  - `Contract qualification` (this contract gate).
- Require branches to be up to date before merging (so the exact-HEAD
  evidence the merge reconcilers revalidate is the evidence that was
  reviewed).
- Do not grant bypass allowances for normal development. Bypass, if it
  exists at all, is reserved for documented break-glass with post-merge
  review, never for routine changes.
- Block force pushes and branch deletion.

With that protection in place, the Definition of Done holds:

- A deliberately broken lifecycle change is rejected before merge
  (red/stale/absent exact-HEAD evidence blocks both merge paths).
- A deliberately stranded `automation:in-progress` scenario is detected
  and recovered, or fails loudly in qualification.
- The scheduled live canary proves forward progress on current `main`.
- Every supported merge path is blocked when contract evidence is
  absent/stale/red.
- No direct development commit can reach `main` outside the protected PR
  path.
