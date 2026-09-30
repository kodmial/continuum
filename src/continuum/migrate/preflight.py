"""Whether this repository may be cut over, with the reasons it may not be.

Preflight is the difference between a migration and a hope. By the time
:mod:`continuum.migrate.apply` opens anything, every question this module asks
has already been answered against a live reading, and the answer is a document
rather than an opinion.

The rules, and what each one is protecting against:

* **An incomplete reading is not a READY one.** If any inventory read failed, the
  result is NOT READY regardless of how healthy everything else looks. A
  controller that planned a cutover from a partial reading would be planning it
  against files nobody opened.

* **The candidate revision is a commit.** A branch name means the code that will
  decide whether a pull request merges is whatever that branch points at when the
  run starts, which is not a property anyone can review. The architecture
  document's versioning invariant and #92's repin controller both assume the pin
  is a SHA, so a candidate that is not one is refused before a file is written.

* **A required secret is a *name*, and only the paths that need it.** #84 is
  explicit: existing secrets are reused in place and a missing one may block only
  when the active target path actually requires it. So requirements are derived
  from the declared capability and the phase -- ``review: true`` needs the review
  credential, ``release: true`` needs the signing material, and neither needs
  the other -- and an optional secret that is absent is reported as absent, never
  as a blocker.

* **The reviewed audit has to be about this repository.** Writer roles are the
  only authority on which file implements which controller. An audit of a
  different repository cannot decide anything about this one, so it is refused
  rather than approximated.

* **A floating pin is a finding, not a cosmetic one.** A consumer already calling
  Continuum from ``@main`` is running unreviewed code for a repair or a merge
  decision today. The cutover fixes it, which is good, but the preflight names it
  so the person reading the report learns that the consumer's current behaviour
  was not what its file appeared to say.

Nothing here writes, and nothing here contacts a model. The most privileged thing
it does is decide that it does not yet know enough.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ..shadow import baseline
from ..shadow.cutover import PHASE_A, PHASE_B, PHASES, phase_writers
from . import inventory as inventory_module
from .inventory import Inventory

PREFLIGHT_SCHEMA = "continuum.migration-preflight/v1"

_COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: What a preflight found. READY means "every question below is answered
#: affirmatively against this reading"; anything else means the cutover is not
#: authorized and the reasons are listed.
READY = "ready"
NOT_READY = "not-ready"

#: The reusable workflows a cutover calls. Named here rather than derived from
#: the caller set so that a plan cannot quietly drop a controller: the scheduler
#: is what replaces the consumer's legacy one, and a plan that forgot it would
#: retire a writer without installing its replacement.
PHASE_A_REUSABLES: Tuple[str, ...] = (
    "consumer-scheduler.yml",
    "consumer-opencode.yml",
    "consumer-repair.yml",
    "consumer-review-gate.yml",
    "consumer-auto-merge.yml",
    "review-queue.yml",
)

#: The release phase reuses the same control-plane reusables. What it adds is the
#: release core, which a consumer calls from its own release entrypoint rather
#: than through a new reusable, so there is nothing extra to verify here. Naming
#: the list per phase anyway keeps the check total: a future release caller that
#: does add a reusable will be caught by adding a name, rather than by a caller
#: generating a reference and discovering at run time that it does not resolve.
PHASE_B_REUSABLES: Tuple[str, ...] = PHASE_A_REUSABLES

#: The transitional cross-workflow credential. Named because #84 accepted it
#: explicitly for this migration, not because Continuum requires it: a consumer
#: whose review adapter runs on the caller context needs no PAT at all, and
#: requiring one there would be inventing a prerequisite.
TRANSITIONAL_TRUSTED_TOKEN = "TAP_PAT"

#: Optional by design. The configured OpenCode route works without one, so its
#: absence is reported and never blocks.
OPTIONAL_OPENCODE_KEY = "OPENCODE_API_KEY"


class PreflightError(ValueError):
    """The preflight cannot be evaluated from what it was given."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class Requirement:
    """One credential the active target path needs, and why it needs it."""

    secret: str
    why: str
    required: bool = True

    def describe(self) -> Dict[str, Any]:
        return {"secret": self.secret, "why": self.why, "required": self.required}


