# Parity ledger

<!-- GENERATED FILE. Edit docs/parity-ledger.json and run `PYTHONPATH=src python3 -m continuum.tools.parity_ledger`; tests/test_parity_ledger.py refuses a stale copy. -->

Every semantic the source repository gained between those two commits, and what Continuum did about it. The machine-readable form is `parity-ledger.json`; this file is rendered from it and is the one to read.

`kodmial/nanodictate` over `64e89a3bcbab..54db335ea7c0` (18 commits, 948 insertions(+), 66 deletions(-)), reading 6 workflow files.

## Why this exists

A consumer that adopts Continuum inherits nothing of the source repository's CI by accident. Every behaviour that repository gained is either reimplemented here in generic form, deliberately left to the consumer, or recorded as a decision not to have it. What must never happen is the third thing: a semantic that is simply not mentioned, which is indistinguishable from one nobody thought about.

Each row therefore states a classification, a rationale, and where the proof is.

## Classifications

- **Must port** — A semantic Continuum did not hold. Ported generically, with regression coverage tied to the originating incident.
- **Absorbed** — Continuum already holds the semantic, in a form at least as strong. No change; the evidence names where.
- **Superseded** — The source fix is replaced by something Continuum now provides, so porting it would duplicate and eventually diverge.
- **Not applicable** — The semantic does not exist in Continuum's architecture. Recorded so the omission is a decision, not an oversight.
- **Consumer-local** — Real, load-bearing behaviour that belongs to one consumer's own repository and toolchain. Deliberately not ported.

## Summary

| Classification | Count |
| --- | --- |
| Must port | 5 |
| Absorbed | 14 |
| Superseded | 1 |
| Not applicable | 3 |
| Consumer-local | 3 |
| **Total** | **26** |

5 of 5 must-port items are landed, each with a regression test tied to the incident that produced it.

## Must port

### PR-03 — A head carrying only provider statuses -- skipped, stale, or absent -- is still owed a review and must wake the shared controller.

- Surface: `.github/workflows/auto-merge.yml`
- Source: `bdc41f3`

The incident is queue starvation with a green build: the head was marked current from a success status, so no request was ever dispatched, and the head was neither reviewed nor unblocked. Continuum's adapter previously counted any success on the provider's status context as coverage. `covers_head` is now answered from submitted reviews alone, and the status arguments are retained only because the provider registry asks every adapter the same question.

- Changed: `src/continuum/review/coderabbit.py`.
- Tests: `tests/test_coderabbit.py::StatusOnlyHeadTests::test_a_skipped_status_alone_does_not_settle_the_slot`, `tests/test_queue.py::ControllerTests::test_a_cleared_head_does_not_consume_the_slot_for_an_untouched_head`.
- Incident: kodmial/nanodictate#59 (queue starvation on skipped-status heads).
- Issues: #11, #37.

### PR-04 — A controller that deliberately sleeps out a shared provider cooldown must not be cancelled by later wake-ups.

- Surface: `.github/workflows/coderabbit-retry.yml`
- Source: `e5f9a84`

Continuum's reconciler waits out the provider cooldown inside one run. Cancelling it on every wake-up reset that timer, and status, check-run, and review events arrive continuously, so the queue starved. Both the shared controller and the consumer entry workflow now declare `cancel-in-progress: false`.

- Changed: `.github/workflows/review-queue.yml`, `fixtures/consumer-repo/.github/workflows/review-queue.yml`.
- Tests: `tests/test_queue_workflows.py::WakeUpTriggerTests::test_the_reconciler_is_single_flight_and_never_cancelled`.
- Incident: kodmial/nanodictate#59 (starvation from continuous status events).
- Issues: #11, #37.

### PR-11 — A version assertion must be a token claim, not a substring claim.

- Surface: `.github/workflows/release-automation-merge.yml`
- Source: `05ac023`

The source fix anchors the check to the whole line after `includes` matched the wrong version. Continuum's audit used a raw substring test, so a manifest pinned to a longer version that merely started with the release version -- a silent downgrade -- satisfied the check. `names_version` compares on a boundary that admits an optional `v` and rejects any adjacent version character.

- Changed: `src/continuum/release/publishers/validation.py`.
- Tests: `tests/test_release_publishers.py::VersionTokenBoundaryTests::test_a_longer_version_that_starts_with_the_release_is_not_a_match`.
- Incident: kodmial/nanodictate#59 (manifest version check matched a different version).
- Issues: #37, #60.

### PR-12 — An unfilled-placeholder scan must ignore comment lines.

- Surface: `.github/workflows/release-automation-merge.yml`
- Source: `a0ae005`

