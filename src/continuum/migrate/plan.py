"""The one atomic change that installs Continuum and retires what it replaces.

#28 step 9 is the whole of this module:

    install NanoDictate thin callers/config pinned to one immutable Continuum
    SHA; disable/remove the legacy duplicate **writers** in the same cutover so
    old and new scheduler/repair/merge/release controllers never mutate
    production simultaneously.

Both halves are in one change because they have to be. Installing the generated
callers first would leave NanoDictate with its legacy scheduler *and* a Continuum
scheduler dispatching the same queue; removing them first would leave a
repository with no scheduler at all. There is no order in which two steps are
safe, so the controller does not offer two steps.

What the plan is allowed to put in the repository is equally narrow. Every
generated caller declares what GitHub requires to be static -- the event
subscriptions, the minimum permissions, the immutable revision -- and delegates
every decision to a reusable workflow at that revision. There is no queue logic,
no retry policy, no merge rule, no review parsing, and no release state machine
here, because a caller that grew any of those would be a second implementation
of the thing it is supposed to be calling.

Three invariants are asserted on the *result*, not on the intent:

* **Every Continuum reference is a full commit SHA.** A plan built against
  ``@main`` is refused at plan time, not left for a reviewer to notice.

* **Every legacy writer this phase replaces is retired in the same change, and
  every writer this phase does not replace is left alone.** The phase boundary
  comes from the ledger's writer roles, so it cannot be crossed by naming a
  release workflow something else.

* **No role ends up with two implementations.** Not "one file per role" --
  NanoDictate's review lifecycle is several files and Continuum's is too -- but
  for each replaced role, every surviving path is a generated caller. A legacy
  writer left standing beside a Continuum caller is a duplicate orchestration
  surface, which is the exact failure this cutover exists to end.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..shadow import baseline
from ..shadow.cutover import PHASE_A, PHASES, ChangeSet, FileChange, phase_writers
from . import inventory as inventory_module
from .inventory import CONSUMER_CONFIG_PATH, CONTINUUM_REPOSITORY, Inventory

PLAN_SCHEMA = "continuum.migration-plan/v1"
OVERLAY_SCHEMA = "continuum.migration-ledger-overlay/v1"

#: The Continuum-owned paths this controller generates. Reserved prefix, so a
#: product file is never mistaken for a generated one and a generated one is
#: never mistaken for a product file.
CALLER_PREFIX = ".github/workflows/continuum-"

_COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: The repository variables whose values the plan carries forward, with the
#: fallback Continuum uses when the variable is unset. Named here rather than in
#: the templates so that "preserve the consumer's current scheduling policy" is
#: one table a reviewer can check against NanoDictate's own workflows, which is
#: what #84 asks for: behaviour must not change silently during a migration.
PRESERVED_VARIABLES: Tuple[Tuple[str, str], ...] = (
    ("AUTOMATION_WIP_LIMIT", "2"),
    ("AUTOMATION_LEASE_MINUTES", "45"),
    ("AUTOMATION_MAX_DISPATCH_ATTEMPTS", "2"),
    ("AUTOMATION_REVIEW_WAIT_MINUTES", "45"),
    ("OPENCODE_MODEL", "opencode/big-pickle"),
    ("AUTOMATION_AGENT_RUNNER", "ubuntu-latest"),
    ("AUTOMATION_SWIFT_VERSION", ""),
)


class PlanError(ValueError):
    """The plan cannot be built from what it was given."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Consumer-owned facts
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ConsumerProfile:
    """The consumer's own facts, resolved once and rendered into every caller.

    Every value here is repository-specific product/toolchain data: the sort of
    thing the architecture document puts on the consumer's side of the boundary.
    Continuum owns the decision to use them and the lifecycle around them.
    """

    #: Display names of the consumer's own CI workflows. ``workflow_run`` matches
    #: on display name, not filename, so these are names and not paths.
    required_ci: Tuple[str, ...] = ()
    #: Extra exact-head gates beyond the primary CI workflow, comma-joined into
    #: the merge and repair controllers.
    additional_gates: Tuple[str, ...] = ()
    #: The consumer's own agent runner and language toolchain.
    runner: str = "ubuntu-latest"
    swift_version: str = ""
    model: str = "opencode/big-pickle"
    wip_limit: str = "2"
    lease_minutes: str = "45"
    max_attempts: str = "2"
    review_wait_minutes: str = "45"
    #: The consumer's own release entrypoint, by filename. Empty when the
    #: repository has no release workflow, which is a legitimate reading.
    release_workflow: str = ""
    #: The consumer-owned write token passed to the reusable controllers that
    #: need to fan out to downstream workflows. Name only.
    token_secret: str = "TAP_PAT"
    opencode_secret: str = "OPENCODE_API_KEY"
    review: bool = True
    release: bool = False

    @classmethod
    def from_inventory(
        cls,
        inventory: Inventory,
        *,
        required_ci: Sequence[str],
        additional_gates: Sequence[str] = (),
        token_secret: str = "TAP_PAT",
        overrides: Optional[Mapping[str, str]] = None,
    ) -> "ConsumerProfile":
        """Resolve the profile from the inventory's variables, with documented defaults.

        ``overrides`` wins over the repository variable, which wins over the
        fallback. A migration that let configuration change the *pin* would be
        choosing which code runs, so the pin is not overridable here.
        """

        if not required_ci:
            raise PlanError(
                "no_required_ci",
                "the plan needs at least one consumer CI workflow display name; the "
                "merge and repair controllers gate on the exact head of these runs "
                "and cannot be configured without them",
            )
        values: Dict[str, str] = {}
        for name, fallback in PRESERVED_VARIABLES:
            values[name] = str(inventory.variables.get(name, "") or fallback)
        for name, value in (overrides or {}).items():
            values[str(name)] = str(value)

        known = {
            entry.display_name for entry in inventory.workflows if entry.display_name
        }
        missing = [name for name in list(required_ci) + list(additional_gates) if name not in known]
        if missing:
            raise PlanError(
                "named_ci_workflow_absent",
                "{} names {} as a gate, and no active workflow in {} has that display "
                "name. A workflow_run trigger that names a workflow GitHub cannot find "
                "never fires, so the gate would be silently inapplicable.".format(
                    inventory.repository, ", ".join(missing), inventory.repository
                ),
            )

        release = sorted(
            entry.path.split("/")[-1]
            for entry in inventory.workflows
            if entry.writer == baseline.RELEASE_WRITER
            and entry.kind == inventory_module.KIND_LEGACY_WRITER
        )
        config = inventory.consumer_config
        return cls(
            required_ci=tuple(required_ci),
            additional_gates=tuple(additional_gates),
            runner=values["AUTOMATION_AGENT_RUNNER"],
            swift_version=values["AUTOMATION_SWIFT_VERSION"],
            model=values["OPENCODE_MODEL"],
            wip_limit=values["AUTOMATION_WIP_LIMIT"],
            lease_minutes=values["AUTOMATION_LEASE_MINUTES"],
            max_attempts=values["AUTOMATION_MAX_DISPATCH_ATTEMPTS"],
            review_wait_minutes=values["AUTOMATION_REVIEW_WAIT_MINUTES"],
            # The reviewed audit's own word for the release writer, not a guess
            # from a filename. In Phase A there is exactly one, and it stays.
            release_workflow=release[0] if release else "",
            token_secret=token_secret,
            # The contract's own switches, preserved verbatim. A cutover that
            # "helpfully" turned review on for a repository that had it off would
            # be a behaviour change wearing a migration's clothes.
            review=bool(config.review) if config.review is not None else False,
            release=bool(config.release) if config.release is not None else False,
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "required_ci": list(self.required_ci),
            "additional_gates": list(self.additional_gates),
            "runner": self.runner,
            "swift_version": self.swift_version,
            "model": self.model,
            "wip_limit": self.wip_limit,
            "lease_minutes": self.lease_minutes,
            "max_attempts": self.max_attempts,
            "review_wait_minutes": self.review_wait_minutes,
            "release_workflow": self.release_workflow,
            "token_secret": self.token_secret,
            "opencode_secret": self.opencode_secret,
            "review": self.review,
            "release": self.release,
        }


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class FileEdit:
    """One path the atomic change touches."""

    path: str
    #: One of ``cutover.CHANGE_ACTIONS``. ``remove`` carries no content.
    action: str
    writer: str
    content: str = ""

    @property
    def blob_sha(self) -> str:
        """The git blob SHA this content will have once committed.

        Computed locally from the exact bytes rather than read back after the
        push, so the reviewed ledger entry the cutover proposes is provably about
        the content the cutover writes rather than about whatever the branch
        happens to hold later.
        """

        return "" if self.action == "remove" else inventory_module.git_blob_sha(self.content)

    def describe(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "action": self.action,
            "writer": self.writer,
            "blob_sha": self.blob_sha,
            "bytes": len(self.content.encode("utf-8")) if self.content else 0,
        }

    def to_change(self) -> FileChange:
        return FileChange(path=self.path, action=self.action, writer=self.writer)


