"""Authoritative credential-actor matrix for same-repository mutations (#259).

Visible comment branding (#258) solves ambiguity in the body, but GitHub
should also show a machine actor wherever the operation can safely use the
repository-scoped ``GITHUB_TOKEN``. This module is the single executable
definition of that per-operation migration: every same-repository Continuum
operation that historically used ``TAP_PAT`` is classified here, and
``tests/test_credential_matrix.py`` enforces the classification against the
shipped workflow bodies.

Migration rule (all must hold to use ``GITHUB_TOKEN``):

1. same repository;
2. required write permission can be granted by the caller;
3. no downstream lifecycle depends on a normally suppressed
   token-generated event;
4. no cross-repository access;
5. no push/workflow-file/dispatch capability requiring PAT;
6. no intentional human/PAT actor semantics.

Anything failing one criterion keeps PAT/App-capable authentication with an
explicit documented reason. This is never a global PAT replacement: at least
one retained PAT path must always exist, and the contract test fails closed
if it ever disappears.
"""

from __future__ import annotations

GITHUB_TOKEN = "GITHUB_TOKEN"
KEEP_PAT = "PAT"

#: Secrets a workflow may reference. No new user-created secret is allowed by
#: #259: only the pre-existing TAP_PAT, the Render key (Render API only), the
#: delegated child runtime token, and the automatic github.token.
ALLOWED_SECRETS = frozenset(
    {
        "TAP_PAT",
        "RENDER_API_KEY",
        "CHILD_RUNTIME_TOKEN",
        "CHILD_RUNTIME_REPOSITORIES",
    }
)

