"""Deterministic shadow fixtures: one capture, one event per scenario class.

Every scenario class the issue names is exercised here against the same
repository capture, so a test can assert on the *decision* rather than on the
plumbing. The pull requests are chosen so the trust gate, the CI gate, and the
repair ladder each have both a passing and a refusing input available:

* #41 -- trusted, ``review-ready``, failing CI: a review that has not passed yet
  and a merge that must not happen;
* #42 -- fork head: the trust policy must refuse it whatever else is true;
* #43 -- trusted, ``review-ready``, green CI, and three commits behind: a
  dispatch *and* a merge are both correct here, which is the only capture that
  exercises the second consumer of ``workflow_run: CI completed``;
* #7 / #8 -- issues, one blocked by an open blocker, so the dependency check
  has something to read.

Nothing here contacts GitHub, a model, or a release registry.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from continuum.shadow.config import ShadowConfig, config_version, nanodictate_config
from continuum.shadow.event import ShadowEvent, from_payload
from continuum.shadow.state import ObservedState, state_from_payload

HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40
HEAD_E = "e" * 40

CAPTURED_AT_MS = 1_700_000_000_000


def capture() -> Dict[str, Any]:
    """The repository snapshot every scenario reads."""

    return {
        "repository": "acme/widgets",
        "repository_owner": "acme",
        "trusted_actors": "acme",
        "base_ref": "main",
        "captured_at_ms": CAPTURED_AT_MS,
        "pulls": [
            {
                "number": 41,
                "state": "open",
                "draft": False,
                "head": {"ref": "opencode/issue7-thing", "sha": HEAD_A, "repository": "acme/widgets"},
                "base": {"ref": "main", "sha": HEAD_B, "repository": "acme/widgets"},
                "body": "Closes #7",
                "labels": ["review-ready"],
                "mergeable": "mergeable",
                "mergeable_state": "clean",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:10:00Z",
            },
            {
                "number": 42,
                "state": "open",
                "head": {"ref": "opencode/issue9-other", "sha": HEAD_C, "repository": "fork/widgets"},
                "base": {"ref": "main", "sha": HEAD_B, "repository": "acme/widgets"},
                "body": "",
                "labels": [],
                "mergeable": "mergeable",
                "mergeable_state": "clean",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:10:00Z",
            },
            {
                "number": 43,
                "state": "open",
                "head": {"ref": "opencode/issue8-green", "sha": HEAD_E, "repository": "acme/widgets"},
                "base": {"ref": "main", "sha": HEAD_B, "repository": "acme/widgets"},
                "body": "Closes #8",
                "labels": ["review-ready"],
                "mergeable": "mergeable",
                "mergeable_state": "clean",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:40:00Z",
            },
        ],
        "issues": [
            {
                "number": 8,
                "state": "open",
                "author": "acme",
                "author_association": "OWNER",
                "title": "green thing",
                "body": "",
                "labels": [],
            },
            {
                "number": 7,
                "state": "open",
                "author": "acme",
                "author_association": "OWNER",
                "title": "Do the thing",
                "body": "please",
                "labels": ["review-ready"],
            },
        ],
        # An empty list is a captured answer ("this has no comments"), which is
        # different from an absent key ("nobody looked").
        "issue_comments": {"7": ["looks fine"], "8": [], "41": [], "42": [], "43": []},
        "reviews": {
            "41": [["coderabbitai[bot]", "APPROVED", "2026-01-01T00:05:00Z"]],
            "43": [["coderabbitai[bot]", "APPROVED", "2026-01-01T00:05:00Z"]],
        },
        "review_comments": {"41": [], "42": [], "43": []},
        "check_runs": {
            HEAD_A: [["CI", "success", "CI / test"], ["Packaging smoke", "failure", "smoke"]],
            HEAD_E: [["CI", "success", "CI / test"], ["Packaging smoke", "success", "smoke"]],
        },
        "combined_status": {HEAD_A: "failure", HEAD_E: "success"},
        "changed_files": {"41": ["src/app.py", "pyproject.toml"], "42": [], "43": ["src/app.py"]},
        "review_threads": {"41": [], "42": [], "43": []},
        "attempts": {"41": 0, "42": 0, "43": 0},
        "repair_in_flight": {"41": False, "42": False, "43": False},
        # #7 cannot be worked yet: #8 is open. The scheduler's own endpoint is
        # what the dependency handler reads, so the capture carries it directly.
        "blocked_by": {"7": [[8, "open"]]},
    }


def state() -> ObservedState:
    return state_from_payload(capture())


def config() -> ShadowConfig:
    effective = nanodictate_config()
    return ShadowConfig(
        config=effective,
        version=config_version(effective),
        source="<nanodictate-snapshot>",
        origin="default",
    )


def event(
    event_type: str,
    *,
    number: int = 0,
    action: str = "",
    head_sha: str = "",
    correlation_id: str = "e-1",
    observed_at_ms: int = CAPTURED_AT_MS,
    observed_at: str = "2026-01-01T00:00:00Z",
    replays: str = "",
    extra: Mapping[str, Any] | None = None,
    release: Mapping[str, Any] | None = None,
) -> ShadowEvent:
    payload: Dict[str, Any] = {
        "correlation_id": correlation_id,
        "repository": "acme/widgets",
        "event_type": event_type,
        "action": action,
        "observed_at": observed_at,
        "observed_at_ms": observed_at_ms,
    }
    if number:
        payload["number"] = number
    if head_sha:
        payload["head_sha"] = head_sha
    if replays:
        payload["replays"] = replays
    if extra:
        payload["extra"] = dict(extra)
    if release:
        payload["release"] = dict(release)
    return from_payload(payload)


# -- one event per scenario class ------------------------------------------ #


def issue_opened() -> ShadowEvent:
    return event("issue.opened", number=7, action="opened", correlation_id="e-issue-opened")


def pull_synchronize_fork() -> ShadowEvent:
    return event(
        "pull_request.synchronize",
        number=42,
        action="synchronize",
        head_sha=HEAD_C,
        correlation_id="e-pr-synchronize-fork",
    )


def ci_repair_behind() -> ShadowEvent:
    """A green-but-behind pull request: the ladder must dispatch, and the merge
    sweep started by the same webhook must merge it."""

    return event(
        "workflow_run.completed",
        number=43,
        action="completed",
        head_sha=HEAD_E,
        correlation_id="e-ci-behind",
        observed_at="2026-01-01T00:40:00Z",
        observed_at_ms=CAPTURED_AT_MS + 400_000,
        extra={"behind_by": 3},
    )


def check_suite_green() -> ShadowEvent:
    return event(
        "check_suite.completed",
        number=41,
        action="completed",
        head_sha=HEAD_A,
        correlation_id="e-ci",
        observed_at_ms=CAPTURED_AT_MS + 100_000,
    )


def coderabbit_review() -> ShadowEvent:
    return event(
        "pull_request.review",
        number=41,
        action="submitted",
        head_sha=HEAD_A,
        correlation_id="e-review",
        observed_at_ms=CAPTURED_AT_MS + 200_000,
    )


def coderabbit_review_comment() -> ShadowEvent:
    return event(
        "pull_request.review_comment",
        number=41,
        action="created",
        head_sha=HEAD_A,
        correlation_id="e-review-comment",
        observed_at_ms=CAPTURED_AT_MS + 200_000,
    )


def merge_sweep() -> ShadowEvent:
    """The schedule tick that runs the merge reconciler.

    #43 is the interesting subject: green, trusted, ready. #41 is included so
    the report can show a refusal next to an allowance rather than one alone.
    """

    return event(
        "schedule.tick",
        number=43,
        action="merge",
        correlation_id="e-schedule-merge",
        observed_at_ms=CAPTURED_AT_MS + 300_000,
    )


def release_event() -> ShadowEvent:
    """A release published from a pinned commit.

    The payload puts the commit in ``release`` where the contract reads it, not
    in ``extra``: the release event is the one place where the sha *is* the
    decision input, so it is part of the release object.
    """

    return event(
        "release.event",
        action="published",
        correlation_id="e-release",
        observed_at="2026-01-01T01:00:00Z",
        observed_at_ms=CAPTURED_AT_MS + 3_600_000,
        release={
            "tag": "v0.1.0",
            "source_sha": HEAD_E,
            "version": "0.1.0",
            "dry_run": True,
        },
    )


def duplicate_replay(original: str = "check_suite.completed") -> ShadowEvent:
    return event(
        "duplicate.replay",
        number=43,
        action="redelivered",
        head_sha=HEAD_E,
        replays="e-ci",
        correlation_id="e-duplicate",
        observed_at_ms=CAPTURED_AT_MS + 500_000,
        extra={"behind_by": 3, "original_event_type": original},
    )


def timeout_detected() -> ShadowEvent:
    return event(
        "timeout.detected",
        number=41,
        action="repair-timeout",
        correlation_id="e-timeout",
        observed_at_ms=CAPTURED_AT_MS + 700_000,
    )


def issue_labelled() -> ShadowEvent:
    return event("issue.labelled", number=7, action="labeled", correlation_id="e-labelled")


def issue_labelled_unblocked() -> ShadowEvent:
    """The same event with the blocker closed: the dependency check must read the
    change out of the capture rather than out of the event."""

    payload = capture()
    payload["blocked_by"] = {"7": [[8, "closed"]]}
    payload["issues"][1]["labels"] = ["review-ready"]
    state = state_from_payload(payload)
    return event("issue.labelled", number=7, action="labeled", correlation_id="e-labelled-clear"), state


#: Every scenario class, for the coverage check. Kept as data so the coverage
#: test and the sweep test cannot disagree about what the list is.
SCENARIO_EVENTS = (
    "issue-lifecycle",
    "ci-repair",
    "coderabbit-finding",
    "merge-decision",
    "release-planning",
    "duplicate-replay",
    "timeout-recovery",
    "dependency-blocked",
)


def event_for(scenario: str) -> ShadowEvent:
    from continuum.shadow import planner

    builder = {
        planner.SCENARIO_ISSUE_LIFECYCLE: issue_opened,
        planner.SCENARIO_CI_REPAIR: ci_repair_behind,
        planner.SCENARIO_CODERABBIT_FINDING: coderabbit_review,
        planner.SCENARIO_MERGE_DECISION: merge_sweep,
        planner.SCENARIO_RELEASE_PLANNING: release_event,
        planner.SCENARIO_DUPLICATE_REPLAY: duplicate_replay,
        planner.SCENARIO_TIMEOUT_RECOVERY: timeout_detected,
        planner.SCENARIO_DEPENDENCY_BLOCKED: issue_labelled,
    }
    try:
        return builder[scenario]()
    except KeyError:
        raise AssertionError("no fixture event for scenario {!r}".format(scenario)) from None


def states_for(scenario: str) -> List[ObservedState]:
    """The captures a scenario may legitimately be read against."""

    from continuum.shadow import planner

    if scenario == planner.SCENARIO_DEPENDENCY_BLOCKED:
        return [state(), issue_labelled_unblocked()[1]]
    return [state()]