@dataclasses.dataclass(frozen=True)
class CutoverPlan:
    """What the atomic change is, bound to the reading it was planned from."""

    repository: str
    phase: str
    candidate: str
    inventory_digest: str
    rollback_revision: str
    edits: Tuple[FileEdit, ...] = ()
    #: Paths the plan considered and deliberately left alone. Reported so that a
    #: reader can see the plan looked at them; deliberately *not* part of the
    #: change set, because the gate refuses any mention of a release writer in
    #: Phase A -- and a workflow this change does not touch is not a change.
    retained: Tuple[Tuple[str, str], ...] = ()
    labels: Tuple[Tuple[str, str, str], ...] = ()
    #: The head the change set is bound to. Empty until the change is committed,
    #: because a change set has to name one exact pull request head.
    cutover_head: str = ""

    # -- projections ------------------------------------------------------- #

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "repository": self.repository,
            "phase": self.phase,
            "candidate": self.candidate,
            "inventory_digest": self.inventory_digest,
            "rollback_revision": self.rollback_revision,
            "cutover_head": self.cutover_head,
            "edits": [edit.describe() for edit in self.edits],
            # The bytes, so a later invocation resumes from what was reviewed
            # rather than re-rendering against its own template. A plan whose
            # content disagreed with the template that produced it would be a
            # controller silently rewriting a reviewed change.
            "contents": {
                edit.path: edit.content for edit in self.edits if edit.action != "remove"
            },
            "retained": [
                {"path": path, "writer": writer} for path, writer in self.retained
            ],
            "labels": [
                {"name": name, "color": color, "description": description}
                for name, color, description in self.labels
            ],
        }

    def change_set(self) -> ChangeSet:
        """The change set the cutover gate reads, in the gate's own schema.

        Only real changes are listed. The gate's ``release_writer_in_phase_a``
        blocker fires on *any* mention of a release writer in Phase A, which is
        the right answer for a path the cutover is replacing and the wrong answer
        for one it is not touching -- so paths the plan retained are reported on
        the plan and left out of this document rather than being mislabelled as a
        change.
        """

        return ChangeSet(
            cutover_head=self.cutover_head,
            phase=self.phase,
            changes=tuple(edit.to_change() for edit in self.edits),
        )

    def with_head(self, head: str) -> "CutoverPlan":
        """Bind the plan to the commit that carries it."""

        if not _COMMIT.match(head or ""):
            raise PlanError(
                "cutover_head_not_a_commit",
                "a change set has to name the cutover pull request head as a full "
                "commit SHA, got {!r}".format(head),
            )
        return dataclasses.replace(self, cutover_head=head)

    @property
    def change_set_document(self) -> Dict[str, Any]:
        """The change set as JSON, refused unless the plan is bound to a head."""

        if not self.cutover_head:
            raise PlanError(
                "change_set_not_bound",
                "the change set names the cutover pull request head; the plan has "
                "not been committed yet, so there is nothing to bind it to",
            )
        return self.change_set().describe()

    @property
    def overlay_document(self) -> Dict[str, Any]:
        """A proposal for the reviewed audit covering every path this change adds.

        The gate refuses a change set that touches a path the ledger does not
        classify, and it refuses it for a good reason: an unreviewed writer is the
        thing the cutover is removing. So the cutover proposes its own entries,
        with the blob SHA of the bytes it will write, and a reviewer decides
        whether the classification is right before the gate is asked to judge.
        """

        entries = []
        for edit in self.edits:
            if edit.action == "remove":
                # Removals are not proposed. The ledger already classifies every
                # path the cutover retires -- that is where its writer role came
                # from -- so re-proposing them would be proposing an entry for a
                # file that is on its way out.
                continue
            entries.append(
                {
                    "path": edit.path,
                    "blob_sha": edit.blob_sha,
                    "classification": baseline.CLASSIFICATION_ABSORBED,
                    "writer": edit.writer,
                    "owner": _OWNERS.get(edit.writer, "#60"),
                    "rationale": _OVERLAY_RATIONALE.get(
                        edit.writer, _OVERLAY_RATIONALE[baseline.WRITER_OTHER]
                    ).format(self.candidate),
                }
            )
        return {
            "schema": OVERLAY_SCHEMA,
            "repository": self.repository,
            "candidate": self.candidate,
            "entries": sorted(entries, key=lambda item: item["path"]),
        }

    # -- assertions -------------------------------------------------------- #

    @property
    def writers_after(self) -> Dict[str, Tuple[str, ...]]:
        """Writer role -> surviving active paths, as they will be after the change."""

        found: Dict[str, List[str]] = {}
        for edit in self.edits:
            if edit.writer and edit.action != "remove":
                found.setdefault(edit.writer, []).append(edit.path)
        for path, writer in self.retained:
            if writer:
                found.setdefault(writer, []).append(path)
        return {role: tuple(sorted(paths)) for role, paths in found.items()}

    @property
    def duplicated_roles(self) -> Dict[str, Tuple[str, ...]]:
        """Roles that would still have a non-generated writer beside a generated one.

        This is the invariant the whole cutover turns on. It is not "one file per
        role" -- a role may legitimately be several generated callers -- it is
        "no independently maintained implementation survives". A role with a
        legacy writer left standing is reported even if the phase did not
        nominally replace it, because that is how a second control plane survives
        a cutover that reported success.
        """

        generated = {
            edit.writer
            for edit in self.edits
            if edit.action != "remove" and edit.writer != baseline.WRITER_OTHER
        }
        found: Dict[str, List[str]] = {}
        for path, writer in self.retained:
            if writer in generated and writer != baseline.WRITER_OTHER:
                found.setdefault(writer, []).append(path)
        return {role: tuple(sorted(paths)) for role, paths in found.items()}

    def unimplemented_roles(self) -> Tuple[str, ...]:
        """Replaced roles with no surviving writer at all."""

        after = self.writers_after
        return tuple(
            role for role in phase_writers(self.phase) if not after.get(role)
        )

    def phase_violations(self) -> Tuple[str, ...]:
        """Ways this change crosses the boundary its own phase draws.

        Checked here as well as by the gate, because a plan that removes a release
        workflow in Phase A has no business being committed and pushed at all --
        the gate would refuse it later, after the branch already existed.
        """

        violations: List[str] = []
        replaced = set(phase_writers(self.phase))
        for edit in self.edits:
            if edit.writer == baseline.WRITER_OTHER:
                continue
            if edit.action == "remove" and edit.writer not in replaced:
                violations.append(
                    "{} is a {} writer and {} only replaces {}".format(
                        edit.path, edit.writer, self.phase, ", ".join(sorted(replaced))
                    )
                )
            if self.phase == PHASE_A and edit.writer == baseline.RELEASE_WRITER:
                violations.append(
                    "{} is the release writer, which {} leaves in place".format(
                        edit.path, self.phase
                    )
                )
        for role, paths in sorted(self.duplicated_roles.items()):
            violations.append(
                "{} would still be implemented by {} beside the generated caller".format(
                    role, ", ".join(paths)
                )
            )
        violations.extend(
            "{} would have no writer at all after the change".format(role)
            for role in self.unimplemented_roles()
        )
        return tuple(violations)


