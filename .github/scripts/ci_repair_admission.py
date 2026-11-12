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
  attempt's run-name marker, never retried blindly;
- trusted receipt authors are registered in independent repository variables,
  so token rotation or a concurrent admission pass cannot erase writer history;
- a separate trusted retirement record marks when older controller instances
  stopped writing, so an unproved receipt after the migration baseline holds
  until the operator can prove no legacy controller is still running.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
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
_ISO_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
# A rotated TAP_PAT can leave receipts authored by a former collaborator. If
# no run proves that writer, admission must hold instead of treating the
# receipt as absent. Any receipt created at or before the registry baseline
# stays unverifiable: its run may already be deleted, and an accepted dispatch
# whose outcome failed to persist can leave only a ``claimed`` receipt behind.
# An older controller can also write after the baseline, so a post-baseline
# receipt from an author with no trusted writer evidence holds until the trusted
# legacy-controller retirement record shows every older instance is gone. After
# that record it is a lookalike and cannot suppress a repair, while a spent
# exact HEAD is never re-dispatched.
_HISTORICAL_WRITER_ASSOCIATIONS = frozenset({"MEMBER", "COLLABORATOR"})
#: Legacy comma/newline-separated registry of logins that authored trusted
#: receipts. It is only read now: a pre-existing value is migrated into
#: per-writer variables so history recorded before that format is not lost.
_CI_REPAIR_WRITERS_VARIABLE = "CONTINUUM_CI_REPAIR_WRITERS"
#: One repository variable per trusted writer. Registering a writer creates
#: only its own variable, so two exact-HEAD admission passes can never erase
#: each other's registration through a read-merge-write race. The value is the
#: lowercase login; the suffix is a digest because a login may contain
#: characters that repository variable names do not allow.
_CI_REPAIR_WRITER_VARIABLE_PREFIX = "CONTINUUM_CI_REPAIR_WRITER_"
#: One-time migration frontier for the writer registry. Its value is the UTC
#: timestamp at which the durable registry was first established. A receipt
#: comment created at or before that instant that records a dispatch (its run
#: may already be deleted) may have been written by a historical writer that was
#: never migrated, so an unproved author of such a receipt is unverifiable
#: rather than a proven lookalike. The frontier has second precision, so the
#: comparison is inclusive. The name deliberately does not start with the
#: per-writer prefix.
_CI_REPAIR_WRITERS_BASELINE_VARIABLE = "CONTINUUM_CI_REPAIR_WRITERS_BASELINE"
#: Trusted legacy-controller retirement record. The registry baseline alone
#: cannot make an unproved receipt after it a lookalike: an older controller can
#: still be running, or remain pinned, and write a receipt after the frontier.
#: Only an operator assertion that every legacy controller instance was retired
#: separates that case from an unrelated commenter. Its value is the UTC instant
#: at or before which legacy controllers may still have written receipts; a
#: receipt created strictly after it cannot come from a legacy controller. Until
#: the record exists every unproved post-baseline receipt holds, so a spent exact
#: HEAD is never re-dispatched. It deliberately does not start with the
#: per-writer prefix.
_CI_REPAIR_LEGACY_RETIRED_VARIABLE = "CONTINUUM_CI_REPAIR_LEGACY_RETIRED_AT"


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


def _writer_variable_name(login: str) -> str:
    """Return the deterministic per-writer repository variable name."""

    digest = hashlib.sha256(login.encode("utf-8")).hexdigest()
    return "{}{}".format(_CI_REPAIR_WRITER_VARIABLE_PREFIX, digest)


def _legacy_historical_writers(repository: str) -> Optional[Tuple[str, ...]]:
    """Read the legacy single-variable writer registry.

    ``None`` means the registry could not be read and must not be treated as
    empty. A missing variable is a normal empty baseline; its contents are
    migrated into per-writer variables by :func:`_persist_historical_writers`.
    """

    url = "repos/{}/actions/variables/{}".format(
        repository, _CI_REPAIR_WRITERS_VARIABLE
    )
    _, stdout, stderr = _gh(["api", "--include", url])
    status, body = _parse_include(stdout, stderr)
    if status == 404:
        return ()
    if status != 200:
        print(
            "::warning::Could not read legacy historical CI-repair writer registry: "
            "{}".format((body or stderr).strip() or "HTTP status {}".format(status))
        )
        return None
    try:
        data = json.loads(body or "{}")
    except ValueError:
        print(
            "::warning::Legacy historical CI-repair writer registry is not valid JSON."
        )
        return None
    if not isinstance(data, Mapping):
        print(
            "::warning::Legacy historical CI-repair writer registry is not an object."
        )
        return None
    return _normalize_writer_logins(data.get("value"))


