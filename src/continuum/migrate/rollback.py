"""The way back, and the record of where it points.

A cutover with no recorded way back is refused by the gate with ``no_rollback``,
and that is not a formality. #28 keeps the pre-cutover revision as the rollback
target until the first Continuum-managed production lifecycle *and* release have
been verified, so for the length of the observation window that revision is the
difference between a bad afternoon and an incident.

Three properties, and each one is about a way this can go wrong.

**The target is recorded inside the cutover itself.** The atomic change writes a
small record naming the exact default-branch SHA it was planned against. It
travels with the merge, so the rollback controller finds the target by reading
one file -- not by asking Continuum's main branch what it remembers, which is
exactly the knowledge a later force-push there, or a rollback of Continuum
itself, would take away at the worst possible moment.

**Rollback is one change, not two.** The state being escaped from is a repository
where the legacy scheduler has been removed and the Continuum one is not there
yet: no scheduler at all, and a different failure from the dual-writer window
this cutover was built to avoid. So the restore reinstates the recorded workflows
and removes the generated callers in a single commit, and it asserts the same
single-writer property on the way back that the plan asserted on the way in.

**It refuses to half-do itself.** A rollback whose recorded revision does not
contain a file the record says it removed, or whose restore would leave a role
with two implementations, is refused rather than partially applied. A rollback
that has already happened is a no-op that reports success, because the second
thing an operator does after pressing the button is press it again.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..shadow import baseline
from . import inventory as inventory_module
from .inventory import CUTOVER_RECORD_PATH, Inventory
from .plan import FILE_MODE, generated_role

RECORD_SCHEMA = "continuum.cutover-record/v1"

#: Where the cutover records where it can be undone, re-exported from the
#: inventory so there is one definition of which paths Continuum owns. Under
#: ``.github/`` because that is where a repository's automation metadata lives
#: and where a person looking for it will look. It carries ``writer: other``, so
#: nothing mistakes it for a controller and the single-writer rule ignores it.
RECORD_PATH = CUTOVER_RECORD_PATH

#: The branch a rollback pull request lives on. Fixed, like the cutover branch, so
#: a second rollback finds the first one instead of opening a rival.
BRANCH = "continuum/rollback"


class RollbackError(ValueError):
    """The rollback cannot be performed from what it was given."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class CutoverRecord:
    """What the cutover left behind, readable by anyone with read permission."""

    repository: str
    phase: str
    #: The immutable Continuum revision that was installed.
    revision: str = ""
    #: The default-branch SHA the cutover was planned against: the last revision
    #: at which the legacy writers were the only writers.
    rollback_revision: str = ""
    #: The generated callers the cutover added. Removing these is the rollback.
    installed: Tuple[str, ...] = ()
    #: The legacy writers the cutover removed. Restoring these is the rollback.
    retired: Tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        return bool(self.rollback_revision) and bool(self.installed or self.retired)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": RECORD_SCHEMA,
            "repository": self.repository,
            "phase": self.phase,
            "revision": self.revision,
            "rollback_revision": self.rollback_revision,
            "installed": list(self.installed),
            "retired": list(self.retired),
            "actionable": self.actionable,
        }


def render_record(
    repository: str,
    phase: str,
    *,
    revision: str,
    rollback_revision: str,
    installed: Sequence[str] = (),
    retired: Sequence[str] = (),
) -> str:
    """The record, as the bytes the atomic change writes.

    Rendered by the plan rather than by the apply stage so it is part of the
    reviewed change. A rollback target discovered after the merge, by a controller
    reading its own history, is a rollback target that can disagree with the one
    everybody agreed to.
    """

    record = CutoverRecord(
        repository=repository,
        phase=phase,
        revision=revision,
        rollback_revision=rollback_revision,
        installed=tuple(installed),
        retired=tuple(retired),
    )
    return json.dumps(record.describe(), indent=2, sort_keys=True) + "\n"


