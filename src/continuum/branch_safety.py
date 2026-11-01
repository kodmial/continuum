"""Canonical fail-closed branch safety for kodmial/continuum#302.

Every code-producing mutation must follow::

    task/recovery -> non-default work branch -> PR -> exact-HEAD gates -> merge

Only the PR merge operation may update the repository default branch,
and only after exact-HEAD gates pass. Metadata writes (labels, comments,
statuses, dispatches) are not code writes and are outside this
prohibition.

This module is the single decision procedure for that invariant. It
performs no I/O: workflows discover the actual repository default
branch at runtime (``gh api repos/{repo} --jq .default_branch`` or the
equivalent REST field), pass the discovered value here, and fail closed
when discovery yields nothing. The default branch is never hardcoded
here and no caller may supply a fallback: an undiscoverable default
refuses the mutation.

Rules enforced here (mirrored by the ``continuum_assert_safe_push``
shell guard embedded in every code-publishing workflow):

* empty destinations fail closed;
* ambiguous destinations (``HEAD``, ``@``, revision syntax such as
  ``..``/``~``/``^``/``?``/``*``/``[``/``@{``, whitespace, leading
  ``-``/``/``, trailing ``/``, ``//``, ``*.lock``) fail closed;
* any destination equal to the discovered default branch fails closed,
  including the ``refs/heads/<default>`` spelling;
* repair validates the destination against the exact current PR head
  when the caller knows it (``expected_head``); a mismatch fails
  closed, and a head that is itself the default branch fails closed;
* workflow-owned checkpoint/internal refs
  (``opencode/429-checkpoint-*``, ``opencode/checkpoint-*``) are
  permitted only as explicitly non-default refs; there is no path that
  permits a checkpoint ref to alias the default branch;
* there is no bypass: no flag, environment variable, or override flips
  a rejection into permission.

Standard library only.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

#: Workflow-owned checkpoint/internal ref prefixes. These refs are
#: permitted destinations only when they are proven non-default below;
#: the prefix alone never authorizes a push to the default branch.
CHECKPOINT_REF_PREFIXES = (
    "opencode/429-checkpoint-",
    "opencode/checkpoint-",
)

#: Exact ambiguous symbolic refs that must never be a push destination.
_AMBIGUOUS_LITERAL_REFS = frozenset(
    {
        "HEAD",
        "@",
        "FETCH_HEAD",
        "ORIG_HEAD",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_HEAD",
    }
)

_REF_HEADS_PREFIX = "refs/heads/"


class BranchSafetyError(ValueError):
    """Raised when a branch identity cannot be interpreted safely."""


def _strip_heads_prefix(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith(_REF_HEADS_PREFIX):
        text = text[len(_REF_HEADS_PREFIX):].strip()
    return text


def normalize_branch_name(ref: Any) -> str:
    """Normalize a branch ref to its short name.

    Strips surrounding whitespace and one ``refs/heads/`` prefix.
    Raises :class:`BranchSafetyError` for empty input (including a bare
    ``refs/heads/`` with no branch remainder).
    """

    text = _strip_heads_prefix(ref if isinstance(ref, str) else str(ref or ""))
    if not text:
        raise BranchSafetyError("branch destination is empty")
    return text


def is_ambiguous_ref(ref: Any) -> bool:
    """Whether a destination ref is empty or ambiguous (unpushable)."""

    if ref is None:
        return True
    text = _strip_heads_prefix(ref if isinstance(ref, str) else str(ref or ""))
    if not text or not text.strip():
        return True
    if text != text.strip():
        return True
    if text in _AMBIGUOUS_LITERAL_REFS:
        return True
    if "@{" in text:
        return True
    for token in ("..", "~", "^", "?", "*", "[", " ", "\t", "\n", "\r", "\\"):
        if token in text:
            return True
    if text.startswith("-") or text.startswith("/"):
        return True
    if text.endswith("/") or text.endswith(".lock"):
        return True
    if "//" in text:
        return True
    if text.startswith(".") or text.endswith("."):
        return True
    if text != text.strip():
        return True
    return False


def resolve_default_branch(value: Any) -> str:
    """Resolve a runtime-discovered default branch, failing closed.

    ``value`` must be the repository's actual default branch as read
    from repository metadata (for example the ``default_branch`` REST
    field). Empty or ambiguous values raise :class:`BranchSafetyError`;
    there is deliberately no hardcoded fallback.
    """

    if value is None:
        raise BranchSafetyError("default branch is undiscoverable")
    text = _strip_heads_prefix(value if isinstance(value, str) else str(value or ""))
    if not text:
        raise BranchSafetyError("default branch is undiscoverable")
    if is_ambiguous_ref(text):
        raise BranchSafetyError("default branch is ambiguous: {!r}".format(value))
    return text


def is_checkpoint_ref(ref: Any) -> bool:
    """Whether a ref is a workflow-owned checkpoint/internal ref."""

    try:
        text = normalize_branch_name(ref)
    except BranchSafetyError:
        return False
    if is_ambiguous_ref(text):
        return False
    remainder_ok = any(
        text.startswith(prefix) and len(text) > len(prefix)
        for prefix in CHECKPOINT_REF_PREFIXES
    )
    return remainder_ok


def checkpoint_ref(operation: Any, generation: Any = 0) -> str:
    """Build a durable workflow-owned checkpoint ref (never the default)."""

    import re as _re

    text = _re.sub(r"[^A-Za-z0-9-]+", "-", str(operation or "").strip().lower())
    text = _re.sub(r"-{2,}", "-", text).strip("-") or "unknown"
    try:
        gen = int(generation)
    except (TypeError, ValueError):
        gen = 0
    return "{}opencode/429-checkpoint-{}-gen-{}".format(
        "", text, max(0, gen))


def validate_push_destination(
    destination: Any,
    default_branch: Any,
    expected_head: Optional[Any] = None,
) -> Tuple[bool, str]:
    """Validate a code-push destination. Returns ``(ok, reason)``.

    ``default_branch`` must be the runtime-discovered default; an
    undiscoverable default fails closed. When ``expected_head`` is
    supplied the destination must equal that exact PR head (repair
    paths); any mismatch fails closed.
    """

    try:
        dest = normalize_branch_name(destination)
    except BranchSafetyError:
        return False, "push destination is empty"
    if is_ambiguous_ref(dest):
        return False, "push destination is ambiguous: {!r}".format(destination)
    try:
        default = resolve_default_branch(default_branch)
    except BranchSafetyError:
        return False, "default branch is undiscoverable; refusing push"
    if dest == default:
        return False, "push to the default branch is prohibited: {!r}".format(dest)
    if expected_head is not None:
        try:
            expected = normalize_branch_name(expected_head)
        except BranchSafetyError:
            return False, "expected PR head is empty; refusing push"
        if is_ambiguous_ref(expected):
            return False, "expected PR head is ambiguous; refusing push"
        if expected == default:
            return False, "expected PR head is the default branch; refusing push"
        if dest != expected:
            return (
                False,
                "push destination {!r} does not match the exact PR head "
                "{!r}; refusing push".format(dest, expected),
            )
    return True, "push destination is a proven non-default branch"


def validate_new_branch(branch: Any, default_branch: Any) -> Tuple[bool, str]:
    """Validate a newly created task/work branch name."""

    return validate_push_destination(branch, default_branch)


def validate_checkpoint_push(ref: Any, default_branch: Any) -> Tuple[bool, str]:
    """Validate a checkpoint/internal ref push (must stay off default)."""

    if not is_checkpoint_ref(ref):
        raise BranchSafetyError(
            "checkpoint ref must start with one of: {}".format(
                ", ".join(CHECKPOINT_REF_PREFIXES)))
    ok, reason = validate_push_destination(ref, default_branch)
    if not ok:
        raise BranchSafetyError(reason)
    return ok, reason


__all__ = [
    "CHECKPOINT_REF_PREFIXES",
    "BranchSafetyError",
    "checkpoint_ref",
    "is_ambiguous_ref",
    "is_checkpoint_ref",
    "normalize_branch_name",
    "resolve_default_branch",
    "validate_checkpoint_push",
    "validate_new_branch",
    "validate_push_destination",
]
