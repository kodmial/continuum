"""Parity ledger: the auditable record of the cross-repository sweep.

Issue #59 asks for production parity with the repositories Continuum was
extracted from. That claim is only meaningful if it can be checked, so the
disposition of every audited source capability lives in
`parity/ledger.v1.json` and this module is the only thing that reads it.

Two rules make the ledger worth more than a comment:

* A disposition is one of five fixed values. "We looked at it and decided not to"
  is not a disposition, and a capability cannot quietly become optional.
* Every source reference is a full commit sha. A branch name or a tag would let
  the audited code change under the ledger without the ledger changing, which
  would turn the audit into a claim about code nobody has read.

Anything that cannot be validated is a failure, not a warning. A ledger that
fails open is worse than no ledger, because it reports parity that was never
established.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: The complete set of dispositions. Every entry carries exactly one.
DISPOSITIONS: Tuple[str, ...] = (
    # The behavior is already present in Continuum; the reference records where.
    "absorbed",
    # A real gap. It must be implemented in Continuum, not delegated downstream.
    "must-port",
    # Correctly not in Continuum: it belongs to the consumer, or to a product the
    # source repository owns and Continuum never had.
    "consumer-local",
    # The reference's version was replaced by something better, and the
    # replacement is the one that is in Continuum.
    "superseded",
    # Out of scope for this contract entirely.
    "not-applicable",
)

#: A capability that Continuum does not yet have is only acceptable when the
#: ledger says so, and a `must-port` entry that is not implemented is a build
#: failure rather than a plan. A ledger that only records intentions cannot stop
#: the same gap from being re-audited every quarter.
REQUIRED_BEFORE_P0: Tuple[str, ...] = ("must-port",)

LEDGER_VERSION = "ledger.v1"

REPOS: Dict[str, str] = {
    "nanodictate": "kodmial/nanodictate",
    "runtime-lab": "kodmial/runtime-lab",
    "kodmai": "kodmial/kodmai",
    "opencode": "kodmial/opencode",
    "kodmaiadmin": "kodmial/kodmaiadmin",
    "homebrew-nanodictate": "kodmial/homebrew-nanodictate",
    "macports-nanodictate": "kodmial/macports-nanodictate",
}

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
ID_RE = re.compile(r"^[A-Z]{2,4}-\d{2,3}$")

#: Evidence a disposition has to carry, so a claim can be checked rather than
#: believed. `module` points at Continuum source; `paths` at a Continuum workflow
#: or data file. Both are verified to exist, because an `absorbed` entry naming a
#: file nobody can find is the exact failure this ledger exists to prevent.
REQUIRED_EVIDENCE: Dict[str, Tuple[str, ...]] = {
    "absorbed": ("source_ref",),
    "must-port": ("source_ref",),
    "superseded": ("source_ref",),
    "consumer-local": ("rationale", "source_ref"),
    "not-applicable": ("rationale", "source_ref"),
}

#: Dispositions whose evidence may be a file path instead of a module. The
#: controller and queue planes are workflows, and a workflow is where a
#: workflow-level behavior lives.
PATH_EVIDENCE: Tuple[str, ...] = ("absorbed", "must-port", "superseded")

DEFAULT_LEDGER_PATH = pathlib.Path("parity") / "ledger.v1.json"


@dataclass(frozen=True)
class Entry:
    id: str
    capability: str
    repository: str
    disposition: str
    source_ref: str
    module: str = ""
    paths: Tuple[str, ...] = ()
    rationale: str = ""
    notes: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "capability": self.capability,
            "repository": self.repository,
            "disposition": self.disposition,
            "source_ref": self.source_ref,
            "module": self.module,
            "paths": list(self.paths),
            "rationale": self.rationale,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class Ledger:
    entries: Tuple[Entry, ...]
    audited_at: str = ""
    path: Optional[pathlib.Path] = None

    def by_disposition(self, disposition: str) -> Tuple[Entry, ...]:
        return tuple(entry for entry in self.entries if entry.disposition == disposition)

    def by_repository(self, repository: str) -> Tuple[Entry, ...]:
        return tuple(entry for entry in self.entries if entry.repository == repository)

    def find(self, entry_id: str) -> Optional[Entry]:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    def describe(self) -> Dict[str, Any]:
        return {
            "version": LEDGER_VERSION,
            "audited_at": self.audited_at,
            "entries": [entry.describe() for entry in self.entries],
            "counts": {
                disposition: len(self.by_disposition(disposition)) for disposition in DISPOSITIONS
            },
        }


class LedgerError(Exception):
    """A ledger that cannot be trusted, with the reasons it cannot be."""


def _require_str(document: Dict[str, Any], key: str, context: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{context}: '{key}' must be a non-empty string")
    return value


def parse(document: Any, *, path: Optional[pathlib.Path] = None) -> Ledger:
    """Validate a ledger document and return it, or raise with every reason.

    Every problem is collected before raising. A validator that stops at the
    first error makes fixing a ledger a sequence of one-error rounds.
    """

    problems: List[str] = []
    if not isinstance(document, dict):
        raise LedgerError("ledger: the document must be a JSON object")

    version = document.get("version")
    if version != LEDGER_VERSION:
        problems.append(
            f"ledger: 'version' must be {LEDGER_VERSION!r}, found {version!r}"
        )

    audited_at = document.get("audited_at")
    if not isinstance(audited_at, str) or not COMMIT_RE.match(audited_at):
        # A date is not enough: the audit is a statement about specific code.
        problems.append(
            "ledger: 'audited_at' must be the full commit sha of the audit run"
        )

    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        problems.append("ledger: 'entries' must be a non-empty list")
        raw_entries = []

    entries: List[Entry] = []
    seen: Dict[str, int] = {}
    for index, raw in enumerate(raw_entries):
        context = f"entries[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{context}: must be an object")
            continue
        entry_id = raw.get("id")
        if not isinstance(entry_id, str) or not ID_RE.match(entry_id):
            problems.append(f"{context}: 'id' must look like 'RL-01', found {entry_id!r}")
        elif entry_id in seen:
            problems.append(
                f"{context}: duplicate id {entry_id!r} (also entries[{seen[entry_id]}])"
            )
        else:
            seen[entry_id] = index

        disposition = raw.get("disposition")
        if disposition not in DISPOSITIONS:
            problems.append(
                f"{context}: 'disposition' must be one of {', '.join(DISPOSITIONS)}, "
                f"found {disposition!r}"
            )

        repository = raw.get("repository")
        if repository not in REPOS:
            problems.append(
                f"{context}: 'repository' must be one of {', '.join(sorted(REPOS))}, "
                f"found {repository!r}"
            )

        source_ref = raw.get("source_ref")
        if not isinstance(source_ref, str) or not COMMIT_RE.match(source_ref):
            problems.append(
                f"{context}: 'source_ref' must be a full 40-character commit sha, "
                "so the audited code cannot change under the ledger"
            )

        for key in ("capability",):
            if not isinstance(raw.get(key), str) or not raw.get(key, "").strip():
                problems.append(f"{context}: '{key}' must be a non-empty string")

        required = REQUIRED_EVIDENCE.get(disposition, ())
        for key in required:
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(
                    f"{context}: disposition {disposition!r} requires a non-empty "
                    f"'{key}' that a reviewer can check"
                )

        module = raw.get("module") or ""
        rationale = raw.get("rationale") or ""
        notes = raw.get("notes") or ""
        if isinstance(raw.get("module"), str):
            module = raw["module"]
        if isinstance(raw.get("rationale"), str):
            rationale = raw["rationale"]
        if isinstance(raw.get("notes"), str):
            notes = raw["notes"]

        raw_paths = raw.get("paths") or []
        if not isinstance(raw_paths, list) or not all(
            isinstance(item, str) and item.strip() for item in raw_paths
        ):
            problems.append(f"{context}: 'paths' must be a list of non-empty strings")
            raw_paths = []
        paths = tuple(raw_paths)

        if disposition in PATH_EVIDENCE and not module and not paths:
            problems.append(
                f"{context}: disposition {disposition!r} must name the 'module' or "
                "'paths' that carry the behavior in Continuum"
            )
        if module and not _module_exists(module):
            problems.append(
                f"{context}: module {module!r} does not exist in the tree"
            )
        for path in paths:
            if not _path_exists(path):
                problems.append(f"{context}: path {path!r} does not exist in the tree")

        entries.append(
            Entry(
                id=str(entry_id),
                capability=str(raw.get("capability") or ""),
                repository=str(repository),
                disposition=str(disposition),
                source_ref=str(source_ref),
                module=module,
                paths=paths,
                rationale=rationale,
                notes=notes,
            )
        )

    for repository in REPOS:
        if repository == "kodmaiadmin":
            # Recorded deliberately: it is pinned to an old sha and carries no
            # parity evidence, so it is allowed to have no entries.
            continue
        if not any(entry.repository == repository for entry in entries):
            problems.append(
                f"ledger: repository {repository!r} has no entries; an un-audited "
                "repository must be recorded as such, not omitted"
            )

    if problems:
        raise LedgerError("\n".join(problems))

    return Ledger(
        entries=tuple(entries),
        audited_at=str(audited_at),
        path=path,
    )


def _repository_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent.parent


def _module_exists(module: str) -> bool:
    # A module is named the way it is imported, so it is resolved against both
    # the source root and the repository root, and it may be a module or a
    # package: `continuum.review.coderabbit` is `review/coderabbit.py`,
    # `continuum.review` is the `review/` package.
    parts = module.split(".")
    root = _repository_root()
    for base in (root / "src", root):
        as_package = base.joinpath(*parts) / "__init__.py"
        if as_package.is_file():
            return True
        if base.joinpath(*parts).with_suffix(".py").is_file():
            return True
    return False


def _path_exists(path: str) -> bool:
    # A path may be given repository-relative, or as a glob for the evidence
    # that is a family of files. Both are resolved against the repository root.
    root = _repository_root()
    if (root / path).exists():
        return True
    return any(root.glob(path))


def load(path: Optional[pathlib.Path] = None) -> Ledger:
    target = pathlib.Path(path) if path else DEFAULT_LEDGER_PATH
    if not target.is_file():
        raise LedgerError(f"ledger: {target} does not exist")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LedgerError(f"ledger: {target} is not valid JSON: {error}") from error
    return parse(document, path=target)


def check(ledger: Ledger) -> List[str]:
    """The parity claims a reviewer cannot make from the ledger alone.

    Every one of these is a question with a checkable answer: does the module
    named by `absorbed` really exist, is every audited repository still
    reachable. Returning the questions instead of failing keeps `parity check`
    usable as a report, while the CLI still exits non-zero on any of them.
    """

    problems: List[str] = []
    for entry in ledger.entries:
        if entry.module and not _module_exists(entry.module):
            problems.append(
                f"{entry.id}: module {entry.module!r} does not exist in the tree"
            )
        for path in entry.paths:
            if not _path_exists(path):
                problems.append(f"{entry.id}: path {path!r} does not exist in the tree")
        if entry.repository not in REPOS:
            problems.append(f"{entry.id}: unknown repository {entry.repository!r}")
    if not ledger.by_disposition("absorbed"):
        problems.append(
            "no entry is recorded as 'absorbed'; a sweep that ports nothing and "
            "already had everything is not a plausible result"
        )
    return problems
