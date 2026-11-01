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

Qualification DAG semantics (kodmial/continuum#278)
--------------------------------------------------
The qualification relation is first-class, not a normal implementation
dependency:

* A capability that declares ``automation-qualification: #Q`` must never treat
  ``#Q`` as a normal blocker: during qualification the capability
  intentionally remains open/``automation:qualifying``, so waiting on ``#Q``
  as ordinary work strands the lifecycle. Such an edge is a direct
  capability<->qualification back-edge and is deterministically ignored for
  qualification readiness with a clear diagnostic, never honored as a blocker.
* A qualification tracker ``#Q`` that declares ``automation-blocked-by: #C``
  (where ``#C`` is the capability it validates) must still be dispatched for
  that exact ``#C`` run: qualification dispatch bypasses normal
  implementation-readiness admission for the declared capability relation
  while still honoring the immutable required SHA and every
  qualification-specific safety rule.
* Qualification/validation trackers are never auto-admitted to generic
  implementation: an issue that is declared as a qualification of another
  issue needs explicit qualification dispatch and stays independent of
  ``automation:ready``.
* Reusable trackers stay open across revisions; consumers gate on the trusted
  exact revision/fingerprint PASS tuple (:func:`qualification_result_tuple`),
  never on tracker closure.

Standard library only.
"""

from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

#: Marker a capability issue carries to declare mandatory qualifications.
QUALIFICATION_DECLARATION_RE = re.compile(
    r"<!--\s*automation-qualification\s*:\s*([0-9#\s,]+?)\s*-->",
    re.IGNORECASE,
)

#: Marker an issue carries to declare normal implementation blockers. A
#: qualification tracker that lists its own capability here creates the
#: capability<->qualification back-edge described above; the qualification
#: relation itself is never expressed with this marker.
BLOCKED_BY_RE = re.compile(
    r"<!--\s*automation-blocked-by\s*:\s*([0-9#\s,]+?)\s*-->",
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

#: Comment authors whose lifecycle markers are trusted. Public/untrusted
#: commenters can forge syntactically valid markers, so only these GitHub
#: author associations may satisfy the gate or drive dispatch.
TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: Automation logins trusted for lifecycle control and result evidence where
#: explicitly intended (Docker/Render controllers and the scheduler post
#: through this identity).
TRUSTED_AUTOMATION_LOGINS = frozenset({"github-actions[bot]"})

#: Bounded automatic-retry backoff for mandatory qualification (seconds).
#: Exhausted immediate retries fall back to this lease instead of a terminal
#: human-gated pause, so qualification resumes without label removal.
QUALIFICATION_RETRY_BACKOFF_SECONDS = (60, 300, 900, 3600)

#: Marker recording a repair handoff for a failed qualification.
REPAIR_MARKER_RE = re.compile(
    r"<!--\s*continuum-qualification-repair\s+"
    r"source\s*=\s*(?P<source>\d+)\s+"
    r"capability\s*=\s*(?P<capability>\d+)\s*-->",
    re.IGNORECASE,
)

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


def _comment_body(comment: object) -> Optional[str]:
    if isinstance(comment, str):
        return comment
    if isinstance(comment, dict):
        body = comment.get("body")
        return body if isinstance(body, str) else None
    body = getattr(comment, "body", None)
    return body if isinstance(body, str) else None


def _comment_association(comment: object) -> Optional[str]:
    if isinstance(comment, str):
        return None
    if isinstance(comment, dict):
        for key in ("author_association", "association", "authorAssociation"):
            value = comment.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().upper()
        author = comment.get("author") or comment.get("user") or {}
        if isinstance(author, dict):
            for key in ("association", "author_association"):
                value = author.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip().upper()
        return None
    for attr in ("author_association", "association"):
        value = getattr(comment, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _comment_login(comment: object) -> Optional[str]:
    if isinstance(comment, str):
        return None
    if isinstance(comment, dict):
        for key in ("author_login", "login"):
            value = comment.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("author", "user"):
            author = comment.get(key)
            if isinstance(author, dict):
                login = author.get("login")
                if isinstance(login, str) and login.strip():
                    return login.strip()
            elif isinstance(author, str) and author.strip():
                return author.strip()
        return None
    for attr in ("author_login", "login"):
        value = getattr(comment, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def is_trusted_comment(comment: object, allow_automation: bool = False) -> bool:
    """Whether a comment author may drive the qualification lifecycle.

    ``OWNER``/``MEMBER``/``COLLABORATOR`` are always trusted. Repository
    automation (``github-actions[bot]``) is trusted only where the caller
    explicitly allows it (dispatch control and controller result payloads).
    Plain string bodies predate author metadata and are treated as trusted
    for backward compatibility of already-vetted local inputs only; live
    callers aggregating issue comments must pass mappings carrying
    ``author_association``/``user.login`` so public forgeries are rejected
    (see ``qualification_gate.py evidence-state``, which builds a
    structured comment from caller-attested authorship instead of passing
    a bare body).
    """

    if isinstance(comment, str):
        return True
    association = _comment_association(comment)
    if association in TRUSTED_AUTHOR_ASSOCIATIONS:
        return True
    if allow_automation:
        login = (_comment_login(comment) or "").lower()
        if login in {name.lower() for name in TRUSTED_AUTOMATION_LOGINS}:
            return True
        if association == "BOT" and login in {
            name.lower() for name in TRUSTED_AUTOMATION_LOGINS
        }:
            return True
    return False


def contains_dispatch_marker(body: object) -> bool:
    """Whether a body carries a qualification dispatch marker."""

    if not isinstance(body, str) or not body:
        return False
    return DISPATCH_RE.search(body) is not None


def is_qualification_result_comment(body: object) -> bool:
    """Whether a body carries any supported qualification result marker."""

    if not isinstance(body, str) or not body:
        return False
    if EVIDENCE_RE.search(body) is not None:
        return True
    return (
        DOCKER_RESULT_MARKER in body or RENDER_RESULT_MARKER in body
    )


def qualification_run_identity(
    capability_number: int, qualification_number: int, sha: str
) -> Tuple[int, int, str]:
    """The immutable identity of one qualification run.

    A new SHA is a distinct run identity; a retry without evidence preserves
    the same triple so recovery never retargets the run.
    """

    normalized = _normalize_sha(sha)
    if normalized is None:
        raise ValueError("sha must be a 40-character hex revision, got {!r}".format(sha))
    return (int(capability_number), int(qualification_number), normalized)


def qualification_run_key(
    capability_number: int, qualification_number: int, sha: str
) -> str:
    triple = qualification_run_identity(capability_number, qualification_number, sha)
    return "capability={} qualification={} sha={}".format(*triple)


def dispatch_matches_run(
    capability_number: int,
    qualification_number: int,
    sha: str,
    run: Tuple[int, int, str],
) -> bool:
    """Whether a dispatch marker triple binds exactly to a running run."""

    normalized = _normalize_sha(sha)
    if normalized is None:
        return False
    try:
        return (
            int(capability_number) == int(run[0])
            and int(qualification_number) == int(run[1])
            and normalized == _normalize_sha(run[2])
        )
    except (TypeError, ValueError, IndexError):
        return False


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


def parse_blocked_by_refs(body: object, self_number: object = None) -> Tuple[int, ...]:
    """Normal implementation blocker numbers declared in a body.

    Only the machine-readable ``automation-blocked-by`` marker counts;
    prose never does. The result is sorted, deduplicated, and never contains
    the issue itself. This is the normal implementation dependency relation;
    the qualification relation is declared separately with
    ``automation-qualification`` and must never be modeled through it.
    """

    if not isinstance(body, str):
        return ()
    try:
        own = int(self_number) if self_number is not None else None
    except (TypeError, ValueError):
        own = None
    found = set()
    for match in BLOCKED_BY_RE.finditer(body):
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


def collect_qualification_trackers(
    bodies_by_issue: Mapping[object, object],
) -> Tuple[int, ...]:
    """Every issue number declared as a qualification by any mapped body.

    The input maps capability issue number to body text. The result is the
    sorted, deduplicated union of :func:`parse_qualification_refs` across all
    bodies. Schedulers consult this set to keep qualification/validation
    trackers out of generic implementation admission: a tracker needs
    explicit qualification dispatch and is independent of
    ``automation:ready``.
    """

    found: set = set()
    if not bodies_by_issue:
        return ()
    try:
        items = list(bodies_by_issue.items())
    except AttributeError:
        return ()
    for self_number, body in items:
        try:
            own = int(self_number)
        except (TypeError, ValueError):
            own = None
        for ref in parse_qualification_refs(body, own):
            found.add(int(ref))
    return tuple(sorted(found))


def is_qualification_tracker(
    issue_number: object, bodies_by_issue: Mapping[object, object]
) -> bool:
    """Whether an issue is declared as a qualification tracker elsewhere."""

    try:
        wanted = int(issue_number)
    except (TypeError, ValueError):
        return False
    return wanted in set(collect_qualification_trackers(bodies_by_issue or {}))


def should_admit_to_implementation(
    issue_number: object, bodies_by_issue: Mapping[object, object]
) -> Tuple[bool, str]:
    """Whether generic implementation scheduling may admit an issue.

    Qualification/validation trackers must not be auto-admitted to generic
    implementation simply because they are open and unblocked: they need
    explicit qualification dispatch, which remains independent of
    ``automation:ready``. Returns ``(False,
    "qualification-tracker-needs-explicit-dispatch")`` for a declared
    tracker and ``(True, "ready")`` otherwise. Repair issues (which carry
    the ``continuum-qualification-repair`` marker, not a qualification
    declaration) are ordinary implementation work and stay admissible.
    """

    if is_qualification_tracker(issue_number, bodies_by_issue or {}):
        return False, "qualification-tracker-needs-explicit-dispatch"
    return True, "ready"


def qualification_cycle_edges(
    capability_number: object,
    qualification_refs: Iterable[int],
    capability_blocked_by: Iterable[object] = (),
    qualification_blocked_by: Optional[Mapping[object, Iterable[object]]] = None,
) -> Dict[str, Tuple[int, ...]]:
    """The direct capability<->qualification back-edges for one capability.

    Returns ``{"capability_blocked_by_qualification": (...),
    "qualifications_blocked_by_capability": (...)}`` where the first tuple
    holds declared qualifications that the capability also lists as normal
    blockers, and the second holds declared qualifications whose own
    ``automation-blocked-by`` lists the capability. Either tuple being
    non-empty is the directly impossible topology (for example AA ``#6``
    declaring qualification ``#7`` while ``#7`` declares
    ``automation-blocked-by: #6``): the validator must never be modeled as
    a normal downstream implementation dependency.
    """

    try:
        capability = int(capability_number)
    except (TypeError, ValueError):
        return {
            "capability_blocked_by_qualification": (),
            "qualifications_blocked_by_capability": (),
        }
    refs = tuple(sorted({int(item) for item in (qualification_refs or ())}))
    try:
        blockers = {int(item) for item in (capability_blocked_by or [])}
    except (TypeError, ValueError):
        blockers = set()
    forward = tuple(sorted(set(refs) & blockers))
    reverse: List[int] = []
    mapping = qualification_blocked_by or {}
    try:
        pairs = list(mapping.items())
    except AttributeError:
        pairs = []
    blocked_sets: Dict[int, set] = {}
    for key, values in pairs:
        try:
            qualification = int(key)
        except (TypeError, ValueError):
            continue
        try:
            blocked_sets[qualification] = {int(item) for item in (values or [])}
        except (TypeError, ValueError):
            continue
    for ref in refs:
        if capability in blocked_sets.get(ref, set()):
            reverse.append(int(ref))
    return {
        "capability_blocked_by_qualification": forward,
        "qualifications_blocked_by_capability": tuple(sorted(reverse)),
    }


def has_qualification_blocker_cycle(
    capability_number: object,
    qualification_refs: Iterable[int],
    capability_blocked_by: Iterable[object] = (),
    qualification_blocked_by: Optional[Mapping[object, Iterable[object]]] = None,
) -> bool:
    """Whether the direct capability<->qualification topology is cyclic."""

    edges = qualification_cycle_edges(
        capability_number,
        qualification_refs,
        capability_blocked_by,
        qualification_blocked_by,
    )
    return bool(
        edges["capability_blocked_by_qualification"]
        or edges["qualifications_blocked_by_capability"]
    )


def effective_qualification_blockers(
    qualification_refs: Iterable[int],
    open_blockers: Iterable[object] = (),
) -> Tuple[List[int], Tuple[int, ...]]:
    """Capability blockers with qualification back-edges deterministically ignored.

    Returns ``(effective_blockers, ignored_back_edges)`` where ``ignored``
    holds open blockers that are also declared qualifications of this
    capability. An open capability intentionally remains open while it
    qualifies, so waiting on its own validator as ordinary work would strand
    the lifecycle; the back-edge is ignored with a diagnostic instead of
    being honored. Any remaining effective blocker still blocks.
    """

    refs = {int(item) for item in (qualification_refs or ())}
    blockers: List[int] = []
    for item in open_blockers or []:
        try:
            blockers.append(int(item))
        except (TypeError, ValueError):
            continue
    ignored = tuple(sorted(set(blockers) & refs))
    effective = [item for item in blockers if item not in refs]
    return effective, ignored


def qualification_dispatch_bypass(
    qualification_number: object,
    capability_number: object,
    qualification_open_blockers: Iterable[object] = (),
) -> Tuple[List[int], Tuple[int, ...], str]:
    """Reverse-edge bypass for dispatching one qualification tracker.

    A qualification tracker commonly declares ``automation-blocked-by`` on
    the capability it validates (AA ``#7`` blocked by ``#6``). Normal
    implementation readiness would then wait for the capability to close,
    while the capability waits for qualification evidence to close: a
    deadlock. Qualification dispatch bypasses exactly that declared
    capability relation and still honors every other open blocker and all
    qualification-specific safety rules (immutable required SHA, trusted
    evidence, idempotent dispatch). Returns ``(effective_blockers,
    bypassed, reason)`` with reason ``"dispatch"`` when nothing was
    bypassed, ``"dispatch-ignoring-capability-back-edge"`` when only the
    capability edge was bypassed, and ``"blocked"`` when other blockers
    remain.
    """

    try:
        qualification = int(qualification_number)
    except (TypeError, ValueError):
        return [], (), "blocked"
    try:
        capability = int(capability_number)
    except (TypeError, ValueError):
        blockers: List[int] = []
        for item in qualification_open_blockers or []:
            try:
                blockers.append(int(item))
            except (TypeError, ValueError):
                continue
        if blockers:
            return blockers, (), "blocked"
        return [], (), "dispatch"
    blockers = []
    for item in qualification_open_blockers or []:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number == qualification:
            continue
        blockers.append(number)
    bypassed = tuple(sorted({item for item in blockers if item == capability}))
    effective = [item for item in blockers if item != capability]
    if effective:
        return effective, bypassed, "blocked"
    if bypassed:
        return [], bypassed, "dispatch-ignoring-capability-back-edge"
    return [], (), "dispatch"


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
    """Whether a trusted dispatch was already recorded for this exact triple.

    Only markers authored by trusted repository actors/automation count;
    public/untrusted forged markers must not suppress, redirect, or
    manufacture qualification dispatch. Plain string bodies predate author
    metadata and are treated as trusted for backward compatibility.
    """

    normalized = _normalize_sha(sha)
    if normalized is None:
        return False
    for comment in bodies or []:
        if not is_trusted_comment(comment, allow_automation=True):
            continue
        body = _comment_body(comment)
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


def has_untrusted_dispatch_forgery(
    bodies: Iterable[object],
    capability_number: int,
    qualification_number: int,
    sha: str,
) -> bool:
    """Whether an untrusted comment forges a dispatch for this triple."""

    normalized = _normalize_sha(sha)
    if normalized is None:
        return False
    for comment in bodies or []:
        if is_trusted_comment(comment, allow_automation=True):
            continue
        body = _comment_body(comment)
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


def _fingerprint_from_attrs(attrs: Dict[str, str]) -> Optional[str]:
    for key in ("fingerprint", "fp", "digest"):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _fingerprint_from_payload(payload: dict) -> Optional[str]:
    for key in ("fingerprint", "fp", "digest"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_evidence_entries(comment_body: object) -> List[Dict[str, object]]:
    """Every machine-readable evidence entry in one comment body.

    Canonical markers and Docker/Render controller payloads are accepted.
    Prose verdicts never count. Each entry is a mapping with ``sha`` and
    ``result`` (``pass``/``fail``), plus ``source`` and ``issue`` when known
    and ``fingerprint`` when the payload carries one (controller results;
    the canonical marker carries none).
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
            {
                "source": "canonical",
                "issue": issue,
                "sha": sha,
                "result": verdict,
                "fingerprint": _fingerprint_from_attrs(attrs),
            }
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
                            "fingerprint": _fingerprint_from_payload(payload),
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
    """The trusted evidence verdict for one qualification at the required SHA.

    Returns ``pass`` only when the latest *trusted* evidence entry for the
    exact required SHA is a pass. A dispatch/instruction comment never counts
    as evidence, public/untrusted forged markers are rejected, and only
    ``OWNER``/``MEMBER``/``COLLABORATOR`` plus explicitly allowed repository
    automation are accepted. A fail for the required SHA returns ``fail``.
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
        if not is_trusted_comment(comment, allow_automation=True):
            continue
        body = _comment_body(comment)
        if not isinstance(body, str) or not body:
            continue
        if contains_dispatch_marker(body):
            # A qualification dispatch/instruction comment must never count
            # as result evidence, even if its prose quotes a marker shape.
            continue
        for entry in parse_evidence_entries(body):
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


def latest_trusted_evidence_body(
    comments: Iterable[object],
    qualification_number: int,
    required_sha: object,
) -> Optional[str]:
    """The latest trusted comment body carrying evidence for the exact SHA."""

    sha = _normalize_sha(required_sha)
    if sha is None:
        return None
    try:
        wanted = int(qualification_number)
    except (TypeError, ValueError):
        return None
    latest: Optional[str] = None
    for comment in comments or []:
        if not is_trusted_comment(comment, allow_automation=True):
            continue
        body = _comment_body(comment)
        if not isinstance(body, str) or not body:
            continue
        if contains_dispatch_marker(body):
            continue
        matched = False
        for entry in parse_evidence_entries(body):
            issue = entry.get("issue")
            if issue is not None and int(issue) != wanted:
                continue
            if entry.get("sha") == sha:
                matched = True
        if matched:
            latest = body
    return latest


def qualification_result_tuple(
    comments: Iterable[object],
    qualification_number: int,
    required_sha: object,
) -> Optional[Dict[str, object]]:
    """The trusted reusable result tuple for one qualification at one SHA.

    Returns ``{"issue": int, "sha": str, "result": "pass"/"fail",
    "fingerprint": Optional[str], "source": str}`` for the latest trusted
    evidence entry matching the exact required SHA, or ``None`` when there
    is no such entry. The tuple is the reusable contract consumers gate on:
    it is consumable without closing the qualification tracker, so a
    reusable validation issue stays open across revisions while each
    revision carries its own pass/fail evidence. Trust, dispatch-exclusion,
    and exact-SHA rules are identical to
    :func:`qualification_evidence_state`; only the shape differs.
    """

    sha = _normalize_sha(required_sha)
    if sha is None:
        return None
    try:
        wanted = int(qualification_number)
    except (TypeError, ValueError):
        return None
    latest: Optional[Dict[str, object]] = None
    for comment in comments or []:
        if not is_trusted_comment(comment, allow_automation=True):
            continue
        body = _comment_body(comment)
        if not isinstance(body, str) or not body:
            continue
        if contains_dispatch_marker(body):
            continue
        for entry in parse_evidence_entries(body):
            issue = entry.get("issue")
            if issue is not None and int(issue) != wanted:
                continue
            if entry.get("sha") != sha:
                continue
            latest = {
                "issue": wanted,
                "sha": sha,
                "result": str(entry.get("result")),
                "fingerprint": entry.get("fingerprint"),
                "source": str(entry.get("source")),
            }
    return latest


def has_trusted_pass_for_sha(
    comments: Iterable[object],
    qualification_number: int,
    required_sha: object,
) -> bool:
    """Whether trusted exact-SHA PASS evidence exists, without closure.

    Reusable qualification trackers are never required to close: a ``True``
    result means the exact revision is proven good even while the tracker
    stays open for the next revision.
    """

    result = qualification_result_tuple(comments, qualification_number, required_sha)
    return result is not None and result.get("result") == EVIDENCE_PASS


def trusted_pass_fingerprint(
    comments: Iterable[object],
    qualification_number: int,
    required_sha: object,
) -> Optional[str]:
    """The fingerprint of the trusted exact-SHA PASS, if any carries one."""

    result = qualification_result_tuple(comments, qualification_number, required_sha)
    if result is None or result.get("result") != EVIDENCE_PASS:
        return None
    fingerprint = result.get("fingerprint")
    if isinstance(fingerprint, str) and fingerprint.strip():
        return fingerprint.strip()
    return None


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


def should_start_qualification(
    qualification_refs: Iterable[int],
    required_sha: object,
    open_blockers: Iterable[object] = (),
) -> Tuple[bool, str]:
    """Whether qualification may start for a capability.

    Declaring ``automation-qualification`` is only a relationship: it never
    starts qualification by itself and never synthesizes a required SHA from
    current main. Only a trusted implementation-merge transition recording
    the first ``continuum-qualification-required`` SHA may start it. Open
    blockers/unimplemented capability work remain blockers, except for a
    direct capability<->qualification back-edge: an open blocker that is
    itself a declared qualification of this capability is deterministically
    ignored with the ``ready-ignoring-qualification-back-edge`` reason,
    because the capability intentionally remains open while it qualifies.
    Qualification cannot otherwise leapfrog blockers.
    """

    refs = tuple(int(item) for item in (qualification_refs or ()))
    if not refs:
        return False, "no-qualification"
    if _normalize_sha(required_sha) is None:
        return False, "no-required-sha"
    effective, ignored = effective_qualification_blockers(refs, open_blockers)
    if effective:
        return False, "blocked"
    if ignored:
        return True, "ready-ignoring-qualification-back-edge"
    return True, "ready"


def can_enter_qualifying_state(
    required_sha: object,
    open_blockers: Iterable[object] = (),
) -> Tuple[bool, str]:
    """Whether a merge event may record the qualifying SHA and dispatch."""

    return should_start_qualification((1,), required_sha, open_blockers)


def repair_marker_text(source_number: int, capability_number: int) -> str:
    return "<!-- continuum-qualification-repair source={} capability={} -->".format(
        int(source_number), int(capability_number)
    )


def repair_issue_body(
    capability_number: int,
    qualification_number: int,
    required_sha: str,
    failure_evidence: Optional[str] = None,
) -> str:
    """The body for the automatically created/reused repair issue.

    The repair worker must read the failure matrix before editing code, so
    the body always points at the exact qualification issue, required SHA,
    and latest trusted failure evidence for that SHA.
    """

    normalized = _normalize_sha(required_sha)
    if normalized is None:
        raise ValueError("sha must be a 40-character hex revision, got {!r}".format(required_sha))
    lines = [
        repair_marker_text(int(qualification_number), int(capability_number)),
        "",
        "Mandatory qualification #{} for capability #{} failed at main `{}`.".format(
            int(qualification_number), int(capability_number), normalized
        ),
        "",
        "Failure handoff (read before editing code):",
        "- qualification issue: #{}".format(int(qualification_number)),
        "- capability issue: #{}".format(int(capability_number)),
        "- required SHA: `{}`".format(normalized),
    ]
    if failure_evidence and failure_evidence.strip():
        lines += [
            "",
            "Latest trusted failure evidence for this exact SHA:",
            "",
            failure_evidence.strip(),
        ]
    lines += [
        "",
        "Fix the concrete failing scenarios from the evidence above; do not",
        "guess from the generic title alone. Merge the fix and the",
        "qualification gate automatically reruns qualification #{} against".format(
            int(qualification_number)
        ),
        "the new exact main SHA. Do not close capability #{} manually:".format(
            int(capability_number)
        ),
        "only exact-SHA pass evidence may complete it.",
    ]
    return "\n".join(lines)


def repair_body_needs_refresh(
    existing_body: object, required_sha: object, failure_evidence: Optional[str]
) -> bool:
    """Whether an existing open repair issue keeps stale evidence."""

    sha = _normalize_sha(required_sha)
    if sha is None or not isinstance(existing_body, str):
        return True
    if sha not in existing_body:
        return True
    evidence = (failure_evidence or "").strip()
    if evidence and evidence not in existing_body:
        return True
    return False


def next_qualification_retry_delay_seconds(attempt: int) -> int:
    """Bounded lease/backoff delay before the next automatic qualification try."""

    try:
        index = int(attempt)
    except (TypeError, ValueError):
        index = 0
    if index < 0:
        index = 0
    if index >= len(QUALIFICATION_RETRY_BACKOFF_SECONDS):
        return QUALIFICATION_RETRY_BACKOFF_SECONDS[-1]
    return QUALIFICATION_RETRY_BACKOFF_SECONDS[index]


def qualification_needs_retry(
    evidence_state: str,
    qualification_open: bool,
    capability_state: str,
) -> Tuple[bool, str]:
    """Whether mandatory qualification must retry without human intervention.

    A failed/missing-evidence qualification must never become permanently
    stranded behind ``automation:paused``. Deliberate owner pauses are
    represented by the same label, so the scheduler treats pause as a reason
    to unpause-and-retry with bounded backoff rather than as terminal state.
    """

    if capability_state not in (STAY_QUALIFYING, STAY_BLOCKED):
        return False, "capability-complete"
    if not qualification_open:
        return False, "qualification-closed"
    if evidence_state == EVIDENCE_PASS:
        return False, "already-passing"
    return True, "retry-with-backoff"


def evaluate_qualification_run(
    has_code_diff: bool,
    evidence_state: str,
    pushed_product_changes: bool = False,
) -> Tuple[str, str]:
    """The outcome of one qualification-mode execution.

    Pure validation work with no code diff is a successful execution when it
    leaves valid pass/fail evidence; without evidence it fails closed but
    remains automatically recoverable. A fail verdict is a lifecycle signal
    that routes to repair, not an infrastructure failure. Pushing product
    changes from qualification mode is forbidden.
    """

    if pushed_product_changes or has_code_diff:
        return "forbidden", "qualification-mode-cannot-push-product-changes"
    if evidence_state == EVIDENCE_PASS:
        return "success", "pass-evidence-recorded"
    if evidence_state == EVIDENCE_FAIL:
        return "success", "fail-evidence-routes-to-repair"
    return "fail-closed", "missing-evidence-retry"
