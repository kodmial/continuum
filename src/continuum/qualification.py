"""Mandatory live-qualification lifecycle gate.

An implementation merge may mark implementation as complete, but it must never
mark the *capability* complete while any mandatory qualification remains
pending, paused, failed, or lacks exact-revision evidence. This module owns the
deterministic rules behind that gate so every Continuum controller (the issue
scheduler, the OpenCode PR publisher, and the qualification runners) parses the
same relationship and reaches the same verdict.

Machine-readable relationship
-----------------------------
A capability issue declares its mandatory qualifications with a hidden HTML
marker in its body, never with prose::

    <!-- automation-qualification: #184 -->

Several qualifications may be listed::

    <!-- automation-qualification: #184, #185 -->

The marker is parsed with a single regular expression
(:data:`QUALIFICATION_DECLARATION_RE`). Prose that merely mentions an issue
number never qualifies.

Lifecycle states
----------------
Implementation completion and capability completion are distinct states,
carried by labels:

* ``automation:in-progress`` — implementation reserved or running;
* ``automation:qualifying`` — implementation merged, qualification pending;
* ``automation:blocked`` — qualification failed, repair required;
* closed — capability proven complete (only with exact-SHA pass evidence).

Evidence
--------
A qualification run must publish machine-readable evidence on the
*qualification* issue that identifies the tested revision and the verdict::

    <!-- continuum-qualification-result issue=184 sha=<40-hex> result=pass -->

``sha=`` accepts the alias ``head=``; ``result=`` accepts the aliases
``classification=`` and ``verdict=`` with the values ``pass``/``fail`` (the
Docker controller's ``pass`` classification and any other value meaning
``fail``). Docker-controller payloads are also accepted: a
``<!-- continuum-docker-qualification-result -->`` marker followed by a JSON
document whose ``head_sha`` equals the required SHA and whose
``classification`` equals ``pass`` counts as a pass for that SHA. Evidence
for any other SHA is stale and never satisfies the gate.

Merge bookkeeping
-----------------
When the implementation PR merges, the controller records the exact merged
``main`` SHA on the capability issue::

    <!-- continuum-qualification-required capability=182 sha=<40-hex> -->

and dispatches each qualification at most once per SHA::

    <!-- continuum-qualification-dispatch capability=182 qualification=184 sha=<40-hex> -->

After a repair merges, ``main`` advances, the required SHA changes, stale
evidence no longer matches, and the gate reruns the qualification.

Closure safety
--------------
A pull-request body that carries a GitHub auto-close keyword (``Fixes #N``,
``Closes #N``, ``Resolves #N``, ...) for a capability with mandatory
qualification would bypass the gate. :func:`contains_closing_keyword` detects
that shape so the publisher can use a non-closing body, and
:func:`capability_status` fails closed: a closed capability whose
qualifications are not all satisfied for the required SHA reports
``must-reopen``, never ``complete``.

Standard library only.
"""

from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional, Tuple

#: Marker a capability issue carries to declare mandatory qualifications.
QUALIFICATION_DECLARATION_RE = re.compile(
    r"<!--\s*automation-qualification\s*:\s*([0-9#\s,]+?)\s*-->",
    re.IGNORECASE,
)

#: Marker recording which merged main SHA the capability is waiting on.
REQUIRED_SHA_RE = re.compile(
    r"<!--\s*continuum-qualification-required\s+"
    r"capability\s*=\s*(?P<capability>\d+)\s+"
    r"sha\s*=\s*(?P<sha>[0-9a-fA-F]{40})\s*-->",
    re.IGNORECASE,
)

#: Idempotency marker: one dispatch per (capability, qualification, SHA).
DISPATCH_RE = re.compile(
    r"<!--\s*continuum-qualification-dispatch\s+"
    r"capability\s*=\s*(?P<capability>\d+)\s+"
    r"qualification\s*=\s*(?P<qualification>\d+)\s+"
    r"sha\s*=\s*(?P<sha>[0-9a-fA-F]{40})\s*-->",
    re.IGNORECASE,
)

#: Canonical per-run evidence marker published on the qualification issue.
EVIDENCE_RE = re.compile(
    r"<!--\s*continuum-qualification-result\b"
    r"(?P<attrs>[^>]*?)-->",
    re.IGNORECASE,
)

#: Docker-controller evidence marker; the JSON payload follows the marker.
DOCKER_RESULT_MARKER = "<!-- continuum-docker-qualification-result -->"

