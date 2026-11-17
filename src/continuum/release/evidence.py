"""Evidence that the exact commit being released is the one that passed.

A release is downstream of review and CI, not a second opinion about them. The
failure this module prevents is subtle and common: a pull request was green, the
branch moved, and a release workflow ran against a commit nobody reviewed. The
old run's checks still exist, the old review still says "approved", and none of
it is about the bytes that would ship.

So every answer is keyed to one commit:

* a check run counts only if its ``head_sha`` is the release's commit;
* a required check that is missing or still running is not green;
* an approval counts only for the commit it approved, and a later
  ``CHANGES_REQUESTED`` is a refusal no number of old approvals outweighs;
* a pull request whose head has moved is reported as a stale head, not as
  "approved earlier".

The component reads through a port rather than through GitHub, so the matrix of
stale, pending, failed, and approved evidence is a table-driven test and the
real API is one small adapter in the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contract import ContractError, require_source_sha

EVIDENCE_NAME = "release-evidence"

CODE_READY = "evidence-ready"
CODE_CHECKS_MISSING = "checks-missing"
CODE_CHECKS_PENDING = "checks-pending"
CODE_CHECKS_FAILED = "checks-failed"
CODE_STALE_HEAD = "stale-head"
CODE_REVIEW_MISSING = "review-missing"
CODE_REVIEW_STALE = "review-stale"
CODE_CHANGES_REQUESTED = "changes-requested"
CODE_PR_NOT_MERGED = "pull-request-not-merged"
CODE_UNAVAILABLE = "evidence-unavailable"


@dataclass(frozen=True)
class EvidenceReport:
    """What the evidence says about one commit.

    ``retryable`` separates "CI is still running" from "the commit failed" and
    from "the branch moved". A retry can fix the first; only a new commit fixes
    the other two.
    """

    ok: bool
    code: str
    reason: str
    retryable: bool = False
    detail: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.ok and not self.reason:
            raise ContractError("an evidence refusal must say why")
        if self.ok and self.reason and not self.detail:
            raise ContractError(
                "an evidence report that succeeded must not carry a failure reason"
            )

    def describe(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "reason": self.reason,
            "retryable": self.retryable,
            "detail": list(self.detail),
        }


def ready(detail: Sequence[str] = ()) -> EvidenceReport:
    return EvidenceReport(ok=True, code=CODE_READY, reason="", detail=tuple(detail))


def refused(
    code: str, reason: str, *, retryable: bool = False, detail: Sequence[str] = ()
) -> EvidenceReport:
    return EvidenceReport(
        ok=False, code=code, reason=reason, retryable=retryable, detail=tuple(detail)
    )


class EvidenceGate:
    """The release chain's evidence component.

    It implements the ``evidence`` port: `evaluate` answers for one exact
    commit, and the answer is a value the validation stage can record.
    """

    SUPPORTS_DRY_RUN = True
    name = EVIDENCE_NAME

    def __init__(
        self,
        *,
        check_runs: Any,
        pull_request: Optional[Any] = None,
        reviews: Optional[Any] = None,
        required_checks: Sequence[str] = (),
        require_review: bool = False,
        require_merged: bool = False,
    ) -> None:
        if not callable(getattr(check_runs, "check_runs", None)):
            raise ContractError(
                "the evidence gate needs a source with check_runs(sha); without it "
                "'CI passed' is an assertion nobody can check"
            )
        self._check_runs = check_runs
        self._pull_request = pull_request
        self._reviews = reviews
        self._required = tuple(name for name in required_checks if name)
        self._require_review = require_review
        self._require_merged = require_merged
        if (require_review or require_merged) and (
            pull_request is None or reviews is None
        ):
            raise ContractError(
                "requiring review or a merged pull request needs a pull-request and "
                "review source; an empty answer is not evidence"
            )

    def intent(self) -> str:
        checks = ", ".join(self._required) or "every configured check"
        review = ", a review of the same commit" if self._require_review else ""
        return f"require {checks}{review} to be green on the exact commit being released"

    def evaluate(
        self,
        sha: str,
        *,
        pull_request: int = 0,
        dry_run: bool = False,
    ) -> EvidenceReport:
        require_source_sha(sha, "release evidence")
        try:
            runs = list(self._check_runs.check_runs(sha) or ())
        except Exception as exc:  # noqa: BLE001 - the source is somebody else's code
            return refused(
                CODE_UNAVAILABLE,
                f"the check-run source could not answer for {sha}: {exc}",
                retryable=True,
            )

        by_name: Dict[str, Mapping[str, Any]] = {}
        for run in runs:
            name = str(run.get("name") or "")
            if not name:
                continue
            by_name[name] = run
        if self._required:
            missing = [
                name
                for name in self._required
                if name not in by_name
                or str(by_name[name].get("head_sha") or sha) != sha
            ]
            if missing:
                return refused(
                    CODE_CHECKS_MISSING,
                    f"the exact commit {sha} has no completed required check for: "
                    + ", ".join(missing),
                    retryable=True,
                    detail=tuple(missing),
                )
            pending = [
                name
                for name in self._required
                if str(by_name[name].get("status") or "") != "completed"
            ]
            if pending:
                return refused(
                    CODE_CHECKS_PENDING,
                    "required check(s) are still running on the exact commit: "
                    + ", ".join(pending),
                    retryable=True,
                    detail=tuple(pending),
                )
            failed = [
                name
                for name in self._required
                if str(by_name[name].get("conclusion") or "") != "success"
            ]
            if failed:
                return refused(
                    CODE_CHECKS_FAILED,
                    "required check(s) did not succeed on the exact commit: "
                    + ", ".join(
                        f"{name}={by_name[name].get('conclusion') or 'unknown'}"
                        for name in failed
                    ),
                    detail=tuple(failed),
                )
        else:
            stale = [
                str(run.get("name") or "")
                for run in runs
                if str(run.get("head_sha") or sha) != sha
            ]
            if stale:
                return refused(
                    CODE_STALE_HEAD,
                    "some check runs describe a different commit than the one being "
                    "released: " + ", ".join(stale),
                    detail=tuple(stale),
                )
            failed = [
                str(run.get("name") or "")
                for run in runs
                if str(run.get("conclusion") or "") not in ("success", "neutral", "skipped")
                and str(run.get("status") or "") == "completed"
            ]
            if failed:
                return refused(
                    CODE_CHECKS_FAILED,
                    "check run(s) failed on the exact commit: " + ", ".join(failed),
                    detail=tuple(failed),
                )

        if not (self._require_review or self._require_merged):
            return ready(tuple(sorted(by_name)))
        if not pull_request:
            return refused(
                CODE_UNAVAILABLE,
                "review evidence was required, but the release names no pull request",
            )
        try:
            pull = self._pull_request.pull_request(int(pull_request))
            reviews = list(self._reviews.reviews(int(pull_request)) or ())
        except Exception as exc:  # noqa: BLE001 - the source is somebody else's code
            return refused(
                CODE_UNAVAILABLE,
                f"the pull-request source could not answer for #{pull_request}: {exc}",
                retryable=True,
            )
        head = str((pull.get("head") or {}).get("sha") or "")
        if head != sha:
            return refused(
                CODE_STALE_HEAD,
                f"pull request #{pull_request} now points at {head or 'an unknown commit'}, "
                f"not at the release commit {sha}; an approval of an older head is not "
                "an approval of these bytes",
                detail=(head,),
            )
        if self._require_merged:
            merged = bool(pull.get("merged")) or str(pull.get("state") or "") == "closed" and bool(pull.get("merged_at"))
            if not merged:
                return refused(
                    CODE_PR_NOT_MERGED,
                    f"pull request #{pull_request} is not merged, so this release is not "
                    "downstream of an accepted change",
                    retryable=True,
                )
        if self._require_review:
            latest: Dict[str, str] = {}
            approved: Dict[str, bool] = {}
            for review in sorted(
                reviews, key=lambda item: str(item.get("submitted_at") or "")
            ):
                login = str((review.get("user") or {}).get("login") or "")
                state = str(review.get("state") or "").upper()
                if not login or state in ("COMMENTED", "DISMISSED", "PENDING"):
                    continue
                if state == "CHANGES_REQUESTED":
                    latest[login] = "CHANGES_REQUESTED"
                    approved[login] = False
                elif state == "APPROVED":
                    if latest.get(login) != "CHANGES_REQUESTED":
                        if str(review.get("commit_id") or "") == sha:
                            latest[login] = "APPROVED"
                            approved[login] = True
                        else:
                            latest[login] = "STALE"
                            approved[login] = False
            if any(state == "CHANGES_REQUESTED" for state in latest.values()):
                return refused(
                    CODE_CHANGES_REQUESTED,
                    "the pull request still has an outstanding changes-requested review",
                )
            approvers = [login for login, ok in approved.items() if ok]
            if not approvers:
                stale = [login for login, state in latest.items() if state == "STALE"]
                if stale:
                    return refused(
                        CODE_REVIEW_STALE,
                        f"the approval(s) from {', '.join(sorted(stale))} describe an "
                        "earlier commit, not the one being released",
                    )
                return refused(
                    CODE_REVIEW_MISSING,
                    f"pull request #{pull_request} has no approval of the release commit",
                    retryable=True,
                )
            return ready(tuple(sorted(by_name)) + tuple(f"approved-by:{item}" for item in approvers))
        return ready(tuple(sorted(by_name)))


class UnavailableEvidence:
    """An evidence source that refuses rather than claims.

    Used where a caller asked for evidence but the API client could not be
    constructed. Returning "ready" would turn a missing credential into a green
    release, which is the one failure this module exists to prevent.
    """

    SUPPORTS_DRY_RUN = True
    name = "unavailable-evidence"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def intent(self) -> str:
        return "refuse the release because evidence could not be read"

    def evaluate(self, sha: str, *, pull_request: int = 0, dry_run: bool = False) -> EvidenceReport:
        return refused(CODE_UNAVAILABLE, self._reason)


__all__ = [
    "CODE_CHANGES_REQUESTED",
    "CODE_CHECKS_FAILED",
    "CODE_CHECKS_MISSING",
    "CODE_CHECKS_PENDING",
    "CODE_PR_NOT_MERGED",
    "CODE_READY",
    "CODE_REVIEW_MISSING",
    "CODE_REVIEW_STALE",
    "CODE_STALE_HEAD",
    "CODE_UNAVAILABLE",
    "EVIDENCE_NAME",
    "EvidenceGate",
    "EvidenceReport",
    "UnavailableEvidence",
    "ready",
    "refused",
]