def requirements_for(inventory: Inventory, phase: str = PHASE_A) -> Tuple[Requirement, ...]:
    """What this consumer's *declared* capabilities actually require.

    Derived rather than hard-coded per repository, because a controller that
    knows NanoDictate's secret names has a second copy of the consumer's
    configuration, and the next repository would come back with a diff. What
    varies is which capability is switched on, and that is in the consumer's own
    configuration file.

    ``OPENCODE_API_KEY`` is listed as optional unconditionally: it is reported
    either way so a reader can see the decision was made, and an optional
    requirement is not something that can turn a READY into a NOT READY.
    """

    if phase not in PHASES:
        raise PreflightError("unknown_phase", "{!r} is not one of {}".format(phase, ", ".join(PHASES)))

    found: List[Requirement] = []
    if inventory.consumer_config.review:
        found.append(
            Requirement(
                secret=TRANSITIONAL_TRUSTED_TOKEN,
                why="review: true closes the review loop through a caller-scoped "
                "credential; the reusable workflow receives no token of its own",
            )
        )
    if phase == PHASE_B and inventory.consumer_config.release:
        found.append(
            Requirement(
                secret="NANODICTATE_SIGNING_P12",
                why="release: true signs artifacts, and the signing identity has to "
                "stay constant across releases",
            )
        )
        found.append(
            Requirement(
                secret="NANODICTATE_SIGNING_PASSWORD",
                why="release: true signs artifacts, and the imported identity needs its "
                "password to be usable at all",
            )
        )
    found.append(
        Requirement(
            secret=OPTIONAL_OPENCODE_KEY,
            why="the configured OpenCode route works anonymously; recorded so its "
            "absence is visible rather than assumed",
            required=False,
        )
    )
    return tuple(found)


@dataclasses.dataclass(frozen=True)
class Reason:
    """One thing standing between this repository and a cutover."""

    code: str
    message: str
    subject: str = ""

    def describe(self) -> Dict[str, Any]:
        return {"code": self.code, "subject": self.subject, "message": self.message}


@dataclasses.dataclass(frozen=True)
class Preflight:
    """A structured READY / NOT READY, never a boolean."""

    repository: str
    phase: str
    inventory_digest: str
    candidate: str
    rollback_revision: str
    reasons: Tuple[Reason, ...] = ()
    requirements: Tuple[Requirement, ...] = ()
    #: Role -> the Continuum callers that already implement it. Reconciled by
    #: the planner so a generated caller adopts an existing path instead of
    #: adding a second writer beside it.
    caller_roles: Mapping[str, Tuple[str, ...]] = dataclasses.field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return not self.reasons

    @property
    def verdict(self) -> str:
        return READY if self.ready else NOT_READY

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(reason.code for reason in self.reasons)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": PREFLIGHT_SCHEMA,
            "repository": self.repository,
            "phase": self.phase,
            "verdict": self.verdict,
            "inventory_digest": self.inventory_digest,
            "candidate": self.candidate,
            "rollback_revision": self.rollback_revision,
            "reasons": [reason.describe() for reason in self.reasons],
            "requirements": [item.describe() for item in self.requirements],
            "caller_roles": {
                role: list(paths) for role, paths in sorted(self.caller_roles.items())
            },
        }

    def summary_line(self) -> str:
        if self.ready:
            return "preflight READY {} phase={} candidate={} rollback={}".format(
                self.repository, self.phase, self.candidate[:12], self.rollback_revision[:12]
            )
        return "preflight NOT READY {} phase={}: {}".format(
            self.repository,
            self.phase,
            ", ".join(
                "{}{}".format(reason.code, "[{}]".format(reason.subject) if reason.subject else "")
                for reason in self.reasons
            ),
        )


