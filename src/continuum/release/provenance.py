"""Publishing only what an exact-commit verification run already exercised.

A release is a claim: "these bytes, at this version, are the thing". Before this
module nothing in Continuum could check the claim. A target's plan built and
published in one job, so the artifacts that reached a release were exactly the
artifacts nothing else had ever run. Re-running the build at publication time
makes it worse rather than better: the bytes are then whatever the toolchain
produced a second time, and the verification that did happen applied to a
different set of bytes that no longer exists anywhere.

The property this module enforces is narrow and generic:

    **Publication is bound to one immutable commit, and to the artifacts a
    declared verification run produced for that exact commit.**

Three rules, each of which exists because breaking it is invisible:

* **The commit is exact, never a branch.** A verifying run on `main` says
  nothing about the commit being released, because `main` may have moved. Only a
  run whose head SHA *is* the released SHA is evidence for it.
* **Green is necessary and not sufficient.** A run can be green and have produced
  nothing — a path-filtered job, a matrix leg that did not run, an upload that
  found no files. Every artifact the consumer declared must be present in that
  run, by name.
* **The gate reports a stable reason code.** A caller that decides whether to
  wait, retry, or stop branches on a code, never on prose. A gate that cannot
  find its run and a gate whose run failed are different problems with different
  fixes, and they must not collapse into one word.

Nothing here names a product, a package manager, or a platform. A consumer
declares which workflow verifies its targets and which artifact names must exist;
the workflow it wires this into is its own. The run id this returns is what lets
a publication consume the *tested* bytes instead of rebuilding them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

VERDICT_SCHEMA = "continuum.release-verification/v1"

# -- reason codes ------------------------------------------------------------

#: The declared verification run exists for this exact commit, concluded
#: successfully, and carries every declared artifact.
VERIFIED = "verified"

#: No verification is declared. Publication is unconstrained, which is a valid
#: configuration and must be reported as such rather than as a pass.
NOT_CONFIGURED = "not-configured"

#: No run of the declared workflow exists for this exact commit. Usually means
#: the gate ran before the verifying workflow was created: the two are triggered
#: by the same push, so discovery has to be bounded rather than instant.
RUN_MISSING = "verification-missing"

#: A run exists but has not finished. Waiting is correct; publishing is not.
RUN_INCOMPLETE = "verification-incomplete"

#: A run exists and finished, unsuccessfully.
RUN_FAILED = "verification-failed"

#: The run is green but a declared job never ran, so the run that answered is
#: not the run that was required.
JOB_MISSING = "verification-job-missing"

#: The run is green and every job ran, but a declared artifact is absent. This is
#: the check that catches a matrix leg or an upload that quietly produced nothing.
ARTIFACT_MISSING = "verification-artifact-missing"

#: Nothing can be verified against an empty commit.
HEAD_REQUIRED = "verification-head-required"

#: A failure that is about the world rather than about the request is worth a
#: second attempt; a missing artifact or an unrun job is not, and a caller that
#: retries those just burns a runner while the same wrong answer comes back.
RETRYABLE_REASONS = frozenset({RUN_MISSING, RUN_INCOMPLETE})

_WORKFLOW_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}\.ya?ml$")
_EVENT_RE = re.compile(r"^[a-z_]{1,40}$")


def is_retryable(reason: str) -> bool:
    return reason in RETRYABLE_REASONS


@dataclass(frozen=True)
class VerificationRequirement:
    """What a consumer declares must have happened before it may publish.

    `workflow` is the file name of a workflow in the *consumer's* repository.
    `jobs` and `artifacts` are the names that workflow must have used. They are
    data, not vocabulary this module understands: only exact matches count, and
    an empty list means "nothing further to prove".
    """

    workflow: str
    event: str = ""
    jobs: Tuple[str, ...] = ()
    artifacts: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _WORKFLOW_RE.match(self.workflow or ""):
            raise ValueError(
                f"verification workflow must be a bare .yml file name; got {self.workflow!r}"
            )
        if self.event and not _EVENT_RE.match(self.event or ""):
            raise ValueError(
                f"verification event must be a GitHub event name; got {self.event!r}"
            )
        for label, values in (("jobs", self.jobs), ("artifacts", self.artifacts)):
            for value in values or ():
                if not str(value).strip():
                    raise ValueError(f"verification {label} entries must be non-empty names")

    @property
    def declared(self) -> bool:
        return bool(self.workflow)

    def describe(self) -> Dict[str, Any]:
        return {
            "workflow": self.workflow,
            "event": self.event,
            "jobs": list(self.jobs),
            "artifacts": list(self.artifacts),
        }


@dataclass(frozen=True)
class VerificationVerdict:
    """One answer, with the run that produced it so publication can use it."""

    ok: bool
    reason: str
    detail: str = ""
    workflow: str = ""
    head_sha: str = ""
    event: str = ""
    run_id: int = 0
    conclusion: str = ""
    status: str = ""
    artifacts: Tuple[str, ...] = ()
    missing_jobs: Tuple[str, ...] = ()
    missing_artifacts: Tuple[str, ...] = ()

    @property
    def retryable(self) -> bool:
        return is_retryable(self.reason)

    @property
    def tested_bytes(self) -> bool:
        """Whether this verdict may be used to publish the run's own artifacts.

        A blocked verdict never authorizes anything, however close it came: a
        green run missing one declared artifact has proved nothing about the
        bytes that are missing.
        """

        return self.ok and self.run_id > 0

    def contract(self) -> str:
        """The one-line normalized form a merge/readiness gate can match.

        The same shape the review gate publishes, and for the same reason: a
        consumer's branch protection, and any other consumer, must be able to
        read the verdict without knowing how it was produced.
        """

        verdict = "PASS" if self.ok else "BLOCK"
        head = self.head_sha or "unknown"
        run = str(self.run_id) if self.run_id else "none"
        return f"verdict={verdict} reason={self.reason} head={head} run={run}"

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": VERDICT_SCHEMA,
            "ok": self.ok,
            "verdict": "PASS" if self.ok else "BLOCK",
            "reason": self.reason,
            "detail": self.detail,
            "retryable": self.retryable,
            "tested_bytes": self.tested_bytes,
            "requirement": {
                "workflow": self.workflow,
                "event": self.event,
                "jobs": list(self.missing_jobs),
                "artifacts": list(self.artifacts),
            },
            "head_sha": self.head_sha,
            "run_id": self.run_id,
            "status": self.status,
            "conclusion": self.conclusion,
            "present_artifacts": list(self.artifacts),
            "missing_artifacts": list(self.missing_artifacts),
            "contract": self.contract(),
        }


# -- selection ---------------------------------------------------------------


def _matches_workflow(run: Mapping[str, Any], workflow: str) -> bool:
    name = str(run.get("name") or "")
    path = str(run.get("path") or "")
    return name == workflow or path.rsplit("/", 1)[-1] == workflow


def _matches_event(run: Mapping[str, Any], event: str) -> bool:
    return not event or str(run.get("event") or "") == event


def _run_time(run: Mapping[str, Any]) -> str:
    return str(run.get("run_started_at") or run.get("created_at") or "")


def select_run(
    runs: Sequence[Mapping[str, Any]],
    requirement: VerificationRequirement,
    head_sha: str,
) -> Optional[Mapping[str, Any]]:
    """The newest run of the declared workflow for this exact commit, or `None`.

    "Exact" is the whole point. A run of the same workflow on the same branch is
    evidence about the branch, not about the commit, and the two diverge every
    time anything lands after the run started.
    """

    head = (head_sha or "").strip().lower()
    if not head or not requirement.declared:
        return None
    matching = [
        run
        for run in runs or []
        if _matches_workflow(run, requirement.workflow)
        and _matches_event(run, requirement.event)
        and str(run.get("head_sha") or "").strip().lower() == head
    ]
    if not matching:
        return None
    return sorted(matching, key=lambda run: (_run_time(run), int(run.get("id") or 0)))[-1]


# -- evaluation --------------------------------------------------------------


def _names(items: Optional[Iterable[Any]]) -> Tuple[str, ...]:
    """Collect the names out of either a list of strings or a list of payloads."""

    names: List[str] = []
    for item in items or []:
        if isinstance(item, Mapping):
            value = item.get("name")
        else:
            value = item
        text = str(value or "").strip()
        if text and text not in names:
            names.append(text)
    return tuple(names)


def evaluate(
    requirement: Optional[VerificationRequirement],
    head_sha: str,
    *,
    runs: Sequence[Mapping[str, Any]] = (),
    jobs: Optional[Iterable[Any]] = None,
    artifacts: Optional[Iterable[Any]] = None,
    check_jobs: bool = True,
    check_artifacts: bool = True,
) -> VerificationVerdict:
    """Decide whether `head_sha` may publish, given what the repository reports.

    `jobs` and `artifacts` are the declared job and artifact names *of the
    selected run*. Passing them for some other run would make the verdict a
    statement about the wrong run, so they are read only after selection.

    `check_jobs` and `check_artifacts` say whether a dimension is being proved at
    all. Turning one off means *not required* -- not *required and found
    absent*. Conflating the two would let a caller that skipped a check be
    answered "the job never ran", which is the opposite of what it asked for.
    """

    head = (head_sha or "").strip()
    if requirement is None or not requirement.declared:
        return VerificationVerdict(
            ok=True,
            reason=NOT_CONFIGURED,
            detail=(
                "no verification workflow is declared, so this publication is not "
                "bound to a verification run"
            ),
            head_sha=head,
        )
    if not head:
        return VerificationVerdict(
            ok=False,
            reason=HEAD_REQUIRED,
            detail=(
                "publication must name the exact commit being released; verification "
                "against a branch proves nothing about that commit"
            ),
            workflow=requirement.workflow,
            event=requirement.event,
        )

    run = select_run(runs, requirement, head)
    if run is None:
        return VerificationVerdict(
            ok=False,
            reason=RUN_MISSING,
            detail=(
                f"no {requirement.workflow} run"
                + (f" for event {requirement.event!r}" if requirement.event else "")
                + f" exists for commit {head}; publication is blocked"
            ),
            workflow=requirement.workflow,
            event=requirement.event,
            head_sha=head,
        )

    run_id = int(run.get("id") or 0)
    status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    base = dict(
        workflow=requirement.workflow,
        event=requirement.event,
        head_sha=head,
        run_id=run_id,
        status=status,
        conclusion=conclusion,
    )
    if status != "completed":
        return VerificationVerdict(
            ok=False,
            reason=RUN_INCOMPLETE,
            detail=(
                f"{requirement.workflow} run {run_id} is {status or 'not finished'} for "
                f"commit {head}; publication must wait for the verification result"
            ),
            **base,
        )
    if conclusion != "success":
        return VerificationVerdict(
            ok=False,
            reason=RUN_FAILED,
            detail=(
                f"{requirement.workflow} run {run_id} concluded {conclusion or 'nothing'}"
                f" for commit {head}; only a green run may gate a publication"
            ),
            **base,
        )

    if check_jobs:
        present_jobs = _names(jobs)
        missing_jobs = tuple(name for name in requirement.jobs if name not in present_jobs)
        if missing_jobs:
            return VerificationVerdict(
                ok=False,
                reason=JOB_MISSING,
                detail=(
                    f"{requirement.workflow} run {run_id} is green but never ran "
                    f"{', '.join(missing_jobs)}; the run that answered is not the run "
                    "that was required"
                ),
                missing_jobs=missing_jobs,
                **base,
            )

    present_artifacts = _names(artifacts)
    if check_artifacts:
        missing_artifacts = tuple(
            name for name in requirement.artifacts if name not in present_artifacts
        )
        if missing_artifacts:
            return VerificationVerdict(
                ok=False,
                reason=ARTIFACT_MISSING,
                detail=(
                    f"{requirement.workflow} run {run_id} is green but did not produce "
                    f"{', '.join(missing_artifacts)}; a green run that produced nothing "
                    "has verified nothing"
                ),
                artifacts=present_artifacts,
                missing_artifacts=missing_artifacts,
                **base,
            )

    return VerificationVerdict(
        ok=True,
        reason=VERIFIED,
        detail=(
            f"{requirement.workflow} run {run_id} is green on exact commit {head}"
            + (
                f" and carries {len(requirement.artifacts)} declared artifact(s)"
                if check_artifacts
                else ""
            )
        ),
        artifacts=present_artifacts,
        **base,
    )


__all__ = [
    "ARTIFACT_MISSING",
    "HEAD_REQUIRED",
    "JOB_MISSING",
    "NOT_CONFIGURED",
    "RETRYABLE_REASONS",
    "RUN_FAILED",
    "RUN_INCOMPLETE",
    "RUN_MISSING",
    "VERDICT_SCHEMA",
    "VERIFIED",
    "VerificationRequirement",
    "VerificationVerdict",
    "evaluate",
    "is_retryable",
    "select_run",
]
