"""Provider-neutral merge lifecycle decisions (kodmial/continuum#304).

This module is the single canonical implementation of the shared
provider-neutral lifecycle owned equally by the CodeRabbit path
(``continuum-auto-merge.yml``) and the PR-Agent path
(``continuum-pr-agent-auto-merge.yml``):

- PR / current-HEAD reconciliation;
- current-HEAD CI / gate validation;
- ``no-auto-merge``;
- main-sync policy;
- conflict-repair plumbing constants (decisions stay in
  :mod:`continuum.conflict_repair`);
- Packaging smoke / configurable required gates;
- final exact-HEAD validation;
- atomic merge (exact-``sha:`` guard);
- merge-title normalization;
- post-merge wakeups;
- generic idempotency / concurrency / race protection.

It is a pure decision library, not a controller, reducer, or workflow:
it performs no I/O, dispatches nothing, merges nothing, and introduces
no new runtime authority. The two auto-merge workflows remain the only
executors; their embedded JavaScript mirrors this module at runtime
(see ``.github/scripts/merge_lifecycle.js``). Provider-specific
retry/review policy (CodeRabbit quota/rate-limit/unresolved semantics,
PR-Agent structured review/persistent-state/improve semantics) is
deliberately absent here and stays in
:mod:`continuum.coderabbit_adapter` and
:mod:`continuum.pr_agent_lifecycle` respectively.

Branch safety (#302) stays canonical in
:mod:`continuum.branch_safety`; conflict-repair decisions stay canonical
in :mod:`continuum.conflict_repair`. This module delegates to both and
never duplicates them.

Standard library only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import branch_safety as _branch_safety
from . import conflict_repair as _conflict_repair

#: Label that opts a PR out of automatic merging. Identical in both
#: providers; checked before every write and rechecked immediately
#: before merge.
AUTO_MERGE_BLOCK_LABEL = "no-auto-merge"

#: Conflict-repair lock label. Canonical definition lives in
#: :mod:`continuum.conflict_repair`; re-exported here so merge
#: call sites have one import for the shared lifecycle surface.
CONFLICT_LOCK_LABEL = _conflict_repair.CONFLICT_LOCK_LABEL

#: Bounded per-HEAD conflict-repair budget. Canonical in
#: :mod:`continuum.conflict_repair`; re-exported, not redefined.
MAX_CONFLICT_DISPATCHES_PER_HEAD = _conflict_repair.MAX_DISPATCHES_PER_HEAD
CONFLICT_DISPATCH_GRACE_SECONDS = _conflict_repair.DISPATCH_GRACE_SECONDS

#: Review-provider selection contract. Exactly these three values;
#: there is no ``both`` mode.
REVIEW_PROVIDERS = ("none", "coderabbit", "pr-agent")

#: Conventional-commit prefixes accepted without a ``fix:`` fallback.
#: Byte-equivalent to the ``conventionalTitle`` regex embedded in both
#: auto-merge workflows:
#: ``/^(?:fix|feat|perf|refactor)(?:\\([^)]*\\))?!?:\\s/i``.
_CONVENTIONAL_TITLE_RE = re.compile(
    r"^(?:fix|feat|perf|refactor)(?:\([^)]*\))?!?:\s", re.IGNORECASE
)

_BRANCH_SANITIZE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class MergeLifecycleError(ValueError):
    """Raised when merge-lifecycle inputs cannot be interpreted safely."""


def resolve_review_provider(value: Any) -> str:
    """Resolve the authoritative review provider (no ``both`` mode)."""

    text = str(value or "none").strip().lower() or "none"
    if text not in REVIEW_PROVIDERS:
        raise MergeLifecycleError(
            "invalid CONTINUUM_REVIEW_PROVIDER: expected none, coderabbit, or pr-agent"
        )
    return text


def is_same_head(expected_sha: Any, actual_sha: Any) -> bool:
    """Whether two HEAD values name the same exact commit."""

    expected = str(expected_sha or "").strip().lower()
    actual = str(actual_sha or "").strip().lower()
    if not expected or not actual:
        return False
    return expected == actual


def head_matches(reviewed_head: Any, live_head: Any) -> bool:
    """Alias for :func:`is_same_head` at merge-gate call sites."""

    return is_same_head(reviewed_head, live_head)


def validate_exact_head(reviewed_head: Any, live_head: Any) -> Tuple[bool, str]:
    """Fail-closed exact-HEAD check performed immediately before merge."""

    if not str(reviewed_head or "").strip():
        return False, "reviewed HEAD is missing: failing closed"
    if not str(live_head or "").strip():
        return False, "live PR HEAD is missing: failing closed"
    if not is_same_head(reviewed_head, live_head):
        return (
            False,
            "PR head moved ({} -> {}): failing closed".format(
                str(reviewed_head).strip().lower(), str(live_head).strip()
            ),
        )
    return True, "exact HEAD verified"


def is_merge_blocked(labels: Sequence[Any]) -> bool:
    """Whether the ``no-auto-merge`` block label is present."""

    names = set()
    for label in labels or []:
        if isinstance(label, str):
            names.add(label)
        elif isinstance(label, Mapping):
            name = label.get("name")
            if isinstance(name, str):
                names.add(name)
    return AUTO_MERGE_BLOCK_LABEL in names


def is_non_merge_critical_main_path(path: Any) -> bool:
    """Whether a main-delta path never forces a PR re-sync.

    Mirrors ``isNonMergeCriticalMainPath`` in both auto-merge workflows
    exactly: Continuum caller pins (``.github/workflows/continuum-*``)
    are executable control-plane code and always merge-critical; docs,
    ``.github/`` metadata, license/ignore/changelog entries are not.
    """

    text = str(path or "")
    if text.startswith(".github/workflows/continuum-"):
        return False
    return (
        text == "LICENSE"
        or text == ".gitignore"
        or text == ".release-please-manifest.json"
        or text == "CHANGELOG.md"
        or text.endswith(".md")
        or text.startswith("docs/")
        or text.startswith(".github/")
    )


def decide_main_sync(
    behind_by: Any, files: Optional[Sequence[Any]]
) -> Dict[str, Any]:
    """Decide whether main advanced in merge-critical files.

    ``behind_by`` is the ``compare(main...head).behind_by`` count;
    ``files`` is the reverse-delta file list (``head...main``). Returns
    ``{"required": bool, "reason": str}`` with one of ``up-to-date``,
    ``non-merge-critical-main-delta``, ``merge-critical-main-delta``,
    or ``unknown-main-delta``. An unknown delta (no file list) requires
    a sync rather than silently bypassing integration validation.
    """

    try:
        behind = int(behind_by or 0)
    except (TypeError, ValueError):
        behind = 0
    names = [str(item) for item in (files or [])]
    if behind <= 0:
        return {
            "required": False,
            "reason": "up-to-date",
            "files": names,
            "critical_files": [],
        }
    critical = [item for item in names if not is_non_merge_critical_main_path(item)]
    if names and not critical:
        return {
            "required": False,
            "reason": "non-merge-critical-main-delta",
            "files": names,
            "critical_files": critical,
        }
    return {
        "required": True,
        "reason": "unknown-main-delta" if not names else "merge-critical-main-delta",
        "files": names,
        "critical_files": critical,
    }


def sanitize_branch(name: Any, fallback: str = "main") -> str:
    """Validate a branch/ref value, falling back to ``fallback``.

    Mirrors ``sanitizeBranch`` in ``continuum-pr-agent-auto-merge.yml``
    and the wakeup-ref guard in ``continuum-auto-merge.yml``: hidden
    components, ``..``/``//``/``@{``, trailing ``/``/``.``/``.lock``,
    ``HEAD``/``@``, and ``refs/``/``-``/``.`` prefixes fall back.
    """

    value = str(name or "").strip() or str(fallback or "").strip() or "main"
    if not _BRANCH_SANITIZE_RE.match(value):
        return "main"
    if ".." in value or "//" in value or "@{" in value:
        return "main"
    if value.endswith("/") or value.endswith(".") or value.endswith(".lock"):
        return "main"
    if value in ("HEAD", "@"):
        return "main"
    if value.startswith("refs/") or value.startswith("-") or value.startswith("."):
        return "main"
    if any(part.startswith(".") or part.endswith(".lock") for part in value.split("/")):
        return "main"
    return value


def sanitize_wakeup_ref(value: Any, fallback: str = "main") -> str:
    """Validate a post-merge wakeup ref (same rules as branches)."""

    return sanitize_branch(value, fallback)


def parse_post_merge_wakeups(value: Any) -> List[str]:
    """Parse the consumer-owned post-merge wakeup CSV list."""

    items = []
    for part in str(value or "").split(","):
        name = part.strip()
        if name:
            items.append(name)
    return items


def normalize_merge_title(title: Any) -> str:
    """Normalize a merge commit title (human-friendly ``fix:`` fallback)."""

    text = str(title or "").strip()
    if not text:
        raise MergeLifecycleError("merge title must not be empty")
    if _CONVENTIONAL_TITLE_RE.match(text):
        return text
    return "fix: {}".format(text)


def build_merge_params(
    *, reviewed_head: Any, live_head: Any, title: Any
) -> Dict[str, Any]:
    """Build exact-HEAD atomic merge parameters or fail closed.

    Returns ``{"commit_title": ..., "sha": ...}`` for
    ``pulls.merge``. Any HEAD movement, missing HEAD, or empty title
    raises :class:`MergeLifecycleError` instead of merging a stale HEAD.
    """

    ok, reason = validate_exact_head(reviewed_head, live_head)
    if not ok:
        raise MergeLifecycleError(reason)
    sha = str(reviewed_head).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        raise MergeLifecycleError("reviewed HEAD is not a commit SHA: failing closed")
    return {"commit_title": normalize_merge_title(title), "sha": sha}


def lease_key(pr_number: Any, head_sha: Any) -> str:
    """In-memory per-PR/HEAD lease key for duplicate-wakeup coalescing."""

    return "{}:{}".format(str(pr_number), str(head_sha or "").strip().lower())


def _run_gate_state(run: Any) -> Tuple[str, str]:
    if not isinstance(run, Mapping):
        return ("missing", "missing")
    return (
        str(run.get("status") or "missing"),
        str(run.get("conclusion") or "pending"),
    )


def evaluate_packaging_gate(packaging_run: Any) -> Tuple[bool, str]:
    """Evaluate the conditional Packaging smoke gate.

    Absent packaging evidence passes (conditional gate); a present run
    for the exact HEAD must be completed/success.
    """

    if packaging_run is None:
        return True, "packaging smoke absent: gate passes"
    status, conclusion = _run_gate_state(packaging_run)
    if status != "completed" or conclusion != "success":
        return False, "Packaging smoke is {}/{}".format(status, conclusion)
    return True, "packaging smoke successful"


def evaluate_required_workflow_gate(
    *,
    labels: Sequence[Any],
    gate_label: Any,
    gate_name: Any,
    required_run: Any,
) -> Tuple[bool, str]:
    """Evaluate the configurable label-gated required workflow gate."""

    label = str(gate_label or "").strip()
    name = str(gate_name or "").strip()
    if not label or not name:
        return True, "required workflow gate not configured"
    if not is_merge_blocked([]) and not _has_label(labels, label):
        return True, "required workflow gate label absent"
    if required_run is None:
        return False, "required workflow {} is not successful".format(name)
    status, conclusion = _run_gate_state(required_run)
    if status != "completed" or conclusion != "success":
        return False, "required workflow {} is not successful".format(name)
    return True, "required workflow gate successful"


def _has_label(labels: Sequence[Any], name: str) -> bool:
    for label in labels or []:
        if isinstance(label, str) and label == name:
            return True
        if isinstance(label, Mapping) and label.get("name") == name:
            return True
    return False


def evaluate_current_head_gates(
    *,
    ci_run: Any,
    contract_gate_run: Any = None,
    contract_qualification_run: Any = None,
    packaging_run: Any = None,
    labels: Sequence[Any] = (),
    required_gate_label: Any = "",
    required_gate_name: Any = "",
    required_gate_run: Any = None,
    repository: str = "",
    delegated_skipped_ci: bool = False,
) -> Tuple[bool, str]:
    """Evaluate every current-HEAD quality gate for one exact HEAD.

    ``ci_run`` must be completed/success on the exact HEAD (a delegated
    ``skipped`` CI is accepted only when ``delegated_skipped_ci`` is
    set). Continuum-owned gates (Contract Gate, Contract qualification)
    apply only when ``repository == "kodmial/continuum"``. Packaging
    smoke and the configurable required workflow gate follow S6/S7.
    """

    if ci_run is None:
        return False, "Current-head CI is not acceptable"
    status, conclusion = _run_gate_state(ci_run)
    if status != "completed" or (
        conclusion != "success" and not delegated_skipped_ci
    ):
        return False, "Current-head CI is not acceptable"
    if str(repository or "") == "kodmial/continuum":
        for run, gate_name in (
            (contract_gate_run, "Continuum Contract Gate"),
            (contract_qualification_run, "Contract qualification"),
        ):
            if run is None:
                return False, "{} is not successful".format(gate_name)
            gate_status, gate_conclusion = _run_gate_state(run)
            if gate_status != "completed" or gate_conclusion != "success":
                return False, "{} is not successful".format(gate_name)
    ok, reason = evaluate_packaging_gate(packaging_run)
    if not ok:
        return False, reason
    return evaluate_required_workflow_gate(
        labels=labels,
        gate_label=required_gate_label,
        gate_name=required_gate_name,
        required_run=required_gate_run,
    )


def assert_safe_main_sync_destination(
    destination: Any, default_branch: Any
) -> Tuple[bool, str]:
    """Fail-closed branch-safety check for main-sync mutations.

    Delegates to :mod:`continuum.branch_safety` (#302 canonical); only
    the PR merge operation may update the default branch.
    """

    return _branch_safety.validate_push_destination(destination, default_branch)


def conflict_decision_kwargs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Document the conflict-repair delegation (no local duplication).

    Conflict-repair decisions stay canonical in
    :func:`continuum.conflict_repair.decide_conflict_repair`; this
    helper exists only so the verification map has a stable import path
    and raises if called with unknown inputs.
    """

    if args:
        raise MergeLifecycleError("conflict decisions take keyword inputs only")
    allowed = {
        "mergeable",
        "mergeable_state",
        "has_lock",
        "active_repair_run",
        "dispatches_for_head",
        "marker_age_seconds",
        "max_dispatches_per_head",
        "dispatch_grace_seconds",
        "head_changed_since_dispatch",
    }
    unknown = sorted(set(kwargs) - allowed)
    if unknown:
        raise MergeLifecycleError(
            "unknown conflict inputs: {}".format(", ".join(unknown))
        )
    decision = _conflict_repair.decide_conflict_repair(**kwargs)
    return {
        "action": decision.action,
        "attempt": decision.attempt,
        "reason": decision.reason,
        "reconcile_lock": decision.reconcile_lock,
    }


__all__ = [
    "AUTO_MERGE_BLOCK_LABEL",
    "CONFLICT_DISPATCH_GRACE_SECONDS",
    "CONFLICT_LOCK_LABEL",
    "MAX_CONFLICT_DISPATCHES_PER_HEAD",
    "REVIEW_PROVIDERS",
    "MergeLifecycleError",
    "assert_safe_main_sync_destination",
    "build_merge_params",
    "conflict_decision_kwargs",
    "decide_main_sync",
    "evaluate_current_head_gates",
    "evaluate_packaging_gate",
    "evaluate_required_workflow_gate",
    "head_matches",
    "is_merge_blocked",
    "is_non_merge_critical_main_path",
    "is_same_head",
    "lease_key",
    "normalize_merge_title",
    "parse_post_merge_wakeups",
    "resolve_review_provider",
    "sanitize_branch",
    "sanitize_wakeup_ref",
    "validate_exact_head",
]
