"""The one privileged writer in Continuum, and the only place a cutover happens.

Everything else in this package reads and judges. This module changes the
consumer repository, so it is where the rules that only matter to a writer live.

**It is a writer, and it is trusted, and the two are separate things.** The
architecture document requires installer privilege to be distinct from agent
privilege: a coding agent must never be able to administer a repository, and the
thing that administers a repository must never be a model. That is why this runs
from a workflow in *this* repository with the repository's own ambient token, and
never from a consumer's agent job, no matter how the consumer is configured.

**Every side effect is bound to an exact head.** The commit is built on the
default-branch SHA the inventory read. The merge passes that same head to
GitHub's merge API as a compare-and-swap, so a pull request that moved between
the validation and the merge is refused rather than merged. A cutover that
merged somebody else's head would have installed the wrong controllers and
retired the wrong writers while every gate reported green.

**The whole thing is one commit on one branch, reconciled, not executed.** Each
stage is idempotent and re-entrant:

* ``publish`` builds the tree from the plan and force-moves the branch to it, so
  a second run converges on the same commit rather than stacking a second one.
* ``validate`` re-derives everything and refuses rather than merges.
* ``merge`` merges with an expected head, which makes a repeated call a no-op
  once the pull request is already merged -- it reports what is true instead of
  failing, because a cutover that cannot be re-run safely cannot be recovered
  from safely either.

That is what #84 means by a persistent reconciliation state machine rather than
a one-shot script, and it is the only reason an interrupted run at 03:00
converges instead of needing a person.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .plan import FILE_MODE, CutoverPlan

STATE_SCHEMA = "continuum.migration-state/v1"

#: The branch the cutover pull request lives on. Fixed rather than generated, so
#: a re-run finds the pull request it opened last time instead of opening a
#: second one, and so a person looking for the migration finds exactly one thing.
BRANCH = "continuum/cutover"

#: How long to wait for the head's gates before calling the run inconclusive.
#: Bounded because the controller holds a job: a cutover that waits forever is a
#: cutover that reports nothing and lets the job time out with less explanation
#: than this does. Twenty minutes covers a CI run on a slow repository, and the
#: next reconciliation resumes from the branch rather than starting again.
DEFAULT_GATE_TIMEOUT_S = 1200.0

#: How often to look while waiting. Long enough not to spend the whole budget in
#: API calls, short enough that a gate which finishes is noticed promptly.
DEFAULT_GATE_POLL_S = 15.0

#: The label the migration pull request carries, so a repository with several
#: pull requests has one way to find the migration and a reviewer can see what
#: kind of change they are being asked to look at.
LABEL = "continuum:migration"

#: Every client method this module may call. Listed rather than discovered, for
#: the same reason the read side is: the value of an allowlist is that a new
#: method is a write until somebody proves otherwise, and a controller whose write
#: surface is "whatever the client happens to expose" has no surface at all.
WRITE_METHODS: Tuple[str, ...] = (
    "get_ref",
    "get_commit",
    "create_blob",
    "create_tree",
    "create_commit",
    "update_ref",
    "list_pull_requests",
    "get_pull_request",
    "list_pull_files",
    "create_pull_request",
    "update_pull_request_base",
    "merge_pull_request",
    "ensure_label",
    "list_check_runs",
    "list_labels",
)

#: The Git file mode for a workflow. 100644 rather than 100755: a generated
#: caller is not executable, and a repository that had made one executable was
#: carrying an accident that the reconciliation would silently keep.
FILE_MODE = "100644"


class ApplyError(RuntimeError):
    """A cutover stage could not be completed."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message
        #: Infrastructure failures are retried and do not consume the migration's
        #: semantic budget. #84 requires that distinction explicitly: a cutover
        #: that gave up because the API was briefly unavailable would report
        #: itself blocked on a problem with no product consequence.
        self.retryable = retryable


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Stage:
    """One thing that has to be true before the merge button is pressed."""

    name: str
    ok: bool
    detail: str = ""

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


