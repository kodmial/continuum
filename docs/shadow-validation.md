# Shadow validation

Status: **operational**. This document describes how the validation plane is
installed, what its evidence means, and what has to be true before NanoDictate
hands a decision to Continuum.

Scope: the shadow plane answers one question — *for this real lifecycle event,
what would Continuum have done?* — and records the answer. It never acts. The
production decision is still NanoDictate's, made by the same planner, the same
controllers and the same guards. Everything below is in service of that
comparison being meaningful, and of nobody mistaking a shadow decision for a real
one.

## The shape of it

Three files, in three repositories:

| File | Where it lives | What it does |
| --- | --- | --- |
| `shadow-bridge.yml` | NanoDictate, `.github/workflows/` | Names the lifecycle event and hands a small immutable document to Continuum |
| `continuum-shadow.yml` | Continuum, `.github/workflows/` | Checks out a pinned engine, captures live state, decides, records, uploads |
| `src/continuum/shadow/` | Continuum | The engine: capture, planner, barrier, parity, liveness, cutover |

The bridge is the only thing a production repository has to add, and it is
deliberately thin: one shaping step and one call. It has no token, no
permissions, no retry, no queue and no decision. A bridge that grew logic would
be a second orchestration surface, and the value of the whole exercise is that
the two surfaces being compared are the same decision.

## Installing the bridge

1. Copy `reference/nanodictate-workflows/shadow-bridge.yml` into
   `.github/workflows/continuum-shadow-bridge.yml`.
2. Set the reusable-workflow pin to a full commit SHA of Continuum that
   contains `continuum-shadow.yml`. A branch reference would mean the evidence in
   the window was produced by code nobody reviewed. **The reference copy ships
   with a placeholder pin from before the workflow existed; replacing it is a
   required step, not a formality.**
3. Create `SHADOW_READ_TOKEN`: a fine-grained token with **read** scopes only
   (Issues, Pull requests, Checks, Commit statuses, Actions, Metadata, Contents)
   on NanoDictate and nothing else. The shadow plane never writes, so a
   write-capable token would make that claim unverifiable rather than merely
   untrue. A reusable workflow called across repositories receives no
   `GITHUB_TOKEN` of its own, which is the only reason this token exists.
4. Set the `AUTOMATION_TRUSTED_ACTORS` repository variable to the same value
   production uses, so the shadow plane applies the same trust policy to the
   same actors. A shadow plane with a different trust policy is comparing
   something else.

## What happens on one event

For each forwarded event the workflow, in this order:

1. checks out Continuum at the pinned SHA and refuses to continue if the working
   tree is not that commit, so the journal can name the code that produced it;
2. installs the process-level write barrier **before** it reads anything;
3. records the acceptance, so a run that dies leaves an orphan the window can
   see rather than an event that silently never happened;
4. captures live state through the read-only token;
5. plans the event with the production planner and records the expected actions;
6. compares the journal with what NanoDictate actually did, if that was
   forwarded;
7. accounts for the run's liveness;
8. renders a summary into the job summary and uploads the evidence, with
   `if: always()`.

Order matters and is asserted in `tests/test_shadow_workflow.py`. A barrier
proved after the decision is a barrier that was not there when the decision was
made; an acceptance recorded after the run is an acceptance that cannot tell an
orphan from an event that never happened.

## Reading the evidence

Every run writes `shadow-out/`, uploaded as an artifact. The files:

| File | What it holds |
| --- | --- |
| `journals/<correlation>.json` | The decision: status, decision code, guards, planned and suppressed effects, engine and config provenance |
| `parity/<correlation>.json` | The comparison with the production outcome, and each difference by name |
| `liveness/<now>-report.json` | One verdict per accepted run, plus the answer rate |
| `barrier/check.json` | Every write path the barrier refused |
| `cases/<correlation>.case.json` | A replayable case: the same event and state, re-runnable |

Read it in the job summary first — `continuum.shadow.cli summary` renders the
same documents as markdown — and open the artifact only for the line you need
to check. The summary reports absence as absence: a run with no parity document
says the parity was *not evaluated*, which is a different state from *matched*.

### Parity classifications

| Verdict | Meaning |
| --- | --- |
| `exact` | The same effects, in the same order, with the same terminal state |
| `explainable` | The two agree about what matters; a difference is accounted for |
| `missing-action` | Continuum planned an effect production did not take |
| `extra-action` | Production took an effect Continuum did not plan |
| `ordering-guard` | Same effects, different order, and a guard made the order matter |
| `terminal-state` | Same effects, different terminal state |
| `shadow-failure` | The shadow run itself failed; not a divergence |
| `unevaluable` | The production outcome was not forwarded, or could not be matched to the run |

`shadow-failure` and `unevaluable` are the two a reader most often confuses with
agreement. Neither counts as agreement, and neither may appear in a window that
supports a cutover.

### Liveness verdicts

| Verdict | Meaning |
| --- | --- |
| `live` | A terminal decision arrived inside the budget |
| `slow` | A terminal decision arrived, close to the budget |
| `stalled` | The run exceeded its budget and was stopped |
| `orphaned` | Accepted, and no result at all |
| `crashed` | A terminal result arrived, and it is a failure of the run |
| `abandoned` | Deliberately not run: a duplicate, a superseded event |

`abandoned` is neither a pass nor a failure and is out of the answer rate. It was
never expected to answer, and counting it as a decision that was taken — or as a
failure that blocks the window — would both be wrong in ways that matter.

## The eight scenario classes

A window is only as good as the paths it exercised. Cutover requires **live**
evidence for all of these:

