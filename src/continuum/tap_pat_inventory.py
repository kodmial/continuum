"""Final TAP_PAT usage inventory and justification (kodmial/continuum#305).

This module is the executable half of the #305 Definition of Done item 10:
every remaining ``TAP_PAT`` credential binding in the shipped reusable
workflows is mapped to the capability that genuinely requires it.

Scope and non-goals (the issue body is authoritative):

- Former Work-LOCK #58 items 1-6 are baseline compatibility and are not
  reimplemented, broadened, or cleaned up here. Their behavior must remain
  green and unchanged.
- Only former items 7-10 are candidates, refreshed against current ``main``:

  7. No additional PAT rate-limit reserve/governor is needed beyond the
     reset-aware bounded recovery already present (see
     :data:`GOVERNOR_FINDING` and :func:`governor_evidence`). Low rate limit
     degrades to bounded autonomous ``WAIT``/recovery, never to a permanent
     human-required state.
  8. Every remaining same-repository PAT-backed mutation is audited below
     for a genuine event-fanout/identity requirement. Anything migratable
     behind the current reusable-workflow contract without a new caller
     permission, secret, variable, token, event-wiring change, or consumer
     file update would be listed as migratable; the audit finds none, so
     every entry below is retained with its reason.
  9. PAT-event chaining is not converted globally. Explicit
     ``createWorkflowDispatch`` is already used wherever it is equivalent
     (see :data:`CHAINING_FINDING`); comment/label chaining is retained only
     where the downstream trigger is an ``issue_comment``/label event that
     ``GITHUB_TOKEN`` writes suppress, which an explicit dispatch cannot
     replace without a consumer event-wiring change.
  10. This module *is* the final inventory: :func:`scan_workflow_bindings`
      enumerates every ``secrets.TAP_PAT`` binding in the shipped workflows
      and :func:`coverage_gaps` proves each one is classified here.

Consumer contract freeze: this inventory changes no workflow, caller stub,
input, secret, variable, permission, label, or workflow name. It only
documents credential choice and request volume behind the current
reusable-workflow contract.
"""

from __future__ import annotations

import os
import re

# ---------------------------------------------------------------------------
# Capability taxonomy
# ---------------------------------------------------------------------------

#: Push agent/result branches and fan out into CI/review workflows. A
#: ``GITHUB_TOKEN`` push does not reliably trigger downstream push/merge
#: workflows, and agent pushes touching ``.github/workflows/**`` additionally
#: need the classic PAT ``workflow`` scope the caller intentionally does not
#: grant (the scheduler header documents the narrow-grant design).
PUSH_FANOUT = "push-fanout"
#: Explicit repository/workflow dispatch. Dispatches authenticate with PAT
#: because reusable workflows deliberately request no ``actions: write`` so
#: the caller's ``GITHUB_TOKEN`` keeps its narrow grant; elevating the caller
#: would be a consumer-contract change.
DISPATCH = "dispatch"
#: Comment/label writes that must emit ``issue_comment``/label events to wake
#: a downstream caller (scheduler, OpenCode, watchdog). ``GITHUB_TOKEN``
#: writes are suppressed by GitHub and would silently break recovery.
CHAINING = "chaining"
#: Merge/push/dispatch coupling that must share one actor and fan out
#: together (auto-merge, PR-Agent auto-merge). Splitting the credential
#: would break the atomic merge-gate evidence chain.
MERGE_COUPLING = "merge-coupling"
#: Cross-repository access (parent/child delegation, delegated PR-Agent
#: targets, bootstrap key fetch). The run-scoped token cannot read or write
#: across repository boundaries.
CROSS_REPO = "cross-repo"
#: Delegated-aware conditional credential: same expression evaluates to
#: ``github.token`` for local same-repo execution and to ``TAP_PAT`` only for
#: delegated cross-repository execution. This is already the optimal
#: contract-invisible split; unifying on either credential alone would break
#: one side (privacy regression or cross-repo failure).
DELEGATED_CONDITIONAL = "delegated-conditional"
#: Bounded liveness fallback: same-repo reads go through ``GITHUB_TOKEN``
#: first and use ``TAP_PAT`` only after a 401/403 (and, for task-critical
#: admission reads, 429) or for the true cross-repo fetch. Removing the
#: fallback would trade autonomous liveness for budget savings.
LIVENESS_FALLBACK = "liveness-fallback"
#: Exactly-once claim coupling: a durable claim comment and its dispatch
#: share one step so crash ordering is preserved. Splitting credentials or
#: steps would open duplicate-dispatch races.
EXACTLY_ONCE_CLAIM = "exactly-once-claim"
#: Durable controller-state publication (no-progress markers, repair/review
#: retry state, handoff payloads) that downstream reconcilers and wakeups
#: read back. State writes share the PAT actor so exactly-once and
#: lease semantics stay under one identity.
CONTROLLER_STATE = "controller-state"
#: Owner-visible actor semantics: the mutation must appear as a normal owner
#: action (scheduler ``/oc`` dispatch gate requires the repository owner;
#: snapshot/readiness markers must be owner-visible).
OWNER_ACTOR = "owner-actor"
#: Bounded ``TAP_PAT || github.token`` fallback for qualification evidence
#: reads. Prefers PAT where available but still runs on the repository token;
#: no new credential is introduced either way.
BOUNDED_FALLBACK = "bounded-fallback"
#: Inert credential binding: the owning step performs no GitHub REST call
#: with the bound value (pure input gating). Migrating it would save zero
#: shared-budget requests, so it is retained as-is to avoid churn.
INERT = "inert"
#: Secrets-management scope: fetching the Actions secrets public key needs a
#: PAT scope the caller does not grant. Migration would require a caller
#: permission elevation (contract change).
ADMIN_SCOPE = "admin-scope"
#: Split-actor step: state mutations use ``GITHUB_TOKEN`` while only the
#: dispatch/command-comment half keeps PAT. Already optimal; listed here so
#: the PAT half stays justified.
SPLIT_ACTOR = "split-actor"