#: Who owns each role's classification, so the proposed audit entry routes to an
#: issue a reader already knows. These are the same owners the ledger already
#: uses for the legacy files each role replaces.
_OWNERS: Dict[str, str] = {
    baseline.WRITER_SCHEDULER: "#27",
    baseline.WRITER_OPENCODE: "#27",
    baseline.WRITER_REPAIR: "#27",
    baseline.WRITER_REVIEW: "#11",
    baseline.WRITER_MERGE: "#11",
    baseline.WRITER_RELEASE: "#21",
}

#: What a reviewer is being asked to agree to, per role. Written out rather than
#: templated from one string because these are read one at a time, and the
#: difference between "implements nothing itself" and "declares two booleans" is
#: exactly the difference a reviewer needs to see spelled out.
_OVERLAY_RATIONALE: Dict[str, str] = {
    baseline.WRITER_SCHEDULER: (
        "generated caller for the Continuum scheduler at the immutable revision {0}; "
        "it declares the cron subscription and permissions and contains no queue "
        "logic, so the implementation is Continuum's"
    ),
    baseline.WRITER_OPENCODE: (
        "generated caller for the Continuum agent controller at the immutable "
        "revision {0}; it declares the events and permissions and contains no "
        "prompt, retry or escalation logic"
    ),
    baseline.WRITER_REPAIR: (
        "generated caller for the Continuum repair ladder at the immutable "
        "revision {0}; it declares the workflow_run subscription and permissions "
        "and contains no ladder logic"
    ),
    baseline.WRITER_REVIEW: (
        "generated caller for the Continuum review gate at the immutable revision "
        "{0}; it declares the review events and permissions and contains no "
        "CodeRabbit parsing, queue or retry logic"
    ),
    baseline.WRITER_MERGE: (
        "generated caller for the Continuum merge controller at the immutable "
        "revision {0}; it declares the pull request events and permissions and "
        "contains no readiness or merge rule"
    ),
    baseline.WRITER_OTHER: (
        "generated by the Continuum migration controller at the immutable revision "
        "{0}; configuration or a record rather than a controller, so it dispatches "
        "nothing and implements no writer role"
    ),
}