def evaluate(
    inventory: Inventory,
    *,
    phase: str = PHASE_A,
    candidate: str = "",
    ledger: Optional["baseline.ParityLedger"] = None,
    requirements: Optional[Sequence[Requirement]] = None,
    available_reusables: Sequence[str] = (),
) -> Preflight:
    """Decide whether ``inventory`` may be cut over in ``phase``.

    Every reason is collected before anything is returned. A caller that stops at
    the first failure would have to re-run the controller to discover the next
    one, and a migration whose report names one problem at a time is a migration
    that takes a week to converge.
    """

    if phase not in PHASES:
        raise PreflightError("unknown_phase", "{!r} is not one of {}".format(phase, ", ".join(PHASES)))

    reasons: List[Reason] = []
    needed = tuple(requirements) if requirements is not None else requirements_for(inventory, phase)
    callers = _caller_roles(inventory)

    # -- the reading itself ------------------------------------------------ #
    if not inventory.complete:
        reasons.append(
            Reason(
                "inventory_incomplete",
                "the inventory could not read {} of the repository, so nothing may be "
                "planned from it: {}".format(len(inventory.limits), "; ".join(inventory.limits)),
            )
        )
    if not inventory.default_branch or not inventory.head_sha:
        reasons.append(
            Reason(
                "rollback_target_missing",
                "the default-branch HEAD could not be read, so there is no recorded "
                "revision to roll back to",
            )
        )
    elif not _COMMIT.match(inventory.head_sha):
        reasons.append(
            Reason(
                "rollback_target_not_immutable",
                "the default-branch HEAD reads as {!r}, which is not a full commit SHA; "
                "a rollback target has to name one commit".format(inventory.head_sha),
            )
        )

    # -- the candidate revision -------------------------------------------- #
    if not candidate:
        reasons.append(
            Reason("candidate_missing", "no Continuum revision was offered to pin to")
        )
    elif not _COMMIT.match(candidate):
        reasons.append(
            Reason(
                "candidate_not_immutable",
                "the Continuum revision reads as {!r}; a consumer must pin a full commit "
                "SHA, never a branch or tag".format(candidate),
            )
        )

    if available_reusables:
        missing = _missing_reusables(phase, available_reusables)
        for name in missing:
            reasons.append(
                Reason(
                    "reusable_workflow_missing",
                    "{} is not present at the candidate revision, so a caller generated "
                    "from this controller would reference a workflow that does not "
                    "exist".format(name),
                    subject=name,
                )
            )

    # -- the reviewed audit ------------------------------------------------ #
    if ledger is None:
        reasons.append(
            Reason(
                "no_reviewed_audit",
                "no parity ledger was supplied, so nothing says which workflow implements "
                "which controller and no writer may be planned",
            )
        )
    else:
        if ledger.repository and ledger.repository != inventory.repository:
            reasons.append(
                Reason(
                    "ledger_repository_mismatch",
                    "the reviewed audit is of {}, not {}".format(
                        ledger.repository, inventory.repository
                    ),
                )
            )
        else:
            reasons.extend(_ledger_reasons(inventory, ledger, phase))

    # -- existing Continuum references ------------------------------------- #
    for workflow in inventory.floating_pins:
        for pin in workflow.floating_pins:
            reasons.append(
                Reason(
                    "floating_continuum_pin",
                    "{} calls {}@{}; until this cutover lands, a branch reference means "
                    "the code running that controller is whatever the branch points at".format(
                        workflow.path, pin.path, pin.ref
                    ),
                    subject=workflow.path,
                )
            )

    # -- two writers for one job ------------------------------------------- #
    #
    # Deliberately *not* a blocker that more than one active workflow carries a
    # role. NanoDictate's review lifecycle is five files, all audited as the
    # ``review`` writer, and they compose one responsibility; refusing that would
    # mean the controller could never cut over the repository it was written for.
    # The invariant that must hold is about the state *after* the change, so it is
    # asserted by :mod:`continuum.migrate.plan` on the post-change writer map.
    # What the preflight reports is which roles already have a Continuum caller,
    # so the planner knows to adopt that path rather than add a second one beside
    # it.

    # -- credentials ------------------------------------------------------- #
    for requirement in needed:
        present = inventory.secrets.get(requirement.secret, False)
        if not present and requirement.required:
            reasons.append(
                Reason(
                    "missing_secret",
                    "{} is not set and the active path needs it: {}".format(
                        requirement.secret, requirement.why
                    ),
                    subject=requirement.secret,
                )
            )

    # -- undeclared capability --------------------------------------------- #
    for key, value in (
        ("review", inventory.consumer_config.review),
        ("release", inventory.consumer_config.release),
    ):
        if inventory.consumer_config.present and value is None:
            reasons.append(
                Reason(
                    "undeclared_capability",
                    "{} is present but does not say whether {} is on, so the plan would "
                    "have to invent a value".format(
                        inventory_module.CONSUMER_CONFIG_PATH, key
                    ),
                    subject=key,
                )
            )

    return Preflight(
        repository=inventory.repository,
        phase=phase,
        inventory_digest=inventory.digest,
        candidate=candidate,
        rollback_revision=inventory.head_sha,
        reasons=tuple(reasons),
        requirements=needed,
        caller_roles=dict(callers),
    )


