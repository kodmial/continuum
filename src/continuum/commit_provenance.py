"""Authoritative commit and review provenance contract (#260).

Machine-generated commits must be visibly distinguishable from human
commits, and automated review actions must never be mistaken for the
repository owner's manual approval. This module is the single canonical
definition of that provenance contract; workflow bodies must conform to
it byte-for-byte (bot identity and trailers), and the deterministic
suites in ``tests/test_commit_provenance.py`` and
``scripts/test-continuum.rb`` enforce conformance across every
commit-producing and review-gating path.

Commit contract
--------------
Every Continuum-owned ``git commit`` uses the GitHub Actions bot
identity unless a stronger dedicated App identity already exists::

    name:  github-actions[bot]
    email: 41898282+github-actions[bot]@users.noreply.github.com

Every workflow-owned commit message carries one stable trailer on its
own line::

    Continuum-Component: <component>

``component`` is a lowercase kebab-case producer name without slashes,
matching the comment-attribution component namespace, so a private
``owner/repo`` identity or secret can never hide inside a trailer.
Developer-authored commits are never rewritten merely for appearance;
only the workflow-owned sites listed in :data:`COMMIT_SITES` carry the
trailer.

Ownership model
---------------
The workflow owns the final commit and sets the bot identity. Agent
prompts forbid the tool from committing, pushing, rebasing, or opening
pull requests directly ("the workflow owns Git state"); the workflow
performs ``git add -A`` followed by ``git commit`` with the bot
identity. No tool commit impersonates the repository owner.

Review contract
---------------
1. Automation never manufactures a human-looking approval: no workflow,
   script, or engine module may call the pull-request review creation
   APIs (``pulls.createReview`` / ``reviews.create`` / ``submitReview``
   / ``gh pr review``). Merge gating reads existing review decisions
   (``review.state``) and deterministic check/status gates instead.
2. Machine review findings visibly identify the automated
   reviewer/component through the #258 attribution contract
   (``continuum-origin`` marker plus visible header).
3. Where ``GITHUB_TOKEN`` cannot provide the required review/event
   semantics, the safe PAT credential is retained but the visible
   attribution contract from #258 still applies.
4. Required merge gating relies on deterministic checks/statuses bound
   to the exact HEAD (``sha:`` pinned to ``HEAD_SHA``/``TARGET_SHA`` and
   ``pulls.merge`` with an explicit ``sha:``), never on pretending a PAT
   owner personally approved.
5. Operations that truly need a distinct actor beyond
   ``github-actions[bot]`` are isolated in :data:`RESIDUAL_PAT_OPS` as
   candidates for a future Continuum GitHub App (see the GitHub App
   installation authentication model
   https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/about-authentication-with-a-github-app)
   rather than silently using the owner's identity.
"""

from __future__ import annotations

import re

#: Machine identity for every workflow-owned commit.
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

#: Stable commit trailer prefix. The trailer sits on its own line in the
#: commit message body, after a blank line following the subject.
TRAILER_PREFIX = "Continuum-Component:"