# --------------------------------------------------------------------------- #
# Building the plan
# --------------------------------------------------------------------------- #


def build_plan(
    inventory: Inventory,
    profile: ConsumerProfile,
    *,
    phase: str = PHASE_A,
    candidate: str = "",
) -> CutoverPlan:
    """Render the atomic change.

    ``candidate`` must be a full commit SHA. That is checked here rather than
    trusted from the caller, because the alternative is a repository whose
    controllers run whatever a branch points at when the workflow starts -- and
    that failure mode is invisible in review, since the YAML looks correct.
    """

    if phase not in PHASES:
        raise PlanError("unknown_phase", "{!r} is not one of {}".format(phase, ", ".join(PHASES)))
    if phase != PHASE_A:
        # Said out loud rather than half-implemented. The release phase replaces
        # the release writer, and this controller has no generated release caller
        # to replace it with -- by design, because #28 keeps the consumer's own
        # release workflows as its release writers and has Continuum call into
        # them. A plan that retired them and installed nothing would satisfy
        # every assertion in this module and leave the repository with no release
        # at all, so the phase is refused until the release entrypoint it needs
        # exists. See #21 and #22.
        raise PlanError(
            "phase_not_implemented",
            "this controller plans {} only. {} additionally replaces the release "
            "writer, and the release entrypoint it would install does not exist "
            "yet; see #21 and #22.".format(PHASE_A, phase),
        )
    if not _COMMIT.match(candidate or ""):
        raise PlanError(
            "candidate_not_immutable",
            "the Continuum revision reads as {!r}; a consumer pins a full commit SHA, "
            "never a branch or tag, so a plan cannot be built against one".format(candidate),
        )

    # Two kinds need no ledger entry, and both are identified structurally rather
    # than by their filename: a Continuum caller references Continuum, and the
    # shadow bridge is the one path this controller owns by name and keeps in
    # every phase because the next cutover is judged on what it recorded.
    unclassified = sorted(
        workflow.path
        for workflow in inventory.workflows
        if not workflow.writer
        and workflow.kind
        not in (inventory_module.KIND_CONTINUUM_CALLER, inventory_module.KIND_SHADOW_BRIDGE)
    )
    if unclassified:
        # Fail closed, and the reason is specific: with these paths unclassified
        # every one of them reads as `other`, so the single-writer rule below
        # cannot see them and passes vacuously. The result would be a change that
        # installs five generated callers directly beside five unidentified legacy
        # ones -- exactly the dual-writer state this cutover exists to end, in a
        # change set whose own assertions said it was clean.
        raise PlanError(
            "unclassified_workflow",
            "no reviewed audit names a writer for {} of this repository's workflows, "
            "so the cutover cannot tell which of them it is about to replace: {}. "
            "Review and classify them before planning.".format(
                len(unclassified), ", ".join(unclassified)
            ),
        )

    known = inventory.workflow_map
    edits: List[FileEdit] = []
    retained: List[Tuple[str, str]] = []
    #: Paths already settled as "correctly installed", so the install loop does
    #: not also propose to rewrite them. Both loops look at the same paths -- one
    #: deciding whether an existing caller is correct, the other deciding what to
    #: install -- and without this a correct caller would be both retained and
    #: modified in one change set.
    settled: set = set()
    replaced = set(phase_writers(phase))

    # -- retire what this phase replaces ------------------------------------ #
    for workflow in sorted(inventory.workflows, key=lambda item: item.path):
        if workflow.kind == inventory_module.KIND_SHADOW_BRIDGE:
            # Read-only, and it survives every phase: it is the evidence that the
            # next cutover is judged on.
            retained.append((workflow.path, workflow.writer or baseline.WRITER_OTHER))
            continue
        if workflow.kind == inventory_module.KIND_CONTINUUM_CALLER:
            if is_generated(workflow.path):
                # Retained only if it is byte-identical to what this plan would
                # write. A generated caller whose pin has been edited by hand is
                # *not* retained: it is corrected by the install loop below, as a
                # modification. Retaining anything at a matching path would make
                # the reconciliation unable to repair drift, which is the one
                # thing it exists to do.
                spec = _CALLER_SPECS.get(generated_role(workflow.path))
                expected = (
                    spec.render(profile, candidate=candidate, phase=phase) if spec else None
                )
                if expected is not None and _existing_matches(workflow, expected):
                    retained.append(
                        (workflow.path, workflow.writer or generated_role(workflow.path))
                    )
                    settled.add(workflow.path)
                    continue
            # A hand-written caller somewhere else is still a caller, and leaving
            # it while adding the generated one would be two writers for one job.
            edits.append(
                FileEdit(
                    path=workflow.path,
                    action="remove",
                    writer=workflow.writer or generated_role(workflow.path),
                )
            )
            continue
        if workflow.writer and workflow.writer in replaced:
            edits.append(
                FileEdit(path=workflow.path, action="remove", writer=workflow.writer)
            )
            continue
        retained.append((workflow.path, workflow.writer or baseline.WRITER_OTHER))

    # -- install the generated callers ------------------------------------- #
    # Only the roles this controller has a caller for. ``release`` is absent on
    # purpose: #28 keeps the consumer's own release workflows as its release
    # writers and has Continuum call into them from a release entrypoint the
    # consumer already owns, so a generated *release* caller would replace the
    # thing Phase B exists to preserve. The role is retired in Phase B and its
    # replacement is the consumer's retained entrypoint, which `unimplemented_roles`
    # is what checks.
    for role in sorted(replaced & set(_CALLER_PATHS)):
        path = _CALLER_PATHS[role]
        if path in settled:
            continue
        spec = _CALLER_SPECS[role]
        content = spec.render(profile, candidate=candidate, phase=phase)
        # Already handled above; reaching here means either there is no file at
        # this path or the installed bytes differ from this plan's, so the
        # reconciliation writes them.
        action = "modify" if path in known else "add"
        edits.append(FileEdit(path=path, action=action, writer=role, content=content))

    # -- reconcile the consumer's declared switches ------------------------ #
    # `writer` is `other`: the contract document is configuration, not a
    # controller, and claiming a role for it would make the single-writer
    # invariant complain about a file that cannot dispatch anything.
    config_text = render_consumer_config(profile)
    config = inventory.consumer_config
    if config.present and config.blob_sha == inventory_module.git_blob_sha(config_text):
        retained.append((CONSUMER_CONFIG_PATH, baseline.WRITER_OTHER))
    else:
        edits.append(
            FileEdit(
                path=CONSUMER_CONFIG_PATH,
                action="modify" if config.present else "add",
                writer=baseline.WRITER_OTHER,
                content=config_text,
            )
        )

    # -- record where the rollback points ----------------------------------- #
    # Part of the reviewed change, not something the apply stage writes
    # afterwards: a rollback target that appears after the merge is a target
    # nobody agreed to, and one that appears only in Continuum's own history is a
    # target a force-push can take away.
    #
    # Imported here rather than at module scope because `rollback` imports `plan`
    # for the generated-file conventions. The cycle is real; resolving it by
    # moving those two constants into a module of their own would be tidier, and
    # would also be a second place that has to be kept in step with this one.
    from .rollback import render_record

    installed = tuple(
        sorted(
            edit.path
            for edit in edits
            if is_generated(edit.path) and edit.action != "remove"
        )
    )
    retired = tuple(
        sorted(edit.path for edit in edits if edit.action == "remove")
    )
    record_text = render_record(
        inventory.repository,
        phase,
        revision=candidate,
        rollback_revision=inventory.head_sha,
        installed=installed,
        retired=retired,
    )
    if inventory.record_blob_sha and inventory.record_blob_sha == inventory_module.git_blob_sha(
        record_text
    ):
        retained.append((ROLLBACK_RECORD_PATH, baseline.WRITER_OTHER))
    else:
        edits.append(
            FileEdit(
                path=ROLLBACK_RECORD_PATH,
                action="modify" if inventory.record_blob_sha else "add",
                writer=baseline.WRITER_OTHER,
                content=record_text,
            )
        )

    edits.sort(key=lambda edit: edit.path)
    plan = CutoverPlan(
        repository=inventory.repository,
        phase=phase,
        candidate=candidate,
        inventory_digest=inventory.digest,
        # Bound to the head this change is *built on*, not to a commit that does
        # not exist yet. That is the head the cutover gate needs in order to
        # judge the change, and it is the head `apply` refuses to work from if
        # the default branch has moved in the meantime. `apply` re-binds the
        # same plan to the commit it publishes once there is one.
        cutover_head=inventory.head_sha,
        rollback_revision=inventory.head_sha,
        edits=tuple(edits),
        retained=tuple(sorted(set(retained))),
        labels=continuum_labels(),
    )
    violations = plan.phase_violations()
    if violations:
        raise PlanError(
            "phase_violation",
            "the change this plan describes is not valid for {}: {}".format(
                phase, "; ".join(violations)
            ),
        )
    return plan