#: Render-controller evidence marker; the JSON payload follows the marker.
RENDER_RESULT_MARKER = "<!-- continuum-render-qualification-result -->"

#: GitHub auto-close keywords that would bypass the gate via ``Fixes #N``.
CLOSING_KEYWORDS_RE = re.compile(
    r"\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
    r"\s*:?\s*#?\d+",
    re.IGNORECASE,
)

#: Lifecycle label holding an implementation reservation.
IN_PROGRESS_LABEL = "automation:in-progress"

#: Lifecycle label marking implementation-merged, qualification pending.
QUALIFYING_LABEL = "automation:qualifying"

#: Lifecycle label marking qualification failed, repair required.
BLOCKED_LABEL = "automation:blocked"

#: Label that pauses automation until removed.
PAUSED_LABEL = "automation:paused"

#: Evidence verdicts.
EVIDENCE_PASS = "pass"
EVIDENCE_FAIL = "fail"
EVIDENCE_UNKNOWN = "unknown"

#: Capability lifecycle outcomes of :func:`capability_status`.
MAY_COMPLETE = "complete"
MUST_REOPEN = "must-reopen"
STAY_QUALIFYING = "qualifying"
STAY_BLOCKED = "blocked"
NO_QUALIFICATION = "no-qualification"

_SHA_RE = re.compile(r"\A[0-9a-fA-F]{40}\Z")