Generated manifests document the tokens they fill, so scanning every line reported the documentation of the placeholders as the failure. That makes a correct manifest red and trains everyone to ignore the check, which is exactly when a manifest with a real hole ships. `unfilled_template_tokens` scans only lines a package manager would execute.

- Changed: `src/continuum/release/publishers/validation.py`.
- Tests: `tests/test_release_publishers.py::CommentAwareTokenScanTests::test_a_comment_line_does_not_supply_an_unfilled_token`.
- Incident: kodmial/nanodictate#59 (false unfilled-placeholder failures on documented tokens).
- Issues: #37, #60.

### PR-13 — Publication must be gated on a green run of the declared verification workflow, for the exact commit, carrying the declared artifacts.

- Surface: `.github/workflows/release.yml`
- Source: `640cb97`

The incident is that release artifacts were built and published in one job, so what shipped was exactly what nothing else had run. The source gate matches runs by `head_sha` rather than branch, waits a bounded window for the run to appear because both workflows are triggered by the same push, then requires each declared job and each declared artifact. Continuum now expresses that generically: a consumer names a workflow, an event, job names, and artifact names, and the core insists on an exact-commit green run. The run id in the verdict is what lets a publication consume the tested bytes instead of rebuilding them.

- Changed: `src/continuum/release/provenance.py`, `.github/workflows/release-verify.yml`.
- Tests: `tests/test_release_verification.py::ExactHeadTests::test_a_run_on_another_commit_is_not_selected`, `tests/test_release_verification.py::VerdictTests::test_a_green_run_missing_the_declared_artifact_blocks`, `tests/test_release_verification.py::CliGateTests::test_a_blocked_gate_exits_nonzero_and_names_the_reason`.
- Incident: kodmial/nanodictate#59 (release shipped untested bytes).
- Issues: #37, #60.

## Absorbed

### PR-01 — Only an exact-head provider verdict settles a head; merge must revalidate the whole set immediately before the write.

- Surface: `.github/workflows/auto-merge.yml`
- Source: `e3c4206`, `bf3d449`

Continuum's gate computes the verdict from the reviewed head and re-runs immediately before applying it, so a head that moved is never merged on a stale approval.

- Changed: `src/continuum/review/gate.py`, `src/continuum/review/result.py`.
- Tests: `tests/test_gate.py::DecisionTests::test_historical_summary_cannot_stand_in_for_current_head`.
- Issues: #11.

### PR-02 — A durable exact-head approval must survive a later provider status that says the review was skipped.

- Surface: `.github/workflows/auto-merge.yml`
- Source: `e3c4206`

Held before the audit: an approval is a submitted review, and the status description is never the verdict.

- Changed: `src/continuum/review/coderabbit.py`.
- Tests: `tests/test_coderabbit.py::QuotaBehaviourTests::test_a_cleared_head_with_a_skipped_status_is_never_re_requested`.
- Issues: #11.

### PR-05 — GitHub concurrency retains at most one pending successor behind a running job, so a non-cancelling controller loses nothing.

- Surface: `.github/workflows/coderabbit-retry.yml`
- Source: `e5f9a84`

Stated as the justification for PR-04 in both workflow files rather than as code. The platform guarantee is identical in both repositories.

- Changed: `.github/workflows/review-queue.yml`, `fixtures/consumer-repo/.github/workflows/review-queue.yml`.
- Issues: #11.

### PR-06 — A stalled shared review slot must be released rather than blocking the queue forever.

- Surface: `.github/workflows/coderabbit-retry.yml`
- Source: `8b8966f`

Continuum releases a slot when the head moved or the pull request is no longer visible, and never waits out a lock timeout.

- Changed: `src/continuum/review/queue.py`.
- Tests: `tests/test_queue.py::LockTests::test_lock_is_released_when_the_candidate_moved_to_a_new_head`, `tests/test_queue.py::LockTests::test_stale_lock_on_a_closed_pull_request_is_released_immediately`.
- Issues: #11.

### PR-07 — Priority ordering must stay strict, so a reordering cannot quietly promote a lower-priority candidate.

- Surface: `.github/workflows/coderabbit-retry.yml`
- Source: `bf3d449`

Continuum's reconciler sorts by explicit priority rank and re-derives the order on every pass.

- Changed: `src/continuum/review/queue.py`, `src/continuum/review/queue_controller.py`.
- Tests: `tests/test_queue.py::EligibilityTests::test_already_reviewed_head_is_not_a_candidate`.
- Issues: #11.

### PR-08 — A fork's event must never spend the repository-scoped shared provider slot.