def _registered_historical_writers(
    repository: str,
) -> Optional[Tuple[str, ...]]:
    """Read the per-writer registry variables.

    ``None`` means the variable list could not be read and must not be treated
    as an empty registry.
    """

    url = "repos/{}/actions/variables?per_page=100".format(repository)
    code, stdout, stderr = _gh(["api", "--paginate", "--slurp", url])
    if code != 0:
        print(
            "::warning::Could not read historical CI-repair writer registry: "
            "{}".format(stderr.strip() or "gh exited {}".format(code))
        )
        return None
    try:
        pages = json.loads(stdout or "[]")
    except ValueError:
        print(
            "::warning::Historical CI-repair writer registry is not valid JSON."
        )
        return None
    if isinstance(pages, Mapping):
        pages = [pages]
    if not isinstance(pages, list):
        print(
            "::warning::Historical CI-repair writer registry is not an array of pages."
        )
        return None
    logins = []
    for page in pages:
        if not isinstance(page, Mapping):
            print(
                "::warning::Historical CI-repair writer registry page is not an object."
            )
            return None
        variables = page.get("variables")
        if not isinstance(variables, list):
            print(
                "::warning::Historical CI-repair writer registry page has no variables."
            )
            return None
        for variable in variables:
            if not isinstance(variable, Mapping):
                continue
            name = variable.get("name")
            if not isinstance(name, str) or not name.startswith(
                _CI_REPAIR_WRITER_VARIABLE_PREFIX
            ):
                continue
            value = variable.get("value")
            if not isinstance(value, str):
                continue
            logins.extend(_normalize_writer_logins(value))
    return _normalize_writer_logins("\n".join(logins))


def _historical_writer_registry(
    repository: str,
) -> Tuple[Tuple[str, ...], Tuple[str, ...], bool]:
    """Return known writers, writers already in per-writer variables, and health.

    A missing registry is a normal empty baseline and is not proof that no
    historical writer exists. Verified historical writers and the legacy
    registry contents are migrated into per-writer variables, which survive
    run deletion and a later token rotation. An unreadable registry is not
    proof of an empty history, so the caller holds instead of dispatching. A
    receipt with no proving run from an author that no registry, association,
    or run evidence identifies remains an unrelated lookalike; treating every
    such author as historical would let an arbitrary commenter suppress repair.
    """

    legacy = _legacy_historical_writers(repository)
    registered = _registered_historical_writers(repository)
    known = list(legacy or ())
    for login in registered or ():
        if login not in known:
            known.append(login)
    healthy = legacy is not None and registered is not None
    return tuple(known), tuple(registered or ()), healthy


def _writer_variable_records(repository: str, login: str) -> bool:
    """Whether the per-writer variable already records this exact login."""

    url = "repos/{}/actions/variables/{}".format(
        repository, _writer_variable_name(login)
    )
    _, stdout, stderr = _gh(["api", "--include", url])
    status, body = _parse_include(stdout, stderr)
    if status != 200:
        return False
    try:
        data = json.loads(body or "{}")
    except ValueError:
        return False
    if not isinstance(data, Mapping):
        return False
    return login in _normalize_writer_logins(data.get("value"))


def _admission_timestamp(value: Any = None) -> str:
    """Return a normalized UTC timestamp for a new writer baseline."""

    text = str(value or "").strip()
    if _ISO_TIMESTAMP_RE.match(text):
        return text
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_writer_baseline(repository: str) -> Tuple[Optional[str], bool]:
    """Read the one-time writer-registry migration frontier.

    ``(None, True)`` means no baseline has been established yet. That is not
    proof that no historical writer exists: receipts may predate the registry,
    and their proving runs may already be deleted. ``(None, False)`` means the
    baseline could not be read and must also not be treated as absent.
    """

    url = "repos/{}/actions/variables/{}".format(
        repository, _CI_REPAIR_WRITERS_BASELINE_VARIABLE
    )
    _, stdout, stderr = _gh(["api", "--include", url])
    status, body = _parse_include(stdout, stderr)
    if status == 404:
        return None, True
    if status != 200:
        print(
            "::warning::Could not read historical CI-repair writer baseline: "
            "{}".format((body or stderr).strip() or "HTTP status {}".format(status))
        )
        return None, False
    try:
        data = json.loads(body or "{}")
    except ValueError:
        print(
            "::warning::Historical CI-repair writer baseline is not valid JSON."
        )
        return None, False
    if not isinstance(data, Mapping):
        print(
            "::warning::Historical CI-repair writer baseline is not an object."
        )
        return None, False
    value = data.get("value")
    if not isinstance(value, str) or not _ISO_TIMESTAMP_RE.match(value.strip()):
        print(
            "::warning::Historical CI-repair writer baseline is not a usable "
            "timestamp."
        )
        return None, False
    return value.strip(), True