def read_preflight(document: Mapping[str, Any]) -> Preflight:
    """Read a preflight back from an artifact.

    Lenient about the reason fields and strict about the identity fields: a
    preflight document that names a different repository or a different inventory
    digest is a record about something else, and treating it as about this one
    would be the whole reconciliation losing its place.
    """

    if not isinstance(document, Mapping):
        raise PreflightError("preflight_not_a_mapping", "a preflight must be an object")
    schema = document.get("schema")
    if schema != PREFLIGHT_SCHEMA:
        raise PreflightError(
            "unknown_preflight_schema",
            "expected {!r}, found {!r}".format(PREFLIGHT_SCHEMA, schema),
        )
    return Preflight(
        repository=str(document.get("repository", "")),
        phase=str(document.get("phase", "")),
        inventory_digest=str(document.get("inventory_digest", "")),
        candidate=str(document.get("candidate", "")),
        rollback_revision=str(document.get("rollback_revision", "")),
        reasons=tuple(
            Reason(
                code=str(entry.get("code", "")),
                message=str(entry.get("message", "")),
                subject=str(entry.get("subject", "")),
            )
            for entry in document.get("reasons", []) or []
            if isinstance(entry, Mapping)
        ),
        requirements=tuple(
            Requirement(
                secret=str(entry.get("secret", "")),
                why=str(entry.get("why", "")),
                required=bool(entry.get("required", True)),
            )
            for entry in document.get("requirements", []) or []
            if isinstance(entry, Mapping)
        ),
        caller_roles={
            str(role): tuple(str(path) for path in paths)
            for role, paths in (document.get("caller_roles") or {}).items()
        },
    )


# --------------------------------------------------------------------------- #
# The questions
# --------------------------------------------------------------------------- #


def _writer_roles(inventory: Inventory) -> Dict[str, Tuple[str, ...]]:
    """Which active workflows implement each writer, from the reviewed audit.

    Only the roles the gate knows about are reported. An unclassified path is not
    filed under ``other``: that would hide it inside a bucket nobody reads, and
    an unclassified path is exactly what a cutover must not touch silently.
    """

    found: Dict[str, List[str]] = {}
    for workflow in inventory.workflows:
        if workflow.writer and workflow.writer != baseline.WRITER_OTHER:
            found.setdefault(workflow.writer, []).append(workflow.path)
    return {role: tuple(sorted(paths)) for role, paths in found.items()}


def _caller_roles(inventory: Inventory) -> Dict[str, Tuple[str, ...]]:
    """The roles an *existing Continuum caller* already implements.

    NanoDictate reaches Continuum today for review only: one thin caller is
    already installed, pinned to an immutable commit, and it works. The cutover
    has to adopt that file rather than generate a second review caller beside it,
    and it cannot know to do that without being told which roles are covered.

    The role comes from the audit, exactly as everywhere else. A caller whose
    path the audit classifies as ``other`` -- the shadow bridge, for instance --
    is left out, because it is a reader and not a writer, and counting it as
    coverage of a role would let the cutover retire a legacy writer on the
    strength of a file that cannot dispatch anything.
    """

    found: Dict[str, List[str]] = {}
    for workflow in inventory.workflows:
        if workflow.kind != inventory_module.KIND_CONTINUUM_CALLER:
            continue
        if workflow.writer and workflow.writer != baseline.WRITER_OTHER:
            found.setdefault(workflow.writer, []).append(workflow.path)
    return {role: tuple(sorted(paths)) for role, paths in found.items()}