def _existing_matches(workflow: inventory_module.Workflow, content: str) -> bool:
    """Whether an already-installed generated caller is byte-identical.

    Compared against the blob the repository already has, not against the file on
    the controller's disk: the controller is at some commit and the consumer is
    at another, and a reconciliation that compares its own template would rewrite
    a correct file on every run. ``Workflow`` carries the blob SHA rather than
    the text, so the honest comparison available here is the blob, and a caller
    whose recorded blob differs is rewritten -- which is what reconciliation means.
    """

    return bool(workflow.blob_sha) and workflow.blob_sha == inventory_module.git_blob_sha(
        content
    )


def _basename(path: str) -> str:
    """The workflow *filename*, which is how the controllers address one.

    ``workflow_dispatch`` takes a filename, not a repository path, and the
    reusable controllers pass these inputs straight through. Passing a path here
    would produce a dispatch against a workflow that does not exist, which GitHub
    reports as a 404 inside a run that otherwise looks healthy.
    """

    return str(path).split("/")[-1]


#: The git file mode for a generated file. 100644 rather than 100755: a generated
#: caller is not executable, and a repository that had made one executable was
#: carrying an accident that a cutover would otherwise silently keep.
FILE_MODE = "100644"

#: The cutover record this plan writes, aliased from the inventory so that the two
#: modules cannot disagree about which path a rollback reads.
ROLLBACK_RECORD_PATH = inventory_module.CUTOVER_RECORD_PATH


def is_generated(path: str) -> bool:
    """Whether ``path`` is a generated caller this controller owns.

    Distinct from :func:`generated_role`, which answers "which job does this do"
    and returns ``other`` for a path that does none. This one answers "did we
    write it", and the difference decides whether a path belongs in the cutover
    record's ``installed`` list or its ``retired`` one: a product workflow the
    cutover leaves alone is neither.
    """

    return path in set(_CALLER_PATHS.values())


def generated_role(path: str) -> str:
    """The writer role a generated caller path implements, or ``other``.

    Public because the rollback controller needs the same answer. It is asking
    the same question from the other direction -- "if this file goes away, which
    job stops being done?" -- and two implementations of that mapping would be
    two chances to strand a role during a rollback.
    """

    for role, generated in _CALLER_PATHS.items():
        if generated == path:
            return role
    return baseline.WRITER_OTHER


#: Role -> the generated caller's path. Chosen so that every Continuum-owned file
#: is under one reserved prefix, which is what lets the next reconciliation tell a
#: generated file from a product one without consulting a manifest.
_CALLER_PATHS: Dict[str, str] = {
    baseline.WRITER_SCHEDULER: CALLER_PREFIX + "scheduler.yml",
    baseline.WRITER_OPENCODE: CALLER_PREFIX + "opencode.yml",
    baseline.WRITER_REPAIR: CALLER_PREFIX + "repair.yml",
    baseline.WRITER_REVIEW: CALLER_PREFIX + "review-gate.yml",
    baseline.WRITER_MERGE: CALLER_PREFIX + "auto-merge.yml",
}

