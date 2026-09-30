"""One run's evidence, rendered for a person who did not write the run.

A shadow run's evidence is a directory of JSON documents, which is complete and
unreadable at the same time: the run that decided a pull request may merge
carries a journal, a parity result, a liveness report, a barrier report and a
case file, and the three questions a reviewer has -- what was decided, did it
match production, and did the run stay live -- are spread across them. So the
reviewer either downloads the artifact and reads all of it, or reads nothing and
takes the green tick on trust.

This module renders those documents as a markdown job summary, from the same
documents the cutover judge reads. That is the point: what a reviewer reads in
the summary is not a second opinion about the run, it is the run's own evidence
rendered differently, so the two cannot drift.

The one rule it does not bend is that absence is reported as absence. A run with
no parity document says the parity was not evaluated; it does not say the run
matched production, and it does not say nothing. Those are the two states a
window most needs to tell apart, because one is a result and the other is an
absence of one, and only the first justifies a cutover.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


def _documents(directory: Path, subdirectory: str) -> List[Mapping[str, Any]]:
    """Read every JSON document in ``directory/subdirectory``, sorted by name.

    A file that does not parse is skipped rather than raised on: this renders
    evidence, and a summary that fails to render is worse than a summary with
    one fewer line in it. The parse failure is not hidden -- it becomes a line of
    its own.
    """

    found: List[Mapping[str, Any]] = []
    root = directory / subdirectory
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            found.append({"_unreadable": "{}: {}".format(path.name, error)})
            continue
        if isinstance(document, dict):
            document.setdefault("_artifact", path.relative_to(directory).as_posix())
            found.append(document)
    return found


def _first_document(path: Path) -> Optional[Mapping[str, Any]]:
    """Read one JSON document, or ``None`` if it is absent or unreadable.

    Same contract as :func:`_documents` for a file that does not parse: this
    renders evidence, so a document it cannot read becomes "not recorded" rather
    than an exception that costs the reviewer the rest of the summary.
    """

    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _first_line(text: Any) -> str:
    value = str(text or "").strip()
    return value if value else "_not recorded_"


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> List[str]:
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def _event_row(journal: Mapping[str, Any]) -> List[str]:
    event = journal.get("event")
    event = event if isinstance(event, Mapping) else {}
    return [
        # The scenario, not the event type: the eight scenario classes are what
        # a cutover window is measured in, and a reviewer asking "did we cover
        # the coderabbit path" needs the class, not the webhook that fed it.
        str(journal.get("scenario") or event.get("event_type") or journal.get("origin") or ""),
        str(event.get("event_type") or ""),
        "#{}".format(event["number"]) if event.get("number") else "-",
        str(journal.get("status") or ""),
    ]


def _guard_rows(journal: Mapping[str, Any]) -> List[List[str]]:
    rows: List[List[str]] = []
    for guard in journal.get("guards") or ():
        if not isinstance(guard, Mapping):
            continue
        rows.append(
            [
                str(guard.get("name") or ""),
                str(guard.get("outcome") or ""),
                str(guard.get("source") or ""),
                _first_line(
                    guard.get("reason")
                    or _detail_reason(guard.get("detail"))
                ),
            ]
        )
    return rows


def _detail_reason(detail: Any) -> str:
    """The one part of a guard's detail a reader needs, when it has no reason.

    A guard's detail is free-form, so there is no key to reach for. But a guard
    that concluded something always leaves the thing it concluded about in there
    -- the check that failed, the policy that denied -- and a table cell reading
    "not recorded" next to a guard that actually decided is worse than useless.
    """

    if not isinstance(detail, Mapping):
        return ""
    for key in ("reason", "message", "conclusion", "blocking", "blocked_by", "check", "policy"):
        if detail.get(key):
            return _first_line(detail[key])
    return ""


def _action_rows(journal: Mapping[str, Any]) -> List[List[str]]:
    """One row per effect, marked with what the barrier did to it.

    A journal lists every effect twice in shadow mode: once as *planned* and once
    as *suppressed*, because shadow mode suppresses all of them. Rendering both
    lists would print every row twice, and a table that says the same thing
    twice reads as two different effects -- so the union is taken and the
    suppressed marking wins, since "this is what would have happened, and it did
    not" is the whole content of a shadow run.
    """

    ordered: List[Tuple[str, ...]] = []
    rows: Dict[Tuple[str, ...], List[str]] = {}
    for key in ("actions", "suppressed"):
        for effect in journal.get(key) or ():
            if not isinstance(effect, Mapping):
                continue
            identity = (
                str(effect.get("kind") or ""),
                str(effect.get("target") or ""),
                json.dumps(effect.get("detail") or {}, sort_keys=True, default=str),
            )
            state = "suppressed" if key == "suppressed" else "planned"
            if identity in rows:
                if state == "suppressed":
                    rows[identity][2] = "suppressed"
                continue
            rows[identity] = [
                identity[0],
                identity[1],
                state,
                _first_line(effect.get("reason")),
            ]
            ordered.append(identity)
    return [rows[identity] for identity in ordered]


def _parity_lines(directory: Path) -> List[str]:
    documents = _documents(directory, "parity")
    if not documents:
        return [
            "_No parity document was written, so this run was not compared with "
            "the production outcome. That is not the same as matching it._"
        ]
    lines: List[str] = []
    for document in documents:
        if "_unreadable" in document:
            lines.append("- a parity document could not be read: {}".format(document["_unreadable"]))
            continue
        lines.append(
            "- **{}** — {} (`{}`)".format(
                document.get("classification", "?"),
                _first_line(document.get("summary")),
                document.get("_artifact", ""),
            )
        )
        for difference in document.get("differences") or ():
            if isinstance(difference, Mapping):
                lines.append(
                    "  - {}: expected {}, observed {}".format(
                        difference.get("field") or difference.get("kind") or "difference",
                        difference.get("expected", "-"),
                        difference.get("observed", "-"),
                    )
                )
    return lines


def _liveness_lines(directory: Path) -> List[str]:
    documents = _documents(directory, "liveness")
    if not documents:
        return ["_No liveness report was written for this run._"]
    lines: List[str] = []
    for document in documents:
        if "_unreadable" in document:
            lines.append("- a liveness report could not be read: {}".format(document["_unreadable"]))
            continue
        counts = document.get("by_verdict") or {}
        answered = "yes" if document.get("clean") else "no"
        lines.append(
            "- answered **{}**; answer rate {}; clean **{}** ({})".format(
                answered,
                document.get("answer_rate", "-"),
                answered,
                ", ".join(
                    "{} {}".format(counts[key], key) for key in sorted(counts)
                )
                or "no runs",
                )
        )
        for failure in document.get("failures") or ():
            if isinstance(failure, Mapping):
                lines.append(
                    "  - {} ({}): {}".format(
                        failure.get("verdict", "failure"),
                        failure.get("correlation_id", "?"),
                        _first_line(failure.get("reason")),
                    )
                )
        for abandoned in document.get("abandoned") or ():
            if isinstance(abandoned, Mapping):
                lines.append(
                    "  - abandoned, not counted either way ({}): {}".format(
                        abandoned.get("correlation_id", "?"),
                        _first_line(abandoned.get("reason")),
                    )
                )
    return lines


def _barrier_lines(directory: Path) -> List[str]:
    documents = _documents(directory, "barrier")
    if not documents:
        return []
    lines: List[str] = []
    for document in documents:
        if "_unreadable" in document:
            lines.append("- a barrier report could not be read: {}".format(document["_unreadable"]))
            continue
        performed = document.get("performed")
        attempted = document.get("attempted")
        violations = document.get("violations") or []
        lines.append(
            "- attempted {} write path(s); performed **{}**; violations **{}**".format(
                len(attempted) if isinstance(attempted, list) else "-",
                performed if performed in (0, None) else performed,
                len(violations),
            )
        )
        for violation in violations:
            if isinstance(violation, Mapping):
                lines.append(
                    "  - {}: {}".format(
                        violation.get("method", "?"), _first_line(violation.get("reason"))
                    )
                )
    return lines


def run_summary(directory: Path, artifact: str = "") -> str:
    """Render ``directory``'s evidence as the markdown a job summary shows."""

    directory = Path(directory)
    journals = [item for item in _documents(directory, "journals") if "_unreadable" not in item]

    lines: List[str] = ["## Continuum shadow run", ""]
    if artifact:
        lines.append("Evidence: artifact `{artifact}` ({path}/).".format(artifact=artifact, path=directory.as_posix()))
        lines.append("")

    if not journals:
        lines.append(
            "_This run wrote no journal, so it decided nothing. Everything below "
            "is what the run recorded about itself._"
        )
    else:
        lines.extend(
            _table(
                ("scenario", "event", "target", "status"),
                [_event_row(item) for item in journals],
            )
        )
        lines.append("")
        for journal in journals:
            diagnostics = journal.get("diagnostics")
            diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
            lines.append(
                "**{}** — {} ({})".format(
                    journal.get("decision") or "no decision",
                    _first_line(journal.get("reason")),
                    diagnostics.get("continuum_sha", "")[:12] or "no engine sha",
                )
            )
            lines.append("")
            if journal.get("errors"):
                for error in journal["errors"]:
                    if isinstance(error, Mapping):
                        lines.append(
                            "- error `{}`: {}".format(
                                error.get("code", "?"), _first_line(error.get("message"))
                            )
                        )
                lines.append("")
            guards = _guard_rows(journal)
            if guards:
                lines.append("| guard | outcome | owner | reason |")
                lines.append("|---|---|---|---|")
                for row in guards:
                    lines.append("| " + " | ".join(row) + " |")
                lines.append("")
            actions = _action_rows(journal)
            if actions:
                lines.append("| effect | target | recorded as | why |")
                lines.append("|---|---|---|---|")
                for row in actions:
                    lines.append("| " + " | ".join(row) + " |")
                lines.append("")

    lines.append("### Parity with the production outcome")
    lines.append("")
    lines.extend(_parity_lines(directory))
    lines.append("")
    lines.append("### Liveness")
    lines.append("")
    lines.extend(_liveness_lines(directory))
    lines.append("")
    barrier = _barrier_lines(directory)
    if barrier:
        lines.append("### Write barrier")
        lines.append("")
        lines.extend(barrier)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _window_rows(ledger: Mapping[str, Any]) -> List[List[str]]:
    """One row per correlation window, open and closed alike.

    A window that is still open is not a smaller result than a closed one; it is
    the state the observer is in most of the time, because production reaches a
    terminal state hours after the run that started watching it ended. Rendering
    it as its own row is what stops "the observer has not finished" from being
    read as "the observer found nothing".
    """

    rows: List[List[str]] = []
    windows = ledger.get("windows")
    if not isinstance(windows, list):
        return rows
    for window in windows:
        if not isinstance(window, Mapping):
            continue
        closed = window.get("closed_at")
        missing = window.get("missing") or []
        limits = window.get("limits") or []
        rows.append(
            [
                str(window.get("scenario") or "unspecified"),
                str(window.get("subject") or "?"),
                "closed" if closed else "open",
                ", ".join(str(item) for item in missing) or "-",
                ", ".join(str(item) for item in limits) or "-",
            ]
        )
    return rows