- Surface: `.github/workflows/auto-merge.yml`
- Source: `e3c4206`

The reconciler is triggered by `pull_request_target` and skips any candidate whose head repository is not this one.

- Changed: `.github/workflows/review-queue.yml`.
- Tests: `tests/test_queue_workflows.py::WakeUpTriggerTests::test_a_forks_event_never_spends_the_shared_slot`.
- Issues: #11.

### PR-09 — Cron is a recovery backstop only; normal progression must be event driven.

- Surface: `.github/workflows/auto-merge.yml`
- Source: `e3c4206`

Both the shared reconciler and the consumer entry workflow carry a scheduled backstop beside a complete event set, and the reconciler recomputes the whole queue rather than trusting a payload.

- Changed: `.github/workflows/review-queue.yml`.
- Tests: `tests/test_queue_workflows.py::WakeUpTriggerTests::test_provider_and_ci_transitions_are_wake_ups`.
- Issues: #11.

### PR-10 — Cancellation is correct for a fast idempotent reconciler that holds no shared cooldown wait.

- Surface: `.github/workflows/auto-merge.yml`
- Source: `e3c4206`

Recorded because the source repository reaches the opposite conclusion for a different controller, and the difference is the point: the rule is not 'never cancel' but 'never cancel a controller that is waiting on a shared quota'. Continuum's auto-merge reconciler has a ten-minute timeout and no in-run wait, so it is unaffected; only PR-04 needed changing.

- Changed: `.github/workflows/auto-merge.yml`, `.github/workflows/review-queue.yml`.
- Issues: #11.

### PR-14 — A green verification run that produced none of the declared artifacts has verified nothing.

- Surface: `.github/workflows/release.yml`
- Source: `640cb97`

Held for PR-13: the artifact requirement is a distinct reason code from a failed run, because the two have different fixes -- one needs a re-run, the other needs the upload fixed.

- Changed: `src/continuum/release/provenance.py`.
- Tests: `tests/test_release_verification.py::VerdictTests::test_a_green_run_missing_the_declared_artifact_blocks`.
- Issues: #37.

### PR-15 — A green run in which a declared job never ran is not the run that was required.

- Surface: `.github/workflows/release.yml`
- Source: `640cb97`

Held for PR-13 as its own reason code. Continuum reports it as blocked where the source used the job list for run discovery, because judging a specific run and reporting what was missing from it is one statement rather than two.

- Changed: `src/continuum/release/provenance.py`.
- Tests: `tests/test_release_verification.py::VerdictTests::test_a_green_run_that_never_ran_the_declared_job_blocks`.
- Issues: #37.

### PR-16 — A manual or dry-run release must not enable production smoke, and must log the skip instead.

- Surface: `.github/workflows/packaging-smoke.yml`
- Source: `640cb97`

Continuum's dry run returns before any signing or publication step executes and reports itself as a dry run in the job summary.

- Changed: `src/continuum/cli.py`.
- Tests: `tests/test_release_cli.py::ReleaseSignCommandTests::test_a_dry_run_reports_success_without_touching_a_tool`.
- Issues: #37.

### PR-20 — A release must not be able to re-enter the merge reconciler as a workflow run and loop.

- Surface: `.github/workflows/release.yml`
- Source: `54db335`

Continuum's release adapter is `workflow_call`-only, so no run of it appears in the `workflow_run` event stream at all, and the reconciler's allowlist is restricted to CI. The recursion is structurally impossible rather than guarded against.

- Changed: `.github/workflows/release-bun-binary.yml`, `.github/workflows/auto-merge.yml`.
- Tests: `tests/test_queue_workflows.py::WakeUpTriggerTests::test_the_workflow_run_allowlist_cannot_include_a_release`, `tests/test_release_bun_workflow.py::BunReleaseWorkflowContractTests::test_the_adapter_is_reachable_only_by_an_explicit_call`.
- Issues: #37.

### PR-21 — A trigger that does not change anything should be a green no-op, not a red failure.

- Surface: `.github/workflows/release.yml`
- Source: `54db335`

Continuum's release preflight computes skip flags and the dependent steps are guarded by them, so a redundant trigger neither publishes nor fails.

- Changed: `.github/workflows/release-bun-binary.yml`, `src/continuum/release/run.py`.
- Tests: `tests/test_release_run.py::PinEnforcementTests::test_an_assert_step_fails_when_its_pin_is_absent`.
- Issues: #37.

### PR-22 — Teardown must run whatever happened to the steps before it.

- Surface: `.github/workflows/release.yml`
- Source: `54db335`