#: Each entry describes one credential site. ``step`` is a unique substring
#: of the step's ``- name:`` (or job name when the site is job-level env).
#: ``evidence`` lists literals the contract test uses to locate the site.
#: ``verdict`` is ``GITHUB_TOKEN`` (actor is github-actions[bot]) or ``PAT``
#: (retained with ``reason``).
ENTRIES = (
    # -- Migrated: informational/state mutations with no chaining ----------
    {
        "workflow": "continuum-coderabbit-retry.yml",
        "step": "Reconcile global CodeRabbit review queue",
        "operation": "issues.addLabels/removeLabel + @coderabbitai command comment (split actor)",
        "kind": "label/comment",
        "same_repo": True,
        "permission": "issues:write",
        "needs_chaining": False,
        "cross_repo": False,
        "needs_push_dispatch": False,
        "needs_human_actor": False,
        "verdict": KEEP_PAT,
        "reason": "GitHub integration permission rejects the CodeRabbit-triggering PR comment from GITHUB_TOKEN; queue labels remain github-actions[bot] while only the command comment retains PAT.",
        "evidence": ("COMMENT_PAT: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-coderabbit-unresolved.yml",
        "step": "Collect all unresolved findings",
        "operation": "pulls.listReviewComments/pulls.get (read-only)",
        "kind": "read",
        "same_repo": True,
        "permission": "pull-requests:read",
        "needs_chaining": False,
        "cross_repo": False,
        "needs_push_dispatch": False,
        "needs_human_actor": False,
        "verdict": GITHUB_TOKEN,
        "reason": "Same-repo reads; PAT budget reserved for pushes.",
        "evidence": ("github-token: ${{ github.token }}",),
    },
    {
        "workflow": "continuum-coderabbit-unresolved.yml",
        "step": "Ask CodeRabbit to verify every finding in the batch",
        "operation": "review-thread replies (@coderabbitai re-check)",
        "kind": "comment",
        "same_repo": True,
        "permission": "pull-requests:write",
        "needs_chaining": False,
        "cross_repo": False,
        "needs_push_dispatch": False,
        "needs_human_actor": False,
        "verdict": GITHUB_TOKEN,
        "reason": "Replies address CodeRabbit (external app); no GitHub workflow is triggered by them.",
        "evidence": ("github-token: ${{ github.token }}",),
    },
    {
        "workflow": "continuum-add-review-label.yml",
        "step": "Mark PR ready and wake controllers",
        "operation": "issues.addLabels/removeLabel/createLabel (review-ready state)",
        "kind": "label",
        "same_repo": True,
        "permission": "issues:write",
        "needs_chaining": False,
        "cross_repo": False,
        "needs_push_dispatch": False,
        "needs_human_actor": False,
        "verdict": GITHUB_TOKEN,
        "reason": "Labels are queue state polled by dispatch; the dispatches themselves keep PAT via patClient().",
        "evidence": (
            "github-token: ${{ github.token }}",
            "patClient().rest.actions.createWorkflowDispatch",
        ),
    },
    {
        "workflow": "continuum-remove-review-label.yml",
        "step": "remove-label",
        "operation": "issues.removeLabel (review-ready reset on synchronize)",
        "kind": "label",
        "same_repo": True,
        "permission": "issues:write",
        "needs_chaining": False,
        "cross_repo": False,
        "needs_push_dispatch": False,
        "needs_human_actor": False,
        "verdict": GITHUB_TOKEN,
        "reason": "Already implicit GITHUB_TOKEN (no github-token override); label reset needs no chaining.",
        "evidence": ("issues.removeLabel",),
    },
    {
        "workflow": "continuum-opencode-unresolved.yml",
        "step": "Dispatch OpenCode retry",
        "operation": "gh api workflow dispatch + informational state (token-backed)",
        "kind": "comment",
        "same_repo": True,
        "permission": "pull-requests:read",
        "needs_chaining": False,
        "cross_repo": False,
        "needs_push_dispatch": False,
        "needs_human_actor": False,
        "verdict": GITHUB_TOKEN,
        "reason": "Already github.token-backed; reference migrated path.",
        "evidence": ("GH_TOKEN: ${{ github.token }}",),
    },
    {
        "workflow": "continuum-pr-agent.yml",
        "step": "Mark PR-Agent review in flight",
        "operation": "createComment/updateComment/createCommitStatus (local only)",
        "kind": "comment/status",
        "same_repo": True,
        "permission": "issues:write",
        "needs_chaining": False,
        "cross_repo": False,
        "needs_push_dispatch": False,
        "needs_human_actor": False,
        "verdict": GITHUB_TOKEN,
        "reason": "Conditional token: local same-repo writes use github.token, delegated cross-repo writes use PAT.",
        "evidence": (
            "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED == 'true' && secrets.TAP_PAT",
            "github.token",
        ),
    },
    {
        "workflow": "continuum-render-executor.yml",
        "step": "Execute Render lifecycle",
        "operation": "gh issue/PR metadata reads + comments (GitHub side)",
        "kind": "comment",
        "same_repo": True,
        "permission": "issues:write",
        "needs_chaining": False,
        "cross_repo": False,
        "needs_push_dispatch": False,
        "needs_human_actor": False,
        "verdict": GITHUB_TOKEN,
        "reason": "GitHub-side mutations already github.token-backed; Render API keeps RENDER_API_KEY (distinct credential).",
        "evidence": ("GH_TOKEN: ${{ github.token }}",),
    },
    # -- Retained: changing actor would break chaining/push/dispatch --------
    {
        "workflow": "continuum-opencode-watchdog.yml",
        "step": "Recover failed OpenCode issue run",
        "operation": "issues.createComment (/oc retry) + addLabels/removeLabel",
        "kind": "comment/label",
        "same_repo": True,
        "permission": "issues:write",
        "needs_chaining": True,
        "cross_repo": False,
        "needs_push_dispatch": False,
        "needs_human_actor": False,
        "verdict": KEEP_PAT,
        "reason": "The /oc retry comment must trigger the consumer OpenCode caller via issue_comment; GITHUB_TOKEN writes are suppressed and would silently break recovery.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-auto-merge.yml",
        "step": "Merge only fully reviewed current heads",
        "operation": "merge/push + comments/labels + createWorkflowDispatch (coupled)",
        "kind": "merge/comment/label/dispatch",
        "same_repo": True,
        "permission": "contents:write",
        "needs_chaining": True,
        "cross_repo": False,
        "needs_push_dispatch": True,
        "needs_human_actor": False,
        "verdict": KEEP_PAT,
        "reason": "Merge/push/dispatch coupling: GITHUB_TOKEN pushes do not fan out into downstream push/merge workflows.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-issue-scheduler.yml",
        "step": "Reconcile state and dispatch ready issues",
        "operation": "issues.createComment (/oc dispatch) + createWorkflowDispatch",
        "kind": "comment/dispatch",
        "same_repo": True,
        "permission": "issues:read",
        "needs_chaining": True,
        "cross_repo": False,
        "needs_push_dispatch": True,
        "needs_human_actor": False,
        "verdict": KEEP_PAT,
        "reason": "Scheduler /oc dispatch comments must wake OpenCode callers; token-suppressed events would stall the queue.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Checkout repository",
        "operation": "actions/checkout with push credentials",
        "kind": "push",
        "same_repo": True,
        "permission": "contents:write",
        "needs_chaining": True,
        "cross_repo": False,
        "needs_push_dispatch": True,
        "needs_human_actor": False,
        "verdict": KEEP_PAT,
        "reason": "Writable checkout/push semantics: agent branches are pushed and must trigger CI/review workflows.",
        "evidence": ("token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Release failed reservation or pause exhausted issue",
        "operation": "issues.createComment + addLabels/removeLabel (scheduler wake-up)",
        "kind": "comment/label",
        "same_repo": True,
        "permission": "issues:write",
        "needs_chaining": True,
        "cross_repo": False,
        "needs_push_dispatch": False,
        "needs_human_actor": False,
        "verdict": KEEP_PAT,
        "reason": "Reservation release must emit an issue event that wakes the scheduler; GITHUB_TOKEN mutations would not chain.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-coderabbit-unresolved.yml",
        "step": "Checkout current PR branch",
        "operation": "actions/checkout with push credentials",
        "kind": "push",
        "same_repo": True,
        "permission": "contents:write",
        "needs_chaining": True,
        "cross_repo": False,
        "needs_push_dispatch": True,
        "needs_human_actor": False,
        "verdict": KEEP_PAT,
        "reason": "Fix push must trigger CI and review-status recomputation; a token push would suppress those workflows.",
        "evidence": ("token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-recovery.yml",
        "step": "Reconcile PR-Agent latest state",
        "operation": "controller createComment/updateComment/deleteComment + createWorkflowDispatch (exactly-once claim)",
        "kind": "comment/dispatch",
        "same_repo": True,
        "permission": "issues:write",
        "needs_chaining": True,
        "cross_repo": False,
        "needs_push_dispatch": True,
        "needs_human_actor": False,
        "verdict": KEEP_PAT,
        "reason": "Dispatch-coupled exactly-once claim: the claim comment and its dispatch share one step so crash ordering is preserved.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Mark PR-Agent repair in flight",
        "operation": "repos.createCommitStatus (delegated-aware target)",
        "kind": "status",
        "same_repo": False,
        "permission": "statuses:write",
        "needs_chaining": False,
        "cross_repo": True,
        "needs_push_dispatch": False,
        "needs_human_actor": False,
        "verdict": KEEP_PAT,
        "reason": "Delegated cross-repository status writes plus caller lacks statuses:write; migration would require a permission elevation.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-canary.yml",
        "step": "Run the disposable-PR end to end",
        "operation": "git push of disposable branches + verification comments",
        "kind": "push/comment",
        "same_repo": True,
        "permission": "contents:write",
        "needs_chaining": True,
        "cross_repo": False,
        "needs_push_dispatch": True,
        "needs_human_actor": False,
        "verdict": KEEP_PAT,
        "reason": "Disposable-branch pushes must trigger CI on the canary PR; token pushes would not fan out.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
    {
        "workflow": "continuum-pr-agent-auto-merge.yml",
        "step": "Reconcile current PR state and merge only the exact reviewed HEAD",
        "operation": "merge + comments/labels (coupled)",
        "kind": "merge/comment",
        "same_repo": True,
        "permission": "contents:write",
        "needs_chaining": True,
        "cross_repo": False,
        "needs_push_dispatch": True,
        "needs_human_actor": False,
        "verdict": KEEP_PAT,
        "reason": "Merge-gate coupling: merge plus its evidence comments share pre-write revalidation that must fan out.",
        "evidence": ("github-token: ${{ secrets.TAP_PAT }}",),
    },
)


def migrated_entries():
    """Entries whose actor must be github-actions[bot] (GITHUB_TOKEN)."""
    return [entry for entry in ENTRIES if entry["verdict"] == GITHUB_TOKEN]


def retained_entries():
    """Entries that intentionally keep PAT with a documented reason."""
    return [entry for entry in ENTRIES if entry["verdict"] == KEEP_PAT]


def validate_entry(entry):
    """Check one entry satisfies the migration rule and is documented."""
    assert entry["verdict"] in (GITHUB_TOKEN, KEEP_PAT), entry
    assert entry["reason"] and entry["reason"].strip(), entry
    assert entry["workflow"].startswith("continuum-"), entry
    assert entry["evidence"], entry
    if entry["verdict"] == GITHUB_TOKEN:
        assert entry["same_repo"] is True, entry
        assert entry["needs_chaining"] is False, entry
        assert entry["cross_repo"] is False, entry
        assert entry["needs_push_dispatch"] is False, entry
        assert entry["needs_human_actor"] is False, entry
    return True


def validate():
    """Validate the whole matrix (used by the contract test)."""
    for entry in ENTRIES:
        validate_entry(entry)
    assert migrated_entries(), "at least one GITHUB_TOKEN migration is required"
    assert retained_entries(), (
        "at least one retained PAT path is required: "
        "this is never a global token replacement"
    )
    return True
