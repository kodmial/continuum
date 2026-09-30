"""Canonical Continuum label registry for the issue plane.

The dispatch signal used to be the ``/oc`` issue comment. That made a comment
body a privileged input, so anybody who could comment could steer a
write-capable agent, and the activation itself was invisible in the issue's
metadata. The activation is now a single label, owned here, and every component
that has to recognise it -- the scheduler that provisions it, the scheduler that
filters candidates on it, and the trust policy that authorises a dispatch for
it -- reads the name from this module instead of repeating a literal.

Two properties are load-bearing:

* **One name, one place.** ``DISPATCH_LABEL`` is the only definition of the
  activation signal. A literal elsewhere is a drift bug, and
  ``.github/tests/test_continuum_labels.py`` fails when one appears.
* **Fail closed.** ``resolve_label`` returns ``None`` for anything that is not
  a known registry key or a known canonical name. Callers treat that as "not the
  dispatch label" rather than guessing, so an unparseable label can never
  activate work.

Repair-plane labels (``no-auto-merge``, ``opencode-conflict-repair`` and
friends) are deliberately *not* here. They are owned by
``.github/scripts/conflict_repair.py``, describe a change that already exists
rather than a task waiting to start, and are reconciled by a different control
plane with different authority.
"""

import argparse
import dataclasses
import json
import sys
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "DISPATCH_LABEL",
    "IN_PROGRESS_LABEL",
    "PAUSED_LABEL",
    "PRIORITY_HIGH_LABEL",
    "PRIORITY_NORMAL_LABEL",
    "PRIORITY_LOW_LABEL",
    "PRIORITY_LABELS",
    "ISSUE_PLANE_LABELS",
    "LabelSpec",
    "LABEL_SPECS",
    "dispatch_labels_json",
    "as_env",
    "as_json",
    "provision_plan",
    "resolve_label",
    "is_dispatch_label",
]

#: The canonical hand-off signal. Applying it to an issue is the request to
#: start work; removing it withdraws a request that has not started yet.
DISPATCH_LABEL = "continuum:dispatch"

#: Reservation held for the lifetime of one agent attempt. Written by the
#: scheduler before it dispatches, released by the repair controller when the
#: run ends or is abandoned.
IN_PROGRESS_LABEL = "automation:in-progress"

#: Terminal-but-restartable state for an issue that failed its attempt budget or
#: was stopped by a human. A paused issue is never a dispatch candidate.
PAUSED_LABEL = "automation:paused"

PRIORITY_HIGH_LABEL = "priority:p0"
PRIORITY_NORMAL_LABEL = "priority:p1"
PRIORITY_LOW_LABEL = "priority:p2"

#: Ordered highest priority first. The scheduler keeps this order in one place so
#: the ranking cannot drift between the eligibility filter and the retry path.
PRIORITY_LABELS: Tuple[str, ...] = (
    PRIORITY_HIGH_LABEL,
    PRIORITY_NORMAL_LABEL,
    PRIORITY_LOW_LABEL,
)


@dataclasses.dataclass(frozen=True)
class LabelSpec:
    """One repository label: what it is called and what it looks like.

    The name is the identity. ``key`` is the registry handle used by workflow
    code and tests; it is never sent to GitHub.
    """

    key: str
    name: str
    color: str
    description: str

    def as_dict(self) -> Dict[str, str]:
        return dataclasses.asdict(self)


#: Every label the issue plane owns, in the order it is provisioned. The
#: dispatch label comes first because it is the signal: if provisioning fails on
#: a later label, the one that matters already exists.
#:
#: Colours and descriptions for the labels that already exist are unchanged from
#: the values the scheduler used inline, so provisioning these specs against an
#: existing repository is a no-op rather than a redefinition.
LABEL_SPECS: Tuple[LabelSpec, ...] = (
    LabelSpec(
        key="dispatch",
        name=DISPATCH_LABEL,
        color="1D76DB",
        description="Hand this issue to Continuum: the scheduler dispatches it.",
    ),
    LabelSpec(
        key="in-progress",
        name=IN_PROGRESS_LABEL,
        color="5319E7",
        description=(
            "Actively reserved or implemented by the automatic issue scheduler."
        ),
    ),
    LabelSpec(
        key="paused",
        name=PAUSED_LABEL,
        color="BFDADC",
        description=(
            "Automation is paused after a failed/stopped implementation; remove to "
            "retry."
        ),
    ),
    LabelSpec(
        key="priority-p0",
        name=PRIORITY_HIGH_LABEL,
        color="B60205",
        description="P0 — critical work that should be handled immediately.",
    ),
    LabelSpec(
        key="priority-p1",
        name=PRIORITY_NORMAL_LABEL,
        color="D93F0B",
        description="P1 — high-priority planned work.",
    ),
    LabelSpec(
        key="priority-p2",
        name=PRIORITY_LOW_LABEL,
        color="FBCA04",
        description="P2 — normal planned work.",
    ),
)

#: Convenience mapping for callers that want the names rather than the specs.
ISSUE_PLANE_LABELS: Tuple[str, ...] = tuple(spec.name for spec in LABEL_SPECS)


def _keyed_specs() -> Dict[str, LabelSpec]:
    return {spec.key: spec for spec in LABEL_SPECS}


def _canonical_name(name: str) -> str:
    """Fold a label name to the form GitHub compares with.

    Label lookups are case-insensitive, so ``Continuum:Dispatch`` and
    ``continuum:dispatch`` are the same label. Folding once, here, keeps every
    caller from having to remember that.
    """
    return name.strip().lower()