def read_record(document: Mapping[str, Any]) -> CutoverRecord:
    """Read a cutover record back, refusing anything that cannot be acted on.

    Strict about the fields a rollback needs, lenient about the rest, and
    deliberately reporting ``actionable: false`` for a record with no rollback
    revision rather than parsing it into a revision of ``""``. "There is nothing
    to roll back to" and "roll back to the empty tree" must never be the same
    code path.
    """

    if not isinstance(document, Mapping):
        raise RollbackError("record_not_a_mapping", "a cutover record must be an object")
    schema = document.get("schema")
    if schema != RECORD_SCHEMA:
        raise RollbackError(
            "unknown_record_schema",
            "expected {!r}, found {!r}".format(RECORD_SCHEMA, schema),
        )
    return CutoverRecord(
        repository=str(document.get("repository", "")),
        phase=str(document.get("phase", "")),
        revision=str(document.get("revision", "")),
        rollback_revision=str(document.get("rollback_revision", "")),
        installed=tuple(str(item) for item in document.get("installed", []) or []),
        retired=tuple(str(item) for item in document.get("retired", []) or []),
    )


# --------------------------------------------------------------------------- #
# Deciding what to restore
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Restore:
    """What a rollback has to do, derived from two documented readings.

    Derived rather than executed in place, because the decision is the part that
    is easy to get wrong under pressure, and it is a pure function of the cutover
    record and the consumer's current inventory. So it can be printed, reviewed,
    and tested without a token.
    """

    #: Paths to reinstate from the recorded revision.
    restore: Tuple[str, ...] = ()
    #: Paths to remove: generated callers the cutover installed and that are
    #: still present.
    remove: Tuple[str, ...] = ()
    #: Paths named by the record that are present but no longer match the
    #: recorded revision. Left alone, and reported, because overwriting a change
    #: made after the cutover is a second incident.
    divergent: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    @property
    def noop(self) -> bool:
        return not self.restore and not self.remove

    def describe(self) -> Dict[str, Any]:
        return {
            "restore": list(self.restore),
            "remove": list(self.remove),
            "divergent": list(self.divergent),
            "notes": list(self.notes),
            "noop": self.noop,
        }


def compute_restore(
    record: CutoverRecord,
    inventory: Inventory,
    *,
    target_blobs: Optional[Mapping[str, str]] = None,
    target_writers: Optional[Mapping[str, str]] = None,
) -> Restore:
    """Work out the restore, or refuse to.

    ``target_blobs`` and ``target_writers`` describe the recorded rollback
    revision: what each retired path's blob SHA is there, and which writer role
    that revision's file was audited as. Both are read from the recorded revision
    rather than assumed, because a rollback that trusts an assumption about a
    commit it is about to restore is restoring something other than the revision
    the record names.

    The refusal that matters is ``stranded_generated_caller``. Restoring a legacy
    writer beside a file that took over its job while the cutover was in would
    put two implementations of one role back in place. That is the dual-writer
    state this whole two-phase cutover exists to prevent, and a rollback that
    produced one would turn a recoverable incident into an unrecoverable one.
    """

    if not record.actionable:
        raise RollbackError(
            "record_not_actionable",
            "{} names no rollback revision, so there is nothing to restore to. A "
            "cutover is not complete without one, and this record is not one that "
            "was.".format(RECORD_PATH),
        )
    if not inventory.head_sha:
        raise RollbackError(
            "current_head_unread",
            "the consumer's default-branch HEAD could not be read, so a rollback "
            "cannot tell what state it would be undoing",
        )

    target_blobs = target_blobs or {}
    target_writers = target_writers or {}
    notes: List[str] = []
    present = inventory.workflow_map
    restore: List[str] = []
    divergent: List[str] = []
    for path in record.retired:
        current = present.get(path)
        if current is None:
            restore.append(path)
            continue
        expected = str(target_blobs.get(path, ""))
        if not expected:
            notes.append(
                "{} still exists but the recorded revision's copy of it could not be "
                "read, so it will be left as it is".format(path)
            )
            continue
        if current.blob_sha and current.blob_sha != expected:
            divergent.append(path)

    remove: List[str] = [path for path in record.installed if path in present]
    absent_installed = [path for path in record.installed if path not in present]
    if absent_installed:
        notes.append(
            "already absent, nothing to remove: {}".format(", ".join(sorted(absent_installed)))
        )

    stranded = _stranded_roles(record, inventory, restore, remove, target_writers)
    if stranded:
        role, paths = stranded[0]
        raise RollbackError(
            "stranded_generated_caller",
            "rolling back would leave {} implemented twice, by {}. The cutover's "
            "single-writer rule does not apply to a rollback, so this is checked "
            "separately and refused here: close or merge the change that took the "
            "job over first.".format(role, ", ".join(paths)),
        )

    for path in divergent:
        notes.append(
            "{} changed after the cutover (now {}), so it will be left alone rather "
            "than overwritten".format(path, present[path].blob_sha[:12])
        )

    if not restore and not remove:
        notes.append("the repository already matches the recorded cutover; nothing to undo")

    return Restore(
        restore=tuple(sorted(restore)),
        remove=tuple(sorted(remove)),
        divergent=tuple(sorted(divergent)),
        notes=tuple(notes),
    )