#: The generated agent caller's *display name*. The repair controller's
#: ``workflow_run`` trigger and the shadow bridge both match on display name, and
#: the bridge in a consumer already names the agent workflow it expects. Renaming
#: it during a cutover would make those triggers silently never fire -- a
#: repository that merged the cutover successfully and stopped repairing anything.
AGENT_DISPLAY_NAME = "OpenCode agent"


def _pins(workflow_display_names: Sequence[str]) -> str:
    """Render a ``workflow_run.workflows`` list, as YAML flow or block style.

    Flow style for the short common case and block style beyond it, because a
    single flow-mapped list long enough to wrap is exactly the kind of file that
    gets hand-edited later.
    """

    names = [str(name) for name in workflow_display_names if str(name).strip()]
    if not names:
        return "[]"
    if all(len(name) <= 24 for name in names) and sum(len(name) for name in names) <= 60:
        return "[{}]".format(", ".join('"{}"'.format(name) for name in names))
    return "\n" + "".join('      - "{}"\n'.format(name) for name in names).rstrip("\n")


def _gate_list(names: Sequence[str]) -> str:
    return ",".join(str(name) for name in names)


#: The labels Continuum owns. Provisioned idempotently by the apply stage; a
#: repository that already has one keeps its colour and description, because a
#: label colour is a product decision someone may have made on purpose.
def continuum_labels() -> Tuple[Tuple[str, str, str], ...]:
    return (
        (
            "review-ready",
            "0e8a16",
            "Pull request is ready for the Continuum review gate",
        ),
        (
            "automation:in-progress",
            "1d76db",
            "Continuum has dispatched this issue to an agent",
        ),
        (
            "automation:paused",
            "fbca04",
            "This issue is not eligible for automatic dispatch",
        ),
        (
            "opencode-conflict-repair",
            "d93f0b",
            "A repair is in flight for this pull request",
        ),
        (
            "opencode-repair-failed",
            "b60205",
            "The repair ladder is exhausted for this pull request",
        ),
        (
            "no-auto-merge",
            "5319e7",
            "Merge this pull request by hand",
        ),
    )


# --------------------------------------------------------------------------- #
# The rendered callers
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class CallerSpec:
    """How one role's generated caller is rendered."""

    role: str
    reusable: str
    render: Any


def render_consumer_config(profile: ConsumerProfile) -> str:
    """The consumer contract document, with the consumer's own switches preserved.

    Comments are carried because a two-boolean file is exactly the file somebody
    will later add a third key to, and the reason there is no third key is worth
    being in the repository rather than only in this module.
    """

    if profile.release:
        release_note = (
            "#   release: true  hand each merged commit to this repository's own release\n"
            "#                  entrypoint ({workflow}). What is built, how it is signed and\n"
            "#                  where it is published stay in this repository; Continuum\n"
            "#                  decides only when the hand-off happens.\n"
        ).format(workflow=profile.release_workflow or "this repository's release workflow")
    else:
        release_note = (
            "#   release: false this repository's release automation runs on its own\n"
            "#                  schedule, so the merge controller dispatches nothing.\n"
        )

    return (
        "# Continuum consumer contract.\n"
        "#\n"
        "# Generated by the Continuum migration controller. Change this repository's\n"
        "# product facts (required checks, runner, toolchain, model) through the\n"
        "# migration controller rather than by hand: a reconciliation rewrites this\n"
        "# file, and a hand edit would be reverted as if it were a regression.\n"
        "#\n"
        "# Two switches, and nothing a consumer can misread as a choice:\n"
        "#\n"
        "#   review: true   close the review loop. The reviewer is Continuum's\n"
        "#                  choice; the repository never names one.\n"
        "#   review: false  opt out. The gate returns before reading or writing\n"
        "#                  anything on GitHub, so it costs no provider traffic.\n"
        "#\n"
        "{release_note}"
        "\n"
        "review: {review}\n"
        "release: {release}\n".format(
            release_note=release_note,
            review="true" if profile.review else "false",
            release="true" if profile.release else "false",
        )
    )


def _uses(reusable: str, candidate: str) -> str:
    return "{}/.github/workflows/{}@{}".format(CONTINUUM_REPOSITORY, reusable, candidate)


def _scheduler(profile: ConsumerProfile, candidate: str, phase: str) -> str:
    return """name: Continuum scheduler

# GENERATED BY CONTINUUM -- do not edit by hand.
#
# The consumer's event surface and nothing else. GitHub cannot deliver another
# repository's reusable workflow this repository's events, so the wake-up has to
# be declared here; every decision about what to do with it -- priority order,
# the dependency refusal, work in progress, the lease, the attempt limit -- is
# made by the controller at the immutable revision below.
#
# The scheduling policy is this repository's and is stated as inputs, because
# that is where a repository says it once:
#
#   wip_limit        how many issues may be in flight at once
#   lease_minutes    how long a reservation survives without progress
#   max_attempts     how many times a failed run may be re-dispatched
#
# Those are the current values and defaults of this repository, carried forward
# by the cutover so that adopting Continuum does not change how much work it
# does.

on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch: {{}}

permissions:
  actions: write
  contents: read
  issues: write
  pull-requests: read

jobs:
  schedule:
    uses: {scheduler}
    permissions:
      actions: write
      contents: read
      issues: write
      pull-requests: read
    with:
      opencode_workflow: {agent}
      wip_limit: {wip}
      lease_minutes: {lease}
      max_attempts: {attempts}
""".format(
        scheduler=_uses("consumer-scheduler.yml", candidate),
        agent=_basename(_CALLER_PATHS[baseline.WRITER_OPENCODE]),
        wip=profile.wip_limit,
        lease=profile.lease_minutes,
        attempts=profile.max_attempts,
    )