#: Producer component names: lowercase kebab-case without slashes so a
#: private ``owner/repo`` identity can never hide inside a trailer.
COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Secret/token shapes that must never appear in a trailer or subject.
_SECRET_RES = (
    re.compile(r"ghp_[A-Za-z0-9]+"),
    re.compile(r"gho_[A-Za-z0-9_-]+"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"\brnd_[A-Za-z0-9]+\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"xox[abp]-"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

#: Review-creation APIs that would manufacture a human-looking approval.
#: Read-only decision polling (``review.state``, ``reviews.filter``) is
#: allowed; any literal below appearing in workflow/script/engine sources
#: (outside tests) fails the contract. ``gh pr review`` covers all of its
#: event modes (--approve/--request-changes/--comment) because even a
#: machine-labelled approval event renders as a review decision.
FORBIDDEN_REVIEW_APIS = (
    "createReview",
    "submitReview",
    "reviews.create",
    "gh pr review",
    "pulls.createReview",
)

#: ``createCommitStatus`` sites must pin one of these exact-HEAD SHAs.
ALLOWED_STATUS_SHAS = (
    "process.env.HEAD_SHA",
    "process.env.TARGET_SHA",
)

#: Every workflow-owned commit site. ``step`` is a unique substring of the
#: owning step's ``- name:`` (or a nearby unique anchor when the commit
#: lives in a script block); ``component`` is the trailer value;
#: ``subject`` is a unique substring of the commit subject line;
#: ``identity`` describes how the bot identity is applied in that step
#: (``git-config`` for a preceding ``git config user.*`` pair, ``git-c``
#: for inline ``git -c user.name=...`` flags).
COMMIT_SITES = (
    {
        "workflow": "continuum-opencode.yml",
        "step": "Implement issue",
        "component": "opencode",
        "subject": "implement issue #",
        "identity": "git-config",
        "scope": "initial implementation commits",
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Recover agent-managed issue branch",
        "component": "opencode",
        "subject": "recover OpenCode issue changes",
        "identity": "git-config",
        "scope": "CI/repair commits (branch-recovery)",
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Fix CodeRabbit review findings",
        "component": "opencode",
        "subject": "address CodeRabbit review findings for PR #",
        "identity": "git-config",
        "scope": "CI/repair commits (CodeRabbit review repair)",
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Resolve merge conflict with main",
        "component": "opencode",
        "subject": "resolve merge conflict with main for PR #",
        "identity": "git-config",
        "scope": "main-sync/conflict-repair commits",
    },
    {
        "workflow": "continuum-opencode.yml",
        "step": "Fix failed blocking workflow",
        "component": "opencode",
        "subject": "repair blocking workflow for PR #",
        "identity": "git-config",
        "scope": "CI/repair commits (blocking workflow repair)",
    },
    {
        "workflow": "continuum-coderabbit-unresolved.yml",
        "step": "Fix all unresolved findings in one OpenCode run",
        "component": "coderabbit-unresolved",
        "subject": "address unresolved CodeRabbit findings",
        "identity": "git-config",
        "scope": "CI/repair commits (CodeRabbit findings)",
    },
    {
        "workflow": "continuum-pr-agent-repair.yml",
        "step": "Run one bounded OpenCode repair pass over every current item",
        "component": "pr-agent-repair",
        "subject": "apply PR-Agent review findings",
        "identity": "git-c",
        "scope": "PR-Agent repair commits",
    },
    {
        "workflow": "continuum-pr-agent-canary.yml",
        "step": "Run the disposable-PR end to end",
        "component": "pr-agent-canary",
        "subject": "seed disposable PR-Agent canary defect",
        "identity": "git-c",
        "scope": "disposable canary seed commit",
    },
    {
        "workflow": "continuum-pr-agent-canary.yml",
        "step": "Run the disposable-PR end to end",
        "component": "pr-agent-canary",
        "subject": "correct disposable PR-Agent canary defect",
        "identity": "git-c",
        "scope": "disposable canary correction commit",
    },
    {
        "workflow": "continuum-consumer-child-worker.yml",
        "step": "Execute child task",
        "component": "delegation-worker",
        "subject": "delegated implementation",
        "identity": "git-config",
        "scope": "delegated initial implementation commits",
    },
    {
        "workflow": "continuum-consumer-child-review.yml",
        "step": "Review and repair child task",
        "component": "delegation-review",
        "subject": "delegated independent review",
        "identity": "git-config",
        "scope": "delegated review commits",
    },
    {
        "workflow": "continuum-consumer-child-pr-review.yml",
        "step": "Review and repair owner-created child pull request",
        "component": "delegation-pr-review",
        "subject": "repair PR #",
        "identity": "git-config",
        "scope": "delegated PR repair commits",
    },
)

#: Operations that intentionally retain PAT identity because GITHUB_TOKEN
#: semantics cannot provide the required push/merge/dispatch fan-out or
#: cross-repository access. Each entry documents the exact residual
#: operation and why it is a candidate for a future Continuum GitHub App
#: (which would attribute the same operation to an app actor instead of
#: the repository owner). This list is exhaustive for merge/recovery
#: operations: no other workflow-owned path may silently use the owner's
#: identity.
RESIDUAL_PAT_OPS = (
    {
        "operation": "continuum-opencode issue-branch push (PAT checkout + push)",
        "why_pat": "Agent branches are pushed and must trigger CI/review workflows; GITHUB_TOKEN pushes do not fan out.",
        "app_candidate": "Attribute task-branch publication to a Continuum GitHub App instead of the owner PAT.",
    },
    {
        "operation": "continuum-coderabbit-unresolved fix push",
        "why_pat": "Fix pushes must trigger CI and review-status recomputation; token pushes would suppress those workflows.",
        "app_candidate": "Attribute CodeRabbit fix pushes to a Continuum GitHub App.",
    },
    {
        "operation": "continuum-pr-agent-repair exact-HEAD push (force-with-lease)",
        "why_pat": "Repair pushes must trigger CI and exact-HEAD re-review; token pushes would suppress downstream gates.",
        "app_candidate": "Attribute PR-Agent repair pushes to a Continuum GitHub App.",
    },
    {
        "operation": "continuum-pr-agent-canary disposable-branch pushes",
        "why_pat": "Disposable-branch pushes must trigger CI on the canary PR; token pushes would not fan out.",
        "app_candidate": "Attribute disposable canary pushes to a Continuum GitHub App.",
    },
    {
        "operation": "continuum-auto-merge / continuum-pr-agent-auto-merge squash merges (pulls.merge with exact sha)",
        "why_pat": "Merge plus its evidence comments share pre-write exact-HEAD revalidation that must fan out into downstream workflows.",
        "app_candidate": "Attribute automated squash merges to a Continuum GitHub App so the merge actor is the app, not the owner.",
    },
    {
        "operation": "GitHub-native main-sync (update-branch) dispatched by merge controllers",
        "why_pat": "Branch syncs must produce merge commits that trigger CI; the sync is requested via PAT-authenticated API.",
        "app_candidate": "Attribute main-sync requests to a Continuum GitHub App.",
    },
    {
        "operation": "delegated child merge/update-branch (gh pr merge, update-branch in child repositories)",
        "why_pat": "Cross-repository delegated merges require PAT access to the child repository; GITHUB_TOKEN cannot reach it.",
        "app_candidate": "Attribute delegated child merges to a Continuum GitHub App installation per child repository.",
    },
)


def require_component(component: str | None) -> str:
    """Validate a trailer component name (never a repository identity)."""
    text = str(component or "").strip()
    if not COMPONENT_RE.match(text):
        raise ValueError(
            f"invalid provenance component: {component!r} "
            "(lowercase kebab-case, no slashes or repository names)"
        )
    return text


def trailer_line(component: str) -> str:
    """Return the stable commit trailer line for one producer."""
    return f"{TRAILER_PREFIX} {require_component(component)}"


def format_commit_message(subject: str, component: str) -> str:
    """Return a commit message with the provenance trailer appended."""
    clean_subject = str(subject or "").strip()
    if not clean_subject:
        raise ValueError("commit subject must not be empty")
    if contains_secret(clean_subject):
        raise ValueError("commit subject must not contain secret material")
    return f"{clean_subject}\n\n{trailer_line(component)}"


def contains_secret(text: str) -> str | None:
    """Return the offending secret shape in text, or None when clean."""
    for pattern in _SECRET_RES:
        match = pattern.search(text or "")
        if match:
            return match.group(0)[:12]
    return None


def validate_site(site: dict) -> bool:
    """Check one commit-site entry satisfies the provenance contract."""
    assert site["workflow"].startswith("continuum-"), site
    assert site["step"] and site["step"].strip(), site
    assert site["subject"] and site["subject"].strip(), site
    assert site["identity"] in ("git-config", "git-c"), site
    assert site["scope"] and site["scope"].strip(), site
    require_component(site["component"])
    assert contains_secret(site["component"]) is None, site
    assert "/" not in site["component"], site
    return True


def validate() -> bool:
    """Validate the whole provenance matrix (used by the contract test)."""
    assert COMMIT_SITES, "at least one workflow-owned commit site is required"
    for site in COMMIT_SITES:
        validate_site(site)
    assert RESIDUAL_PAT_OPS, "residual PAT operations must be documented"
    for entry in RESIDUAL_PAT_OPS:
        assert entry["operation"] and entry["operation"].strip(), entry
        assert entry["why_pat"] and entry["why_pat"].strip(), entry
        assert entry["app_candidate"] and entry["app_candidate"].strip(), entry
    assert BOT_NAME == "github-actions[bot]", BOT_NAME
    assert BOT_EMAIL.endswith("@users.noreply.github.com"), BOT_EMAIL
    return True