| Scenario | What it exercises |
| --- | --- |
| `issue-lifecycle` | The ordinary issue → worker → pull request path, which is most production traffic |
| `ci-repair` | CI failure → repair/retry: the ladder and its attempt limits |
| `coderabbit-finding` | A blocking review finding → repair → re-review, the path with the most state |
| `merge-decision` | Merge eligibility and the merge itself |
| `dependency-blocked` | A task blocked on another, where refusing to act is the correct answer |
| `release-planning` | Release planning in dry-run mode |
| `duplicate-replay` | A duplicate or replayed delivery, which must not act twice |
| `timeout-recovery` | A timeout or failure, and the recovery rung it selects |

A scenario seen only through replays is `replayed-only`: covered, but not by the
live path, and the cutover gate says so with a `replayed_only_coverage` blocker.

## Running one event locally

The same run the workflow performs, without the workflow, against a recorded
case or against live state:

```
# Against a captured event and state, with no network at all
PYTHONPATH=src python3 -m continuum.shadow.cli run \
  --event shadow-out/../../../event.json \
  --state state.json \
  --out replay-out \
  --save-case

# Against live state, with a read-only token
SHADOW_READ_TOKEN=... PYTHONPATH=src python3 -m continuum.shadow.cli run \
  --event event.json \
  --capture-live \
  --repo kodmial/nanodictate \
  --token-env SHADOW_READ_TOKEN \
  --trusted-actors "$(gh variable get AUTOMATION_TRUSTED_ACTORS || echo '')" \
  --out replay-out
```

Both write a journal and a case, print the decision, and exit `0` — including
when the decision is a refusal, because a run that refused successfully is a
successful run. Exit `3` means the plane could not record an answer, which is
not the same thing and must not be treated as it.

To compare a journal with a production outcome, and to account for a set of
runs:

```
PYTHONPATH=src python3 -m continuum.shadow.cli parity \
  --journal replay-out/journals/<correlation>.json \
  --observed observed.json \
  --out replay-out

PYTHONPATH=src python3 -m continuum.shadow.cli liveness \
  --acceptances acceptance.json \
  --journals replay-out/journals/*.json \
  --out replay-out \
  --budget-ms 300000
```

## Judging a window

Collect the window's parity results, its liveness report, any recorded
resolutions, and the human approval, then:

```
PYTHONPATH=src python3 -m continuum.shadow.cli cutover \
  --parity window/parity/*.json \
  --liveness window/liveness/report.json \
  --resolutions window/resolutions/*.json \
  --origins window/origins.json \
  --approval window/approval.json \
  --canary window/canary.json \
  --rollback window/rollback.json \
  --window-start 2026-03-01T00:00:00Z \
  --window-end 2026-03-08T00:00:00Z \
  --out window
```

Exit code `0` means approved, `1` means the gate is not satisfied, and `3` means
the gate could not run — three states a CI job must not conflate.

The gate blocks on, among others: `uncovered_scenario`,
`replayed_only_coverage`, `unresolved_divergence`, `no_liveness_evidence`,
`liveness_stalled` / `liveness_orphaned` / `liveness_crashed`,
`incomplete_approval`, `stale_approval`, `no_rollback`, and
`approval_over_blocked_window`.

That last one deserves its own note, because it is the failure this whole plane
exists to prevent. A complete, digest-matching approval is still refused when
the window has blockers. A signature on the evidence does not make the evidence
sufficient, and an operator who is told otherwise will learn it at the worst
possible moment.

### The approval is bound to one window

An approval carries an `evidence_digest` over the window's verdicts and linked
actions. If the window moves — one more run, one more divergence — the digest
changes, and the old approval becomes `stale_approval`. Approving a window and
then adding evidence to it invalidates the approval, deliberately: the point of
an approval is to say *this specific evidence was good enough*.

A complete approval also names who approved, when, the canary evidence and the
rollback path. NanoDictate must remain able to resume ownership; a cutover with
no recorded way back is refused with `no_rollback`.

## Replaying one case

```
PYTHONPATH=src python3 -m continuum.shadow.cli replay \
  --case shadow-out/cases/<correlation>.case.json \
  --out replay-out
```

Replay re-runs a captured event against a captured state, with no network, and
writes a replay document naming both engines' verdicts. It answers "would another
Continuum agree with this one?" — which is how a divergence gets attributed to a
change in the engine rather than to a change in the world.

## When something looks wrong

| Symptom | What it usually means |
| --- | --- |
| Every event fails with `unknown_event_type` | The bridge is out of date with the engine's event names; check the pin |
| Parity is `unevaluable` for every event | `observed_json` is not being forwarded, so nothing was compared |
| A capture is `incomplete` and a guard refuses | A read the decision needed failed. The journal names the field; the guard refusing is the point |
| `orphaned` in the liveness report | The run died after the acceptance was recorded. The acceptance exists so this is visible |
| `replayed_only_coverage` | The scenario has evidence, but only from replays. It needs a live event |
| Bridge runs with no event output | The action is not one the projection names. The bridge logs why, and does not guess |
| Labels arrive empty at the planner | The bridge read labels from the wrong place in the payload. The queue dispatches on labels, so this is never benign |

## What this plane is not

- It is not a second scheduler. There is no queue, no retry policy and no
  scheduling decision here.
- It is not a staging environment. State is read live; nothing is written, and
  the plane performs nothing in the repository it observes.
- It is not a licence to cut over on a green tick. A cutover needs the coverage,
  the liveness, the digest-bound approval, and the rollback, and a human still
  decides.