def _observation_rows(observation: Mapping[str, Any]) -> List[List[str]]:
    """One row per outcome the observer is willing to stand behind."""

    rows: List[List[str]] = []
    outcomes = observation.get("outcomes")
    if not isinstance(outcomes, list):
        return rows
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            continue
        links = outcome.get("links")
        links = links if isinstance(links, Mapping) else {}
        actions = outcome.get("actions")
        actions = actions if isinstance(actions, list) else []
        rows.append(
            [
                str(outcome.get("correlation_id") or "?"),
                str(links.get("scenario") or "unspecified"),
                str(outcome.get("fidelity") or "unevaluable"),
                ", ".join(
                    "{} {}".format(item.get("kind") or "?", item.get("target") or "").strip()
                    for item in actions
                    if isinstance(item, Mapping)
                )
                or "_nothing observed_",
            ]
        )
    return rows


def _reconciliation_rows(reconciliation: Mapping[str, Any]) -> List[List[str]]:
    rows: List[List[str]] = []
    pairings = reconciliation.get("pairings")
    if not isinstance(pairings, list):
        return rows
    for pairing in pairings:
        if not isinstance(pairing, Mapping):
            continue
        rows.append(
            [
                str(pairing.get("correlation_id") or "?"),
                str(pairing.get("verdict") or "?"),
                str(pairing.get("classification") or "-"),
            ]
        )
    return rows