def _ledger_reasons(
    inventory: Inventory, ledger: "baseline.ParityLedger", phase: str
) -> List[Reason]:
    """Why the reviewed audit does not (yet) authorise this cutover.

    Three failures, and they are different in kind:

    * An **unclassified** active workflow means nobody has decided what it
      implements, so the plan cannot know whether it is a writer being replaced or
      product automation being preserved -- and guessing would be how a release
      workflow gets retired by a control-plane phase.
    * A **moved blob** means the classification was granted against content that
      is no longer there, so even a classified path has an unknown shape.
    * A **consumer-local writer in a replaced role** is the one that would do real
      damage. The audit is saying this file is the product's own behaviour rather
      than something Continuum absorbs, and the phase's whole premise is that
      Continuum *has* absorbed it. If the two disagree, the audit is the reviewed
      record and the cutover stops.
    """

    reviewed = ledger.workflow_map
    reasons: List[Reason] = []
    replaced = set(phase_writers(phase))

    for workflow in inventory.workflows:
        entry = reviewed.get(workflow.path)
        if entry is None:
            reasons.append(
                Reason(
                    "unclassified_workflow",
                    "{} is active and the reviewed audit classifies nothing for it, so the "
                    "cutover does not know whether it implements a writer this phase "
                    "replaces".format(workflow.path),
                    subject=workflow.path,
                )
            )
            continue
        if entry.blob_sha and workflow.blob_sha and entry.blob_sha != workflow.blob_sha:
            reasons.append(
                Reason(
                    "audited_blob_moved",
                    "{} was audited at blob {} and is now at {}, so the classification "
                    "was granted against different content".format(
                        workflow.path, entry.blob_sha[:12], workflow.blob_sha[:12]
                    ),
                    subject=workflow.path,
                )
            )
            continue
        if (
            entry.writer in replaced
            and entry.classification == baseline.CLASSIFICATION_CONSUMER_LOCAL
            and not workflow.pins
        ):
            reasons.append(
                Reason(
                    "consumer_local_writer_in_replaced_role",
                    "{} implements the {} writer, and the audit records it as "
                    "consumer-local rather than absorbed, so this phase has no mandate to "
                    "replace it".format(workflow.path, entry.writer),
                    subject=workflow.path,
                )
            )

    return reasons


def _missing_reusables(phase: str, available: Sequence[str]) -> Tuple[str, ...]:
    """Reusable workflows this phase needs that the candidate does not have.

    Checked against the candidate's own tree rather than against the checkout
    running the controller, because the two are different things at exactly the
    moment this matters: a controller on ``main`` must not generate a caller to a
    workflow that exists only in a branch nobody pinned.
    """

    present = {str(name) for name in available}
    wanted = PHASE_A_REUSABLES if phase == PHASE_A else PHASE_B_REUSABLES
    return tuple(name for name in wanted if name not in present)


def summarize(document: Union[Preflight, Mapping[str, Any]]) -> str:
    """Render a preflight as the lines a controller run prints.

    Takes the object as well as the document, because the caller that wants a
    summary almost always has the verdict in hand and does not want to serialise
    it only to parse it back -- and a round trip through ``read_preflight`` is one
    more place for a verdict to come back as something other than what was
    decided.
    """

    result = document if isinstance(document, Preflight) else read_preflight(document)
    lines = [
        "repository: {}".format(result.repository),
        "phase: {}".format(result.phase),
        "candidate: {}".format(result.candidate or "<none>"),
        "rollback revision: {}".format(result.rollback_revision or "<none>"),
        "inventory: {}".format(result.inventory_digest),
        "",
    ]
    for requirement in result.requirements:
        lines.append(
            "  secret {:<32} {:<9} {}".format(
                requirement.secret,
                "required" if requirement.required else "optional",
                requirement.why,
            )
        )
    lines.append("")
    if result.ready:
        lines.append("verdict: READY")
    else:
        lines.append("verdict: NOT READY")
        for reason in result.reasons:
            lines.append(
                "  {}{}: {}".format(
                    reason.code,
                    " [{}]".format(reason.subject) if reason.subject else "",
                    reason.message,
                )
            )
    return "\n".join(lines)


def write_json(path: str, document: Mapping[str, Any]) -> str:
    """Write a document the way every other Continuum artifact is written."""

    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return str(target)
