# Provider lifecycle duplication matrix (kodmial/continuum#304)

Baseline SHA: `131c76c2966999778ebd80806e34a5d4df6ac8e3`
(`fix: P0: Eliminate every direct-to-default-branch code mutation path`, #302 closed 2026-10-09.)

This matrix is the mandatory first step of #304. It compares the exact
`main` CodeRabbit path against the exact `main` PR-Agent path, maps each
responsibility to its implementation, names the executable tests that pin
it, judges semantic equivalence, and assigns the canonical owner after
extraction. Behavior is extracted only where semantics are truly
equivalent and already proven; provider-specific retry/review policy
never leaks into the common layer.

Conventions:

- CodeRabbit path = `.github/workflows/continuum-auto-merge.yml` (generic
  reconciler, CodeRabbit-owned when `CONTINUUM_REVIEW_PROVIDER=coderabbit`)
  plus `continuum-coderabbit-retry.yml` /
  `continuum-coderabbit-unresolved.yml`.
- PR-Agent path = `.github/workflows/continuum-pr-agent-auto-merge.yml`
  (reconcile + merge the exact reviewed HEAD) plus `continuum-pr-agent.yml`
  (review/improve), `continuum-pr-agent-repair.yml`,
  `continuum-pr-agent-recovery.yml`, `continuum-pr-agent-router.yml`, and
  `.github/scripts/pr_agent_policy.js`.
- Canonical common owner = `src/continuum/merge_lifecycle.py` (pure
  provider-neutral decisions) mirrored at runtime by
  `.github/scripts/merge_lifecycle.js`. Existing owners
  `src/continuum/branch_safety.py` (#302) and
  `src/continuum/conflict_repair.py` (#246) remain canonical for their
  domains; `merge_lifecycle.py` delegates to them and never duplicates
  them.
- CodeRabbit adapter owner = `src/continuum/coderabbit_adapter.py`
  (provider-specific semantics only).
- PR-Agent adapter owner = `src/continuum/pr_agent_lifecycle.py` plus
  `.github/scripts/pr_agent_policy.js` (provider-specific semantics only).

## 1. Shared provider-neutral lifecycle (equivalent — extracted)

| # | Responsibility | CodeRabbit implementation | PR-Agent implementation | Executable tests (before) | Equivalent? | Canonical owner after extraction |
|---|---|---|---|---|---|---|
| S1 | PR / current-HEAD reconciliation (authoritative branch ref wins over stale pull metadata; result valid only for exact HEAD) | `continuum-auto-merge.yml`: `withAuthoritativeHead`, `getPullWithAuthoritativeHead`, `pr.head.sha` re-reads before every write | `continuum-pr-agent-auto-merge.yml`: `getPull`, `fresh.head.sha.toLowerCase() !== reviewedHead` guards (conflict dispatch, initial/final/pre-merge gates, post-merge verify) | `tests/test_merge_lifecycle.py::ExactHeadTests`; `test_pr_agent_lifecycle.py::StabilizationParityTests.test_pr_agent_merge_is_atomic_against_reviewed_head` | Yes — both re-read live HEAD and fail closed on movement | `merge_lifecycle.head_matches`, `merge_lifecycle.validate_exact_head` |
| S2 | Current-HEAD CI / gate validation (CI success on exact HEAD; Continuum Contract Gate + Contract qualification only on `kodmial/continuum`) | `continuum-auto-merge.yml`: `latestCurrentHeadCi`, `latestContinuumContractGate`, `latestCurrentHeadContractQualification` | `continuum-pr-agent-auto-merge.yml`: `currentHeadGates` (CI + Contract Gate + Contract qualification) | `tests/test_merge_lifecycle.py::CurrentHeadGateTests`; `test_pr_agent_lifecycle.py` gate-window tests | Yes — same gate names, same exact-HEAD scoping, same Continuum-only gating | `merge_lifecycle.evaluate_current_head_gates` |
| S3 | `no-auto-merge` block label | `continuum-auto-merge.yml:392,2864,3142`: `AUTO_MERGE_BLOCK_LABEL = 'no-auto-merge'`, skip + pre-merge recheck | `continuum-pr-agent-auto-merge.yml:680,1289,1326,1362`: same constant, same skip + initial/final/pre-merge rechecks | `tests/test_merge_lifecycle.py::NoAutoMergeTests`; `StabilizationParityTests.test_pr_agent_merge_reuses_mature_common_safety_contract` | Yes — identical label, identical fail-closed placement | `merge_lifecycle.AUTO_MERGE_BLOCK_LABEL`, `merge_lifecycle.is_merge_blocked` |
| S4 | Main-sync policy (behind-by + merge-critical file delta; non-critical deltas do not force sync; sync never carries approval) | `continuum-auto-merge.yml`: `compareWithMain`, `mainDeltaSincePrHead`, `shouldSyncFromMain`, `isNonMergeCriticalMainPath` | `continuum-pr-agent-auto-merge.yml:961-1004`: `mainSyncDecision`, `isNonMergeCriticalMainPath` (same path table) | `tests/test_merge_lifecycle.py::MainSyncTests`; `test_pr_agent_lifecycle.py::StabilizationParityTests.test_pr_agent_main_sync_never_carries_old_review` | Yes — same path table, same three outcomes (`up-to-date`, `non-merge-critical-main-delta`, `merge-critical-main-delta`/`unknown-main-delta`) | `merge_lifecycle.is_non_merge_critical_main_path`, `merge_lifecycle.decide_main_sync` |
| S5 | Conflict-repair plumbing (dirty discovery, idempotent per-HEAD dispatch, bounded retry, stale-lock reconcile, `opencode-conflict-repair` label + `continuum-conflict-repair` marker, configured consumer caller) | `continuum-auto-merge.yml:397,2025-2254,2462`: lock label, markers, `OPENCODE_WORKFLOW` caller resolution | `continuum-pr-agent-auto-merge.yml:681,1011-1102`: lock label, `conflictAttemptMarker`, `releaseConflictLock`, `dispatchConflictRepair` | `tests/test_conflict_repair.py` (full suite); `tests/test_merge_lifecycle.py::ConflictPlumbingTests` | Yes — same label, same marker family, same bounded per-HEAD budget, same caller contract | `conflict_repair.py` remains canonical; `merge_lifecycle.conflict_*` constants delegate to it |
| S6 | Packaging smoke (conditional gate: only blocks when a run exists for the exact HEAD and is not success) | `continuum-auto-merge.yml:786,2943-2954`: `latestCurrentHeadPackagingSmoke` | `continuum-pr-agent-auto-merge.yml:841-852`: same conditional check | `tests/test_merge_lifecycle.py::PackagingAndRequiredGateTests`; `test_api_budget.py`, `test_coderabbit_queue_lifecycle.py` packaging cases | Yes — identical conditional semantics | `merge_lifecycle.evaluate_packaging_gate` |
| S7 | Configurable required workflow gate (label-gated extra workflow must succeed on exact HEAD) | `continuum-auto-merge.yml:798-811,2960-2972`: `requiredWorkflowGate` | `continuum-pr-agent-auto-merge.yml:855-872`: same label+name gate | `tests/test_merge_lifecycle.py::PackagingAndRequiredGateTests` | Yes — identical label/name/exact-HEAD semantics | `merge_lifecycle.evaluate_required_workflow_gate` |
| S8 | Final exact-HEAD validation (re-read PR, compare to reviewed HEAD immediately before merge) | `continuum-auto-merge.yml:3142-3194`: pre-merge HEAD + label + gate revalidation | `continuum-pr-agent-auto-merge.yml:1325-1399`: initial/final/pre-merge triple revalidation against `reviewedHead` | `tests/test_merge_lifecycle.py::ExactHeadTests` | Yes — both fail closed on moved HEAD | `merge_lifecycle.validate_exact_head` |
| S9 | Atomic merge (`pulls.merge` with exact `sha:`, no `gh pr merge` CLI path) | `continuum-auto-merge.yml:3488`: `pulls.merge` with `sha:` guard | `continuum-pr-agent-auto-merge.yml:1410-1416`: `pulls.merge` with `sha: reviewedHead` | `tests/test_merge_lifecycle.py::AtomicMergeTests`; `StabilizationParityTests.test_pr_agent_merge_is_atomic_against_reviewed_head` | Yes — same API, same exact-SHA guard | `merge_lifecycle.build_merge_params` |
| S10 | Merge-title normalization (`fix:` prefix unless conventional) | `continuum-auto-merge.yml:3484-3493`: `/^(?:fix\|feat\|perf\|refactor)(?:\([^)]*\))?!?:\s/i` | `continuum-pr-agent-auto-merge.yml:1403-1406`: identical regex and `fix:` fallback | `tests/test_merge_lifecycle.py::MergeTitleTests` | Yes — byte-identical regex and fallback | `merge_lifecycle.normalize_merge_title` |
| S11 | Post-merge wakeups (consumer-owned CSV list + ref validation with `main` fallback) | `continuum-auto-merge.yml:114-115,560-566`: `POST_MERGE_WAKEUPS`, `POST_MERGE_WAKEUP_REF` | `continuum-pr-agent-auto-merge.yml:648-649,686-695`: same CSV split + same ref validation | `tests/test_merge_lifecycle.py::PostMergeWakeupTests`; `StabilizationParityTests.test_pr_agent_merge_wakes_scheduler_by_default` | Yes — same parsing, same fallback, same empty-means-nothing | `merge_lifecycle.parse_post_merge_wakeups`, `merge_lifecycle.sanitize_wakeup_ref` |
| S12 | Generic idempotency / concurrency / race protection (repository-global serialization + per-PR/HEAD lease + exact-HEAD re-read; no cancellation of in-flight work) | `continuum-auto-merge.yml:70-88,379-391,2830`: `concurrency.group …-auto-merge`, `cancel-in-progress: false`, `ownedLeases`/`leaseKey`/`tryAcquireLease` | `continuum-pr-agent-auto-merge.yml:86-88`: `concurrency.group pr-agent-merge-…`, `cancel-in-progress: false`, plus per-HEAD marker idempotency in `dispatchConflictRepair` | `tests/test_merge_lifecycle.py::IdempotencyTests` | Yes in intent; scopes differ by design (global reconciler vs per-PR merge) and both are preserved | `merge_lifecycle.lease_key`, concurrency documented per workflow (scopes intentionally not unified) |

Branch safety (#302) underpins S4/S5/S8/S9 in both paths
(`continuumAssertSafePush` inline in both workflows, canonical
`src/continuum/branch_safety.py`). The extraction does not duplicate it;
`merge_lifecycle.py` delegates to it.

## 2. Provider-specific semantics (not equivalent — stay in adapters)

| # | Responsibility | CodeRabbit owner | PR-Agent owner | Executable tests | Canonical owner |
|---|---|---|---|---|---|
| P1 | Approval / review basis | `coderabbit_adapter.approval_basis`, `latestCodeRabbitDecision`, carried-approval policy; `continuum-auto-merge.yml` CodeRabbit decision gates | n/a (PR-Agent uses `merge_recommendation`) | `tests/test_merge_lifecycle.py::ProviderBoundaryTests`; `test_coderabbit_queue_lifecycle.py`, `test_coderabbit_controller_lifetime.py` | `coderabbit_adapter.py` |
| P2 | Unresolved / nitpick state | `coderabbit_adapter.unresolved_blocking`, thread-verification timeout; `continuum-coderabbit-unresolved.yml` | n/a | Same as P1 | `coderabbit_adapter.py` |
| P3 | Quota / rate-limit semantics | `coderabbit_adapter.parse_retry_delay_ms`, `retry_due_at_ms`; `retryRateLimitedCodeRabbit`, `latestCodeRabbitRateLimit` | n/a (generic 429 budget in `pr_agent_recovery.py` is a different mechanism) | `tests/test_merge_lifecycle.py::ProviderBoundaryTests` (parser vectors) | `coderabbit_adapter.py` |
| P4 | CodeRabbit review commands | `coderabbit_adapter.review_command_after`, `@coderabbitai review` detection; `continuum-coderabbit-retry.yml` | n/a | Same as P1 | `coderabbit_adapter.py` |
| P5 | Carried-approval policy | `coderabbit_adapter.carried_approval_valid` (approval valid only for same HEAD, no main-sync carry) | n/a | Same as P1 | `coderabbit_adapter.py` |
| P6 | Structured review JSON | n/a | `pr_agent_lifecycle.parse_review_json`, `_unwrap_review` | `tests/test_pr_agent.py`, `tests/test_pr_agent_policy.py` | `pr_agent_lifecycle.py` |
| P7 | Merge recommendation / key issues | n/a | `pr_agent_lifecycle.merge_recommendation`, `current_key_issues`, `reviewDisposition` (`pr_agent_policy.js`) | Same as P6 | `pr_agent_lifecycle.py` + `pr_agent_policy.js` |
| P8 | Improve suggestions | n/a | `pr_agent_lifecycle.qualifying_suggestions`, `parse_improve_push_outputs` | Same as P6 | `pr_agent_lifecycle.py` |
| P9 | Persistent finding state | n/a | `pr_agent_lifecycle.upstream_state_has_active`, `should_skip_improve`, exact-HEAD `last_run` binding | Same as P6 | `pr_agent_lifecycle.py` |
| P10 | Provider-specific no-progress / retry semantics | CodeRabbit queue cooldown stays in CodeRabbit path (P3) | `continuum-pr-agent.yml` / `continuum-pr-agent-repair.yml` controller-state comment, `retry_attempt` bounded backoff `15 * (1 << attempt)`, `attempt >= 2` | `tests/test_pr_agent_lifecycle.py::StabilizationParityTests.test_pr_agent_retry_is_bounded_exact_head_and_isolated` | PR-Agent workflows + `pr_agent_policy.js`; never in `merge_lifecycle.py` |

Deliberately not extracted: per-PR vs repository-global concurrency
scope (S12), CodeRabbit queue serialization, PR-Agent controller-state
election, and every retry/review policy above. Unifying any of them would
change consumer-visible scheduling and is out of scope per the consumer
contract freeze.

## 3. Verification map (removed duplicate → canonical owner → parity test)

| Removed duplicate | Canonical owner | Parity test |
|---|---|---|
| `no-auto-merge` constant + skip/recheck in `continuum-pr-agent-auto-merge.yml` | `merge_lifecycle.AUTO_MERGE_BLOCK_LABEL`, `merge_lifecycle.is_merge_blocked` (S3) | `test_merge_lifecycle.py::NoAutoMergeTests` (both providers) |
| `isNonMergeCriticalMainPath` + `mainSyncDecision` copy in `continuum-pr-agent-auto-merge.yml` | `merge_lifecycle.is_non_merge_critical_main_path`, `merge_lifecycle.decide_main_sync` (S4) | `test_merge_lifecycle.py::MainSyncTests` |
| Conflict label/marker constants + per-HEAD budget copy | `conflict_repair.py` (`CONFLICT_LOCK_LABEL`, `MAX_DISPATCHES_PER_HEAD`, `DISPATCH_GRACE_SECONDS`); `merge_lifecycle` re-exports (S5) | `test_conflict_repair.py` + `test_merge_lifecycle.py::ConflictPlumbingTests` |
| CI / Contract Gate / Contract qualification / Packaging smoke / required-gate copies | `merge_lifecycle.evaluate_current_head_gates`, `evaluate_packaging_gate`, `evaluate_required_workflow_gate` (S2/S6/S7) | `test_merge_lifecycle.py::CurrentHeadGateTests`, `PackagingAndRequiredGateTests` |
| Exact-HEAD re-read + `sha:` merge guard copies | `merge_lifecycle.validate_exact_head`, `merge_lifecycle.build_merge_params` (S1/S8/S9) | `test_merge_lifecycle.py::ExactHeadTests`, `AtomicMergeTests` |
| `conventionalTitle` regex copy | `merge_lifecycle.normalize_merge_title` (S10) | `test_merge_lifecycle.py::MergeTitleTests` |
| `POST_MERGE_WAKEUPS` CSV + wakeup-ref validation copies | `merge_lifecycle.parse_post_merge_wakeups`, `merge_lifecycle.sanitize_wakeup_ref` (S11) | `test_merge_lifecycle.py::PostMergeWakeupTests` |
| Per-PR/HEAD lease + duplicate-wakeup coalescing copies | `merge_lifecycle.lease_key` + workflow concurrency groups (S12, scopes preserved) | `test_merge_lifecycle.py::IdempotencyTests` |
| Inline `continuumAssertSafePush` copies | `branch_safety.py` (unchanged canonical, #302) | `tests/test_branch_safety.py` |

Both workflows keep their existing `workflow_call` inputs, secrets,
permissions, concurrency groups, and caller stubs byte-compatible. No new
`both` provider mode, no new controller/reducer, no new required consumer
input/secret/variable/label/permission/credential/workflow name. Shared
orchestration changes behind the existing stubs only.