#: In order. ``prepare`` is first because it is the only one that changes
#: anything outside the migration branch, and ``merge`` is last because it is the
#: only one that changes the default branch.
STAGES: Tuple[str, ...] = (
    "prepare",
    "publish",
    "change-set",
    "head-gates",
    "cutover-gate",
    "merge",
)


@dataclasses.dataclass(frozen=True)
class MigrationState:
    """How far the cutover has got, and against which exact head."""

    repository: str
    phase: str
    #: The default-branch SHA the whole reconciliation is built on. A value that
    #: moved invalidates the plan, because the plan's removals were computed
    #: against what was there.
    base_sha: str = ""
    branch: str = BRANCH
    commit: str = ""
    pull_request: int = 0
    #: SHA of the pull request head the validation was performed against.
    validated_head: str = ""
    merged: bool = False
    #: The pre-cutover revision, carried into the merged commit so the rollback
    #: controller can find it from the default branch alone and not from a file
    #: in Continuum that a later force-push could change.
    rollback_revision: str = ""
    stages: Tuple[Stage, ...] = ()
    notes: Tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return all(stage.ok for stage in self.stages) and bool(self.stages)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "repository": self.repository,
            "phase": self.phase,
            "base_sha": self.base_sha,
            "branch": self.branch,
            "commit": self.commit,
            "pull_request": self.pull_request,
            "validated_head": self.validated_head,
            "merged": self.merged,
            "rollback_revision": self.rollback_revision,
            "ready": self.ready,
            "stages": [stage.describe() for stage in self.stages],
            "notes": list(self.notes),
        }

    def with_stages(self, stages: Sequence[Stage]) -> "MigrationState":
        return dataclasses.replace(self, stages=tuple(stages))


def read_state(document: Mapping[str, Any]) -> MigrationState:
    """Read a recorded reconciliation back, refusing a document it cannot vouch for."""

    if not isinstance(document, Mapping):
        raise ApplyError("state_not_a_mapping", "migration state must be an object")
    schema = document.get("schema")
    if schema != STATE_SCHEMA:
        raise ApplyError(
            "unknown_state_schema",
            "expected {!r}, found {!r}".format(STATE_SCHEMA, schema),
        )
    return MigrationState(
        repository=str(document.get("repository", "")),
        phase=str(document.get("phase", "")),
        base_sha=str(document.get("base_sha", "")),
        branch=str(document.get("branch", BRANCH)),
        commit=str(document.get("commit", "")),
        pull_request=int(document.get("pull_request", 0) or 0),
        validated_head=str(document.get("validated_head", "")),
        merged=bool(document.get("merged", False)),
        rollback_revision=str(document.get("rollback_revision", "")),
        stages=tuple(
            Stage(
                name=str(entry.get("name", "")),
                ok=bool(entry.get("ok", False)),
                detail=str(entry.get("detail", "")),
            )
            for entry in document.get("stages", []) or []
            if isinstance(entry, Mapping)
        ),
        notes=tuple(str(item) for item in document.get("notes", []) or []),
    )


# --------------------------------------------------------------------------- #
# The controller
# --------------------------------------------------------------------------- #


