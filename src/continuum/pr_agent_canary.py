"""Reusable PR-Agent canary/E2E harness for Continuum.

This is the durable regression capability for issue #174: the missing full
enabled PR-Agent -> GitHub review flow is exercised from a disposable PR
through the real command path (``/review`` plus ``/verify <finding-id>``),
the current OpenCode-backed compatibility bridge, and the supported
free/default route -- without enabling PR-Agent globally and without
changing CodeRabbit behavior.

Design rules (all fail closed, standard library only):

* Disabled by default. The harness does nothing unless
  ``CONTINUUM_PR_AGENT_CANARY_ENABLED`` is truthy. Repository-wide
  ``CONTINUUM_PR_AGENT_ENABLED`` stays ``false``; the live canary enables
  PR-Agent only for the disposable run (workflow ``enabled`` input), so
  repositories that never enable PR-Agent are unaffected.
* CodeRabbit is never touched. This module does not import, read, or write
  any CodeRabbit setting; the canary asserts the CodeRabbit status context
  is unchanged where a caller supplies it.
* Real command path only. The harness validates ``/review`` and
  ``/verify <finding-id>`` command shapes and requires live bridge
  inference evidence (bridge URL + model + OpenCode model + session
  isolation). It never synthesizes a review verdict without bridge output;
  unit tests inject deterministic bridge fixtures only to prove the
  orchestration gates, while the live runner calls the real bridge URL.
* Free/default route only. Paid-provider credentials
  (``OPENCODE_API_KEY``, ``ANTHROPIC_API_KEY``, ``GROQ_API_KEY``) must be
  absent; the requested and inference models must stay provider-neutral via
  :func:`continuum.pr_agent.assert_provider_neutral`.
* Reviewer-only. PR-Agent inference must not mutate the checkout. The
  harness requires an empty ``git status --porcelain`` snapshot and a
  stable ``HEAD`` across the reviewer step; the human/maintainer correction
  commit is a separate, explicitly recorded canary step.
* Stale HEAD, partial coverage, bridge failure, or unexpected mutation
  fails the canary instead of reporting success.

Live GitHub I/O (creating the disposable PR, posting ``/review``,
fetching review/comment IDs, dispatching the workflow) stays in the
maintainer runbook (``docs/pr-agent-canary.md``) and uses ``gh`` plus the
existing ``continuum-pr-agent.yml`` workflow. This module owns the
deterministic orchestration, gating, and evidence validation so the live
run is repeatable and the offline unit tests prove the gates without
network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import pr_agent
from .pr_agent import Coverage, Finding

CANARY_ENABLE_VAR = "CONTINUUM_PR_AGENT_CANARY_ENABLED"

# Paid-provider credentials that must never be introduced by a canary run.
# ``GROQ.KEY`` (dotted form from the issue text) cannot be an environment
# name, so both the dotted and underscore spellings are rejected.
FORBIDDEN_CANARY_ENV_VARS = (
    "OPENCODE_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "GROQ_KEY",
)
FORBIDDEN_CANARY_ENV_SUBSTRINGS = ("GROQ.KEY",)

# The free/default inference route the canary is allowed to use.
DEFAULT_OPENCODE_MODEL = "opencode/muse-spark-1.3-contributor-free"

# Real command path driven by the canary. ``/review`` proves the full
# command -> PR-Agent -> bridge/OpenCode -> GitHub output flow;
# ``/verify <finding-id>`` proves finding re-check semantics. ``/describe``
# and ``/ask`` remain supported pass-through commands but are not required
# for the canary proof.
CANARY_COMMANDS = ("/review", "/verify")
OPTIONAL_COMMANDS = ("/describe", "/ask")

# The known blocking defect the disposable canary PR introduces. The rule
# slug is stable so the finding id is stable across repeated reviews.
CANARY_DEFECT_PATH = "canary/blocking-fixture.txt"
CANARY_DEFECT_LINE = 10
CANARY_DEFECT_RULE = "missing-null-check"
CANARY_DEFECT_SEVERITY = "high"

# Evidence fields a completed live proof must record. ``require_evidence``
# fails closed when any field is missing or empty.
EVIDENCE_FIELDS = (
    "main_sha",
    "canary_pr",
    "review_command_id",
    "verify_command_id",
    "review_id",
    "workflow_run_ids",
    "bridge_evidence",
    "opencode_evidence",
    "finding_id",
    "verify_verdict",
    "final_outcome",
    "cleanup",
)

# CodeRabbit identity the canary must never disturb.
CODERABBIT_STATUS_CONTEXT = "CodeRabbit"


class CanaryError(ValueError):
    """Raised when the canary gate refuses to proceed."""


def is_canary_enabled(env: Mapping[str, str]) -> bool:
    """Whether the canary was explicitly opted in."""

    raw = env.get(CANARY_ENABLE_VAR, "")
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("1", "true", "yes")


def assert_no_paid_secret(env: Mapping[str, str]) -> None:
    """Fail closed when a paid-provider credential is present."""

    for name in FORBIDDEN_CANARY_ENV_VARS:
        if str(env.get(name, "") or "").strip():
            raise CanaryError(
                f"canary must not introduce a paid-provider secret: {name} is set"
            )
    for key, value in env.items():
        if not isinstance(value, str) or not value.strip():
            continue
        for needle in FORBIDDEN_CANARY_ENV_SUBSTRINGS:
            if needle.lower() in str(key).lower() or needle.lower() in value.lower():
                raise CanaryError(
                    f"canary must not introduce a paid-provider secret: {key} references {needle!r}"
                )
    # The generic provider path enforces the same rule on routed values.
    try:
        pr_agent.assert_provider_neutral(
            str(env.get("PR_AGENT_MODEL", "")),
            str(env.get("OPENCODE_MODEL", "")),
            str(env.get("PR_AGENT_API_BASE", "")),
        )
    except pr_agent.PrAgentError as exc:
        raise CanaryError(str(exc)) from None


def assert_free_route(request_model: str, opencode_model: str) -> None:
    """Fail closed unless the free/default route is used."""

    try:
        pr_agent.assert_provider_neutral(request_model, opencode_model)
    except pr_agent.PrAgentError as exc:
        raise CanaryError(str(exc)) from None
    effective = (opencode_model or "").strip() or DEFAULT_OPENCODE_MODEL
    if effective != DEFAULT_OPENCODE_MODEL and "free" not in effective.lower():
        raise CanaryError(
            f"canary must use the supported free/default OpenCode route, got {effective!r}"
        )


def assert_coderabbit_untouched(status_context: str = CODERABBIT_STATUS_CONTEXT) -> None:
    """Guard that the canary never re-routes CodeRabbit behavior."""

    if status_context != CODERABBIT_STATUS_CONTEXT:
        raise CanaryError(
            f"canary must not change CodeRabbit behavior: unexpected status context {status_context!r}"
        )


def parse_canary_command(body: str) -> Tuple[str, str]:
    """Parse one canary command body into (command, argument).

    Accepts the real command path only: ``/review`` with no argument and
    ``/verify <finding-id>`` with exactly one argument. Anything else
    (``/improve``, bare prose, ``/verify`` without an id) fails closed so
    the canary cannot silently exercise a different path.
    """

    text = (body or "").strip()
    if not text:
        raise CanaryError("canary command is empty")
    first_line = text.splitlines()[0].strip()
    if first_line == "/review":
        return ("/review", "")
    if first_line.startswith("/verify"):
        rest = first_line[len("/verify"):].strip()
        if not rest or len(rest.split()) != 1:
            raise CanaryError("canary verify command usage: /verify <finding-id>")
        # Reuse the engine's machine-detectable shape.
        parsed = pr_agent.parse_verify_command(first_line)
        if not parsed:
            raise CanaryError("canary verify command usage: /verify <finding-id>")
        return ("/verify", parsed)
    if first_line.split()[0] in OPTIONAL_COMMANDS:
        return (first_line.split()[0], first_line[len(first_line.split()[0]):].strip())
    raise CanaryError(f"unsupported canary command: {first_line!r}")


def blocking_review_text(
    path: str = CANARY_DEFECT_PATH,
    line: int = CANARY_DEFECT_LINE,
    rule: str = CANARY_DEFECT_RULE,
) -> str:
    """Deterministic blocking-defect review text for fixtures and seeding."""

    return (
        f"{path}:{line} [{CANARY_DEFECT_SEVERITY}] {rule} -- "
        "dereferences None when the canary config is absent\n"
    )


def clean_review_text() -> str:
    """Review text for a corrected canary HEAD (no actionable findings)."""

    return ""


def canary_finding_id(
    path: str = CANARY_DEFECT_PATH,
    line: int = CANARY_DEFECT_LINE,
    rule: str = CANARY_DEFECT_RULE,
) -> str:
    """The stable finding id the canary blocking defect must produce."""

    return pr_agent.finding_id(path, line, rule)


def assert_reviewer_clean(status_porcelain: str, before_sha: str, after_sha: str) -> None:
    """Prove the reviewer step left the checkout untouched."""

    if (status_porcelain or "").strip():
        raise CanaryError(
            "reviewer-only violation: PR-Agent modified the checkout "
            f"(git status: {(status_porcelain or '').strip()[:200]!r})"
        )
    if not before_sha or not after_sha or before_sha.strip() != after_sha.strip():
        raise CanaryError(
            "reviewer-only violation: reviewer step changed HEAD "
            f"({before_sha!r} -> {after_sha!r})"
        )


@dataclass
class CanaryPhase:
    """One review round of the blocking -> fix -> clean sequence."""

    name: str
    reviewed_sha: str
    head_sha: str
    review_text: str
    coverage: Coverage
    reviewer_status_porcelain: str = ""
    reviewer_before_sha: str = ""
    reviewer_after_sha: str = ""


@dataclass
class CanaryResult:
    outcome: str
    finding_id: str
    verify_verdict: str
    final_outcome: str
    findings: List[Finding] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "finding_id": self.finding_id,
            "verify_verdict": self.verify_verdict,
            "final_outcome": self.final_outcome,
            "findings": [finding.describe() for finding in self.findings],
            "detail": dict(self.detail),
        }


def run_canary_flow(
    blocking: CanaryPhase,
    corrected: CanaryPhase,
    clean: CanaryPhase,
    *,
    bridge_ok: bool = True,
) -> CanaryResult:
    """Orchestrate blocking -> correction -> clean verification.

    Each phase carries the exact reviewed HEAD, the current HEAD, bridge
    review text, chunk coverage, and the reviewer cleanliness snapshot.
    The sequence proves:

    1. the blocking defect yields ``CHANGES_REQUESTED`` with a stable id;
    2. correcting it yields ``RESOLVED`` for ``/verify <finding-id>``;
    3. the clean current HEAD reaches ``APPROVED`` with full coverage.

    Any stale HEAD, partial coverage, bridge failure, reviewer mutation, or
    missing blocking finding fails closed.
    """

    if not bridge_ok:
        raise CanaryError("bridge/OpenCode inference failed: failing closed")
    expected_finding = canary_finding_id()

    for phase in (blocking, corrected, clean):
        if not pr_agent.is_current_head(phase.reviewed_sha, phase.head_sha):
            raise CanaryError(
                f"stale HEAD in {phase.name}: reviewed {phase.reviewed_sha!r} "
                f"is not current {phase.head_sha!r}"
            )
        pr_agent.check_coverage(phase.coverage)
        assert_reviewer_clean(
            phase.reviewer_status_porcelain,
            phase.reviewer_before_sha or phase.reviewed_sha,
            phase.reviewer_after_sha or phase.head_sha,
        )

    blocking_findings = pr_agent.parse_findings(blocking.review_text)
    if expected_finding not in {finding.id for finding in blocking_findings}:
        raise CanaryError(
            f"canary blocking defect not reported: expected finding {expected_finding!r}"
        )
    blocking_report = pr_agent.review_report(
        blocking_findings, blocking.reviewed_sha, blocking.head_sha, blocking.coverage
    )
    if blocking_report["outcome"] != "CHANGES_REQUESTED":
        raise CanaryError(
            f"blocking phase must request changes, got {blocking_report['outcome']!r}"
        )

    corrected_findings = pr_agent.parse_findings(corrected.review_text)
    verdict = pr_agent.verify_finding(expected_finding, blocking_findings, corrected_findings)
    if verdict != pr_agent.VERIFY_RESOLVED:
        raise CanaryError(
            f"post-fix verification must resolve {expected_finding!r}, got {verdict!r}"
        )

    clean_findings = pr_agent.parse_findings(clean.review_text)
    blocking_left = [
        finding
        for finding in clean_findings
        if finding.severity.lower() in pr_agent.BLOCKING_SEVERITIES
        and finding.status == "open"
    ]
    if blocking_left:
        raise CanaryError(
            "clean HEAD still carries blocking findings: refusing approval"
        )
    clean_report = pr_agent.review_report(
        clean_findings, clean.reviewed_sha, clean.head_sha, clean.coverage
    )
    if clean_report["outcome"] != "APPROVED":
        raise CanaryError(
            f"clean current HEAD must reach APPROVED, got {clean_report['outcome']!r}"
        )
    if not pr_agent.is_current_head(clean.reviewed_sha, clean.head_sha):
        raise CanaryError("clean HEAD is stale: refusing approval")

    return CanaryResult(
        outcome="CANARY_PASS",
        finding_id=expected_finding,
        verify_verdict=verdict,
        final_outcome=clean_report["outcome"],
        findings=clean_findings,
        detail={
            "blocking_outcome": blocking_report["outcome"],
            "verify": verdict,
            "coverage": {"reviewed": clean.coverage.reviewed, "total": clean.coverage.total},
        },
    )


def disabled_canary_report(reason: str = "PR-Agent canary is not enabled") -> Dict[str, Any]:
    """Explicit disabled-path outcome for non-opted-in repositories."""

    return {"outcome": "DISABLED", "reason": reason, "findings": []}


def build_evidence(
    *,
    main_sha: str,
    canary_pr: str,
    review_command_id: str,
    verify_command_id: str,
    review_id: str,
    workflow_run_ids: Sequence[str],
    bridge_evidence: str,
    opencode_evidence: str,
    finding_id: str,
    verify_verdict: str,
    final_outcome: str,
    cleanup: str,
) -> Dict[str, Any]:
    """Assemble the live-proof evidence bundle (all fields required)."""

    return {
        "main_sha": (main_sha or "").strip(),
        "canary_pr": (canary_pr or "").strip(),
        "review_command_id": (review_command_id or "").strip(),
        "verify_command_id": (verify_command_id or "").strip(),
        "review_id": (review_id or "").strip(),
        "workflow_run_ids": list(workflow_run_ids or []),
        "bridge_evidence": (bridge_evidence or "").strip(),
        "opencode_evidence": (opencode_evidence or "").strip(),
        "finding_id": (finding_id or "").strip(),
        "verify_verdict": (verify_verdict or "").strip(),
        "final_outcome": (final_outcome or "").strip(),
        "cleanup": (cleanup or "").strip(),
    }


def require_evidence(evidence: Mapping[str, Any]) -> Dict[str, Any]:
    """Fail closed when any live-proof evidence field is missing."""

    missing = [
        name
        for name in EVIDENCE_FIELDS
        if not evidence.get(name)
    ]
    if missing:
        raise CanaryError(
            "incomplete canary evidence: missing " + ", ".join(sorted(missing))
        )
    return dict(evidence)


def cleanup_checklist() -> List[str]:
    """Disposable artifacts the live run must remove afterwards."""

    return [
        "close the disposable canary PR",
        "delete the disposable canary branch",
        "remove canary-only labels/comments on the disposable PR",
        "unset any canary-scoped workflow input override (repo variable stays false)",
        "verify no paid-provider secret was added",
        "verify the working tree contains only the reusable harness and its tests",
    ]


def self_check() -> CanaryResult:
    """Offline deterministic proof of the harness gates (no network)."""

    coverage = Coverage(reviewed=1, total=1)
    blocking = CanaryPhase(
        name="blocking",
        reviewed_sha="sha-blocking",
        head_sha="sha-blocking",
        review_text=blocking_review_text(),
        coverage=coverage,
        reviewer_before_sha="sha-blocking",
        reviewer_after_sha="sha-blocking",
    )
    corrected = CanaryPhase(
        name="corrected",
        reviewed_sha="sha-fixed",
        head_sha="sha-fixed",
        review_text=clean_review_text(),
        coverage=coverage,
        reviewer_before_sha="sha-fixed",
        reviewer_after_sha="sha-fixed",
    )
    clean = CanaryPhase(
        name="clean",
        reviewed_sha="sha-clean",
        head_sha="sha-clean",
        review_text=clean_review_text(),
        coverage=coverage,
        reviewer_before_sha="sha-clean",
        reviewer_after_sha="sha-clean",
    )
    return run_canary_flow(blocking, corrected, clean, bridge_ok=True)


def resolve_live_config(env: Mapping[str, Any]) -> Dict[str, Any]:
    """Resolve and gate the live canary configuration.

    Fails closed unless the canary is explicitly opted in, no paid secret
    is present, the free route is selected, and the command path is real.
    """

    if not is_canary_enabled(env):  # type: ignore[arg-type]
        return disabled_canary_report()
    assert_no_paid_secret(env)  # type: ignore[arg-type]
    request_model = str(env.get("PR_AGENT_MODEL", "") or "").strip() or pr_agent.DEFAULT_MODEL
    opencode_model = (
        str(env.get("OPENCODE_MODEL", "") or "").strip() or DEFAULT_OPENCODE_MODEL
    )
    assert_free_route(request_model, opencode_model)
    assert_coderabbit_untouched(str(env.get("CODERABBIT_STATUS_CONTEXT", CODERABBIT_STATUS_CONTEXT)))
    runtime = pr_agent.resolve_runtime(env)  # type: ignore[arg-type]
    if not runtime.enabled and str(env.get("PR_AGENT_ENABLED_INPUT", "") or "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        raise CanaryError(
            "live canary requires the disposable enabled path "
            "(workflow `enabled` input); the repository default stays disabled"
        )
    return {
        "outcome": "READY",
        "runtime": runtime.describe(),
        "request_model": request_model,
        "opencode_model": opencode_model,
    }


__all__ = [
    "CANARY_ENABLE_VAR",
    "CANARY_COMMANDS",
    "DEFAULT_OPENCODE_MODEL",
    "EVIDENCE_FIELDS",
    "CanaryError",
    "CanaryPhase",
    "CanaryResult",
    "assert_coderabbit_untouched",
    "assert_free_route",
    "assert_no_paid_secret",
    "assert_reviewer_clean",
    "blocking_review_text",
    "build_evidence",
    "canary_finding_id",
    "clean_review_text",
    "cleanup_checklist",
    "disabled_canary_report",
    "is_canary_enabled",
    "parse_canary_command",
    "require_evidence",
    "resolve_live_config",
    "run_canary_flow",
    "self_check",
]
