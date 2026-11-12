#!/usr/bin/env python3
"""Serialized exact-HEAD CI-repair admission for the live repair controller.

This wrapper is the production owner of the read -> claim -> dispatch sequence
for kodmial/continuum#446. It is intentionally the only place in the repair
controller that writes a confirmation receipt or asks GitHub to dispatch a
``ci-fix`` run.

The workflow that calls this script must put both entry points -- the consumer
reported ``workflow_dispatch`` and an approved ``workflow_run`` completion --
into one ``per-repository + exact-HEAD`` concurrency group with
``cancel-in-progress: false`` and ``queue: max``. GitHub comments are durable
receipts, not atomic compare-and-swap storage; serialization is what prevents
two controllers from reading the same empty history at the same time.

The script uses only the standard library and the authenticated ``gh`` CLI that
the workflow already provisions. It never falls back to a label, the latest
run by recency, or a partial comment page:
- comments are fully paginated, duplicate-checked, and deterministically
  ordered before the phase-1 fail-closed receipt reader sees them;
- the claim marker is persisted before the dispatch request;
- a 200-with-details or legacy 204 is recorded as ``dispatched``;
- a definite 4xx is recorded as ``rejected`` (a later serialized pass may
  claim a fresh attempt);
- timeout/network/403/429/5xx is ``ambiguous`` and is correlated against the
  attempt's run-name marker, never retried blindly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from continuum.ci_repair_admission import (  # noqa: E402
    ACTION_REARM,
    AdmissionError,
    AdmissionPlan,
    RunInventory,
    attempt_observed_at,
    classify_dispatch_response,
    correlated_run_url,
    normalize_comment_pages,
    normalize_run_pages,
    plan_admission,
    run_actor_logins,
    run_proves_historical_receipt,
    run_repairs_pull,
    select_correlated_run,
    unexpected_dispatch_inputs,
)
from continuum.ci_repair_receipt import (  # noqa: E402
    ACTION_CLAIM,
    ACTION_HOLD,
    ACTION_WAIT,
    RECEIPT_MARKER,
    STATUS_AMBIGUOUS,
    STATUS_CLAIMED,
    STATUS_DISPATCHED,
    STATUS_REJECTED,
    build_receipt,
    comment_login,
    is_trusted_receipt_comment,
    new_attempt_id,
    normalize_head_sha,
    normalize_pull_number,
    normalize_repository,
    parse_receipts,
    read_receipts,
    render_receipt,
    transition_receipt,
)

_HTTP_STATUS_RE = re.compile(rb"^HTTP/\S+\s+([0-9]{3})\b", re.MULTILINE)
_HEADER_SEPARATOR_RE = re.compile(rb"\r?\n\r?\n", re.MULTILINE)
# A rotated TAP_PAT can leave receipts authored by a former collaborator. If
# no run proves that writer, admission must hold instead of treating the
# receipt as absent; an unrelated commenter without repository write access
# can neither authorize nor suppress a repair.
_HISTORICAL_WRITER_ASSOCIATIONS = frozenset({"MEMBER", "COLLABORATOR"})
#: Repository variable recording the logins that have authored trusted
#: receipts. It is write-only through the authenticated TAP_PAT, so an
#: unrelated issue commenter cannot add itself. The list survives run
#: deletion and a later token rotation, when GitHub no longer has a
#: dispatch run to prove the previous writer.
_CI_REPAIR_WRITERS_VARIABLE = "CONTINUUM_CI_REPAIR_WRITERS"


class GhFailure(RuntimeError):
    """A GitHub CLI call that did not produce parseable evidence."""


def _gh(args: Sequence[str], *, stdin: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["gh", *args],
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        # A timeout leaves the remote effect unknown. Callers treat a missing
        # HTTP status as ambiguous evidence and reconcile before retrying.
        return 1, "", "gh timed out after 120 seconds"
    except OSError as exc:
        return 1, "", "could not run gh: {}".format(exc)
    return result.returncode, result.stdout, result.stderr


def _gh_json(arguments: Sequence[str]) -> Mapping[str, Any]:
    code, stdout, stderr = _gh(arguments)
    if code != 0:
        raise GhFailure(stderr.strip() or "gh exited {}".format(code))
    try:
        parsed = json.loads(stdout or "{}")
    except ValueError as exc:
        raise GhFailure("gh returned invalid JSON: {}".format(exc)) from exc
    if not isinstance(parsed, Mapping):
        raise GhFailure("gh returned a non-object JSON document")
    return parsed


def _pull(repository: str, pull_number: int) -> Mapping[str, Any]:
    return _gh_json(["api", "repos/{}/pulls/{}".format(repository, pull_number)])


def _authenticated_login() -> Optional[str]:
    """Return the login of the token that authors receipts, or ``None``.

    A classic PAT may belong to a repository collaborator rather than the
    literal owner, so the token identity is trusted explicitly. Receipts are
    written with that same token. If its identity cannot be proved, the caller
    holds: treating its own earlier claim as an untrusted lookalike would
    authorize a duplicate dispatch for the exact same HEAD.
    """

    try:
        data = _gh_json(["api", "user"])
    except GhFailure:
        return None
    login = data.get("login")
    if not isinstance(login, str):
        return None
    login = login.strip().lower()
    return login or None


def _normalize_writer_logins(value: Any) -> Tuple[str, ...]:
    """Return the normalized unique logins stored in a writer registry."""

    if not isinstance(value, str):
        return ()
    logins = []
    for token in re.split(r"[\s,]+", value.strip()):
        login = token.strip().lower()
        if login and login not in logins:
            logins.append(login)
    return tuple(logins)


def _historical_writer_registry(
    repository: str,
) -> Tuple[Tuple[str, ...], Optional[bool]]:
    """Read logins recorded as trusted CI-repair receipt authors.

    The second value is ``True`` for an existing variable, ``False`` for a
    missing one, and ``None`` when the registry could not be read. A missing
    registry is a valid empty history; an unreadable one is not proof that no
    historical writer exists.
    """

    variable = _CI_REPAIR_WRITERS_VARIABLE
    url = "repos/{}/actions/variables/{}".format(repository, variable)
    _, stdout, stderr = _gh(["api", "--include", url])
    status, body = _parse_include(stdout, stderr)
    if status == 404:
        return (), False
    if status != 200:
        print(
            "::warning::Could not read historical CI-repair writer registry: "
            "{}".format((body or stderr).strip() or "HTTP status {}".format(status))
        )
        return (), None
    try:
        data = json.loads(body or "{}")
    except ValueError:
        print(
            "::warning::Historical CI-repair writer registry is not valid JSON."
        )
        return (), None
    if not isinstance(data, Mapping):
        print(
            "::warning::Historical CI-repair writer registry is not an object."
        )
        return (), None
    return _normalize_writer_logins(data.get("value")), True


def _persist_historical_writers(
    repository: str,
    logins: Sequence[str],
    *,
    known: Sequence[str],
    exists: bool,
) -> Tuple[Tuple[str, ...], bool]:
    """Merge trusted writer logins into the durable repository variable."""

    merged = list(_normalize_writer_logins("\n".join(known)))
    for login in _normalize_writer_logins("\n".join(logins)):
        if login not in merged:
            merged.append(login)
    if tuple(merged) == tuple(known):
        return tuple(merged), exists

    value = "\n".join(merged)
    if exists:
        arguments = [
            "api",
            "--method",
            "PATCH",
            "repos/{}/actions/variables/{}".format(
                repository, _CI_REPAIR_WRITERS_VARIABLE
            ),
            "-f",
            "value={}".format(value),
        ]
    else:
        arguments = [
            "api",
            "--method",
            "POST",
            "repos/{}/actions/variables".format(repository),
            "-f",
            "name={}".format(_CI_REPAIR_WRITERS_VARIABLE),
            "-f",
            "value={}".format(value),
        ]
    code, _, stderr = _gh(arguments)
    if code != 0:
        print(
            "::warning::Could not record historical CI-repair writers: "
            "{}".format(stderr.strip() or "gh exited {}".format(code))
        )
        return tuple(merged), exists
    return tuple(merged), True


def _comment_pages(repository: str, pull_number: int) -> Sequence[Any]:
    code, stdout, stderr = _gh(
        [
            "api",
            "--paginate",
            "--slurp",
            "repos/{}/issues/{}/comments?per_page=100".format(
                repository, pull_number
            ),
        ]
    )
    if code != 0:
        raise GhFailure(stderr.strip() or "comment pagination failed")
    try:
        pages = json.loads(stdout or "[]")
    except ValueError as exc:
        raise GhFailure("comment pages are not JSON: {}".format(exc)) from exc
    if isinstance(pages, Mapping):
        raise GhFailure("comment pages are an object, not an array of pages")
    return pages


def _create_comment(
    repository: str, pull_number: int, body: str
) -> Mapping[str, Any]:
    code, stdout, stderr = _gh(
        [
            "api",
            "--method",
            "POST",
            "repos/{}/issues/{}/comments".format(repository, pull_number),
            "-f",
            "body={}".format(body),
        ]
    )
    if code != 0:
        raise GhFailure(
            "could not persist CI-repair receipt: {}".format(
                stderr.strip() or "gh exited {}".format(code)
            )
        )
    try:
        parsed = json.loads(stdout or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _add_label(repository: str, pull_number: int, label: str) -> None:
    if not label:
        return
    code, _, stderr = _gh(
        [
            "api",
            "--method",
            "POST",
            "repos/{}/issues/{}/labels".format(repository, pull_number),
            "-f",
            "labels[]={}".format(label),
        ]
    )
    if code != 0:
        # Presentation only. A label failure must never block or reverse the
        # durable receipt decision.
        print(
            "::warning::CI-repair presentation label {!r} could not be added: {}".format(
                label, stderr.strip() or "unknown error"
            )
        )


def _parse_include(stdout: str, stderr: str) -> Tuple[Optional[int], str]:
    raw_out = stdout.encode("utf-8", "replace")
    raw_err = stderr.encode("utf-8", "replace")
    status = None
    match = _HTTP_STATUS_RE.search(raw_out) or _HTTP_STATUS_RE.search(raw_err)
    if match:
        status = int(match.group(1))
    body = stdout
    separator = _HEADER_SEPARATOR_RE.search(raw_out)
    if match and separator:
        body = raw_out[separator.end():].decode("utf-8", "replace")
    return status, body.strip()


def _dispatch(
    repository: str,
    workflow: str,
    *,
    ref: str,
    pull_number: int,
    head_ref: str,
    failing_run_id: str,
    attempt_id: str,
    head_sha: str,
) -> Tuple[Optional[int], str, str]:
    """Ask GitHub to create the repair run, with legacy-caller fallback.

    ``ci_repair_identity`` is an optional input in the current Continuum
    caller. A consumer still running an older caller that does not declare it
    rejects the request with HTTP 422 before creating a run. That is a
    *definite* rejection, so retrying once without that input is safe; the
    engine can still discover the claim from the PR receipt and publish its
    own dispatched receipt. The executor refuses that legacy run before any
    repair work, because it carries no admitted HEAD to bind.

    Continuum's checked-in ``opencode.yml`` caller forwards
    ``ci_repair_identity``, so its run is correlated by the run-name marker
    and bound to the exact admitted HEAD.
    """

    base = [
        "api",
        "--include",
        "--method",
        "POST",
        "repos/{}/actions/workflows/{}/dispatches".format(repository, workflow),
        "-f",
        "ref={}".format(ref),
        "-f",
        "inputs[mode]=ci-fix",
        "-f",
        "inputs[pr_number]={}".format(pull_number),
        "-f",
        "inputs[head_ref]={}".format(head_ref),
        "-f",
        "inputs[run_id]={}".format(failing_run_id),
    ]
    # The caller stub has a hard 25-input workflow_dispatch limit. Pack the
    # optional correlation fields into one JSON input, mirroring the existing
    # packed recovery identity, so exact-HEAD correlation costs one slot.
    identity = json.dumps(
        {"ci_attempt_id": attempt_id, "ci_head_sha": head_sha},
        separators=(",", ":"),
        sort_keys=True,
    )
    with_attempt = base + [
        "-f",
        "inputs[ci_repair_identity]={}".format(identity),
    ]

    for include_attempt in (True, False):
        for include_details in (True, False):
            arguments = list(with_attempt if include_attempt else base)
            if include_details:
                arguments.extend(["-F", "return_run_details=true"])
            code, stdout, stderr = _gh(arguments)
            status, body = _parse_include(stdout, stderr)
            detail = (body + "\n" + stderr).strip()
            if (
                status == 422
                and unexpected_dispatch_inputs(detail)
                and (include_attempt or include_details)
            ):
                # No run was created; retry only the narrower, supported shape.
                print(
                    "::notice::CI-repair dispatch target rejected optional "
                    "inputs; retrying with the legacy-compatible input shape."
                )
                continue
            if status is None and code == 0:
                status = 200
            return status, body, detail
    return None, "", "all dispatch shapes were rejected"


def _workflow_runs(
    repository: str,
    workflow: str,
    *,
    since: Optional[str] = None,
) -> RunInventory:
    """Fetch the complete workflow-dispatch run inventory for correlation."""

    arguments = [
        "api",
        "--paginate",
        "--slurp",
        "repos/{}/actions/workflows/{}/runs?event=workflow_dispatch&per_page=100".format(
            repository, workflow
        ),
    ]
    if since:
        # ``gh api`` switches to POST as soon as a field is supplied. An
        # explicit GET keeps this an idempotent listing and appends the field
        # as the ``created`` query filter.
        arguments.extend(["--method", "GET", "-f", "created=>={}".format(since)])
    code, stdout, stderr = _gh(arguments)
    if code != 0:
        raise GhFailure(stderr.strip() or "workflow run listing failed")
    try:
        pages = json.loads(stdout or "[]")
    except ValueError as exc:
        raise GhFailure("workflow run pages are not JSON: {}".format(exc)) from exc
    if isinstance(pages, Mapping):
        pages = [pages]
    return normalize_run_pages(pages)


def _comment_body(receipt, headline: str) -> str:
    return "{marker}\n\n{headline}\n<!-- continuum-origin role=continuum component=ci-repair -->".format(
        marker=render_receipt(receipt),
        headline=headline,
    )


def _pr_head_mismatch(
    pr: Mapping[str, Any], repository: str, head_sha: str
) -> Optional[str]:
    """Return why ``pr`` is not the admitted exact HEAD, or ``None``."""

    state = str(pr.get("state") or "")
    head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
    current_head = str(head.get("sha") or "").strip().lower()
    head_repo = str((head.get("repo") or {}).get("full_name") or "").strip().lower()
    if state != "open":
        return "it is not open (state={})".format(state or "-")
    if head_repo != repository:
        return "its head repository is {} instead of {}".format(
            head_repo or "-", repository
        )
    if current_head != head_sha:
        return "its HEAD moved from admitted {} to {}".format(
            head_sha, current_head or "-"
        )
    return None


def _revalidate_head(
    repository: str, pull_number: int, head_sha: str
) -> Optional[str]:
    """Re-read the PR immediately before dispatch and prove the same HEAD."""

    try:
        pr = _pull(repository, pull_number)
    except GhFailure as exc:
        return "the PR could not be revalidated ({})".format(exc)
    return _pr_head_mismatch(pr, repository, head_sha)


def _historical_receipt_writers(
    repository: str,
    pull_number: int,
    head_sha: str,
    comments: Sequence[Mapping[str, Any]],
    *,
    workflow: str,
    owner_login: str,
    trusted_logins: Sequence[str],
    known_writers: Sequence[str] = (),
    registry_available: bool = True,
) -> Tuple[Tuple[str, ...], bool]:
    """Return historical receipt authors proved by GitHub's run records.

    ``TAP_PAT`` can be rotated to another account. Receipts the previous
    account published must stay authoritative, or the exact HEAD they already
    spent looks unused and admission dispatches it a second time. A receipt is
    only re-trusted here when a run created by the same account carries this
    attempt's run-name marker, or, for a legacy caller without the marker, the
    canonical legacy repair title for the same exact HEAD. A marked run may
    report the dispatch ref (normally ``main``) as its SHA rather than the PR
    HEAD, so the attempt marker and exact run ID, when present, are the binding
    evidence. ``claimed``, ``ambiguous``, and run-detail-less ``dispatched``
    receipts are included because outcome persistence can fail after an
    accepted dispatch. An unrelated commenter cannot borrow a run, so
    unverified lookalikes stay ignored exactly as before.

    The second result reports that history exists which cannot be verified
    because the run inventory failed or is incomplete, or because a plausible
    historical writer's receipt has no proving run. A writer recorded in the
    durable repository registry remains plausible even after its dispatch run
    is deleted. The caller must hold in that case instead of treating those
    receipts as absent.
    """

    candidates = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        body = comment.get("body")
        if not isinstance(body, str) or RECEIPT_MARKER not in body:
            continue
        author = comment_login(comment)
        if not author:
            continue
        if is_trusted_receipt_comment(
            comment,
            owner_login=owner_login,
            trusted_logins=tuple(trusted_logins),
        ):
            continue
        try:
            receipts = parse_receipts(body)
        except ValueError:
            # An untrusted lookalike is ignored, never fatal.
            continue
        for receipt in receipts:
            if (
                receipt.repository == repository
                and receipt.pull_number == pull_number
                and receipt.head_sha == head_sha
                and receipt.status != STATUS_REJECTED
            ):
                candidates.append((author, receipt, comment))
    if not candidates:
        return (), False

    try:
        inventory = _workflow_runs(repository, workflow)
    except GhFailure as exc:
        print(
            "::warning::Could not verify historical CI-repair receipt authors: {}".format(
                exc
            )
        )
        return (), True
    if not inventory.complete:
        print(
            "::warning::Historical CI-repair receipt authors cannot be verified: "
            "{}".format(inventory.reason)
        )
        return (), True

    verified = []
    unmatched = []
    unverifiable = False
    for author, receipt, comment in candidates:
        matches = [
            run
            for run in inventory.runs
            if run_proves_historical_receipt(
                run, receipt, author_login=author
            )
        ]
        if len(matches) > 1:
            # Corrupt evidence cannot establish which run belongs to this
            # attempt; hold rather than guessing or dispatching again.
            unverifiable = True
            continue
        if not matches:
            unmatched.append((author, comment))
            continue
        if author not in verified:
            verified.append(author)

    # A previously authoritative writer can be proved by any one of its
    # receipts. An unproved receipt for that same writer adds no new risk. Only
    # an unproved receipt from a writer with no other proof can hide a spent
    # attempt, so hold when its author is in the durable writer registry, had
    # repository write access, or GitHub shows a repair run for this PR created
    # by that account. The run's SHA can be the dispatch ref (normally ``main``)
    # instead of the PR HEAD, so it is not required to match the receipt HEAD
    # here; an unrelated commenter with none of that evidence cannot suppress a
    # repair.
    known = frozenset(_normalize_writer_logins("\n".join(known_writers)))
    for author, comment in unmatched:
        if author in verified:
            continue
        association = str(comment.get("author_association") or "").strip().upper()
        author_has_repair_run = any(
            author in run_actor_logins(run)
            and run_repairs_pull(run, pull_number)
            for run in inventory.runs
        )
        if (
            author in known
            or not registry_available
            or association in _HISTORICAL_WRITER_ASSOCIATIONS
            or author_has_repair_run
        ):
            unverifiable = True
            break
    return tuple(verified), unverifiable


def _print_plan(plan: AdmissionPlan) -> None:
    level = "notice" if plan.action in (ACTION_CLAIM, ACTION_WAIT) else "warning"
    print(
        "::{level}::CI-repair admission action={action} attempt={attempt} "
        "reason={reason}".format(
            level=level,
            action=plan.action,
            attempt=plan.attempt_id or "-",
            reason=plan.reason.replace("\n", " "),
        )
    )


def run(args) -> int:
    repository = normalize_repository(
        args.repository or os.environ.get("GITHUB_REPOSITORY", "")
    )
    pull_number = normalize_pull_number(args.pull_number)
    head_sha = normalize_head_sha(args.head_sha)

    try:
        pr = _pull(repository, pull_number)
    except GhFailure as exc:
        print("::warning::Could not revalidate PR #{}: {}".format(pull_number, exc))
        return 0
    head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
    head_ref = str(head.get("ref") or args.head_ref or "")
    mismatch = _pr_head_mismatch(pr, repository, head_sha)
    if mismatch:
        print(
            "::notice::CI-repair admission skipped for PR #{} at exact HEAD {}: {}.".format(
                pull_number, head_sha, mismatch
            )
        )
        return 0

    try:
        pages = _comment_pages(repository, pull_number)
    except GhFailure as exc:
        print("::warning::CI-repair receipt history unavailable; holding: {}".format(exc))
        return 0

    trusted_logins = []
    token_login = _authenticated_login()
    if not token_login:
        # Receipts are authored by this token, including a collaborator PAT
        # whose association is not OWNER. Without its identity the reader
        # could classify its own claim as an untrusted lookalike and dispatch
        # a second repair, so hold instead.
        print(
            "::warning::CI-repair token identity is unavailable; holding "
            "instead of risking a duplicate claim."
        )
        return 0
    trusted_logins.append(token_login)

    expected = pr.get("comments")
    history = normalize_comment_pages(
        pages,
        expected_count=expected if isinstance(expected, int) else None,
    )
    if not history.complete:
        print(
            "::warning::CI-repair receipt history is incomplete; holding instead "
            "of dispatching: {}".format(history.reason)
        )
        return 0

    # A rotated TAP_PAT account must not turn previously authoritative receipts
    # into ignored lookalikes. GitHub-attested dispatch evidence and the
    # durable writer registry extend trust; an unrelated commenter can add
    # neither. Recording the current login before it rotates keeps deleted-run
    # history authoritative for the next token.
    known_writers, registry_exists = _historical_writer_registry(repository)
    historical_writers, unverifiable_history = _historical_receipt_writers(
        repository,
        pull_number,
        head_sha,
        history.comments,
        workflow=args.workflow,
        owner_login=args.owner_login or "",
        trusted_logins=tuple(trusted_logins),
        known_writers=known_writers,
        registry_available=registry_exists is not None,
    )
    if registry_exists is not None and not args.dry_run:
        _persist_historical_writers(
            repository,
            (token_login, *historical_writers),
            known=known_writers,
            exists=registry_exists,
        )
    if unverifiable_history:
        print(
            "::warning::Historical CI-repair receipts cannot be verified; "
            "holding instead of risking a duplicate dispatch."
        )
        return 0
    for login in historical_writers:
        trusted_logins.append(login)
        print(
            "::notice::Historical CI-repair receipt author {!r} is verified by "
            "its exact-HEAD dispatch run.".format(login)
        )

    collection = read_receipts(
        history.comments,
        repository=repository,
        pull_number=pull_number,
        head_sha=head_sha,
        history_complete=True,
        history_ordered=True,
        expected_comment_count=(
            len(history.comments) if history.expected_count is not None else None
        ),
        owner_login=args.owner_login or "",
        trusted_logins=tuple(trusted_logins),
    )

    plan = plan_admission(collection)
    attempt = None
    if plan.attempt_id:
        attempt = next(
            (item for item in collection.attempts if item.attempt_id == plan.attempt_id),
            None,
        )
    needs_runs = attempt is not None and (
        attempt.status in (STATUS_CLAIMED, STATUS_AMBIGUOUS)
        or (attempt.status == STATUS_DISPATCHED and attempt.run_id is None)
    )
    if needs_runs:
        observed = attempt_observed_at(
            history.comments,
            attempt.attempt_id,
            repository=repository,
            pull_number=pull_number,
            head_sha=head_sha,
            owner_login=args.owner_login or "",
            trusted_logins=tuple(trusted_logins),
        )
        runs = None
        try:
            runs = _workflow_runs(
                repository,
                args.workflow,
                since=observed,
            )
        except GhFailure as exc:
            print("::warning::Could not reconcile CI-repair runs: {}".format(exc))
        plan = plan_admission(
            collection,
            runs=runs,
            observed_at=observed,
            now=args.now,
            claim_timeout_seconds=args.claim_timeout_seconds,
            pull_number=pull_number,
        )

    if plan.transition is not None:
        receipt = plan.transition
        if args.dry_run:
            print(_comment_body(receipt, "CI-repair receipt transition (dry run)."))
            _print_plan(plan)
            return 0
        try:
            _create_comment(
                repository,
                pull_number,
                _comment_body(
                    receipt,
                    "CI-repair attempt `{}` advanced to `{}`: {}".format(
                        receipt.attempt_id, receipt.status, plan.reason
                    ),
                ),
            )
        except GhFailure as exc:
            print("::error::{}".format(exc))
            return 1
        _print_plan(plan)
        if plan.action != ACTION_REARM:
            return 0
        # The dead claim is now durably rejected. This serialized pass owns the
        # exact HEAD, so it may immediately admit one fresh attempt below.

    if plan.action not in (ACTION_CLAIM, ACTION_REARM):
        _print_plan(plan)
        return 0

    attempt_id = new_attempt_id()
    claimed = build_receipt(
        repository=repository,
        pull_number=pull_number,
        head_sha=head_sha,
        attempt_id=attempt_id,
        status=STATUS_CLAIMED,
    )
    claim_body = _comment_body(
        claimed,
        "Claiming the single automatic CI-repair attempt for exact HEAD "
        "`{}` after a failing approved workflow. The claim is persisted "
        "before dispatch; no ci-repair label participates in this decision.".format(
            head_sha
        ),
    )
    if args.dry_run:
        print(claim_body)
        return 0
    try:
        claim_comment = _create_comment(repository, pull_number, claim_body)
    except GhFailure as exc:
        print(
            "::error::{} Refusing to dispatch without a persisted claim.".format(exc)
        )
        return 1
    claim_created = claim_comment.get("created_at")
    claim_since = (
        claim_created
        if isinstance(claim_created, str) and claim_created
        else None
    )

    # The claim is persisted against an exact HEAD, but the branch can advance
    # between admission and the dispatch request. Dispatching then would let
    # this attempt repair a newer HEAD that independently receives its own
    # attempt, so prove the admitted HEAD is still current immediately before
    # dispatch. The persisted claim stays bound to its own exact HEAD and can
    # never block the newer HEAD's key.
    stale = _revalidate_head(repository, pull_number, head_sha)
    if stale:
        print(
            "::notice::CI-repair dispatch withheld: {}. The persisted claim "
            "remains bound to exact HEAD {}; a newer HEAD admits its own "
            "attempt.".format(stale, head_sha)
        )
        return 0

    status, response_body, detail = _dispatch(
        repository,
        args.workflow,
        ref=args.ref,
        pull_number=pull_number,
        head_ref=head_ref,
        failing_run_id=args.failing_run_id or args.reported_run_id or "",
        attempt_id=attempt_id,
        head_sha=head_sha,
    )
    outcome = classify_dispatch_response(
        claimed,
        http_status=status,
        response_body=response_body,
        network_error=status is None,
    )

    # Legacy 204 and ambiguous outcomes are reconciled before the transition is
    # persisted. A proved run enriches the same attempt; absence of proof keeps
    # the durable hold instead of risking a second dispatch.
    receipt = outcome.receipt
    if (outcome.accepted and receipt.run_id is None) or outcome.ambiguous:
        runs = None
        try:
            runs = _workflow_runs(
                repository,
                args.workflow,
                since=claim_since,
            )
        except GhFailure as exc:
            print("::warning::Could not reconcile CI-repair runs: {}".format(exc))
        if runs is not None and runs.complete:
            try:
                run = select_correlated_run(
                    runs.runs,
                    attempt_id=attempt_id,
                    pull_number=pull_number,
                )
            except Exception as exc:  # noqa: BLE001 - corrupt inventory fails closed
                print("::warning::CI-repair run correlation failed closed: {}".format(exc))
                run = None
            if run is not None:
                receipt = transition_receipt(
                    claimed,
                    status=STATUS_DISPATCHED,
                    run_id=run["id"],
                    run_url=correlated_run_url(repository, run),
                )
                outcome = type(outcome)(
                    kind="accepted",
                    receipt=receipt,
                    reason="reconciled the dispatch to actual run {}".format(run["id"]),
                    accepted=True,
                )

    try:
        _create_comment(
            repository,
            pull_number,
            _comment_body(
                receipt,
                "CI-repair attempt `{}` recorded `{}`: {}".format(
                    receipt.attempt_id, receipt.status, outcome.reason
                ),
            ),
        )
    except GhFailure as exc:
        # The claim remains durable. The next serialized pass can correlate the
        # attempt marker; never invent a second dispatch after an accepted one.
        print(
            "::warning::Dispatch outcome could not be persisted; the durable "
            "claim remains and will be reconciled: {}".format(exc)
        )

    if outcome.accepted:
        _add_label(repository, pull_number, args.label)
        print(
            "::notice::CI-repair dispatch accepted for exact HEAD {} "
            "(attempt {}, run {}).".format(
                head_sha, attempt_id, receipt.run_id or "legacy-204"
            )
        )
    elif outcome.rejected:
        print(
            "::warning::CI-repair dispatch was definitively rejected; attempt "
            "{} is recorded rejected and a later serialized pass may claim a "
            "fresh attempt. Detail: {}".format(attempt_id, detail[:500])
        )
    else:
        print(
            "::warning::CI-repair dispatch is ambiguous for attempt {}; it is "
            "recorded as a visible hold and will only resume after actual-run "
            "correlation. Detail: {}".format(attempt_id, detail[:500])
        )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pull-number", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--failing-run-id", default="")
    parser.add_argument("--reported-run-id", default="")
    parser.add_argument("--label", default="")
    parser.add_argument("--owner-login", default="")
    parser.add_argument("--claim-timeout-seconds", default="900")
    parser.add_argument("--now", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (ValueError, AdmissionError) as exc:
        print("::error::CI-repair admission failed closed: {}".format(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