def _opencode(profile: ConsumerProfile, candidate: str, phase: str) -> str:
    swift = (
        "      swift_version: {}\n".format(profile.swift_version)
        if profile.swift_version
        else ""
    )
    return """name: {display}

# GENERATED BY CONTINUUM -- do not edit by hand.
#
# Where this repository's agent runs, and nothing about what it decides. The
# scheduler, the repair controller and the review controller all dispatch into
# this workflow, so it exists here rather than in Continuum: the runner and the
# toolchain are this repository's facts, and no platform, language or vendor name
# appears anywhere in the shared controller.
#
# `runner` and `swift_version` are the whole of that choice. `model` is this
# repository's configured OpenCode route.
#
# The dispatch modes are Continuum's: `issue` for a new task, `resolve-conflict`,
# `ci-fix` and `review-fix` for the three repair rungs. Which reviewer produced
# the findings in `review-fix` never reaches this file.

on:
  # The controllers dispatch into this workflow, so it has to be dispatchable with
  # exactly the inputs they send and nothing else.
  workflow_dispatch:
    inputs:
      mode:
        description: "issue, resolve-conflict, ci-fix, or review-fix"
        required: true
        type: string
      issue_number:
        required: false
        type: string
      pr_number:
        required: false
        type: string
      head_ref:
        required: false
        type: string
      run_id:
        required: false
        type: string
      rungs_ruled_out:
        required: false
        type: string
      runner:
        required: false
        type: string
      swift_version:
        required: false
        type: string
      model:
        required: false
        type: string

permissions:
  contents: read
  issues: write
  pull-requests: write

jobs:
  agent:
    uses: {opencode}
    permissions:
      actions: read
      contents: write
      issues: write
      pull-requests: write
    secrets:
      OPENCODE_API_KEY: ${{{{ secrets.{api_key} }}}}
    with:
      mode: ${{{{ inputs.mode }}}}
      issue_number: ${{{{ inputs.issue_number }}}}
      pr_number: ${{{{ inputs.pr_number }}}}
      head_ref: ${{{{ inputs.head_ref }}}}
      run_id: ${{{{ inputs.run_id }}}}
      rungs_ruled_out: ${{{{ inputs.rungs_ruled_out }}}}
      runner: {runner}
{swift}      model: ${{{{ inputs.model || '{model}' }}}}
""".format(
        display=AGENT_DISPLAY_NAME,
        opencode=_uses("consumer-opencode.yml", candidate),
        api_key=profile.opencode_secret,
        runner=profile.runner,
        swift=swift,
        model=profile.model,
    )


def _repair(profile: ConsumerProfile, candidate: str, phase: str) -> str:
    # `workflow_run.workflows` matches display names, so the agent workflow and
    # this repository's own CI workflows are both named here. CI recovery is the
    # reason the agent workflow appears at all: a dispatched run that dies has to
    # release its reservation or the queue stops moving.
    watched = [AGENT_DISPLAY_NAME] + list(profile.required_ci) + list(profile.additional_gates)
    return """name: Continuum repair

# GENERATED BY CONTINUUM -- do not edit by hand.
#
# The three repair rungs, in one place: a merge conflict on an open pull
# request, a failing exact-head gate, and a dispatched agent run that died
# without releasing its reservation. The ladder, the attempt limit, the budget and
# the recovery rungs all live in the controller; this file says which events are
# the ones worth reacting to.
#
# `workflow_run.workflows` has to be declared statically, and GitHub matches it
# against each workflow's display name rather than its filename. It names this
# repository's own CI workflows ({gates}) as well as the agent workflow, because
# a name GitHub cannot find produces a trigger that never fires -- a gate that
# looks configured and is silently inapplicable.

on:
  pull_request_target:
    types: [opened, reopened, synchronize, ready_for_review]
  workflow_run:
    workflows: {watched}
    types: [completed]
  workflow_dispatch: {{}}

permissions:
  actions: write
  contents: read
  issues: write
  pull-requests: write

concurrency:
  group: continuum-consumer-repair
  cancel-in-progress: false

jobs:
  repair:
    uses: {repair}
    permissions:
      actions: write
      contents: read
      issues: write
      pull-requests: write
    with:
      opencode_workflow: {agent}
      ci_workflow: {primary}
      additional_blocking_workflows: "{additional}"
      scheduler_workflow: {scheduler}
      max_attempts: {attempts}
""".format(
        repair=_uses("consumer-repair.yml", candidate),
        watched=_pins(watched),
        gates=", ".join(profile.required_ci),
        agent=_basename(_CALLER_PATHS[baseline.WRITER_OPENCODE]),
        primary=profile.required_ci[0],
        additional=_gate_list(profile.additional_gates),
        scheduler=_basename(_CALLER_PATHS[baseline.WRITER_SCHEDULER]),
        attempts=profile.max_attempts,
    )


def _review_gate(profile: ConsumerProfile, candidate: str, phase: str) -> str:
    watched = list(profile.required_ci) + list(profile.additional_gates)
    return """name: Continuum review gate

# GENERATED BY CONTINUUM -- do not edit by hand.
#
# Closes the review loop: observe the review state on the exact head, normalize
# it, dispatch a repair when there is something to repair, and re-check. Nothing
# about that is here. There is no reviewer named in this file and no switch passed
# in -- `review` is a declaration in {config}, read from the base branch on every
# run, because a second place to declare the same switch is a second thing that
# can disagree with the first.
#
# `continuum_token` is this repository's existing write credential, reused in
# place. A reusable workflow called from here receives no token of its own, so
# this is how the controller fans out to downstream workflows.

on:
  # A new or moved HEAD is the only thing that invalidates an exact-head review.
  pull_request_target:
    types: [opened, reopened, synchronize, ready_for_review]

  # CI completing is how a pushed repair comes back to be looked at.
  workflow_run:
    workflows: {watched}
    types: [completed]

  # Recovery. A loop that stalls between two events would leave a pull request
  # waiting for a wake-up that already happened.
  schedule:
    - cron: "*/10 * * * *"

  workflow_dispatch:
    inputs:
      pr_number:
        description: "Pull request number"
        required: false
        type: string

permissions:
  actions: write
  checks: write
  contents: read
  issues: write
  pull-requests: write

concurrency:
  group: continuum-consumer-review-gate
  cancel-in-progress: false

jobs:
  reconcile:
    uses: {gate}
    permissions:
      actions: write
      checks: write
      contents: read
      issues: write
      pull-requests: write
    secrets:
      continuum_token: ${{{{ secrets.{token} }}}}
    with:
      pr_number: ${{{{ github.event.pull_request.number || inputs.pr_number }}}}
      agent_workflow: {agent}
      config_path: {config}
""".format(
        gate=_uses("consumer-review-gate.yml", candidate),
        watched=_pins(watched),
        config=CONSUMER_CONFIG_PATH,
        token=profile.token_secret,
        agent=_basename(_CALLER_PATHS[baseline.WRITER_OPENCODE]),
    )


