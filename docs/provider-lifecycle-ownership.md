# Provider-adapter vs common-lifecycle ownership (kodmial/continuum#304)

This is an **internal Continuum refactor**. Consumers require zero
changes: no repository edits, no caller reinstall, no new or renamed
workflows, no new required `workflow_call` inputs, no semantic/default
changes to existing consumer-facing inputs, no new required secrets,
variables, labels, permissions, or credentials.
`CONTINUUM_REVIEW_PROVIDER=coderabbit|pr-agent` keeps exactly the same
external selection contract, and every existing
`.github/caller-stubs/**` file remains contract-compatible. Shared
orchestration changes behind those stubs only.

## Common lifecycle (provider-neutral, one implementation)

Canonical Python: `src/continuum/merge_lifecycle.py`.
Canonical workflow runtime: `.github/scripts/merge_lifecycle.js`
(decision-for-decision mirror; both auto-merge workflows load it).

Owns only behavior proven equivalent in
`docs/provider-lifecycle-duplication-matrix.md`:

- PR / current-HEAD reconciliation;
- current-HEAD CI / gate validation;
- `no-auto-merge`;
- main-sync policy;
- conflict-repair plumbing (decisions canonical in
  `src/continuum/conflict_repair.py`, re-exported, never duplicated);
- Packaging smoke / configurable required gates;
- final exact-HEAD validation;
- atomic merge (exact-`sha:` guard);
- merge-title normalization;
- post-merge wakeups;
- generic idempotency / concurrency / race protection.

Branch safety stays canonical in `src/continuum/branch_safety.py`
(#302); `merge_lifecycle.py` delegates to it. The common layer holds no
provider-specific retry/review policy, introduces no `both` provider
mode, and introduces no new controller/reducer/shadow lifecycle
authority: the two auto-merge workflows remain the sole executors.

## CodeRabbit adapter (provider-specific only)

Canonical: `src/continuum/coderabbit_adapter.py` plus the
CodeRabbit-owned blocks inside `continuum-auto-merge.yml`,
`continuum-coderabbit-retry.yml`, and
`continuum-coderabbit-unresolved.yml`.

Owns:

- approval / review basis;
- unresolved / nitpick state;
- quota / rate-limit semantics;
- CodeRabbit review commands;
- carried-approval policy (approvals never survive a HEAD move or a
  main-sync).

## PR-Agent adapter (provider-specific only)

Canonical: `src/continuum/pr_agent_lifecycle.py` plus
`.github/scripts/pr_agent_policy.js` and the PR-Agent-owned blocks
inside `continuum-pr-agent.yml`, `continuum-pr-agent-repair.yml`,
`continuum-pr-agent-recovery.yml`, `continuum-pr-agent-router.yml`, and
`continuum-pr-agent-auto-merge.yml` (review gate + attestation).

Owns:

- structured review JSON;
- merge recommendation / key issues;
- improve suggestions;
- persistent finding state;
- provider-specific no-progress / retry semantics that are not generic
  lifecycle behavior (controller-state comment, bounded `retry_attempt`
  backoff, exact-HEAD `expected_head_sha` binding).

## Rules

1. Extract one common responsibility at a time: pin with parity tests,
   extract, prove both providers preserve behavior, remove only the
   now-redundant copy.
2. Never extract behavior merely because code looks similar; the
   duplication matrix judges equivalence first.
3. If an extraction would require changing the consumer interface, it is
   out of scope: leave it duplicated and record a separately reviewed
   compatibility migration.
4. Both modes preserve exact-HEAD / fail-closed behavior on every write.