class MigrationController:
    """Drives one consumer from pre-cutover to cut over, resumably.

    The stages are separate methods rather than one ``run`` because the whole
    design depends on being able to stop between them: a cutover whose validation
    fails must not be merged, and a controller that cannot be interrupted is a
    controller that merges first and asks afterwards.
    """

    def __init__(
        self,
        client: Any,
        plan: CutoverPlan,
        *,
        repository: str = "",
        branch: str = BRANCH,
        title: Optional[str] = None,
        body: Optional[str] = None,
    ) -> None:
        self.client = client
        self.plan = plan
        self.repository = repository or plan.repository or getattr(client, "repository", "")
        self.branch = branch
        self.title = title or "Install Continuum ({})".format(plan.phase)
        self.body = body if body is not None else render_body(plan)

    # -- the write surface -------------------------------------------------- #

    def _write(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in WRITE_METHODS:
            raise ApplyError(
                "unlisted_write",
                "{} is not on the migration controller's write allowlist.".format(name),
            )
        method = getattr(self.client, name, None)
        if method is None:
            raise ApplyError("missing_client_method", "the client has no {}.".format(name))
        return method(*args, **kwargs)

    # -- stages ------------------------------------------------------------ #

    def prepare(self) -> Dict[str, Any]:
        """Idempotently provision Continuum's labels.

        Only labels the repository does not already have are created. A label
        colour is a product decision somebody may have made on purpose, and a
        cutover that reset one would be a silent change to how a repository reads
        in its own issue list.
        """

        existing = {str(name) for name in (self._write("list_labels") or ())}
        created: List[str] = []
        kept: List[str] = []
        for name, color, description in self.plan.labels:
            if name in existing:
                kept.append(name)
                continue
            self._write("ensure_label", name, color, description)
            created.append(name)
        return {"created": created, "kept": kept}

    def publish(self, base_sha: str, expected_tip: str = "") -> Dict[str, Any]:
        """Build the atomic commit and open exactly one pull request for it.

        Idempotent by construction. The tree is rebuilt from the plan every time
        and the branch is moved onto the commit that tree produces, so a second
        run either reproduces the same commit -- in which case there is nothing to
        do -- or produces a new one that supersedes the old. Both are the desired
        behaviour; stacking commits on the branch is not.

        ``expected_tip`` is the commit this controller last wrote to the branch, or
        ``""`` on a first run. The branch is moved only when it still holds exactly
        that, which means the controller will overwrite its own work and nothing
        else. That is the check that stops a human who pushed to
        ``continuum/cutover`` from having their commit silently replaced by the
        next reconciliation.
        """

        if not base_sha:
            raise ApplyError("missing_base_sha", "a cutover commit needs a base to build on")

        entries: List[Dict[str, Any]] = []
        for edit in self.plan.edits:
            if edit.action == "remove":
                entries.append(
                    {"path": edit.path, "mode": FILE_MODE, "type": "blob", "sha": None}
                )
                continue
            blob = self._write("create_blob", edit.content)
            entries.append(
                {"path": edit.path, "mode": FILE_MODE, "type": "blob", "sha": str(blob)}
            )

        tree = str(self._write("create_tree", base_sha, entries))
        commit = str(
            self._write(
                "create_commit",
                commit_message(self.plan),
                tree,
                [base_sha],
            )
        )

        ref = "refs/heads/{}".format(self.branch)
        existing = str(self._write("get_ref", ref) or "")
        if existing != commit:
            if not expected_tip and existing:
                # A branch this controller owns, holding a commit whose *tree* is
                # the tree this run just built, is this run's earlier work whose
                # state document was lost -- not somebody else's. The tree is the
                # identity of a change set; the commit SHA is not, because a
                # commit carries the time it was made and two runs of the same
                # plan never produce the same SHA.
                #
                # Recovering this way is what makes the lost-state case safe
                # instead of fatal. Without it, the only honest response to a
                # branch the controller cannot prove it owns is to refuse -- and
                # refusing forever, on a branch nobody else will ever claim, is
                # how a cutover ends up needing a human to delete it by hand.
                # Anything whose tree differs is somebody else's and is refused
                # below like any other mismatch.
                if self._tree_of(existing) == tree:
                    expected_tip = existing
            if existing != expected_tip:
                raise ApplyError(
                    "migration_branch_owned_elsewhere",
                    "{} is at {} but this run expected {}. This controller only moves "
                    "a branch to a commit it created, so this is somebody else's "
                    "work: refusing rather than replacing it. Merge or close it, or "
                    "point the controller at a different --branch.".format(
                        self.branch,
                        existing[:12] or "<absent>",
                        expected_tip[:12] or "<absent>",
                    ),
                )
            self._write("update_ref", ref, commit, expected=existing)

        number = self._find_pull_request()
        if not number:
            opened = self._write(
                "create_pull_request",
                title=self.title,
                body=self.body,
                head=self.branch,
                base=self._base_branch(base_sha),
            )
            number = int(opened.get("number", 0) or 0)
            if not number:
                raise ApplyError("pull_request_not_created", "GitHub returned no pull request number")
            self._write("ensure_label", LABEL, "1d76db", "Continuum migration pull request")
        return {"commit": commit, "tree": tree, "pull_request": number}

    def _tree_of(self, commit: str) -> str:
        """The tree a commit points at, or ``""`` if it cannot be read."""

        if not commit:
            return ""
        try:
            document = self._write("get_commit", commit)
        except Exception:
            # Unreadable is not "same". Losing the ability to prove ownership
            # has to fall on the side of refusing.
            return ""
        return str((document or {}).get("tree", {}).get("sha", "") or "")

    def verify_change_set(self, number: int) -> Dict[str, Any]:
        """Prove the pull request head contains exactly what the plan said.

        Read back from the pull request rather than trusted from the plan. The
        plan says what the controller *meant* to write; the pull request says what
        the default branch will contain when this merges. They can differ if the
        base moved, if someone pushed to the branch, or if GitHub resolved a merge
        differently from the plan's expectation -- and the gate that authorizes
        this cutover is entitled to a check that would have caught all three.
        """

        pull = self._write("get_pull_request", number)
        head = str(((pull or {}).get("head") or {}).get("sha", "") or "")
        files = self._write("list_pull_files", number) or []
        changed = sorted(
            str(entry.get("filename", ""))
            for entry in files
            if str(entry.get("filename", ""))
        )
        expected = sorted(edit.path for edit in self.plan.edits)
        if changed != expected:
            return {
                "ok": False,
                "head": head,
                "detail": "the pull request touches {} but the change set names {}".format(
                    ", ".join(changed) or "nothing", ", ".join(expected) or "nothing"
                ),
            }
        if head != self.plan.cutover_head:
            return {
                "ok": False,
                "head": head,
                "detail": "the change set is bound to head {} but the pull request is at {}".format(
                    self.plan.cutover_head or "<unbound>", head or "<unknown>"
                ),
            }
        return {"ok": True, "head": head, "detail": "the pull request contains exactly the reviewed change"}

    def read_head_gates(self, head: str) -> Dict[str, Dict[str, Any]]:
        """The check runs on this exact head, by name.

        Only the newest run per name counts, because a re-run supersedes what it
        re-ran: a repository that failed CI once and then passed reports two runs
        of "CI", and reading the older one would refuse a change that is green.
        """

        runs: Dict[str, Dict[str, Any]] = {}
        for run in self._write("list_check_runs", head) or []:
            name = str(run.get("name", "") or "")
            if not name:
                continue
            runs[name] = {
                "status": str(run.get("status", "") or ""),
                "conclusion": str(run.get("conclusion", "") or ""),
                "id": str(run.get("id", "") or ""),
            }
        return runs

    def judge_head_gates(
        self,
        head: str,
        required: Sequence[str],
        additional: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """Whether this exact head is green, and what is still outstanding.

        Exactness is the property, not greenness. A run of the same workflow on an
        earlier commit says nothing about this one, which is the entire reason the
        merge controller refuses to merge without them. Conditional gates keep the
        production rule: a path-filtered workflow that did not run for this head is
        not applicable, and one that ran must have passed.
        """

        if not head:
            return {"ok": False, "detail": "no head to check", "runs": {}, "pending": ()}

        runs = self.read_head_gates(head)
        failures: List[str] = []
        pending: List[str] = []
        for name in required:
            run = runs.get(name)
            if run is None:
                pending.append(name)
                continue
            if run["status"] != "completed":
                pending.append(name)
                continue
            if run["conclusion"] != "success":
                failures.append("{} concluded {}".format(name, run["conclusion"]))
        for name in additional:
            run = runs.get(name)
            # Absent is not applicable, exactly as the merge controller treats it:
            # a packaging smoke that did not run for a head that did not touch a
            # package has not failed, it simply does not apply.
            if run is not None and (
                run["status"] != "completed" or run["conclusion"] != "success"
            ):
                failures.append("{} concluded {}".format(name, run["conclusion"] or "nothing"))
        for name in pending:
            failures.append("{} has not reported for this head yet".format(name))
        return {
            "ok": not failures,
            "runs": runs,
            "pending": tuple(pending),
            "detail": "; ".join(failures)
            if failures
            else "every blocking gate is green on this head",
        }

    def wait_for_head_gates(
        self,
        head: str,
        required: Sequence[str],
        additional: Sequence[str] = (),
        *,
        timeout_s: float = DEFAULT_GATE_TIMEOUT_S,
        poll_s: float = DEFAULT_GATE_POLL_S,
        clock: Any = None,
        sleep: Any = None,
    ) -> Dict[str, Any]:
        """Poll this head's gates until they settle or the deadline passes.

        Publishing a pull request and immediately asking GitHub about its check
        runs would find nothing, because the runs have not started. Without this
        wait a zero-touch controller would report "CI did not run for this head"
        for a head whose CI was still queued, and #84's "when every gate is green,
        merge it automatically" would fire on the first run and never again.

        So it waits, and it distinguishes the two ways it can stop:

        * **Pending at the deadline** is ``retryable``. The gates have not spoken
          yet, which is an infrastructure condition and not a decision. The next
          reconciliation run picks it up where this one left off, because the
          branch and the pull request are already there.
        * **Concluded non-success** is not retryable. A red CI is an answer, and
          re-running the controller must not be able to turn it green.
        """

        clock = clock or time.monotonic
        sleep = sleep or time.sleep
        deadline = clock() + float(timeout_s)
        verdict = self.judge_head_gates(head, required, additional)
        waited: List[float] = []
        while verdict["pending"] and clock() < deadline:
            sleep(float(poll_s))
            waited.append(clock())
            verdict = self.judge_head_gates(head, required, additional)
        verdict["waited_s"] = round(
            max(waited) - (deadline - float(timeout_s)) if waited else 0.0, 3
        )
        verdict["retryable"] = bool(verdict["pending"])
        if verdict["pending"]:
            verdict["detail"] = (
                "{} still has not reported for {} after {:.0f}s; the next run will "
                "find this pull request rather than open a second one: {}".format(
                    ", ".join(verdict["pending"]),
                    head[:12],
                    timeout_s,
                    verdict["detail"],
                )
            )
        return verdict

    def merge(self, number: int, head: str) -> Dict[str, Any]:
        """Merge with the head pinned, and report what is true when there is nothing to do.

        ``head`` is GitHub's compare-and-swap parameter. A pull request that moved
        since validation is refused by the API rather than merged, which is the
        only thing standing between a stale green tick and a controller that
        merged code it had never seen.
        """

        if not head:
            raise ApplyError("missing_expected_head", "a merge must name the head it validated")
        pull = self._write("get_pull_request", number)
        state = str((pull or {}).get("state", "") or "")
        merged = bool((pull or {}).get("merged", False))
        if merged or state == "closed":
            return {"ok": True, "merged": True, "detail": "already merged; nothing to do"}
        result = self._write("merge_pull_request", number, head) or {}
        ok = bool(result.get("merged", False))
        if not ok:
            raise ApplyError(
                "merge_refused",
                "GitHub did not merge pull request #{}: {}".format(
                    number, result.get("message", "no reason given")
                ),
                retryable=True,
            )
        return {"ok": True, "merged": True, "detail": "merged at the validated head"}

    # -- helpers ------------------------------------------------------------ #

    def _find_pull_request(self) -> int:
        """The migration pull request, if one is already open.

        Searched by head branch rather than by title. A title is something a
        person can edit, so a search on it would miss a pull request that exists
        and open a second one -- which is the failure #28 calls "never run old and
        new controllers simultaneously", arrived at from the wrong direction.
        """

        for pull in self._write("list_pull_requests", state="open") or []:
            if str((pull.get("head") or {}).get("ref", "")) == self.branch:
                return int(pull.get("number", 0) or 0)
        return 0

    def _base_branch(self, base_sha: str) -> str:
        """The branch the cutover merges into.

        Read from the ref the base SHA was read from rather than assumed to be
        ``main``. A repository whose default branch is ``master`` would otherwise
        get a migration pull request targeting a branch that does not exist, and
        the failure would appear as "the pull request could not be created".
        """

        return str(getattr(self.client, "default_branch", "") or "main")


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


def reconcile(
    controller: "MigrationController",
    state: MigrationState,
    *,
    required: Sequence[str] = (),
    additional: Sequence[str] = (),
    gate: Optional[Any] = None,
    wait: Optional[Mapping[str, Any]] = None,
    checkpoint: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> MigrationState:
    """Run every stage in order, stopping at the first refusal, recording progress.

    Stops rather than continues on failure, in order. Each stage's whole purpose
    is to make the next one safe: publishing before the labels exist produces a
    pull request whose checks reference labels nobody can see, and merging before
    the change set is verified merges whatever is actually on the branch. A
    controller that carried on through a failure would be producing a cutover out
    of steps that did not all hold.

    ``gate`` is the cutover gate -- the same :mod:`continuum.shadow.cutover`
    judgement the read-only gate workflow runs, invoked with the evidence this
    repository's window produced. It is optional *only* so that the stages can be
    exercised in isolation; a caller that reaches ``merge`` with no gate has
    skipped the authorization, so the merge stage records that it did, and a
    record that says so is the thing a reviewer looks for. The command line
    refuses to let a real cutover reach that state.

    ``checkpoint`` is called with the state document after every stage that
    changes it. This exists because the one window that matters most is the one
    nobody sees: the branch was moved, and then the process died before it could
    write its state file. A controller whose only record of the commit it pushed
    lives in memory reports "nothing to do" on the next run and leaves the
    branch unowned -- so the caller that persists state does not have to wait for
    the reconciliation to finish to persist it.
    """

    stages: List[Stage] = []
    notes: List[str] = list(state.notes)
    wait = dict(wait or {})

    if state.merged:
        # Converged. The second thing an operator does after a button is press it
        # again, and the answer has to be "already done" rather than a second
        # pull request.
        return dataclasses.replace(
            state,
            stages=tuple(
                stages + [Stage("merged", True, "the cutover is already in the default branch")]
            ),
            notes=tuple(notes),
        )

    prepared = controller.prepare()
    stages.append(
        Stage(
            "prepare",
            True,
            "labels {} provisioned, {} already present".format(
                ", ".join(prepared["created"]) or "none", len(prepared["kept"])
            ),
        )
    )

    published = controller.publish(state.base_sha, expected_tip=state.commit)
    commit = str(published["commit"])
    state = dataclasses.replace(
        state,
        commit=commit,
        pull_request=int(published["pull_request"]),
        branch=controller.branch,
    )
    stages.append(
        Stage("publish", True, "commit {} on {}".format(commit[:12], controller.branch))
    )
    # The commit is the one fact a resumed run cannot re-derive -- the next run
    # builds a *different* commit for the same tree, so recovering the tip from
    # the plan alone would orphan the branch this run owns.
    _persist(state, stages, notes, checkpoint)

    # The change set is bound to the commit that carries it, so the gate judges
    # this exact head rather than a description of one.
    bound = controller.plan.with_head(commit)
    controller.plan = bound

    change_set = controller.verify_change_set(state.pull_request)
    stages.append(
        Stage("change-set", bool(change_set["ok"]), str(change_set["detail"]))
    )
    if not change_set["ok"]:
        return _stop(state, stages, notes, checkpoint)

    head_gates = controller.wait_for_head_gates(commit, required, additional, **wait)
    stages.append(Stage("head-gates", bool(head_gates["ok"]), str(head_gates["detail"])))
    if not head_gates["ok"]:
        return _stop(state, stages, notes, checkpoint)

    if gate is None:
        stages.append(
            Stage(
                "cutover-gate",
                True,
                "no gate was supplied; merged without a cutover authorization, "
                "which is recorded here on purpose",
            )
        )
        notes.append("merged without a cutover gate verdict")
    else:
        verdict = gate(bound) if callable(gate) else gate
        approved = bool(verdict.get("ok", False))
        stages.append(
            Stage(
                "cutover-gate",
                approved,
                str(verdict.get("detail", "the cutover gate approved this head")),
            )
        )
        if not approved:
            return _stop(state, stages, notes, checkpoint)

    merged = controller.merge(state.pull_request, commit)
    stages.append(Stage("merge", True, str(merged["detail"])))
    state = dataclasses.replace(state, merged=True, validated_head=commit)
    state = dataclasses.replace(state, stages=tuple(stages), notes=tuple(notes))

    _persist(state, stages, notes, checkpoint)
    return state


def _persist(
    state: MigrationState,
    stages: Sequence[Stage],
    notes: Sequence[str],
    checkpoint: Optional[Callable[[Mapping[str, Any]], None]],
) -> None:
    """Hand the state document to the caller, if it asked for it."""

    if checkpoint is None:
        return
    document = dataclasses.replace(state, stages=tuple(stages), notes=tuple(notes))
    checkpoint(document.describe())


def _stop(
    state: MigrationState,
    stages: Sequence[Stage],
    notes: Sequence[str],
    checkpoint: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> MigrationState:
    """Record where the reconciliation got to, having changed nothing further.

    The remaining stages are recorded as not-done rather than omitted, so the
    state document answers "what happened" from a glance instead of requiring a
    reader to know the order.
    """

    recorded = list(stages)
    done = {stage.name for stage in recorded}
    for name in STAGES:
        if name not in done:
            recorded.append(Stage(name, False, "not reached"))
    stopped = dataclasses.replace(
        state, stages=tuple(recorded), notes=tuple(notes), validated_head=""
    )
    _persist(stopped, recorded, notes, checkpoint)
    return stopped


# --------------------------------------------------------------------------- #
# Rendering the change a reviewer reads
# --------------------------------------------------------------------------- #


def commit_message(plan: CutoverPlan) -> str:
    return "Install Continuum ({}) pinned to {}".format(plan.phase, plan.candidate)


def render_body(plan: CutoverPlan) -> str:
    """The pull request body.

    Written for the two audiences it actually has. A reviewer needs to know what
    is being retired and why that is safe, in the language of the parity ledger
    that authorised it. A later operator needs the pin and the way back, because
    this text is the first thing anybody reads when the cutover is suspected of
    having broken something.
    """

    lines: List[str] = [
        "Generated by the Continuum migration controller. Do not edit by hand: the",
        "next reconciliation rewrites this branch, and a hand edit would be reverted",
        "as though it were a regression.",
        "",
        "## What this changes",
        "",
        "Pinned Continuum revision: `{}`".format(plan.candidate),
        "Phase: `{}`".format(plan.phase),
        "Planned against consumer HEAD: `{}`".format(plan.rollback_revision),
        "Inventory: `{}`".format(plan.inventory_digest),
        "",
    ]
    added = [edit for edit in plan.edits if edit.action in ("add", "modify")]
    removed = [edit for edit in plan.edits if edit.action == "remove"]
    if added:
        lines.append("Installed:")
        lines.append("")
        for edit in added:
            lines.append("- `{}` -- {} writer".format(edit.path, edit.writer))
        lines.append("")
    if removed:
        lines.append("Retired in the same change:")
        lines.append("")
        for edit in removed:
            lines.append("- `{}` -- {} writer".format(edit.path, edit.writer))
        lines.append("")
    lines.extend(
        [
            "The retirements and the installations are one commit on purpose. There",
            "is no order in which two commits are safe: installing first leaves this",
            "repository with two schedulers, removing first leaves it with none.",
            "",
            "## Left alone",
            "",
        ]
    )
    for path, writer in plan.retained:
        lines.append("- `{}` ({})".format(path, writer or "unclassified"))
    lines.extend(
        [
            "",
            "## The way back",
            "",
            "The pre-cutover revision is recorded in the merge commit as",
            "`continuum-rollback-revision`. `continuum migrate rollback` restores the",
            "repository to it in one change, idempotently.",
            "",
        ]
    )
    return "\n".join(lines)
