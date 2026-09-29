"""The human ledger is generated, so it cannot quietly disagree with the machine one.

A parity ledger that is maintained by hand in two formats is a ledger that will
eventually say one thing in Markdown and another in JSON, and the Markdown is the
one a human reads. So `docs/parity-ledger.md` is rendered from
`docs/parity-ledger.json` by this module, and a test refuses to start if the
committed file is not what this renderer produces.

Run it directly to refresh the file:

    PYTHONPATH=src python3 -m continuum.tools.parity_ledger
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

LEDGER_PATH = pathlib.Path(__file__).resolve().parents[3] / "docs" / "parity-ledger.json"
DOCUMENT_PATH = LEDGER_PATH.with_suffix(".md")

CLASSIFICATION_ORDER = (
    "must-port",
    "absorbed",
    "superseded",
    "not-applicable",
    "consumer-local",
)

CLASSIFICATION_LABEL = {
    "must-port": "Must port",
    "absorbed": "Absorbed",
    "superseded": "Superseded",
    "not-applicable": "Not applicable",
    "consumer-local": "Consumer-local",
}


def load(path: pathlib.Path = LEDGER_PATH) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _source_sentence(source: Dict[str, Any]) -> str:
    return (
        f"`{source['repository']}` over `{source['from'][:12]}..{source['to'][:12]}` "
        f"({source['commits']} commits, {source['diffstat']}), reading "
        f"{len(source['files'])} workflow files."
    )


def _group(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {name: [] for name in CLASSIFICATION_ORDER}
    for item in items:
        grouped.setdefault(item["classification"], []).append(item)
    return grouped


def _evidence_lines(item: Dict[str, Any]) -> List[str]:
    landed = item.get("landed") or {}
    tests = list(landed.get("tests", [])) or list(item.get("evidence", {}).get("tests", []))
    files = list(landed.get("files", [])) or list(item.get("evidence", {}).get("files", []))
    lines = []
    if files:
        shown = ", ".join(f"`{path}`" for path in files)
        lines.append(f"- Changed: {shown}.")
    if tests:
        shown = ", ".join(f"`{node}`" for node in tests)
        lines.append(f"- Tests: {shown}.")
    incident = item.get("incident")
    if incident:
        lines.append(f"- Incident: {incident}.")
    issues = item.get("issues") or item.get("evidence", {}).get("issues") or []
    if issues:
        shown = ", ".join(f"#{number}" for number in issues)
        lines.append(f"- Issues: {shown}.")
    return lines


def render(ledger: Dict[str, Any]) -> str:
    source = ledger["source"]
    summary = ledger["summary"]
    grouped = _group(ledger["items"])

    out: List[str] = []
    out.append("# Parity ledger")
    out.append("")
    out.append(
        "<!-- GENERATED FILE. Edit docs/parity-ledger.json and run "
        "`PYTHONPATH=src python3 -m continuum.tools.parity_ledger`; "
        "tests/test_parity_ledger.py refuses a stale copy. -->"
    )
    out.append("")
    out.append(
        f"Every semantic the source repository gained between those two commits, and "
        f"what Continuum did about it. The machine-readable form is "
        f"`{LEDGER_PATH.name}`; this file is rendered from it and is the one to read."
    )
    out.append("")
    out.append(_source_sentence(source))
    out.append("")

    out.append("## Why this exists")
    out.append("")
    out.append(
        "A consumer that adopts Continuum inherits nothing of the source "
        "repository's CI by accident. Every behaviour that repository gained is "
        "either reimplemented here in generic form, deliberately left to the "
        "consumer, or recorded as a decision not to have it. What must never "
        "happen is the third thing: a semantic that is simply not mentioned, which "
        "is indistinguishable from one nobody thought about."
    )
    out.append("")
    out.append("Each row therefore states a classification, a rationale, and where the proof is.")
    out.append("")

    out.append("## Classifications")
    out.append("")
    for name in CLASSIFICATION_ORDER:
        out.append(f"- **{CLASSIFICATION_LABEL[name]}** — {ledger['policy'][name]}")
    out.append("")

    out.append("## Summary")
    out.append("")
    out.append("| Classification | Count |")
    out.append("| --- | --- |")
    for name in CLASSIFICATION_ORDER:
        out.append(f"| {CLASSIFICATION_LABEL[name]} | {summary['by_classification'][name]} |")
    out.append(f"| **Total** | **{summary['total']}** |")
    out.append("")
    out.append(
        f"{summary['must_port_landed']} of {summary['must_port_landed']} must-port items "
        "are landed, each with a regression test tied to the incident that produced it."
    )
    out.append("")

    for name in CLASSIFICATION_ORDER:
        items = grouped.get(name) or []
        if not items:
            continue
        out.append(f"## {CLASSIFICATION_LABEL[name]}")
        out.append("")
        for item in items:
            out.append(f"### {item['id']} — {item['summary']}")
            out.append("")
            out.append(f"- Surface: `{item['surface']}`")
            out.append(
                "- Source: "
                + ", ".join(f"`{commit}`" for commit in item["source_commits"])
            )
            out.append("")
            out.append(item["rationale"])
            out.append("")
            lines = _evidence_lines(item)
            if lines:
                out.extend(lines)
                out.append("")

    out.append("## Cutover")
    out.append("")
    out.append(
        "Neither toggle is enabled by this audit. The parent issue requires an "
        "explicit decision to turn either on, and all must-port items are landed, so "
        "both are now *eligible* for that decision — which is a separate change with "
        "its own review."
    )
    out.append("")
    out.append("---")
    out.append("")
    out.append(f"Schema `{ledger['schema']}`, version {ledger['version']}, updated {ledger['updated']}.")
    out.append("")
    return "\n".join(out)


def write(path: pathlib.Path = DOCUMENT_PATH, source: pathlib.Path = LEDGER_PATH) -> None:
    path.write_text(render(load(source)), encoding="utf-8")


def main() -> int:
    write()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