def observation_summary(directory: Path, artifact: str = "") -> str:
    """Render what the observer read: its ledger, its outcomes, its pairings.

    This is the counterpart to :func:`run_summary`. A run summary answers "what
    did Continuum decide and did production agree"; this one answers "what did
    the observer independently see, and has every decision been compared against
    it yet". The two are rendered from different documents on purpose, because
    they are claims with different authors: a journal is written by the process
    being judged, and an outcome is written by the process doing the looking.
    """

    directory = Path(directory)
    root = directory / "observer"
    ledger = _first_document(root / "ledger.json")
    # Outcomes sit beside the ledger's directory rather than inside it, because
    # that is where `observe` writes them and where the workflow's carry-forward
    # glob looks for them. Reading from a second place would render an empty table
    # for a pass that plainly recorded one.
    outcomes = _documents(directory, "outcomes")
    reconciliation = _first_document(root / "reconciliation.json")
    readable = [item for item in outcomes if "_unreadable" not in item]

    lines: List[str] = ["## Continuum shadow observer", ""]
    if artifact:
        lines.append(
            "Observation: artifact `{artifact}` ({path}/).".format(
                artifact=artifact, path=directory.as_posix()
            )
        )
        lines.append("")

    if ledger is None:
        lines.append(
            "_This pass wrote no ledger, so nothing was observed. Everything below "
            "is what the pass recorded about itself._"
        )
    else:
        windows = ledger.get("windows")
        windows = windows if isinstance(windows, list) else []
        open_windows = [
            item
            for item in windows
            if isinstance(item, Mapping) and not item.get("closed_at")
        ]
        lines.append(
            "Repository `{}` — {} window(s), {} still open, {} outcome(s) carried forward.".format(
                ledger.get("repository") or "?",
                len(windows),
                len(open_windows),
                len(readable),
            )
        )
        lines.append("")
        lines.extend(
            _table(
                ("scenario", "subject", "state", "not yet observed", "not observable"),
                _window_rows(ledger),
            )
        )
        lines.append("")

    lines.append("### What production did")
    lines.append("")
    if not readable:
        lines.append(
            "_No outcome was emitted. That is not the same as production having "
            "done nothing: a window that is still open, or a read that failed, "
            "both produce no outcome, and both are listed above._"
        )
    else:
        lines.extend(
            _table(
                ("correlation", "scenario", "fidelity", "actions"),
                _observation_rows({"outcomes": readable}),
            )
        )
    lines.append("")

    lines.append("### Pairing with Continuum's decisions")
    lines.append("")
    if reconciliation is None:
        lines.append(
            "_No reconciliation pass has been recorded, so no decision has been "
            "compared against what the observer read yet._"
        )
    else:
        lines.append(
            "{} journal(s), {} paired, {} not yet observable, {} window(s) still open.".format(
                reconciliation.get("journals", 0),
                reconciliation.get("paired", 0),
                reconciliation.get("not_yet_observable", 0),
                reconciliation.get("windows_still_open", 0),
            )
        )
        lines.append("")
        lines.extend(
            _table(
                ("correlation", "verdict", "classification"),
                _reconciliation_rows(reconciliation),
            )
        )
    lines.append("")
    barrier = _barrier_lines(directory)
    if barrier:
        lines.append("### Write barrier")
        lines.append("")
        lines.extend(barrier)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def window_summary(directory: Path, artifact: str = "") -> str:
    """Render a judged window: the cutover decision, its blockers, and the rest.

    The window summary leads with the decision because that is the question a
    reviewer opens the page to ask, and it lists blockers before coverage because
    a blocker is the reason a decision went the way it did. Coverage is a table
    rather than a dump: at cutover scale a window is thousands of runs, and
    pasting them would bury the answer the reviewer came for.
    """

    directory = Path(directory)
    lines: List[str] = ["## Continuum shadow window", ""]
    if artifact:
        lines.append(
            "Evidence: artifact `{artifact}` ({path}/).".format(
                artifact=artifact, path=directory.as_posix()
            )
        )
        lines.append("")

    decision: Optional[Mapping[str, Any]] = None
    for document in _documents(directory, "cutover"):
        if document.get("kind") == "decision" or "approved" in document:
            decision = document
            break

    if decision is None:
        lines.append("_No cutover decision was written for this window._")
        lines.append("")
    else:
        lines.append("Cutover: {}.".format(_cutover_headline(decision)))
        lines.append("")
        lines.append(
            "Evidence digest `{}`, window {} to {}.".format(
                str(decision.get("evidence_digest", ""))[:16] or "none",
                decision.get("window_started_at", "?") or "?",
                decision.get("window_ended_at", "?") or "?",
            )
        )
        lines.append("")
        blockers = decision.get("blockers") or []
        if blockers:
            lines.append("| blocker | scenario | why |")
            lines.append("|---|---|---|")
            for blocker in blockers:
                if isinstance(blocker, Mapping):
                    lines.append(
                        "| `{}` | {} | {} |".format(
                            blocker.get("code", "?"),
                            blocker.get("scenario") or "-",
                            _first_line(blocker.get("message")),
                        )
                    )
            lines.append("")
        approval = decision.get("approval")
        if isinstance(approval, Mapping) and approval.get("approved_by"):
            lines.append(
                "Approved by `{who}` against digest `{digest}`.".format(
                    who=approval.get("approved_by"),
                    digest=str(approval.get("evidence_digest", ""))[:16] or "none",
                )
            )
            lines.append("")

    for document in _documents(directory, "liveness"):
        if "_unreadable" in document:
            continue
        lines.append(
            "Liveness: answer rate {}, clean **{}**.".format(
                document.get("answer_rate", "-"),
                "yes" if document.get("clean") else "no",
            )
        )
        lines.append("")

    coverage = decision.get("coverage") if decision else None
    scenarios = coverage.get("scenarios") if isinstance(coverage, Mapping) else None
    if isinstance(scenarios, list) and scenarios:
        lines.append("### Scenario coverage")
        lines.append("")
        lines.append(
            "{} of {} required scenarios live in this window.".format(
                coverage.get("covered", "?"), coverage.get("required", "?")
            )
        )
        lines.append("")
        lines.extend(
            _table(
                ("scenario", "state", "events", "live", "replayed", "classifications"),
                [
                    [
                        str(item.get("scenario", "?")),
                        str(item.get("state", "?")),
                        str(item.get("events", 0)),
                        str(item.get("live_events", 0)),
                        str(item.get("replayed_events", 0)),
                        ", ".join(
                            "{} {}".format(count, name)
                            for name, count in sorted(
                                (item.get("classifications") or {}).items()
                            )
                        )
                        or "-",
                    ]
                    for item in scenarios
                    if isinstance(item, Mapping)
                ],
            )
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _cutover_headline(decision: Mapping[str, Any]) -> str:
    """One sentence, in the gate's own three states.

    ``ready`` and ``approved`` are separate, and conflating them is the mistake
    this plane exists to prevent: evidence that is sufficient is not a decision
    that was taken, and a summary that said "approved" for a window with no
    approval recorded would be the summary inventing the cutover.
    """

    if decision.get("approved"):
        return "**approved** against the evidence digest"
    if decision.get("ready"):
        return "**evidence sufficient, awaiting a recorded approval**"
    codes = [
        str(item.get("code"))
        for item in decision.get("blockers") or ()
        if isinstance(item, Mapping)
    ]
    return "**not approved** — " + (", ".join(codes) if codes else "no reason recorded")