CAPABILITIES = frozenset({
    PUSH_FANOUT,
    DISPATCH,
    CHAINING,
    MERGE_COUPLING,
    CROSS_REPO,
    DELEGATED_CONDITIONAL,
    LIVENESS_FALLBACK,
    EXACTLY_ONCE_CLAIM,
    CONTROLLER_STATE,
    OWNER_ACTOR,
    BOUNDED_FALLBACK,
    INERT,
    ADMIN_SCOPE,
    SPLIT_ACTOR,
})

#: Secrets a workflow may reference. Mirrors
#: :data:`continuum.credential_matrix.ALLOWED_SECRETS` plus the automatic
#: ``github.token``; #305 introduces no new secret.
ALLOWED_SECRETS = frozenset({
    "TAP_PAT",
    "RENDER_API_KEY",
    "CHILD_RUNTIME_TOKEN",
    "CHILD_RUNTIME_REPOSITORIES",
    "CONTINUUM_CHILD_REPOSITORIES",
})

#: Paid-provider keys must never be required, read, or forwarded by core.
FORBIDDEN_PROVIDER_KEYS = (
    "OPENCODE_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "GROQ.KEY",
)

# ---------------------------------------------------------------------------
# Findings for candidates 7 and 9 (evidence-backed, no code churn)
# ---------------------------------------------------------------------------

#: Candidate 7 verdict: no additional PAT rate-limit reserve/governor is
#: needed beyond the reset-aware bounded recovery already present.
GOVERNOR_FINDING = (
    "No additional PAT rate-limit reserve/governor beyond the reset-aware "
    "bounded recovery already present on current main."
)

#: Candidate 9 verdict: no global PAT-event-chaining conversion. Explicit
#: dispatch is already used wherever it is proven equivalent; remaining
#: comment/label chaining wakes downstream callers on events an explicit
#: dispatch cannot replace without a consumer event-wiring change.
CHAINING_FINDING = (
    "No global PAT-event-chaining conversion. Explicit dispatches stay "
    "PAT-backed where equivalent; comment/label chaining is retained only "
    "where the downstream trigger is a token-suppressed event."
)


def governor_evidence() -> dict:
    """Return the mechanisms proving candidate 7 needs no extra governor."""
    return {
        "verdict": GOVERNOR_FINDING,
        "reset_aware_delay": "lifecycle_recovery.next_retry_delay_seconds honors Retry-After/X-RateLimit-Reset/provider reset as a minimum",
        "no_inline_sleep": "lifecycle_recovery.should_defer_dispatch exits the runner for waits over 60s; the schedule redispatches",
        "bounded_budget": "lifecycle_recovery MAX_TRANSIENT_ATTEMPTS=10 executions per exact repo/PR/HEAD/operation identity; never human-required hold for rate limit",
        "bounded_non_critical_reads": "api_budget.RetryPolicy caps non-critical reads at 3 attempts with jittered backoff, then falls back",
        "no_pat_retry_on_ratelimit": "auto-merge/recovery withReadFallback retries on PAT only for 401/non-rate-limit 403, never for 429/rate-limited 403",
        "bounded_scans": "recovery lists only active run statuses (bounded pages); scheduler reuses open snapshots and grace-window comment scans",
    }