def _merge(profile: ConsumerProfile, candidate: str, phase: str) -> str:
    return """name: Continuum merge

# GENERATED BY CONTINUUM -- do not edit by hand.
#
# Merge eligibility, and nothing else. There is no required-check list, no
# review rule and no timestamp comparison in this file: the controller reads the
# normalized gate contract, re-derives eligibility from live state on every
# wake-up, and refuses any pull request whose *current* head lacks a green
# exact-head gate. A decision that lived here would be a second implementation
# of the merge policy, and the two would drift.
#
# `additional_blocking_workflows` is this repository's own extra gates beyond its
# main CI ({extra}). The rule is conditional on presence: a path-filtered
# workflow that did not run for a head is not applicable, and one that did run
# must pass on that exact head. The repair controller is given the same list, so
# a failing one is repairable without anything about it being named in Continuum.

on:
  pull_request_target:
    types: [opened, reopened, synchronize, ready_for_review, labeled, unlabeled]
  pull_request_review:
    types: [submitted]
  workflow_run:
    workflows: {watched}
    types: [completed]
  status: {{}}
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch: {{}}

permissions:
  actions: write
  contents: write
  issues: write
  pull-requests: write
  statuses: read

concurrency:
  group: continuum-consumer-auto-merge
  cancel-in-progress: false

jobs:
  merge:
    uses: {merge}
    permissions:
      actions: write
      contents: write
      issues: write
      pull-requests: write
      statuses: read
    secrets:
      continuum_token: ${{{{ secrets.{token} }}}}
    with:
      config_path: {config}
      additional_blocking_workflows: "{additional}"
      scheduler_workflow: {scheduler}
      release_workflow: {release}
""".format(
        merge=_uses("consumer-auto-merge.yml", candidate),
        watched=_pins(profile.required_ci),
        extra=", ".join(profile.additional_gates) or "none",
        config=CONSUMER_CONFIG_PATH,
        token=profile.token_secret,
        additional=_gate_list(profile.additional_gates),
        scheduler=_basename(_CALLER_PATHS[baseline.WRITER_SCHEDULER]),
        # Phase A leaves the consumer's release machinery exactly where it is, so
        # this names the consumer's own entrypoint rather than a Continuum one.
        # With `release: false` in the contract the controller dispatches nothing;
        # the name is here so the later release phase has the right target
        # without another change to the consumer's workflows.
        release=profile.release_workflow,
    )


_CALLER_SPECS: Dict[str, CallerSpec] = {
    baseline.WRITER_SCHEDULER: CallerSpec(
        role=baseline.WRITER_SCHEDULER,
        reusable="consumer-scheduler.yml",
        render=_scheduler,
    ),
    baseline.WRITER_OPENCODE: CallerSpec(
        role=baseline.WRITER_OPENCODE,
        reusable="consumer-opencode.yml",
        render=_opencode,
    ),
    baseline.WRITER_REPAIR: CallerSpec(
        role=baseline.WRITER_REPAIR,
        reusable="consumer-repair.yml",
        render=_repair,
    ),
    baseline.WRITER_REVIEW: CallerSpec(
        role=baseline.WRITER_REVIEW,
        reusable="consumer-review-gate.yml",
        render=_review_gate,
    ),
    baseline.WRITER_MERGE: CallerSpec(
        role=baseline.WRITER_MERGE,
        reusable="consumer-auto-merge.yml",
        render=_merge,
    ),
}


def read_plan(document: Mapping[str, Any]) -> CutoverPlan:
    """Read a plan back, refusing a document this module cannot vouch for.

    The rollout is a reconciliation rather than a run, so a plan recorded by one
    invocation has to be readable by the next. Edits are restored in full,
    including the content of each generated file, because a controller that
    re-rendered them would be rendering against its own template rather than
    against what the reviewed change actually contains.
    """

    if not isinstance(document, Mapping):
        raise PlanError("plan_not_a_mapping", "a plan must be an object")
    schema = document.get("schema")
    if schema != PLAN_SCHEMA:
        raise PlanError(
            "unknown_plan_schema", "expected {!r}, found {!r}".format(PLAN_SCHEMA, schema)
        )
    edits: List[FileEdit] = []
    contents = document.get("contents", {}) or {}
    for entry in document.get("edits", []) or []:
        if not isinstance(entry, Mapping):
            raise PlanError("plan_edit_not_a_mapping", "each edit must be an object")
        path = str(entry.get("path", ""))
        if not path:
            raise PlanError("plan_edit_path_missing", "every edit must name its path")
        edits.append(
            FileEdit(
                path=path,
                action=str(entry.get("action", "")),
                writer=str(entry.get("writer", "")),
                content=str(contents.get(path, "")),
            )
        )
    return CutoverPlan(
        repository=str(document.get("repository", "")),
        phase=str(document.get("phase", "")),
        candidate=str(document.get("candidate", "")),
        inventory_digest=str(document.get("inventory_digest", "")),
        rollback_revision=str(document.get("rollback_revision", "")),
        edits=tuple(edits),
        retained=tuple(
            (str(entry.get("path", "")), str(entry.get("writer", "")))
            for entry in document.get("retained", []) or []
            if isinstance(entry, Mapping)
        ),
        labels=tuple(
            (str(entry.get("name", "")), str(entry.get("color", "")), str(entry.get("description", "")))
            for entry in document.get("labels", []) or []
            if isinstance(entry, Mapping)
        ),
        cutover_head=str(document.get("cutover_head", "")),
    )