def _normalize_sha(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if _SHA_RE.match(text):
        return text
    return None


def parse_qualification_refs(body: object, self_number: object = None) -> Tuple[int, ...]:
    """Mandatory qualification issue numbers declared in a body.

    Only the machine-readable ``automation-qualification`` marker counts;
    prose never does. The result is sorted, deduplicated, and never contains
    the issue itself.
    """

    if not isinstance(body, str):
        return ()
    try:
        own = int(self_number) if self_number is not None else None
    except (TypeError, ValueError):
        own = None
    found = set()
    for match in QUALIFICATION_DECLARATION_RE.finditer(body):
        for item in re.findall(r"\d+", match.group(1)):
            try:
                number = int(item)
            except ValueError:
                continue
            if number <= 0:
                continue
            if own is not None and number == own:
                continue
            found.add(number)
    return tuple(sorted(found))


def latest_required_sha(body: object, capability_number: object = None) -> Optional[str]:
    """The most recently recorded required main SHA, or None.

    The required SHA is written at implementation-merge time. When several
    markers exist (implementation, then repair, ...), the last one wins, so
    a repair that advances ``main`` invalidates older evidence.
    """

    if not isinstance(body, str):
        return None
    try:
        own = int(capability_number) if capability_number is not None else None
    except (TypeError, ValueError):
        own = None
    latest: Optional[str] = None
    for match in REQUIRED_SHA_RE.finditer(body):
        if own is not None and int(match.group("capability")) != own:
            continue
        sha = _normalize_sha(match.group("sha"))
        if sha is not None:
            latest = sha
    return latest


def required_sha_marker(capability_number: int, sha: str) -> str:
    """The bookkeeping marker an implementation merge records."""

    normalized = _normalize_sha(sha)
    if normalized is None:
        raise ValueError("sha must be a 40-character hex revision, got {!r}".format(sha))
    return "<!-- continuum-qualification-required capability={} sha={} -->".format(
        int(capability_number), normalized
    )


def dispatch_marker(capability_number: int, qualification_number: int, sha: str) -> str:
    """The idempotency marker written when a qualification is dispatched."""

    normalized = _normalize_sha(sha)
    if normalized is None:
        raise ValueError("sha must be a 40-character hex revision, got {!r}".format(sha))
    return (
        "<!-- continuum-qualification-dispatch capability={} qualification={} sha={} -->"
        .format(int(capability_number), int(qualification_number), normalized)
    )


def evidence_marker(qualification_number: int, sha: str, result: str) -> str:
    """The canonical evidence marker a qualification run publishes."""

    normalized = _normalize_sha(sha)
    if normalized is None:
        raise ValueError("sha must be a 40-character hex revision, got {!r}".format(sha))
    verdict = str(result).strip().lower()
    if verdict not in (EVIDENCE_PASS, EVIDENCE_FAIL):
        raise ValueError("result must be 'pass' or 'fail', got {!r}".format(result))
    return "<!-- continuum-qualification-result issue={} sha={} result={} -->".format(
        int(qualification_number), normalized, verdict
    )


def has_dispatch_marker(
    bodies: Iterable[object],
    capability_number: int,
    qualification_number: int,
    sha: str,
) -> bool:
    """Whether a dispatch was already recorded for this exact triple."""

    normalized = _normalize_sha(sha)
    if normalized is None:
        return False
    for body in bodies:
        if not isinstance(body, str):
            continue
        for match in DISPATCH_RE.finditer(body):
            if (
                int(match.group("capability")) == int(capability_number)
                and int(match.group("qualification")) == int(qualification_number)
                and (_normalize_sha(match.group("sha")) == normalized)
            ):
                return True
    return False


def contains_closing_keyword(text: object, issue_number: object = None) -> bool:
    """Whether text carries a GitHub auto-close keyword.

    With an issue number, only a keyword targeting that issue counts;
    otherwise any auto-close keyword counts. A qualification-gated PR body
    must never contain one for its capability issue.
    """

    if not isinstance(text, str) or not text:
        return False
    if issue_number is None:
        return CLOSING_KEYWORDS_RE.search(text) is not None
    try:
        wanted = int(issue_number)
    except (TypeError, ValueError):
        return False
    pattern = re.compile(
        r"\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
        r"\s*:?\s*#?" + str(wanted) + r"(?!\d)",
        re.IGNORECASE,
    )
    return pattern.search(text) is not None


def _attrs_to_dict(attrs: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for match in re.finditer(r"([A-Za-z_]+)\s*=\s*([^\s>]+)", attrs or ""):
        values[match.group(1).strip().lower()] = match.group(2).strip()
    return values


def _verdict_from_aliases(attrs: Dict[str, str]) -> Optional[str]:
    for key in ("result", "classification", "verdict"):
        if key in attrs:
            verdict = attrs[key].strip().lower()
            if verdict in (EVIDENCE_PASS, "passed", "success", "approved"):
                return EVIDENCE_PASS
            return EVIDENCE_FAIL
    return None


def parse_evidence_entries(comment_body: object) -> List[Dict[str, object]]:
    """Every machine-readable evidence entry in one comment body.

    Canonical markers and Docker/Render controller payloads are accepted.
    Prose verdicts never count. Each entry is a mapping with ``sha`` and
    ``result`` (``pass``/``fail``), plus ``source`` and ``issue`` when known.
    """

    if not isinstance(comment_body, str) or not comment_body:
        return []
    entries: List[Dict[str, object]] = []
    for match in EVIDENCE_RE.finditer(comment_body):
        attrs = _attrs_to_dict(match.group("attrs") or "")
        sha = _normalize_sha(attrs.get("sha") or attrs.get("head"))
        verdict = _verdict_from_aliases(attrs)
        if sha is None or verdict is None:
            continue
        issue: Optional[int] = None
        if attrs.get("issue") is not None:
            try:
                issue = int(attrs["issue"])
            except ValueError:
                continue
        entries.append(
            {"source": "canonical", "issue": issue, "sha": sha, "result": verdict}
        )
    for marker, source in (
        (DOCKER_RESULT_MARKER, "docker"),
        (RENDER_RESULT_MARKER, "render"),
    ):
        index = comment_body.find(marker)
        while index != -1:
            tail = comment_body[index + len(marker):].strip()
            payload: Optional[dict] = None
            try:
                payload = json.loads(tail.split("-->", 1)[0].strip() or tail)
            except (ValueError, AttributeError):
                payload = None
            if isinstance(payload, dict):
                sha = _normalize_sha(
                    payload.get("head_sha") or payload.get("sha") or payload.get("head")
                )
                classification = str(
                    payload.get("classification")
                    or payload.get("result")
                    or payload.get("verdict")
                    or ""
                ).strip().lower()
                if sha is not None and classification:
                    entries.append(
                        {
                            "source": source,
                            "issue": None,
                            "sha": sha,
                            "result": (
                                EVIDENCE_PASS
                                if classification in ("pass", "passed", "success", "approved")
                                else EVIDENCE_FAIL
                            ),
                        }
                    )
                    break
            # Only the first JSON document after the marker is evidence; a
            # marker with no parseable payload contributes nothing.
            break
    return entries


def qualification_evidence_state(
    comments: Iterable[object],
    qualification_number: int,
    required_sha: object,
) -> str:
    """The evidence verdict for one qualification at the required SHA.

    Returns ``pass`` only when the latest evidence entry for the exact
    required SHA is a pass. A fail for the required SHA returns ``fail``.
    Anything else — no entries, entries only for older SHAs, malformed
    markers, prose — returns ``unknown``. A merely closed qualification
    issue without evidence is ``unknown`` by construction.
    """

    sha = _normalize_sha(required_sha)
    if sha is None:
        return EVIDENCE_UNKNOWN
    try:
        wanted = int(qualification_number)
    except (TypeError, ValueError):
        return EVIDENCE_UNKNOWN
    latest: Optional[str] = None
    for comment in comments or []:
        for entry in parse_evidence_entries(comment):
            issue = entry.get("issue")
            if issue is not None and int(issue) != wanted:
                continue
            if entry.get("sha") != sha:
                # Stale evidence for an older main SHA never satisfies the
                # gate for a newer implementation.
                continue
            latest = str(entry.get("result"))
    if latest == EVIDENCE_PASS:
        return EVIDENCE_PASS
    if latest == EVIDENCE_FAIL:
        return EVIDENCE_FAIL
    return EVIDENCE_UNKNOWN


def capability_status(
    capability_open: bool,
    qualification_refs: Iterable[int],
    evidence_by_qualification: Dict[int, str],
    required_sha: object = None,
) -> str:
    """The lifecycle outcome for a capability issue.

    * No declared qualifications → ``no-qualification`` (normal lifecycle).
    * No required SHA recorded yet (not merged) → ``qualifying``.
    * Every qualification ``pass`` for the required SHA → ``complete`` when
      open, ``complete`` when already closed (nothing to repair).
    * Any ``fail`` → ``blocked`` when open, ``must-reopen`` when closed.
    * Otherwise (pending/paused/unknown/stale) → ``qualifying`` when open,
      ``must-reopen`` when closed: GitHub auto-close keywords cannot bypass
      the gate because a closed capability without exact-SHA pass evidence
      fails closed.
    """

    refs = tuple(int(item) for item in (qualification_refs or ()))
    if not refs:
        return NO_QUALIFICATION
    sha = _normalize_sha(required_sha)
    if sha is None:
        return STAY_QUALIFYING if capability_open else MUST_REOPEN
    states = [str(evidence_by_qualification.get(ref, EVIDENCE_UNKNOWN)) for ref in refs]
    if all(state == EVIDENCE_PASS for state in states):
        return MAY_COMPLETE
    if any(state == EVIDENCE_FAIL for state in states):
        return STAY_BLOCKED if capability_open else MUST_REOPEN
    return STAY_QUALIFYING if capability_open else MUST_REOPEN


def should_dispatch_qualification(
    qualification_open: bool,
    qualification_paused: bool,
    capability_state: str,
    already_dispatched_for_sha: bool,
    required_sha: object,
) -> Tuple[bool, str]:
    """Whether the scheduler must (re)dispatch a mandatory qualification.

    A paused qualification never strands the lifecycle: pause is a reason to
    unpause-and-dispatch, not a reason to skip. Dispatches are idempotent
    per SHA so duplicate merge events cannot start qualification storms; a
    closed qualification is never dispatched (it must be reopened first).
    """

    if _normalize_sha(required_sha) is None:
        return False, "no-required-sha"
    if capability_state not in (STAY_QUALIFYING, STAY_BLOCKED):
        return False, "capability-complete"
    if not qualification_open:
        return False, "qualification-closed"
    if already_dispatched_for_sha:
        return False, "already-dispatched"
    if qualification_paused:
        return True, "unpause-and-dispatch"
    return True, "dispatch"


def repair_targets(evidence_by_qualification: Dict[int, str]) -> Tuple[int, ...]:
    """Qualifications whose failure routes repair work back into the lifecycle."""

    return tuple(
        sorted(
            int(number)
            for number, state in (evidence_by_qualification or {}).items()
            if str(state) == EVIDENCE_FAIL
        )
    )


def sha_superseded(old_required_sha: object, current_main_sha: object) -> bool:
    """Whether a repair merge invalidated the previous qualification.

    Any change of the ``main`` SHA after the required SHA was recorded means
    old evidence is stale and the qualification must rerun against the new
    exact SHA.
    """

    old = _normalize_sha(old_required_sha)
    current = _normalize_sha(current_main_sha)
    if old is None or current is None:
        return False
    return old != current