def chaining_evidence() -> dict:
    """Return the mechanisms proving candidate 9 needs no global conversion."""
    return {
        "verdict": CHAINING_FINDING,
        "explicit_dispatch_kept": "scheduler downstream-dispatcher wake, recovery review dispatch, router heavy-workflow dispatch, and auto-merge post-merge wakeups use explicit createWorkflowDispatch on PAT",
        "comment_chaining_retained": "watchdog /oc retry, scheduler /oc dispatch, opencode reservation release, and repair-lock releases must emit issue_comment/label events that GITHUB_TOKEN writes suppress",
        "equivalence_rule": "a chaining path converts to explicit dispatch only with a proven-equivalent trigger and no consumer event-wiring change; no such unconverted path exists on current main",
    }


# ---------------------------------------------------------------------------
# Inventory entries: one per owning step that binds secrets.TAP_PAT.
# ---------------------------------------------------------------------------
#
# ``step`` is a unique substring of the owning ``- name:`` line.
# ``capability`` is one of CAPABILITIES. ``same_repo`` marks whether the
# step runs against the same repository (False only for genuine
# cross-repository work). ``reason`` must name the mechanism that makes
# ``GITHUB_TOKEN`` non-equivalent. ``evidence`` lists literals the contract
# test uses to locate the site inside the owning step.

