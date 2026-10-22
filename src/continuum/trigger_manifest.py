"""Authoritative trigger-and-owner manifest for Continuum automatic wake-ups.

Phase A (continuum#343) inventories *every* current trigger surface before any
state-driven reconciliation can replace it.  The checked-in manifest in
``trigger_owner_manifest.json`` is machine-tested against the actual reusable
workflows, self-dogfood entry workflows, and installed caller stubs.  This
module only reads repository files: it never dispatches, writes, or mutates
GitHub state.

The manifest is intentionally an inventory, not a second controller.  Each
surface names the canonical policy that already owns the behavior and whether
the trigger carries event-payload fields that a future reconciliation must
derive from trusted state instead of from ``github.event``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


MANIFEST_PATH = Path(__file__).with_name("trigger_owner_manifest.json")

WORKFLOW_GLOB = ".github/workflows/*.yml"
STUB_GLOBS = (
    ".github/caller-stubs/*.yml",
    ".github/caller-stubs/tech/*.yml",
    ".github/caller-stubs/parent/*.yml",
)

_INLINE_LIST_KEYS = ("types", "workflows", "branches")
_LIST_KEYS = ("crons",)


class ManifestError(ValueError):
    """Raised when the manifest cannot be interpreted safely."""


@dataclass(frozen=True)
class ManifestFinding:
    """One diff finding between the manifest and the repository tree."""

    path: str
    message: str

    def __str__(self) -> str:
        return "{}: {}".format(self.path, self.message)


@dataclass(frozen=True)
class TriggerSurface:
    """One workflow or caller-stub trigger surface from the manifest."""

    path: str
    name: str
    layer: str
    installer_set: str
    triggers: Tuple[Mapping[str, Any], ...]
    jobs: Tuple[str, ...]
    owners: Tuple[str, ...]
    event_payload_dependent: bool = False
    job_owners: Mapping[str, str] = field(default_factory=dict)


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _parse_inline_list(value: str) -> List[str]:
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [_strip_quotes(part) for part in text.split(",") if part.strip()]


def extract_triggers(text: str) -> List[Dict[str, Any]]:
    """Extract the top-level ``on:`` trigger shape without a YAML dependency.

    Continuum workflows embed action scripts and block scalars that the
    dependency-free :mod:`continuum.yamlmini` subset intentionally rejects.
    Trigger ownership only needs the top-level event keys and their direct
    ``types``/``workflows``/``branches``/``cron`` filters, so this parser
    reads exactly that block and ignores the rest of the workflow.
    """

    lines = text.splitlines()
    start = None
    for index, raw in enumerate(lines):
        code = _strip_comment(raw)
        if code.strip() == "on:" and _indent(code) == 0:
            start = index + 1
            break
    if start is None:
        raise ManifestError("workflow has no top-level on: block")

    events: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    event_indent: Optional[int] = None
    list_key: Optional[str] = None

    for raw in lines[start:]:
        code = _strip_comment(raw)
        if not code.strip():
            continue
        indent = _indent(code)
        if indent == 0:
            break
        stripped = code.strip()
        if event_indent is None:
            event_indent = indent
        if indent == event_indent and not stripped.startswith("- "):
            key = stripped.split(":", 1)[0].strip()
            current = {"event": _strip_quotes(key)}
            events.append(current)
            list_key = "crons" if key == "schedule" else None
            continue
        if current is None:
            continue
        if stripped.startswith("- "):
            item = stripped[2:].strip()
            if list_key in ("crons", "workflows", "branches", "types"):
                value: Any = item
                if list_key == "crons" and item.startswith("cron:"):
                    value = _strip_quotes(item.split(":", 1)[1])
                current.setdefault(list_key, []).append(_strip_quotes(str(value)))
            continue
        if ":" not in stripped:
            continue
        key, value_text = stripped.split(":", 1)
        key = key.strip()
        value_text = value_text.strip()
        if key in _INLINE_LIST_KEYS and value_text:
            current[key] = _parse_inline_list(value_text)
            list_key = None
        elif key in ("workflows", "branches", "types") and not value_text:
            current.setdefault(key, [])
            list_key = key
        elif key == "schedule" and not value_text:
            list_key = "crons"
        elif key == "cron":
            current.setdefault("crons", []).append(_strip_quotes(value_text))
            list_key = None
        else:
            list_key = None
    return events


def _strip_comment(raw: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(raw):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return raw[:index]
    return raw


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def normalize_trigger(trigger: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize one trigger to an order-insensitive comparable mapping."""

    event = str(trigger.get("event") or "").strip()
    if not event:
        raise ManifestError("trigger is missing an event")
    normalized: Dict[str, Any] = {"event": event}
    for key in ("types", "workflows", "branches", "crons"):
        values = trigger.get(key)
        if values:
            normalized[key] = tuple(sorted(str(value) for value in values))
    return normalized