def _read_legacy_retirement(repository: str) -> Tuple[Optional[str], bool]:
    """Read the trusted legacy-controller retirement record.

    ``(None, True)`` means legacy controllers have not been declared retired, so
    an unproved post-baseline receipt cannot be classified as a lookalike and
    must hold. ``(None, False)`` means the record could not be read and must not
    be treated as retired.
    """

    url = "repos/{}/actions/variables/{}".format(
        repository, _CI_REPAIR_LEGACY_RETIRED_VARIABLE
    )
    _, stdout, stderr = _gh(["api", "--include", url])
    status, body = _parse_include(stdout, stderr)
    if status == 404:
        return None, True
    if status != 200:
        print(
            "::warning::Could not read the CI-repair legacy-controller retirement "
            "record: {}".format(
                (body or stderr).strip() or "HTTP status {}".format(status)
            )
        )
        return None, False
    try:
        data = json.loads(body or "{}")
    except ValueError:
        print(
            "::warning::CI-repair legacy-controller retirement record is not "
            "valid JSON."
        )
        return None, False
    if not isinstance(data, Mapping):
        print(
            "::warning::CI-repair legacy-controller retirement record is not an "
            "object."
        )
        return None, False
    value = data.get("value")
    if not isinstance(value, str) or not _ISO_TIMESTAMP_RE.match(value.strip()):
        print(
            "::warning::CI-repair legacy-controller retirement record is not a "
            "usable timestamp."
        )
        return None, False
    return value.strip(), True


def _persist_writer_baseline(repository: str, timestamp: str) -> bool:
    """Record the one-time writer-registry baseline without moving it."""

    code, _, stderr = _gh(
        [
            "api",
            "--method",
            "POST",
            "repos/{}/actions/variables".format(repository),
            "-f",
            "name={}".format(_CI_REPAIR_WRITERS_BASELINE_VARIABLE),
            "-f",
            "value={}".format(timestamp),
        ]
    )
    if code == 0:
        return True
    # A concurrent pass may have established the baseline first. Keep that
    # frontier instead of overwriting it with a later timestamp.
    existing, healthy = _read_writer_baseline(repository)
    if healthy and existing is not None:
        return True
    print(
        "::warning::Could not record the historical CI-repair writer baseline: "
        "{}".format(stderr.strip() or "gh exited {}".format(code))
    )
    return False


def _persist_historical_writers(
    repository: str,
    logins: Sequence[str],
    *,
    known: Sequence[str] = (),
    registered: Sequence[str] = (),
) -> Tuple[Tuple[str, ...], bool]:
    """Record each trusted writer in its own durable repository variable.

    Each login is written independently, so concurrent admission passes for
    different exact HEADs cannot overwrite a writer another pass discovered.
    The second result is ``False`` when any registration is not durably
    recorded; the caller must then hold instead of dispatching.
    """

    merged = list(_normalize_writer_logins("\n".join(known)))
    for login in _normalize_writer_logins("\n".join(logins)):
        if login not in merged:
            merged.append(login)

    already = frozenset(_normalize_writer_logins("\n".join(registered)))
    durable = True
    for login in merged:
        if login in already:
            continue
        code, _, stderr = _gh(
            [
                "api",
                "--method",
                "POST",
                "repos/{}/actions/variables".format(repository),
                "-f",
                "name={}".format(_writer_variable_name(login)),
                "-f",
                "value={}".format(login),
            ]
        )
        if code == 0:
            continue
        # A concurrent pass may have created the exact same per-writer
        # variable first. That is success, not a lost registration.
        if _writer_variable_records(repository, login):
            continue
        print(
            "::warning::Could not record historical CI-repair writer {!r}: "
            "{}".format(login, stderr.strip() or "gh exited {}".format(code))
        )
        durable = False
    return tuple(merged), durable


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