def _stranded_roles(
    record: CutoverRecord,
    inventory: Inventory,
    restore: Sequence[str],
    remove: Sequence[str],
    target_writers: Mapping[str, str],
) -> List[Tuple[str, Tuple[str, ...]]]:
    """Roles that the restore would implement twice.

    Computed from the whole post-change surface rather than from the files being
    touched, because the second implementation is usually a file nobody thinks of
    as a writer at all: the retry workflow, the label reconciliation, the queue.
    Each is a separate file implementing one job, which is why "one file per role"
    was never the invariant.

    Generated callers do not count as a survivor unless the role they implement
    is one this rollback is handing back. A Continuum caller still present after
    the rollback is either one this rollback removes -- handled by ``remove`` --
    or a Phase B caller for a role Phase A never installed, and a Phase A restore
    cannot collide with either of those. A caller for a role that *is* being
    handed back is the collision, and is counted.
    """

    handed_back = {
        str(target_writers.get(path, ""))
        for path in restore
        if str(target_writers.get(path, ""))
    }

    survivors: Dict[str, List[str]] = {}
    for entry in inventory.workflows:
        if entry.path in remove:
            continue
        if entry.writer in ("", baseline.WRITER_OTHER):
            continue
        if entry.kind == inventory_module.KIND_CONTINUUM_CALLER and generated_role(
            entry.path
        ) in handed_back:
            continue
        survivors.setdefault(entry.writer, []).append(entry.path)

    for path in restore:
        role = str(target_writers.get(path, ""))
        if not role or role == baseline.WRITER_OTHER:
            continue
        survivors.setdefault(role, []).append(path)

    stranded: List[Tuple[str, Tuple[str, ...]]] = []
    for role, paths in sorted(survivors.items()):
        # One path per role is the restored file itself; two is the problem.
        unique = tuple(sorted(set(paths)))
        if len(unique) > 1:
            stranded.append((role, unique))
    return stranded


# --------------------------------------------------------------------------- #
# Performing the rollback
# --------------------------------------------------------------------------- #


