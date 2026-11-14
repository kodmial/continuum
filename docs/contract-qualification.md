# Continuum contract qualification (kodmial/continuum#286)

One merge-blocking qualification contract with three layers. The
deterministic rules live in `src/continuum/contract_qualification.py`;
the executable gate is the reusable workflow
`continuum-contract-qualification.yml` (surfaced as the **Contract
qualification** check); the merge enforcement lives in both merge
reconcilers.

## Layer 1 — deterministic regression gate (every Continuum PR)

- The `Contract qualification` workflow checks out the exact PR HEAD and
  runs the full Continuum CI/contract suite on it: `scripts/test-continuum.rb`,
  the Python engine suite (`tests/`), and the delegation runtime suite.
- Gate evidence must belong to the exact current PR HEAD. A run for any
  other SHA is stale and never satisfies the gate.
- Fail closed inside the `kodmial/continuum` repository: absent, stale, or
  red evidence blocks both Continuum merge paths (generic auto-merge and
  PR-Agent merge). Consumer/Child repositories do not run this gate; they
  retain their own CI, packaging, and configured required-workflow gates.

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

- The project-owned `contract-qualification-canary.yml` entry in the Continuum
  repository runs on a bounded schedule (`43 */6 * * *`) plus
  `workflow_dispatch`. It is intentionally not installed into consumers because
  its regression layer executes Continuum's own test suite.
- The canary runs in a dedicated reusable-workflow job that holds issue and
  action writes for bounded recovery; the pull-request qualification caller
  stays verify-only so untrusted PR code never shares those permissions.
- The canary derives its stage from real issue, run, and pull-request state. It
  never reports a completed end-to-end pass from constructed data.
- The previous implementation required an operator-set `CONTINUUM_CANARY_ISSUE`
  variable and therefore failed before observation on run
  [43497991808](https://github.com/kodmial/continuum/actions/runs/43497991808).
  That run's `qualify / Qualify` job succeeded, then `canary / Live canary`
  failed in `Observe the live canary task` with
  `CONTINUUM_CANARY_ISSUE must name the live canary issue; refusing to report
  canary success without an observed task.` The captured failure is preserved in
  `tests/fixtures/contract-canary/run-43497991808.json` and drives the
  missing-configuration regression test.
- `CONTINUUM_CANARY_ISSUE`, when set, still overrides discovery for existing
  installations. When it is unset, the scheduled dogfood canary discovers one
  repository-owned issue in `kodmial/continuum` by its stable
  `<!-- continuum-contract-canary repo=kodmial/continuum task=canary-main -->`
  marker and `automation:ready` label.
- If no owned canary exists, the canary creates one harmless issue whose only
  permitted task is updating `docs/contract-canary-fixture.txt`. The ordinary
  scheduler, OpenCode worker, CI, Contract qualification, guarded merge, and
  source-issue reconciliation own the rest of the lifecycle; the canary never
  opens a pull request or writes to `main`.
- A fresh canary issue is created only when the previous one is conclusively
  complete (closed through a merged PR with exact-HEAD `CI` and `Contract
  qualification` evidence) and the documented seven-day window measured from
  its creation has elapsed. A completed canary inside that window is observed
  and reported with its age instead of being replaced. Older completed canaries
  remain closed history; only more than one *open* owned canary is an ownership
  conflict.
- Duplicate or malformed markers, missing labels, untrusted authors, more than
  one open owned canary, incomplete pagination, and every GitHub read failure
  (403/404/429/422/5xx) fail closed. Issue and pull-request inventories are
  fully paginated; a completed canary refetches runs for its exact PR HEAD so a
  bounded global run page cannot hide the evidence. The workflow never creates a
  duplicate from uncertain metadata. The scheduled entry also serializes canary
  runs so two deliveries cannot race an empty inventory.
- Bootstrap is allowed only in `kodmial/continuum`. No repository
  administration/settings API, new PAT permission, new secret, consumer write,
  or child write is used.
- The canary probe checks forward progress against bounded per-stage
  lifecycle deadlines (see `CANARY_STAGE_DEADLINES`).
- A stage past its deadline records the exact canary/issue/PR/run IDs and
  the stalled stage, triggers bounded autonomous recovery where safe
  (`MAX_CANARY_RECOVERY_ATTEMPTS`), and otherwise fails loudly.
- The commit status is `success` only after the real canary issue is closed
  through a merged PR with exact-HEAD CI/contract evidence. A live lifecycle
  that is still progressing publishes `pending` with its observed stage and
  age; it is not counted as a completed end-to-end pass. The first post-merge
  run therefore reports `bootstrapped`/`pending`; only the subsequent recorded
  issue/PR/HEAD/merge/run sequence is live end-to-end proof.

### Manual dispatch and the legacy `mode` input

`mode` remains a public `workflow_call` input on
`continuum-contract-qualification.yml` (`type: string`, `default: verify`,
accepted values `verify` and `canary`) so existing callers that still pass
`mode:` do not fail input validation. `mode: verify` is the PR-evidence path.
`mode: canary` additionally runs the read-only canary-deadline contract probe
(`evaluate_canary`), which is the legacy behavior of that input.

The live, write-capable canary is deliberately *not* executed from this
workflow: a called workflow cannot elevate the caller's `GITHUB_TOKEN`, so a
write-capable canary job here would force the read-only pull-request caller to
grant issue/action writes. The live canary therefore lives once, in
`continuum-contract-canary.yml`, and is never reimplemented here.

The manual dispatch equivalent of `mode: canary` is the project-owned
`Contract qualification canary` workflow (`contract-qualification-canary.yml`):
choosing **Run workflow** executes the qualification job and the live canary
job in one run. Both routes drive the same single canary implementation.

## Main-branch safety (server-side, required)

Code cannot protect `main` by itself. Configure GitHub branch protection
(a ruleset) on `main` with all of the following:

- Require a pull request before merging; no direct pushes.
- Require status checks to pass before merging, including at minimum the
  checks published by the `CI` workflow (its `Validate` job and the
  `continuum/validation` status context) and the `Contract qualification`
  workflow. Copy the exact check names from a real pull-request run before
  saving the rule; no check run is literally named `CI`.
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