The release runner guarantees teardown on every path, which is why a skipped or failed step cannot leave signing material behind.

- Changed: `src/continuum/release/run.py`, `src/continuum/release/plan.py`.
- Tests: `tests/test_release_run.py::RunnerBehaviourTests::test_teardown_runs_after_a_failure_so_material_never_outlives_the_job`.
- Issues: #37.

## Superseded

### PR-26 — A release adapter must be reachable only by explicit call, never by an event a run it created could retrigger.

- Surface: `.github/workflows/release.yml`
- Source: `54db335`

The source repository's release workflow reaches itself through `push` on tags and `main`, and reasons at length about why that does not loop. Continuum's adapter is `workflow_call`-only and has no push trigger at all, so the question does not arise. Porting the source design would add triggers Continuum deliberately does not have.

- Changed: `.github/workflows/release-bun-binary.yml`.
- Tests: `tests/test_release_bun_workflow.py::BunReleaseWorkflowContractTests::test_the_adapter_is_reachable_only_by_an_explicit_call`, `tests/test_release_bun_workflow.py::BunReleaseWorkflowContractTests::test_a_run_of_the_adapter_cannot_appear_in_a_workflow_run_event`.
- Issues: #37.

## Not applicable

### PR-23 — A separate manifest reconciler needs a `push` trigger to catch a merge it was not awake for.

- Surface: `.github/workflows/release-automation-merge.yml`
- Source: `05ac023`

Continuum has no standalone manifest reconciler. Manifests are a `needs`-gated step inside the release workflow, so there is no window in which a merge can be missed. Would become applicable only if manifest publication were split into its own event-driven workflow.

- Changed: `.github/workflows/release-bun-binary.yml`.
- Issues: #37.

### PR-24 — Work deferred while another controller was running must be woken when that controller finishes, or it is stranded forever.

- Surface: `.github/workflows/release-pr.yml`
- Source: `18ad709`

Continuum defers nothing on an in-flight release: the review queue's only pending states belong to CI, and its wake-up set already includes that controller's completion event. Recorded as a decision because the gap is architectural -- if release-time deferral is ever introduced, PR-09's event set must gain the publishing workflow's `workflow_run` completion, or this row must be reclassified.

- Changed: `.github/workflows/review-queue.yml`, `src/continuum/review/queue.py`.
- Tests: `tests/test_queue_workflows.py::WakeUpTriggerTests::test_provider_and_ci_transitions_are_wake_ups`.
- Issues: #37, #59.

### PR-25 — The `secrets` context is unavailable in an `if:` condition, so a secret-gated step must be guarded through `env:`.

- Surface: `.github/workflows/release.yml`
- Source: `54db335`

A platform constraint rather than a portable semantic: the fact is the same in every repository and Continuum has no behaviour that depends on it. Recorded so the classification set is complete, not because anything was ported.

- Changed: `kodmial/nanodictate@54db335:.github/workflows/release.yml`.
- Issues: #37.

## Consumer-local

### PR-17 — Install and uninstall each published surface on a clean machine, in a matrix, before it counts as a release.

- Surface: `.github/workflows/packaging-smoke.yml`
- Source: `640cb97`

Load-bearing and correct, and entirely about one consumer's package managers. The generic form is PR-13: publish only what somebody else's run on the exact commit verified. The legs themselves stay in the consumer's repository.

- Changed: `kodmial/nanodictate@54db335:.github/workflows/packaging-smoke.yml`.
- Issues: #37.

### PR-18 — A shell command that is still on the hashed path reports it as present after the file is gone.

- Surface: `.github/workflows/packaging-smoke.yml`
- Source: `640cb97`

A real false failure the consumer fixed by clearing the hash table before its cleanup check. It is a property of one consumer's shell script, not a semantic Continuum should carry.

- Changed: `kodmial/nanodictate@54db335:.github/workflows/packaging-smoke.yml`.
- Issues: #37.

### PR-19 — A privileged installer cannot read back into a locked-down runner home, so the candidate tree is staged world-readable elsewhere.

- Surface: `.github/workflows/packaging-smoke.yml`
- Source: `640cb97`

The consumer stages its candidate tree under a temporary directory because its own installer drops privileges. Product-specific by construction.

- Changed: `kodmial/nanodictate@54db335:.github/workflows/packaging-smoke.yml`.
- Issues: #37.

## Cutover

Neither toggle is enabled by this audit. The parent issue requires an explicit decision to turn either on, and all must-port items are landed, so both are now *eligible* for that decision — which is a separate change with its own review.

---

Schema `continuum.parity-ledger/v1`, version 1, updated 2026-09-29.