def reconcile(
    client: Any,
    record: CutoverRecord,
    inventory: Inventory,
    *,
    branch: str = BRANCH,
    base_sha: str = "",
    target_blobs: Optional[Mapping[str, str]] = None,
    target_writers: Optional[Mapping[str, str]] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Perform the rollback as one commit on one branch, idempotently.

    The same reconciliation shape as the cutover and for the same reasons: a
    restore that needs two steps has a window in which the repository is in a
    state neither the old nor the new system can act in, and a restore that opens
    a new pull request each time is a restore nobody can find.
    """

    restore = compute_restore(
        record, inventory, target_blobs=target_blobs, target_writers=target_writers
    )
    if restore.noop:
        return {"ok": True, "noop": True, "restore": restore.describe()}

    base_sha = base_sha or inventory.head_sha
    entries: List[Dict[str, Any]] = []
    reinstated: List[str] = []
    for path in restore.restore:
        content = client.read_file_at_ref(path, record.rollback_revision)
        if content is None:
            raise RollbackError(
                "rollback_target_missing_file",
                "{} is recorded as retired by the cutover, but it does not exist at "
                "{}, so the recorded revision is not the one this rollback would "
                "restore.".format(path, record.rollback_revision),
            )
        entries.append(
            {
                "path": path,
                "mode": FILE_MODE,
                "type": "blob",
                "sha": str(client.create_blob(content)),
            }
        )
        reinstated.append(path)
    for path in restore.remove:
        entries.append({"path": path, "mode": FILE_MODE, "type": "blob", "sha": None})
    # The record goes with the change. Leaving it behind would tell the next
    # reader a Continuum cutover is in force when it has just been undone, and a
    # second rollback would then try to undo the first.
    entries.append({"path": RECORD_PATH, "mode": FILE_MODE, "type": "blob", "sha": None})

    tree = str(client.create_tree(base_sha, entries))
    commit = str(
        client.create_commit(
            "Roll back the Continuum cutover to {}".format(record.rollback_revision),
            tree,
            [base_sha],
        )
    )
    existing = client.get_ref("refs/heads/{}".format(branch)) or ""
    if not existing:
        client.update_ref("refs/heads/{}".format(branch), commit)
    elif str(existing) != commit:
        client.update_ref("refs/heads/{}".format(branch), commit, expected=str(existing))

    number = _find_pull_request(client, branch)
    if not number:
        opened = client.create_pull_request(
            title=title or "Roll back the Continuum cutover",
            body=render_body(record, restore),
            head=branch,
            base=str(getattr(client, "default_branch", "") or "main"),
        )
        number = int((opened or {}).get("number", 0) or 0)
        if not number:
            raise RollbackError(
                "rollback_pull_request_not_created",
                "GitHub returned no pull request number for the rollback",
            )

    return {
        "ok": True,
        "noop": False,
        "commit": commit,
        "pull_request": number,
        "reinstated": reinstated,
        "removed": list(restore.remove),
        "divergent": list(restore.divergent),
        "notes": list(restore.notes),
    }


def _find_pull_request(client: Any, branch: str) -> int:
    """The open rollback pull request on this branch, if there is one.

    By head branch rather than by title, for the same reason the cutover searches
    that way: a title can be edited, and a search that misses an existing pull
    request opens a rival one.
    """

    for pull in client.list_pull_requests(state="open") or []:
        if str((pull.get("head") or {}).get("ref", "")) == branch:
            return int(pull.get("number", 0) or 0)
    return 0


def render_body(record: CutoverRecord, restore: Restore) -> str:
    """The rollback pull request body.

    For the person reading it while something is broken, so the revision and the
    reason come first and the mechanism second.
    """

    lines = [
        "Generated by the Continuum migration controller.",
        "",
        "Restores `{}` to `{}`.".format(record.repository, record.rollback_revision),
        "",
        "This is one change on purpose. The state being escaped from has the legacy",
        "writers removed and the generated callers not yet reinstated, and a",
        "repository with no scheduler is worse than either side of the cutover.",
        "",
        "Reinstated:",
        "",
    ]
    lines.extend("- `{}`".format(path) for path in restore.restore or ("nothing",))
    lines.extend(["", "Removed:", ""])
    lines.extend("- `{}`".format(path) for path in restore.remove or ("nothing",))
    if restore.divergent:
        lines.extend(["", "Left alone, because they changed after the cutover:", ""])
        lines.extend("- `{}`".format(path) for path in restore.divergent)
    for note in restore.notes:
        lines.extend(["", note])
    return "\n".join(lines) + "\n"
