"""OpenCode 429 runner-death recovery contract (kodmial/continuum#290).

An OpenCode/model HTTP 429 (``FreeUsageLimitError``) is an
infrastructure/runner lifecycle failure, not a task failure. Once the model
returns 429 on a VM, Continuum must never invoke the model again on that VM
and must never turn the incident into ``automation:paused``.

Lifecycle implemented here (pure decisions; workflows own Git/API state)::

    agent 429 -> burn current VM (no second model call on that runner)
             -> workflow-owned evacuation to a durable checkpoint ref
             -> old run terminates (exit 75)
             -> watchdog dispatches a NEW workflow_dispatch with an explicit
                recovery identity (operation id + checkpoint ref/SHA +
                stage + generation) onto a fresh VM
             -> new run restores the exact checkpoint and continues from the
                last completed stage without repeating completed work

Rules enforced by this module:

* 429 classification covers every agent mode (issue implementation,
  CodeRabbit repair, conflict repair, CI repair, qualification, and future
  agent modes) through one shared classifier; ``400/401/plain 403/404/422``
  and deterministic configuration/auth errors stay fail-closed and are never
  classified as runner death; transient rate-limit ``403`` handling stays
  separate from model 429 runner death.
* A burned runner never invokes the model again (wrapper fast-path).
* 429 runner replacement never consumes task-failure budgets (implementation
  retries, dispatch-attempt budgets, review-repair budgets, qualification
  failure counters).
* Exhausted fast replacements never apply ``automation:paused`` and never
  require manual ``/oc``: the operation enters bounded infrastructure
  backoff and the scheduler/cron resumes it automatically with the same
  checkpoint/operation identity.
* Resumptions deduplicate by stable operation identity so concurrent
  watchdog runs cannot create competing resumptions.
* Public Parent logs keep Child identities opaque (numbers only, never
  repository names).

Standard library only.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Mapping, Optional, Tuple

#: Dedicated infrastructure outcome used when a runner is burned by 429.
#: The existing exit 75 is retained.
RUNNER_DEATH_EXIT_CODE = 75

#: Marker emitted into logs when 429 runner death is classified. The
#: watchdog keys its fresh-VM recovery off this exact string.
RESTART_REQUIRED_MARKER = "CONTINUUM_OPENCODE_429_RESTART_REQUIRED"

#: Marker emitted when a publish race needs a same-input retry. This path
#: is intentionally separate from 429 runner death.
PUBLISH_RACE_MARKER = "CONTINUUM_OPENCODE_PUBLISH_RACE_RETRY_REQUIRED"

#: Lifecycle stages tracked on the checkpoint. Ordered from least to most
#: complete; a fresh run resumes at the first incomplete stage and never
#: repeats a completed one.
STAGES = (
    "implementation-incomplete",
    "implementation-complete",
    "tests-running",
    "tests-passed",
    "publish-pr",
    "review-repair",
    "qualification",
)

#: Stages at which exact-SHA test/qualification evidence already exists, so
#: a fresh run must NOT call OpenCode again merely to "finish": it proceeds
#: directly to workflow-owned commit/push/PR publication.
SKIP_AGENT_STAGES = frozenset({"tests-passed", "publish-pr"})

#: Agent modes covered by the shared 429 classifier. ``future`` is the
#: forward-compatibility bucket for agent modes added later: unknown modes
#: still burn the runner on 429 instead of falling through to task failure.
AGENT_MODES = frozenset(
    {
        "issue",
        "coderabbit-fix",
        "resolve-conflict",
        "ci-fix",
        "qualification",
        "future",
    }
)

#: Fail-closed HTTP statuses: deterministic configuration/auth errors that
#: must never be classified as 429 runner death.
FAIL_CLOSED_STATUSES = frozenset({400, 401, 403, 404, 422})

#: Bounded infrastructure backoff between fresh-VM 429 generations
#: (seconds). Backoff can grow, but the task remains automatically
#: recoverable: exhaustion enters cooldown, never ``automation:paused``.
INFRA_BACKOFF_SCHEDULE = (60, 300, 900, 1800, 3600)

#: Fast runner replacements tolerated before entering infrastructure
#: cooldown. Separate from every task-failure budget.
DEFAULT_MAX_429_RUNNER_RESTARTS = 3

#: Prefix for workflow-owned durable checkpoint refs.
CHECKPOINT_REF_PREFIX = "opencode/429-checkpoint-"

_FREE_USAGE_RE = re.compile(r"FreeUsageLimitError", re.IGNORECASE)
_429_SIGNAL_RE = re.compile(
    r"APIError.*429"
    r"|HTTP[^0-9]*429([^0-9]|$)"
    r"|429[ \t]+Too[ \t]+Many[ \t]+Requests"
    r"|statusCode[^0-9]*429([^0-9]|$)",
    re.IGNORECASE,
)
_FAIL_CLOSED_RE = re.compile(
    r"\b(400|401|403|404|422)\b",
    re.IGNORECASE,
)
_CHILD_REPO_RE = re.compile(
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
)
_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)


def is_opencode_429(log_text: Any, exit_code: Any = 1) -> bool:
    """Whether an OpenCode invocation failed with model 429 runner death.

    ``FreeUsageLimitError`` always classifies (even on exit 0, because the
    provider refused usage). Any other 429 signal classifies only when the
    invocation actually failed (non-zero exit), so a passing run whose log
    merely mentions 429 in prose is never burned. Deterministic
    ``400/401/403/404/422`` failures without a 429 signal never classify.
    """

    text = log_text if isinstance(log_text, str) else ""
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        code = 1
    if _FREE_USAGE_RE.search(text):
        return True
    if code == 0:
        return False
    return bool(_429_SIGNAL_RE.search(text))


def classify_agent_error(log_text: Any, exit_code: Any = 1) -> str:
    """Classify a finished agent invocation.

    Returns ``"runner-death-429"`` for model 429, ``"fail-closed"`` for
    deterministic configuration/auth errors, and ``"task-failure"`` for
    every normal non-429 agent failure (which must never be misclassified
    as runner replacement).
    """

    if is_opencode_429(log_text, exit_code):
        return "runner-death-429"
    text = log_text if isinstance(log_text, str) else ""
    if _FAIL_CLOSED_RE.search(text):
        return "fail-closed"
    return "task-failure"


def is_valid_stage(stage: Any) -> bool:
    """Whether a lifecycle stage name is known."""

    return isinstance(stage, str) and stage in STAGES


def stage_index(stage: Any) -> int:
    """Position of a stage in lifecycle order; unknown stages sort first."""

    try:
        return STAGES.index(stage)
    except ValueError:
        return -1


def should_skip_opencode(stage: Any, exact_sha_evidence_complete: bool = False) -> bool:
    """Whether a fresh run must skip OpenCode and publish directly.

    True only when the checkpoint stage already carries exact-SHA
    test/qualification evidence (``tests-passed`` or ``publish-pr``). Every
    other stage resumes the agent on the new VM.
    """

    return stage in SKIP_AGENT_STAGES and bool(exact_sha_evidence_complete)


def resume_stage(stage: Any) -> str:
    """Normalize a checkpoint stage into the stage a fresh run continues at."""

    if is_valid_stage(stage):
        return str(stage)
    return "implementation-incomplete"


def _safe_token(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9-]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "unknown"


def operation_id(
    mode: Any,
    issue_number: Any = "",
    pr_number: Any = "",
    review_id: Any = "",
    run_id: Any = "",
    qualification_number: Any = "",
    required_sha: Any = "",
) -> str:
    """Stable operation identity for one automatable unit of work.

    The identity is ``repo-agnostic`` on purpose: it carries numbers and
    SHAs only, never repository names, so public Parent logs stay opaque
    about private Child repositories. Concurrent watchdog runs computing
    this for the same operation always agree, which is what makes
    deduplication possible.
    """

    mode_text = _safe_token(mode) or "unknown"
    if mode_text not in AGENT_MODES and mode_text != "unknown":
        mode_text = "future"
    parts = ["opencode-429", mode_text]
    issue = str(issue_number or "").strip()
    pr = str(pr_number or "").strip()
    review = str(review_id or "").strip()
    run = str(run_id or "").strip()
    qual = str(qualification_number or "").strip()
    sha = str(required_sha or "").strip().lower()
    if issue.isdigit():
        parts.append("issue{}".format(issue))
    if pr.isdigit():
        parts.append("pr{}".format(pr))
    if review.isdigit():
        parts.append("review{}".format(review))
    if run.isdigit():
        parts.append("run{}".format(run))
    if qual.isdigit():
        parts.append("qual{}".format(qual))
    if _SHA_RE.fullmatch(sha or ""):
        parts.append("sha{}".format(sha[:12]))
    if len(parts) == 2:
        parts.append("unscoped")
    return "-".join(parts)


def checkpoint_ref(operation: Any, generation: Any = 0) -> str:
    """Durable workflow-owned checkpoint ref for one operation+generation."""

    op = _safe_token(operation) or "unknown"
    try:
        gen = int(generation)
    except (TypeError, ValueError):
        gen = 0
    if gen < 0:
        gen = 0
    return "{}{}-gen-{}".format(CHECKPOINT_REF_PREFIX, op, gen)


def parse_checkpoint_ref(ref: Any) -> Optional[Dict[str, Any]]:
    """Parse a checkpoint ref back into operation+generation, or None."""

    text = str(ref or "").strip()
    if not text.startswith(CHECKPOINT_REF_PREFIX):
        return None
    rest = text[len(CHECKPOINT_REF_PREFIX):]
    if "-gen-" not in rest:
        return None
    operation, _, generation = rest.rpartition("-gen-")
    if not operation or not generation.isdigit():
        return None
    return {"operation_id": operation, "generation": int(generation)}


def next_generation(generation: Any) -> int:
    """Next recovery generation for a fresh-VM resumption."""

    try:
        gen = int(generation)
    except (TypeError, ValueError):
        return 1
    return max(0, gen) + 1


def resumption_dedupe_key(
    operation: Any, generation: Any, checkpoint_sha: Any = ""
) -> str:
    """Key serializing concurrent resumptions for one operation+generation."""

    op = _safe_token(operation) or "unknown"
    try:
        gen = int(generation)
    except (TypeError, ValueError):
        gen = 0
    sha = str(checkpoint_sha or "").strip().lower()
    if _SHA_RE.fullmatch(sha):
        return "{}:gen-{}:sha-{}".format(op, max(0, gen), sha[:12])
    return "{}:gen-{}".format(op, max(0, gen))


def resumption_marker(operation: Any, generation: Any) -> str:
    """Durable comment marker recording one dispatched resumption."""

    try:
        gen = int(generation)
    except (TypeError, ValueError):
        gen = 0
    return "<!-- continuum-429-resumption op={} gen={} -->".format(
        _safe_token(operation) or "unknown", max(0, gen)
    )


def already_resumed(comments_body: Any, operation: Any, generation: Any) -> bool:
    """Whether the resumption marker for this operation+generation exists."""

    if not isinstance(comments_body, str):
        return False
    return resumption_marker(operation, generation) in comments_body


def consumes_task_budget(is_429: bool) -> bool:
    """Whether the incident consumes normal task/review/qualification budgets.

    429 runner replacement never does: it is infrastructure, not a task
    failure. Normal failures do.
    """

    return not bool(is_429)


def infra_backoff_seconds(generation: Any) -> int:
    """Bounded infrastructure backoff before the next fresh-VM attempt."""

    try:
        gen = int(generation)
    except (TypeError, ValueError):
        gen = 0
    if gen <= 0:
        return 0
    index = min(max(0, gen) - 1, len(INFRA_BACKOFF_SCHEDULE) - 1)
    return INFRA_BACKOFF_SCHEDULE[index]


def should_enter_infra_cooldown(generation: Any, max_restarts: Any = None) -> bool:
    """Whether repeated fresh-VM 429s move the operation into cooldown."""

    try:
        gen = int(generation)
    except (TypeError, ValueError):
        return False
    try:
        limit = int(
            DEFAULT_MAX_429_RUNNER_RESTARTS
            if max_restarts in (None, "")
            else max_restarts
        )
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_429_RUNNER_RESTARTS
    return gen > max(0, limit)


def cooldown_comment(operation: Any, generation: Any, checkpoint: Any) -> str:
    """Bounded infrastructure-backoff notice. Never pauses, never needs /oc."""

    wait = infra_backoff_seconds(generation)
    return "\n".join(
        [
            resumption_marker(operation, generation),
            "⚡ **Continuum · opencode 429 infrastructure backoff**",
            "<!-- continuum-origin role=continuum component=opencode-429 -->",
            (
                "Repeated model 429s on fresh runners (generation {}). "
                "This is infrastructure exhaustion, not a task failure: "
                "no task/review/qualification budget was consumed and "
                "`automation:paused` was not applied.".format(generation)
            ),
            "",
            "Checkpoint `{}` is preserved. The scheduler resumes this "
            "operation automatically after ~{}s with the same "
            "checkpoint/operation identity; no manual `/oc` is required.".format(
                checkpoint, wait
            ),
        ]
    )


def recovery_dispatch_inputs(
    operation: Any,
    checkpoint: Any,
    checkpoint_sha: Any = "",
    stage: Any = "implementation-incomplete",
    generation: Any = 0,
    mode: Any = "",
    issue_number: Any = "",
    pr_number: Any = "",
    head_ref: Any = "",
    review_id: Any = "",
    run_id: Any = "",
    base_ref: Any = "",
    capability_number: Any = "",
    qualification_number: Any = "",
    required_sha: Any = "",
) -> Dict[str, str]:
    """Explicit recovery identity for a NEW workflow_dispatch on a fresh VM."""

    return {
        "mode": str(mode or ""),
        "issue_number": str(issue_number or ""),
        "pr_number": str(pr_number or ""),
        "head_ref": str(head_ref or ""),
        "review_id": str(review_id or ""),
        "run_id": str(run_id or ""),
        "base_ref": str(base_ref or ""),
        "capability_number": str(capability_number or ""),
        "qualification_number": str(qualification_number or ""),
        "required_sha": str(required_sha or ""),
        "recovery_operation_id": str(operation or ""),
        "recovery_checkpoint_ref": str(checkpoint or ""),
        "recovery_checkpoint_sha": str(checkpoint_sha or ""),
        "recovery_stage": resume_stage(stage),
        "recovery_generation": str(generation),
    }


#: The five explicit recovery fields carried through the caller stub's
#: single ``recovery_identity`` workflow_dispatch input. A caller stub
#: workflow_dispatch accepts at most 25 inputs, so the stub cannot declare
#: one input per recovery field on top of its existing dispatch surface;
#: the watchdog therefore packs exactly these fields (as produced by
#: :func:`recovery_dispatch_inputs`) into one JSON string, and the stub
#: unpacks them back into the five workflow_call inputs of the reusable
#: workflow. The JSON always carries every field explicitly: no stage,
#: SHA, or generation is ever elided.
RECOVERY_IDENTITY_FIELDS = (
    "recovery_operation_id",
    "recovery_checkpoint_ref",
    "recovery_checkpoint_sha",
    "recovery_stage",
    "recovery_generation",
)


def pack_recovery_identity(inputs: Mapping[str, Any]) -> str:
    """Pack the explicit recovery identity into one dispatch input string.

    Returns compact JSON carrying exactly :data:`RECOVERY_IDENTITY_FIELDS`
    (missing fields become empty strings), or ``""`` when no recovery was
    requested (no operation identity and no checkpoint ref).
    """

    if not isinstance(inputs, Mapping):
        return ""
    packed = {field: str(inputs.get(field) or "") for field in RECOVERY_IDENTITY_FIELDS}
    if not packed["recovery_operation_id"] and not packed["recovery_checkpoint_ref"]:
        return ""
    return json.dumps(packed, sort_keys=True, separators=(",", ":"))


def unpack_recovery_identity(payload: Any) -> Dict[str, str]:
    """Unpack one dispatch ``recovery_identity`` string into recovery fields.

    Returns the five :data:`RECOVERY_IDENTITY_FIELDS` (absent fields become
    empty strings; an empty payload means no recovery was requested).
    Raises :class:`ValueError` on malformed JSON or a non-object payload so
    callers fail closed instead of resuming from a truncated identity.
    """

    if payload is None or (isinstance(payload, str) and not payload.strip()):
        return {field: "" for field in RECOVERY_IDENTITY_FIELDS}
    if not isinstance(payload, str):
        raise ValueError("recovery identity must be a JSON string")
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("recovery identity is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("recovery identity must be a JSON object")
    return {
        field: str(parsed.get(field) or "") for field in RECOVERY_IDENTITY_FIELDS
    }


def redact_child_identity(text: Any) -> str:
    """Redact ``owner/repo`` identities so Parent logs stay opaque.

    SHA-like tokens, issue/PR numbers, and operation ids pass through
    unchanged; anything shaped like a repository name becomes
    ``<redacted-repo>``.
    """

    if not isinstance(text, str):
        return ""
    redacted = _CHILD_REPO_RE.sub("<redacted-repo>", text)
    return redacted


def validate_recovery_inputs(inputs: Mapping[str, Any]) -> Tuple[bool, str]:
    """Validate a fresh run's recovery identity. Returns (ok, reason)."""

    if not isinstance(inputs, Mapping):
        return False, "recovery inputs are not a mapping"
    operation = str(inputs.get("recovery_operation_id") or "").strip()
    checkpoint = str(inputs.get("recovery_checkpoint_ref") or "").strip()
    if not operation and not checkpoint:
        return True, "no recovery requested"
    if not operation:
        return False, "recovery checkpoint without operation identity"
    if not checkpoint:
        return False, "recovery operation without checkpoint ref"
    parsed = parse_checkpoint_ref(checkpoint)
    if parsed is None:
        return False, "recovery checkpoint ref is malformed"
    stage = inputs.get("recovery_stage") or "implementation-incomplete"
    if not is_valid_stage(stage):
        return False, "recovery stage is unknown"
    try:
        generation = int(inputs.get("recovery_generation") or 0)
    except (TypeError, ValueError):
        return False, "recovery generation is not an integer"
    if generation < 0:
        return False, "recovery generation is negative"
    if _safe_token(operation) != parsed["operation_id"]:
        return False, "recovery operation does not own the checkpoint ref"
    if parsed["generation"] != generation:
        return False, "recovery generation does not match the checkpoint ref"
    sha = str(inputs.get("recovery_checkpoint_sha") or "").strip()
    if sha and not _SHA_RE.fullmatch(sha):
        return False, "recovery checkpoint SHA is malformed"
    return True, "recovery identity is valid"


__all__ = [
    "AGENT_MODES",
    "CHECKPOINT_REF_PREFIX",
    "DEFAULT_MAX_429_RUNNER_RESTARTS",
    "FAIL_CLOSED_STATUSES",
    "INFRA_BACKOFF_SCHEDULE",
    "PUBLISH_RACE_MARKER",
    "RESTART_REQUIRED_MARKER",
    "RECOVERY_IDENTITY_FIELDS",
    "RUNNER_DEATH_EXIT_CODE",
    "SKIP_AGENT_STAGES",
    "STAGES",
    "already_resumed",
    "checkpoint_ref",
    "classify_agent_error",
    "consumes_task_budget",
    "cooldown_comment",
    "infra_backoff_seconds",
    "is_opencode_429",
    "is_valid_stage",
    "next_generation",
    "operation_id",
    "parse_checkpoint_ref",
    "pack_recovery_identity",
    "recovery_dispatch_inputs",
    "redact_child_identity",
    "resume_stage",
    "resumption_dedupe_key",
    "resumption_marker",
    "should_enter_infra_cooldown",
    "should_skip_opencode",
    "stage_index",
    "unpack_recovery_identity",
    "validate_recovery_inputs",
]