def trigger_signature(trigger: Mapping[str, Any]) -> Tuple[Any, ...]:
    """Hashable signature used for exact diffing."""

    normalized = normalize_trigger(trigger)
    return tuple(
        (key, normalized[key]) if key != "event" else ("event", normalized[key])
        for key in ("event", "types", "workflows", "branches", "crons")
        if key in normalized
    )


def load_manifest(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the checked-in manifest and perform structural checks."""

    source = Path(path) if path is not None else MANIFEST_PATH
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("cannot read trigger owner manifest: {}".format(exc)) from exc
    if not isinstance(payload, dict):
        raise ManifestError("trigger owner manifest must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ManifestError("unsupported trigger owner manifest schema version")
    if not isinstance(payload.get("owners"), list) or not payload["owners"]:
        raise ManifestError("trigger owner manifest has no owners")
    if not isinstance(payload.get("surfaces"), list) or not payload["surfaces"]:
        raise ManifestError("trigger owner manifest has no surfaces")
    owner_ids = {str(owner.get("id") or "") for owner in payload["owners"]}
    if "" in owner_ids or len(owner_ids) != len(payload["owners"]):
        raise ManifestError("trigger owner ids must be unique and non-empty")
    for owner in payload["owners"]:
        if not owner.get("canonical_policy"):
            raise ManifestError(
                "owner {} has no canonical policy".format(owner.get("id"))
            )
        if "event_payload_dependencies" not in owner:
            raise ManifestError(
                "owner {} does not declare event-payload dependencies".format(
                    owner.get("id")
                )
            )
        if not isinstance(owner.get("event_payload_dependencies"), list):
            raise ManifestError(
                "owner {} event-payload dependencies must be a list".format(
                    owner.get("id")
                )
            )
    seen_paths = set()
    for surface in payload["surfaces"]:
        path = str(surface.get("path") or "")
        if not path or path in seen_paths:
            raise ManifestError("surface paths must be unique and non-empty")
        seen_paths.add(path)
        if not isinstance(surface.get("event_payload_dependent"), bool):
            raise ManifestError(
                "surface {} must declare event_payload_dependent".format(path)
            )
        owners = [str(owner) for owner in (surface.get("owners") or [])]
        if not owners:
            raise ManifestError("surface {} has no owner".format(path))
        unknown = sorted(set(owners) - owner_ids)
        if unknown:
            raise ManifestError(
                "surface {} names unknown owner(s): {}".format(path, ", ".join(unknown))
            )
        job_owners = surface.get("job_owners") or {}
        for job, owner in job_owners.items():
            if str(owner) not in owner_ids:
                raise ManifestError(
                    "surface {} job {} names unknown owner {}".format(
                        path, job, owner
                    )
                )
    return payload


def manifest_surfaces(payload: Mapping[str, Any]) -> Dict[str, TriggerSurface]:
    """Index manifest surfaces by repository-relative path."""

    indexed: Dict[str, TriggerSurface] = {}
    for surface in payload.get("surfaces") or []:
        path = str(surface["path"])
        indexed[path] = TriggerSurface(
            path=path,
            name=str(surface.get("name") or ""),
            layer=str(surface.get("layer") or ""),
            installer_set=str(surface.get("installer_set") or ""),
            triggers=tuple(surface.get("triggers") or ()),
            jobs=tuple(str(job) for job in (surface.get("jobs") or ())),
            owners=tuple(str(owner) for owner in (surface.get("owners") or ())),
            event_payload_dependent=bool(surface.get("event_payload_dependent", False)),
            job_owners={
                str(job): str(owner)
                for job, owner in (surface.get("job_owners") or {}).items()
            },
        )
    return indexed


def repository_surfaces(root: Path) -> List[Path]:
    """Every workflow or caller stub that participates in the manifest."""

    root = Path(root)
    paths = sorted(root.glob(WORKFLOW_GLOB))
    for pattern in STUB_GLOBS:
        paths.extend(sorted(root.glob(pattern)))
    return paths


def expected_installer_set(path: str) -> str:
    """Installer set implied by the file's layer (verbatim install contract)."""

    if path.startswith(".github/caller-stubs/tech/"):
        return "tech"
    if path.startswith(".github/caller-stubs/parent/"):
        return "parent"
    if path.startswith(".github/caller-stubs/"):
        return "core"
    return "not-installed"


def validate_manifest(
    payload: Optional[Mapping[str, Any]] = None,
    root: Optional[Path] = None,
) -> List[ManifestFinding]:
    """Diff the manifest against the actual workflow/stub trigger blocks.

    Returns every finding instead of raising so CI can print the complete
    drift.  An empty list means every pre-existing trigger surface is
    accounted for exactly once and every owner reference resolves.
    """

    base = Path(root) if root is not None else MANIFEST_PATH.parents[2]
    data = dict(payload) if payload is not None else load_manifest()
    surfaces = manifest_surfaces(data)
    findings: List[ManifestFinding] = []

    actual_paths = repository_surfaces(base)
    actual_relative = {
        path.relative_to(base).as_posix(): path for path in actual_paths
    }
    for path in sorted(set(surfaces) - set(actual_relative)):
        findings.append(ManifestFinding(path, "manifest surface does not exist"))
    for path in sorted(set(actual_relative) - set(surfaces)):
        findings.append(ManifestFinding(path, "unmanifested workflow or caller stub"))

    for path, surface in sorted(surfaces.items()):
        actual = actual_relative.get(path)
        if actual is None:
            continue
        try:
            actual_triggers = extract_triggers(actual.read_text(encoding="utf-8"))
        except ManifestError as exc:
            findings.append(ManifestFinding(path, str(exc)))
            continue
        manifest_signatures = {
            trigger_signature(trigger) for trigger in surface.triggers
        }
        actual_signatures = {
            trigger_signature(trigger) for trigger in actual_triggers
        }
        for signature in sorted(manifest_signatures - actual_signatures):
            findings.append(
                ManifestFinding(path, "manifest trigger not present: {}".format(signature))
            )
        for signature in sorted(actual_signatures - manifest_signatures):
            findings.append(
                ManifestFinding(path, "unmanifested trigger: {}".format(signature))
            )
        actual_has_payload_dependency = any(
            str(trigger.get("event"))
            not in ("schedule", "workflow_dispatch", "workflow_call")
            for trigger in actual_triggers
        )
        if surface.event_payload_dependent != actual_has_payload_dependency:
            findings.append(
                ManifestFinding(
                    path,
                    "event_payload_dependent is {} but actual triggers imply {}".format(
                        surface.event_payload_dependent,
                        actual_has_payload_dependency,
                    ),
                )
            )
        expected_set = expected_installer_set(path)
        if surface.installer_set != expected_set:
            findings.append(
                ManifestFinding(
                    path,
                    "installer set is {} but the layer requires {}".format(
                        surface.installer_set, expected_set
                    ),
                )
            )
        for job, owner in surface.job_owners.items():
            if job not in surface.jobs:
                findings.append(
                    ManifestFinding(
                        path, "job owner names unknown job {}".format(job)
                    )
                )
                continue
            if owner not in surface.owners:
                findings.append(
                    ManifestFinding(
                        path,
                        "job {} owner {} is not a surface owner".format(job, owner),
                    )
                )
    return findings


def categories_covered(payload: Mapping[str, Any]) -> List[str]:
    """Return required owner categories that have no obvious owner mapping.

    The mapping is intentionally explicit and small: it is a guard against
    silently dropping a whole automatic path from the manifest, not a second
    policy engine.
    """

    owners = {str(owner.get("id") or "") for owner in payload.get("owners") or []}
    required = {
        "issue scheduler": {"issue-scheduler"},
        "watchdog": {"opencode-watchdog"},
        "review-ready": {"review-ready"},
        "CI repair": {"ci-repair"},
        "CodeRabbit queue/unresolved": {"coderabbit-queue", "coderabbit-unresolved"},
        "PR-Agent review/recovery/merge": {
            "pr-agent-review",
            "pr-agent-recovery",
            "pr-agent-merge",
        },
        "global watchdog": {"global-watchdog"},
        "qualifications": {"qualification", "contract-qualification"},
        "Parent/Child delegation": {"delegation"},
        "self-dogfood": {"validation"},
        "installer callers": {"validation"},
    }
    missing = []
    for category, expected in required.items():
        if not expected <= owners:
            missing.append(category)
    return missing


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point used by ``scripts/validate-continuum.sh``."""

    args = list(argv if argv is not None else [])
    if args and args[0] not in ("--check",):
        print("usage: python3 -m continuum.trigger_manifest [--check]")
        return 2
    payload = load_manifest()
    findings = validate_manifest(payload)
    missing = categories_covered(payload)
    for finding in findings:
        print("trigger-manifest: {}".format(finding))
    for category in missing:
        print("trigger-manifest: required category is not covered: {}".format(category))
    if findings or missing:
        return 1
    print(
        "trigger-manifest: {} surfaces and {} owners are in sync".format(
            len(payload["surfaces"]), len(payload["owners"])
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