def resolve_label(value: object) -> Optional[LabelSpec]:
    """Resolve a registry key or label name to its spec, or ``None``.

    ``None`` means "this is not a label this registry owns". It is the answer
    for unrelated repository labels (``bug``, ``help wanted``) and for anything
    that is not a non-empty string, so an attacker-controlled label can never
    resolve to the dispatch label.

    Keys are accepted for ergonomics in the CLI and in workflow expressions.
    They are *not* label names, so nothing on the activation path may use this
    function to decide whether work may start: see :func:`is_dispatch_label`.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    specs = _keyed_specs()
    if text in specs:
        return specs[text]

    folded = _canonical_name(text)
    for spec in LABEL_SPECS:
        if _canonical_name(spec.name) == folded:
            return spec
    return None


def is_dispatch_label(value: object) -> bool:
    """True only for the canonical dispatch label.

    This is the single question every activation check asks, so it is
    deliberately stricter than :func:`resolve_label`: it compares against the
    label *name* and never a registry key. A bare ``dispatch`` label is some
    repository's own unrelated label, and treating it as a hand-off would mean
    the answer to "is this issue still dispatched?" depended on a second
    spelling of the same word.

    Anything unknown, empty or non-string is False.
    """
    if not isinstance(value, str):
        return False
    folded = _canonical_name(value)
    return folded == _canonical_name(DISPATCH_LABEL)


def as_env(specs: Optional[Iterable[LabelSpec]] = None) -> str:
    """Render specs as ``KEY=name`` lines for a workflow step's environment.

    Workflows cannot read Python at expression time, so the registry is
    converted once into an environment block that ``github-script`` can parse.
    Keys are upper-cnake so they read as ordinary environment variables.
    """
    chosen = LABEL_SPECS if specs is None else tuple(specs)
    lines = []
    for spec in chosen:
        lines.append("{0}={1}".format(_env_key(spec.key), spec.name))
    return "\n".join(lines)


def _env_key(key: str) -> str:
    return "CONTINUUM_LABEL_" + key.upper().replace("-", "_")


def _env_values(specs: Optional[Iterable[LabelSpec]] = None) -> Dict[str, str]:
    chosen = LABEL_SPECS if specs is None else tuple(specs)
    return {spec.key: spec.name for spec in chosen}


def as_json(specs: Optional[Iterable[LabelSpec]] = None) -> str:
    """Render specs as the JSON array ``github.rest.issues.createLabel`` takes.

    Each entry carries only ``name``, ``color`` and ``description`` so the result
    can be spread straight into the API call.
    """
    chosen = LABEL_SPECS if specs is None else tuple(specs)
    return json.dumps(
        [
            {"name": spec.name, "color": spec.color, "description": spec.description}
            for spec in chosen
        ]
    )


def provision_plan(specs: Optional[Iterable[LabelSpec]] = None) -> List[Dict[str, str]]:
    """The createLabel arguments for every spec, in provisioning order."""
    chosen = LABEL_SPECS if specs is None else tuple(specs)
    return [
        {"name": spec.name, "color": spec.color, "description": spec.description}
        for spec in chosen
    ]


def dispatch_labels_json(specs: Optional[Iterable[LabelSpec]] = None) -> str:
    """JSON array of the label names the issue plane owns.

    Used by workflows that filter or filter-out by label set in one step.
    """
    chosen = LABEL_SPECS if specs is None else tuple(specs)
    return json.dumps([spec.name for spec in chosen])


def _validate() -> None:
    """Reject a registry that cannot be treated as canonical.

    Two specs sharing a key or a folded name would make "the" dispatch label
    ambiguous, so that is a startup failure rather than a runtime surprise.
    """
    keys = [spec.key for spec in LABEL_SPECS]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate label key in registry: {0}".format(keys))

    names = [_canonical_name(spec.name) for spec in LABEL_SPECS]
    if len(set(names)) != len(names):
        raise ValueError("duplicate label name in registry: {0}".format(names))

    for spec in LABEL_SPECS:
        if not spec.key or not spec.name:
            raise ValueError("label spec must have a key and a name: {0!r}".format(spec))
        if len(spec.color) != 6 or any(c not in "0123456789ABCDEFabcdef" for c in spec.color):
            raise ValueError("label {0} has a non-hex colour {1!r}".format(spec.name, spec.color))


_validate()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="continuum_labels.py",
        description="Read the canonical Continuum issue-plane label registry.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="print the registry")
    listing.add_argument(
        "--format",
        choices=("env", "json", "names", "keys"),
        default="env",
        help="output shape (default: env)",
    )

    sub.add_parser("provision-plan", help="print createLabel arguments as JSON")

    check = sub.add_parser(
        "check",
        help="test whether a value is the dispatch label, by name only",
    )
    check.add_argument("value", help="label name, not a registry key")

    name = sub.add_parser("name", help="print the canonical name for a registry key")
    name.add_argument("key", help="registry key, for example dispatch")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "list":
        if args.format == "env":
            print(as_env())
        elif args.format == "json":
            print(as_json())
        elif args.format == "names":
            print(dispatch_labels_json())
        else:
            print("\n".join(spec.key for spec in LABEL_SPECS))
        return 0

    if args.command == "provision-plan":
        print(json.dumps(provision_plan()))
        return 0

    if args.command == "check":
        if is_dispatch_label(args.value):
            print("{0} is the canonical dispatch label.".format(DISPATCH_LABEL))
            return 0
        print("{0!r} is not the canonical dispatch label.".format(args.value))
        return 1

    if args.command == "name":
        spec = resolve_label(args.key)
        if spec is None or spec.key != args.key.strip():
            print("unknown label key: {0!r}".format(args.key), file=sys.stderr)
            return 2
        print(spec.name)
        return 0

    raise AssertionError("unreachable command: {0!r}".format(args.command))


if __name__ == "__main__":
    raise SystemExit(main())