def _carry_proved_runs(
    inventory: RunInventory,
    proved_runs: Sequence[Mapping[str, Any]],
) -> RunInventory:
    """Keep earlier verified runs in a later complete recovery inventory.

    Historical-writer verification and stale-claim recovery read the run
    inventory at different instants. A run can prove an already-dispatched
    attempt and then be deleted before recovery. Treating that deletion as proof
    that no run exists would re-arm the claim and dispatch the same exact HEAD
    again, so the earlier verified run stays part of the recovery evidence. The
    caller only reaches here with a complete current inventory; this adds a
    previously proved run, it never hides a partial read.
    """

    existing = {str(run.get("id")) for run in inventory.runs}
    missing = []
    for run in proved_runs:
        run_id = str(run.get("id"))
        if run_id in existing:
            continue
        existing.add(run_id)
        missing.append(run)
    if not missing:
        return inventory
    return RunInventory(
        runs=tuple(inventory.runs) + tuple(missing),
        complete=inventory.complete,
        reason=inventory.reason,
        expected_count=inventory.expected_count,
    )


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
    baseline: Optional[str] = None,
    legacy_retired: Optional[str] = None,
    proved_runs: Optional[list[Mapping[str, Any]]] = None,
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

    ``baseline`` is the durable writer-registry migration frontier. Any receipt
    created at or before it may belong to a historical writer that was never
    migrated, whose dispatch run may already be deleted; such an unproved author
    is unverifiable, not a proven lookalike. The comparison is inclusive because
    the frontier has second precision. This includes ``claimed`` receipts: the
    claim is persisted before the dispatch request, so a dispatch can be
    accepted while persisting its outcome fails, leaving only the claim behind.
    With no established baseline every unproved receipt is unverifiable, so a
    missing registry can never reauthorize a spent exact HEAD.

    The baseline alone still cannot classify a receipt after it: an older
    controller for another exact HEAD can remain running or pinned and write a
    receipt after the frontier, and token rotation can later leave its account
    with no association and its run deleted. ``legacy_retired`` is the trusted
    migration record that names the instant at which every such older controller
    was retired. Only a receipt created strictly after both frontiers cannot
    come from a legacy controller; without that record every unproved
    post-baseline receipt holds. Once it is present, trusted historical-writer
    evidence (the durable writer registry, repository write access, or a
    GitHub-attested repair run by that account) separates a retired-era writer
    from an unrelated lookalike. A post-baseline receipt whose author has none of
    that evidence is then a lookalike and stays ignored, so an unrelated
    commenter cannot suppress a repair by posting a receipt, whatever its
    format. A receipt created at or before the retirement frontier can still be
    from an overlapping older controller, so its unproved receipt holds
    regardless of format.

    The second result reports that history exists which cannot be verified
    because the run inventory failed or is incomplete, or because a plausible
    historical writer's receipt has no proving run. A writer recorded in the
    durable repository registry remains plausible even after its dispatch run
    is deleted. The caller must hold in that case instead of treating those
    receipts as absent.

    ``proved_runs``, when supplied, is extended with each run that proves a
    candidate receipt. The caller carries this evidence into stale-claim
    recovery: a run verified here can be deleted before the later recovery
    inventory, and losing it must not turn a spent claim into a re-armable one.
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
            unmatched.append((author, receipt, comment))
            continue
        if proved_runs is not None:
            proved_id = matches[0].get("id")
            if all(run.get("id") != proved_id for run in proved_runs):
                proved_runs.append(matches[0])
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
    baseline_text = str(baseline or "").strip()
    baseline_usable = bool(_ISO_TIMESTAMP_RE.match(baseline_text))
    retired_text = str(legacy_retired or "").strip()
    retired_usable = bool(_ISO_TIMESTAMP_RE.match(retired_text))
    for author, receipt, comment in unmatched:
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
        if not baseline_usable:
            # Without a migration frontier no receipt can be classified as a
            # lookalike, so fail closed.
            unverifiable = True
            break
        created = comment.get("created_at")
        if not isinstance(created, str) or not _ISO_TIMESTAMP_RE.match(created):
            unverifiable = True
            break
        # The baseline is recorded with second precision, so a receipt created
        # in the same second is ambiguous and must not be treated as newer.
        predates_baseline = created <= baseline_text
        # Any pre-baseline receipt can hide a spent exact HEAD. A ``claimed``
        # receipt is normally persisted before the dispatch request, but the
        # dispatch can be accepted while persisting its outcome fails, leaving
        # only the claim behind; its run may then be deleted. Without registry,
        # association, or run evidence that author cannot be told apart from a
        # historical writer, so the receipt fails closed instead of authorizing
        # a duplicate dispatch.
        if predates_baseline:
            unverifiable = True
            break
        # The baseline only proves that the registry existed, not that an older
        # controller stopped writing. Until the trusted retirement record says
        # every legacy instance is gone, a post-baseline receipt can still be a
        # genuine spent attempt whose run was deleted after token rotation, so
        # it must hold. Both records have second precision, so a receipt in the
        # same second as the retirement instant is treated as older.
        if not retired_usable or created <= retired_text:
            unverifiable = True
            break
        # Strictly after both frontiers the author cannot be a legacy
        # controller. An author with none of the trusted evidence above is then
        # an unrelated lookalike: holding for it would let any commenter
        # suppress a repair, so it stays ignored instead.
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
    # history authoritative for the next token. Per-writer variables make the
    # registration independent, so concurrent exact-HEAD passes cannot erase
    # each other's writer. A one-time baseline records when the registry became
    # authoritative, so every pre-baseline receipt is unverifiable rather than a
    # lookalike: its dispatch run may already be deleted, and an accepted
    # dispatch whose outcome failed to persist can leave only a claim behind.
    # The separate retirement record is the only trusted evidence that an older,
    # possibly pinned, controller can no longer write legacy receipts after that
    # frontier; without it every unproved post-baseline receipt holds.
    known_writers, registered_writers, registry_healthy = _historical_writer_registry(
        repository
    )
    if not registry_healthy:
        # The registry may contain a writer whose dispatch run is gone. Without
        # that history the exact HEAD could look unused, so do not dispatch.
        # Still attempt to record this token so a later run can recover.
        if not args.dry_run:
            _persist_historical_writers(repository, (token_login,))
        print(
            "::warning::Historical CI-repair writer registry cannot be read; "
            "holding instead of risking a duplicate dispatch."
        )
        return 0
    baseline, baseline_healthy = _read_writer_baseline(repository)
    if not baseline_healthy:
        print(
            "::warning::Historical CI-repair writer baseline cannot be read; "
            "holding instead of risking a duplicate dispatch."
        )
        return 0
    if baseline is None:
        # Establish the migration frontier now. Receipts already present may
        # have been written by a historical writer that was never registered
        # and whose proving run is already deleted, so an unproved author of
        # those receipts stays unverifiable instead of authorizing a duplicate.
        baseline = _admission_timestamp(args.now)
        if not args.dry_run and not _persist_writer_baseline(
            repository, baseline
        ):
            print(
                "::warning::Historical CI-repair writer baseline is not durably "
                "recorded; holding instead of risking a duplicate dispatch."
            )
            return 0
    legacy_retired, retirement_healthy = _read_legacy_retirement(repository)
    if not retirement_healthy:
        print(
            "::warning::CI-repair legacy-controller retirement record cannot be "
            "read; holding instead of risking a duplicate dispatch."
        )
        return 0
    proved_historical_runs: list[Mapping[str, Any]] = []
    historical_writers, unverifiable_history = _historical_receipt_writers(
        repository,
        pull_number,
        head_sha,
        history.comments,
        workflow=args.workflow,
        owner_login=args.owner_login or "",
        trusted_logins=tuple(trusted_logins),
        known_writers=known_writers,
        registry_available=True,
        baseline=baseline,
        legacy_retired=legacy_retired,
        proved_runs=proved_historical_runs,
    )
    if not args.dry_run:
        _, writers_durable = _persist_historical_writers(
            repository,
            (token_login, *historical_writers, *known_writers),
            known=known_writers,
            registered=registered_writers,
        )
        if not writers_durable:
            print(
                "::warning::Historical CI-repair writers are not durably recorded; "
                "holding instead of risking a duplicate dispatch after rotation."
            )
            return 0
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
        if runs is not None and runs.complete:
            runs = _carry_proved_runs(runs, proved_historical_runs)
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