ENTRIES = (
    # -- issue scheduler (4 steps) --------------------------------------
    {
        "workflow": "continuum-issue-scheduler.yml",
        "step": "Verify relationship and wake parent scheduler",
        "capability": CROSS_REPO,
        "same_repo": False,
        "reason": "Child-to-parent cross-repository reads (relationship variables, parent identity, default branch) plus a cross-repo workflow dispatch; the run token cannot cross repository boundaries.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}", "repos/$parent/actions/workflows"),
    },
    {
        "workflow": "continuum-issue-scheduler.yml",
        "step": "Build delegated child queue",
        "capability": CROSS_REPO,
        "same_repo": False,
        "reason": "Parent-side delegated discovery reads child repositories (including private children) and their Actions history; cross-repo reads require PAT.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-issue-scheduler.yml",
        "step": "Reconcile state and dispatch ready issues",
        "capability": DISPATCH,
        "same_repo": True,
        "reason": "Dispatch plus owner-gated /oc comments: the reusable workflow deliberately requests no actions:write so the caller keeps its narrow grant (header comment), and /oc is accepted only from the repository owner, so the mutation must keep the owner-visible PAT actor while same-repo reads already use READ_GITHUB_TOKEN.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}", "READ_GITHUB_TOKEN"),
    },
    {
        "workflow": "continuum-issue-scheduler.yml",
        "step": "Wake the configured downstream dispatcher",
        "capability": DISPATCH,
        "same_repo": True,
        "reason": "Explicit downstream workflow dispatch keeps PAT so the caller needs no actions:write elevation; a GITHUB_TOKEN dispatch would require a consumer permission change.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}", "dispatches"),
    },
    # -- OpenCode agent (17 steps) --------------------------------------
    {
        "workflow": "continuum-opencode.yml",
        "step": "Classify CodeRabbit feedback and dispatch only actionable code repair",
        "capability": SPLIT_ACTOR,
        "same_repo": True,
        "reason": "Split actor by design: admission reads use github.token while only the no-progress state publication keeps COMMENT_PAT; the marker must be visible under the PAT identity for downstream repair gating.",
        "evidence": ("COMMENT_PAT: ${{ secrets.TAP_PAT }}", "github-token: ${{ github.token }}"),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Checkout repository",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Writable checkout with push credentials: agent branches are pushed and must trigger CI/review workflows, and may include .github/workflows/** changes requiring the classic PAT workflow scope.",
        "evidence": ("token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Resolve authoritative task context",
        "capability": LIVENESS_FALLBACK,
        "same_repo": True,
        "reason": "Same-repo task reads use github.token first; TAP_PAT is a bounded liveness fallback after 401/403/429 plus the true cross-repo task-fetch path. Dropping the fallback trades autonomous liveness for budget savings.",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}", "github-token: ${{ github.token }}", "falling back to TAP_PAT for liveness"),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Pin and verify task specification snapshot",
        "capability": SPLIT_ACTOR,
        "same_repo": True,
        "reason": "Split actor by design: snapshot reads use the repository token while only the one-time owner-visible snapshot comment write uses TAP_PAT.",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}", "github-token: ${{ github.token }}"),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Check issue Definition of Ready",
        "capability": LIVENESS_FALLBACK,
        "same_repo": True,
        "reason": "Same-repo readiness reads use the repository token; PAT is explicit only for event-producing mutations and bounded permission/rate-limit fallback.",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}", "github-token: ${{ github.token }}"),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Skip duplicate issue implementation",
        "capability": LIVENESS_FALLBACK,
        "same_repo": True,
        "reason": "Duplicate-guard reads use the repository token with a bounded TAP_PAT fallback for liveness; cross-repo references still require PAT.",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}", "github-token: ${{ github.token }}"),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Run OpenCode",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Agent execution pushes branches, publishes comments/PRs, and must fan out into CI/review workflows under an actor with workflow scope; a token run would suppress downstream triggers.",
        "evidence": ("GITHUB_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Recover agent-managed issue branch",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Lossless branch recovery pushes the agent branch and must trigger CI; guarded by fail-closed branch safety (#302), which only the merge operation may bypass toward the default branch.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Implement issue",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Issue implementation pushes code and publishes the PR; the push must fan out into CI/review workflows with workflow scope intact.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}", "GITHUB_TOKEN: ${{ secrets.TAP_PAT }}"),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Run mandatory qualification at the exact required SHA",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Exact-SHA qualification runs gh API mutations and checkouts that must observe and trigger the same CI the merge gate observes; token suppression would desynchronize the gate.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Fix CodeRabbit review findings",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Repair pushes to the PR branch must retrigger CI and review-status recomputation; a token push would suppress those workflows.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Ask CodeRabbit to verify every original finding",
        "capability": CHAINING,
        "same_repo": True,
        "reason": "Verification reads the exact reviewed review/PR state and posts verification requests that downstream CodeRabbit automation consumes; migrating the actor would require a caller pull-requests:write elevation (contract change).",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Resolve merge conflict with main",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Conflict resolution pushes the rebased PR branch and must retrigger CI; guarded by fail-closed branch safety so only non-default refs are pushed.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Fix failed blocking workflow",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "CI-fix pushes to the PR branch must retrigger the blocking workflow; token pushes would not fan out.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Re-run the consumer blocking CI workflow for this head",
        "capability": DISPATCH,
        "same_repo": True,
        "reason": "Explicit re-run/dispatch of the consumer blocking workflow requires PAT dispatch scope the caller does not grant.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Clear conflict-repair lock",
        "capability": CHAINING,
        "same_repo": True,
        "reason": "Lock-label removal must emit a label event the scheduler observes; a token-suppressed mutation would leave the queue thinking the lock is held.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Release repair lock even after OpenCode timeout or cancellation",
        "capability": CHAINING,
        "same_repo": True,
        "reason": "Always-run lock release must be observable by the scheduler even after timeout/cancellation; only a fanning-out PAT mutation guarantees the wakeup.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Release failed reservation or pause exhausted issue",
        "capability": CHAINING,
        "same_repo": True,
        "reason": "Reservation release must emit an issue event that wakes the scheduler; GITHUB_TOKEN mutations would not chain.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    # -- merge / watchdog / recovery / repair ----------------------------
    {
        "workflow": "continuum-auto-merge.yml",
        "step": "Merge only fully reviewed current heads",
        "capability": MERGE_COUPLING,
        "same_repo": True,
        "reason": "Merge/push/dispatch coupling: the merge, its evidence comments/labels, and post-merge wakeups share pre-write exact-HEAD revalidation and must fan out together; same-repo discovery reads already use READ_GITHUB_TOKEN.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}", "READ_GITHUB_TOKEN"),
    },
    {
        "workflow": "continuum-opencode-watchdog.yml",
        "step": "Recover failed OpenCode issue run",
        "capability": CHAINING,
        "same_repo": True,
        "reason": "The /oc retry comment must trigger the consumer OpenCode caller via issue_comment; GITHUB_TOKEN writes are suppressed and would silently break recovery.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-recovery.yml",
        "step": "Resolve PR-Agent target context",
        "capability": CROSS_REPO,
        "same_repo": False,
        "reason": "Delegated-aware target resolution: local runs resolve without API calls while delegated runs fetch the child target across repositories; engine files are fetched via READ_GITHUB_TOKEN first.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}", "READ_GITHUB_TOKEN"),
    },
    {
        "workflow": "continuum-pr-agent-recovery.yml",
        "step": "Reconcile PR-Agent latest state",
        "capability": EXACTLY_ONCE_CLAIM,
        "same_repo": True,
        "reason": "Dispatch-coupled exactly-once claim: the claim comment and its dispatch share one step so crash ordering is preserved; same-repo discovery reads already use READ_GITHUB_TOKEN.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}", "READ_GITHUB_TOKEN"),
    },
    {
        "workflow": "continuum-opencode-repair.yml",
        "step": "Update branch or dispatch conflict repair",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "GitHub-native branch update merges the default branch into the PR branch and must fan out; guarded by fail-closed branch safety so only non-default PR heads are updated.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode-repair.yml",
        "step": "Reconcile one CI result reported by the consumer's own CI",
        "capability": DISPATCH,
        "same_repo": True,
        "reason": "CI reconciliation dispatches the OpenCode fix workflow; the dispatch keeps PAT so the caller needs no actions:write elevation.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode-repair.yml",
        "step": "Dispatch one automatic OpenCode fix for failed blocking workflow",
        "capability": DISPATCH,
        "same_repo": True,
        "reason": "Automatic fix dispatch for failed blocking workflows is an explicit workflow dispatch on PAT; converting the actor would require caller permission changes.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    # -- review-label / coderabbit ---------------------------------------
    {
        "workflow": "continuum-add-review-label.yml",
        "step": "Mark PR ready and wake controllers",
        "capability": SPLIT_ACTOR,
        "same_repo": True,
        "reason": "Split actor already optimal: labels use github.rest.issues on GITHUB_TOKEN while only workflow dispatches use patClient(); same-repo reads use READ_GITHUB_TOKEN.",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}", "patClient().rest.actions.createWorkflowDispatch", "READ_GITHUB_TOKEN"),
    },
    {
        "workflow": "continuum-coderabbit-retry.yml",
        "step": "Reconcile global CodeRabbit review queue",
        "capability": SPLIT_ACTOR,
        "same_repo": True,
        "reason": "Split actor: queue labels use github-actions[bot] while only the @coderabbitai command comment keeps COMMENT_PAT because the integration rejects the triggering comment from GITHUB_TOKEN.",
        "evidence": ("COMMENT_PAT: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-coderabbit-unresolved.yml",
        "step": "Checkout current PR branch",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Fix push must trigger CI and review-status recomputation; a token push would suppress those workflows.",
        "evidence": ("token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-coderabbit-unresolved.yml",
        "step": "Fix all unresolved findings in one OpenCode run",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Unresolved-finding repairs push to the PR branch and publish review-thread follow-ups that must retrigger CI and CodeRabbit verification.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    # -- automation / router / bootstrap / render / docker ---------------
    {
        "workflow": "automation.yml",
        "step": "Wake repository-local reconcilers",
        "capability": DISPATCH,
        "same_repo": True,
        "reason": "Global watchdog wakes repository-local reconcilers via explicit dispatches that must fan out; GITHUB_TOKEN dispatches would require widening the workflow permission (contract change).",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-router.yml",
        "step": "Route an actionable /review comment to the heavy workflow",
        "capability": DISPATCH,
        "same_repo": True,
        "reason": "Thin event router: reads and routing run on github.token while TAP_PAT is only the bounded fallback dispatch token for the heavy workflow dispatch.",
        "evidence": ("FALLBACK_DISPATCH_TOKEN: ${{ secrets.TAP_PAT }}", "github-token: ${{ github.token }}"),
    },
    {
        "workflow": "continuum-bootstrap-runtime-secret.yml",
        "step": "Fetch Actions secrets public key",
        "capability": ADMIN_SCOPE,
        "same_repo": True,
        "reason": "Fetching the Actions secrets public key needs a PAT scope the read-only caller does not grant; migration would require a caller permission elevation.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-render-executor.yml",
        "step": "Classify and persist qualification evidence",
        "capability": BOUNDED_FALLBACK,
        "same_repo": True,
        "reason": "Qualification metadata reads prefer TAP_PAT where configured but run on github.token otherwise; the Render API itself uses the distinct RENDER_API_KEY and never PAT (explicit fail-closed when unset).",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT || github.token }}",),
    },
    {
        "workflow": "continuum-docker-qualification.yml",
        "step": "Run exact artifact under the fixed no-swap ceiling",
        "capability": BOUNDED_FALLBACK,
        "same_repo": True,
        "reason": "Qualification artifact run prefers PAT where configured but runs on the repository token otherwise; no new credential is introduced.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT || github.token }}",),
    },
    {
        "workflow": "continuum-docker-qualification.yml",
        "step": "Record Docker result and wake chain",
        "capability": BOUNDED_FALLBACK,
        "same_repo": True,
        "reason": "Result recording and chain wake prefer PAT where configured but run on the repository token otherwise; wakeup fan-out is preserved in both cases.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT || github.token }}",),
    },
    # -- PR-Agent repair unconditional sites ------------------------------
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Resolve the isolated PR-Agent stack",
        "capability": INERT,
        "same_repo": True,
        "reason": "Pure input-gating step performing no GitHub REST call; the bound credential is inert and migrating it would save zero shared-budget requests, so it is retained as-is to avoid churn.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Resolve PR-Agent target context",
        "capability": CROSS_REPO,
        "same_repo": False,
        "reason": "Delegated-aware target resolution across parent/child repositories; local runs resolve without API calls while delegated runs require cross-repo access.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Validate PR-Agent target context",
        "capability": CROSS_REPO,
        "same_repo": False,
        "reason": "Delegated target validation refuses to fall back to github.token for cross-repository runs; the guard fails closed instead of leaking child metadata or silently degrading identity.",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Resolve PR-Agent signal policy",
        "capability": CROSS_REPO,
        "same_repo": False,
        "reason": "Signal-policy resolution reads the delegated target where applicable; unconditional PAT keeps one identity for local and delegated runs instead of splitting the policy path.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Build one bounded repair batch from private handoff",
        "capability": DELEGATED_CONDITIONAL,
        "same_repo": False,
        "reason": "Private-handoff reads are delegated-aware: local same-repo batches use github.token while delegated cross-repo handoffs require PAT; the conditional expression already encodes the optimal split.",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}", "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED"),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Fail closed when delegated no-progress check lacks PAT",
        "capability": CROSS_REPO,
        "same_repo": False,
        "reason": "Fail-closed guard (no API call): delegated no-progress checks require PAT and refuse silent fallback so cross-repo state is never read under the wrong identity.",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Check durable PR-Agent no-progress state",
        "capability": DELEGATED_CONDITIONAL,
        "same_repo": False,
        "reason": "Durable no-progress reads are delegated-aware with the same optimal conditional split as the handoff batch.",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}", "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED"),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Resolve the writable PR source branch",
        "capability": DELEGATED_CONDITIONAL,
        "same_repo": False,
        "reason": "Writable-branch resolution must observe the true cross-repo PR source for delegated runs while local runs use the repository token; the conditional keeps both correct.",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}", "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED"),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Mark PR-Agent repair in flight",
        "capability": CONTROLLER_STATE,
        "same_repo": False,
        "reason": "Delegated-aware commit-status publication: cross-repository status writes plus a caller that lacks statuses:write keep PAT; migration would require a permission elevation.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Checkout the writable PR source branch",
        "capability": PUSH_FANOUT,
        "same_repo": False,
        "reason": "Writable-branch checkout with push credentials covers both local and delegated PR sources; repair pushes must retrigger CI under one identity.",
        "evidence": ("token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Run one bounded OpenCode repair pass over every current item",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Bounded repair execution pushes fixes and must fan out into CI; the batch itself is bounded (one pass over current items) so no N+1 scan remains.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Persist PR-Agent no-progress controller state",
        "capability": CONTROLLER_STATE,
        "same_repo": True,
        "reason": "Durable controller-state write read back by the next reconciliation; shares the PAT actor so lease and exactly-once semantics stay under one identity.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Publish durable PR-Agent repair state",
        "capability": CONTROLLER_STATE,
        "same_repo": True,
        "reason": "Durable repair-state publication consumed by recovery and retry scheduling; single PAT identity preserves exactly-once ordering.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Publish failed PR-Agent repair state",
        "capability": CONTROLLER_STATE,
        "same_repo": True,
        "reason": "Failure-state publication mirrors the success path so a failed repair never strands the controller without observable durable state.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Update persistent PR-Agent repair retry controller state",
        "capability": CONTROLLER_STATE,
        "same_repo": True,
        "reason": "Retry-controller state write backing the bounded retry budget; must share the reconciler identity to keep budget accounting exact.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Schedule bounded retry for retryable PR-Agent repair failure",
        "capability": EXACTLY_ONCE_CLAIM,
        "same_repo": True,
        "reason": "Bounded retry scheduling couples the retry marker with its dispatch under one actor so a lost dispatch replays the same attempt instead of duplicating.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}", "TAP_PAT: ${{ secrets.TAP_PAT }}"),
    },
    # -- PR-Agent auto-merge unconditional sites --------------------------
    {
        "workflow": "continuum-pr-agent-auto-merge.yml",
        "step": "Resolve the isolated PR-Agent stack",
        "capability": INERT,
        "same_repo": True,
        "reason": "Pure input-gating step performing no GitHub REST call; the bound credential is inert and migrating it would save zero shared-budget requests.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-auto-merge.yml",
        "step": "Resolve PR-Agent target context",
        "capability": CROSS_REPO,
        "same_repo": False,
        "reason": "Delegated-aware target resolution across parent/child repositories; local runs resolve without API calls while delegated runs require cross-repo access.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-auto-merge.yml",
        "step": "Validate upstream exact-head merge attestation",
        "capability": DELEGATED_CONDITIONAL,
        "same_repo": False,
        "reason": "Merge-attestation reads are delegated-aware: local attestation uses github.token while delegated cross-repo attestation requires PAT and refuses silent fallback.",
        "evidence": ("TAP_PAT: ${{ secrets.TAP_PAT }}", "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED"),
    },
    {
        "workflow": "continuum-pr-agent-auto-merge.yml",
        "step": "Resolve PR-Agent signal policy",
        "capability": CROSS_REPO,
        "same_repo": False,
        "reason": "Signal-policy resolution reads the delegated target where applicable; unconditional PAT keeps one identity for local and delegated runs.",
        "evidence": ("GH_TOKEN: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-auto-merge.yml",
        "step": "Evaluate the exact-head PR-Agent gate",
        "capability": MERGE_COUPLING,
        "same_repo": True,
        "reason": "Exact-head merge-gate evaluation shares the merge actor so the gate decision and the merge below observe identical state under one identity.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-auto-merge.yml",
        "step": "Reconcile current PR state and merge only the exact reviewed HEAD",
        "capability": MERGE_COUPLING,
        "same_repo": True,
        "reason": "Merge-gate coupling: merge plus its evidence comments share pre-write exact-HEAD revalidation that must fan out.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    # -- PR-Agent canary ---------------------------------------------------
    {
        "workflow": "continuum-pr-agent-canary.yml",
        "step": "Resolve canary configuration",
        "capability": CONTROLLER_STATE,
        "same_repo": True,
        "reason": "Canary configuration resolution runs under the controller identity that also creates disposable PRs; splitting the actor would desynchronize canary gating from canary execution.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-canary.yml",
        "step": "Checkout the canary base",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Canary base checkout with push credentials: disposable branches are pushed and must trigger CI on the canary PR.",
        "evidence": ("token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-canary.yml",
        "step": "Run the disposable-PR end to end",
        "capability": PUSH_FANOUT,
        "same_repo": True,
        "reason": "Disposable-branch pushes must trigger CI on the canary PR; token pushes would not fan out.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
)

#: ``continuum-pr-agent.yml`` delegated-aware family. Every PAT binding in
#: that workflow is either an unconditional delegated-guard/target site or a
#: ``CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED == 'true' && secrets.TAP_PAT``
#: conditional that already evaluates to ``github.token`` for local runs.
#: Local behavior therefore already avoids the shared PAT; unifying on one
#: credential would break delegated privacy or local budget savings.
PR_AGENT_FAMILY = {
    "workflow": "continuum-pr-agent.yml",
    "capability": DELEGATED_CONDITIONAL,
    "reason": (
        "Delegated-aware PR-Agent lifecycle: local same-repo reads/writes "
        "use github.token while delegated parent->child handoffs use "
        "TAP_PAT; the conditional already encodes the optimal split and "
        "refuses silent fallback for delegated runs."
    ),
    "steps": (
        "Resolve PR-Agent target context",
        "Fail closed when delegated without PAT",
        "Admit only a review-ready PR with green CI on the exact HEAD",
        "Mark PR-Agent review in flight",
        "Checkout the pull request head without exposing delegated repository metadata",
        "Revalidate the admitted exact HEAD immediately before review",
        "Resolve exact-HEAD original findings for targeted verification",
        "Run upstream full review on the exact HEAD",
        "Revalidate the PR head and native review output after review",
        "Export native persistent finding state for the reviewed HEAD",
        "Decide whether automatic improve can be skipped",
        "Run upstream improve on the exact HEAD when repair value remains",
        "Record skipped automatic improve for the clean HEAD",
        "Revalidate the PR head after improve",
        "Normalize persistent improve presentation",
        "Publish private exact-head repair handoff",
        "Clear settled PR-Agent controller state",
        "Publish durable PR-Agent review state",
        "Publish failed PR-Agent review state",
        "Fail closed on a moved head",
        "Update persistent PR-Agent retry controller state",
        "Schedule bounded retry for retryable PR-Agent review failure",
    ),
}

_SECRET_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)")
_STEP_RE = re.compile(r"^\s*-\s*name\s*:\s*(.+?)\s*$")


def _owning_step(lines: list, lineno: int) -> str:
    """Return the owning ``- name:`` value for a 1-based line number."""
    for index in range(lineno - 1, max(-1, lineno - 120), -1):
        match = _STEP_RE.match(lines[index])
        if match:
            return match.group(1).strip()
    return ""


def scan_workflow_bindings(workflows_dir: str) -> list:
    """Enumerate every ``secrets.TAP_PAT`` binding in shipped workflows.

    Returns a list of ``{"workflow": ..., "line": ..., "step": ...,
    "binding": ...}`` dicts. Only ``continuum-*.yml`` and the
    repository ``automation.yml`` watchdog are in scope; caller stubs never
    carry engine credentials.
    """
    bindings = []
    for name in sorted(os.listdir(workflows_dir)):
        if not name.endswith(".yml"):
            continue
        if name != "automation.yml" and not name.startswith("continuum-"):
            continue
        path = os.path.join(workflows_dir, name)
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        for lineno, line in enumerate(lines, start=1):
            if "secrets.TAP_PAT" in line:
                bindings.append({
                    "workflow": name,
                    "line": lineno,
                    "step": _owning_step(lines, lineno),
                    "binding": line.strip(),
                })
    return bindings


def _entry_covers(entry: dict, workflow: str, step: str) -> bool:
    return entry["workflow"] == workflow and entry["step"] in step


def covering_entry(workflow: str, step: str) -> dict | None:
    """Return the inventory entry covering one (workflow, step) site."""
    for entry in ENTRIES:
        if _entry_covers(entry, workflow, step):
            return entry
    if workflow == PR_AGENT_FAMILY["workflow"]:
        for family_step in PR_AGENT_FAMILY["steps"]:
            if family_step in step or step in family_step:
                return {
                    "workflow": workflow,
                    "step": family_step,
                    "capability": PR_AGENT_FAMILY["capability"],
                    "same_repo": False,
                    "reason": PR_AGENT_FAMILY["reason"],
                    "evidence": ("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", "secrets.TAP_PAT"),
                }
    return None


def coverage_gaps(workflows_dir: str) -> list:
    """Return bindings with no covering inventory entry (must stay empty)."""
    gaps = []
    for binding in scan_workflow_bindings(workflows_dir):
        if covering_entry(binding["workflow"], binding["step"]) is None:
            gaps.append(binding)
    return gaps


def validate_entry(entry: dict) -> bool:
    """Check one entry satisfies the inventory contract."""
    assert entry["workflow"].startswith("continuum-") or entry["workflow"] == "automation.yml", entry
    assert entry["step"] and entry["step"].strip(), entry
    assert entry["capability"] in CAPABILITIES, entry
    assert entry["reason"] and entry["reason"].strip(), entry
    assert entry["evidence"], entry
    return True


def validate() -> bool:
    """Validate the whole inventory (used by the contract test)."""
    for entry in ENTRIES:
        validate_entry(entry)
    assert PR_AGENT_FAMILY["steps"], "PR-Agent delegated family must list its steps"
    assert len(set(PR_AGENT_FAMILY["steps"])) == len(PR_AGENT_FAMILY["steps"]), "duplicate family step"
    # The inventory is never a global replacement: every entry retains PAT
    # with a documented capability, and the delegated family proves the
    # local path already avoids PAT where equivalent.
    assert ENTRIES, "inventory must not be empty"
    return True
