"""GitHub evidence retrieval shared by the contract-canary workflow scripts.

The canary resolves its replacement decision and observes the live task in two
separate shell steps. Both need the same implementation-PR commits, worker
publication receipt, and repair-publication receipt, and both must fail closed
when GitHub cannot provide them. Keeping one standard-library implementation
here means a receipt-contract or error-handling change cannot be applied to
only one of the two paths.

The module is deliberately free of workflow syntax: the callers pass the
repository explicitly and receive plain data (or a fatal ``SystemExit``), which
keeps the retrieval deterministic and unit-testable. Standard library only.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import zipfile
from typing import Any, Mapping, Optional, Sequence

from .contract_canary import (
    CANARY_PUBLICATION_ARTIFACT_PREFIX,
    CANARY_REPAIR_PUBLICATION_ARTIFACT_PREFIX,
    CANARY_REPAIR_STATUS_CONTEXT,
    publication_artifacts,
    repair_status_run_id,
    select_repair_publication,
)


def _gh(
    arguments: Sequence[str],
    description: str,
    text: bool = True,
) -> "subprocess.CompletedProcess[Any]":
    """Run one ``gh`` call and fail closed with the canonical diagnostic."""
    proc = subprocess.run(
        list(arguments),
        check=False,
        capture_output=True,
        text=text,
    )
    if proc.returncode != 0:
        stderr = proc.stderr or (b"" if not text else "")
        details = (
            stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
        ).strip()[:500]
        raise SystemExit(
            "could not {}: {}".format(description, details)
        )
    return proc


def _artifact_listing(repo: str, run_id: int) -> str:
    return _gh(
        [
            "gh", "api", "--paginate", "--jq",
            ".artifacts[] | [.id, .name, .expired] | @tsv",
            "repos/{}/actions/runs/{}/artifacts?per_page=100".format(repo, run_id),
        ],
        "read publication artifacts for run {}".format(run_id),
    ).stdout


def implementation_commits(
    repo: str,
    pr_number: object,
    branch: str,
) -> list:
    """Return the implementation PR's full commit history.

    Trusted continuity is computed from GitHub's own ``parents`` links, and
    each commit carries the API-written repair statuses that authenticate later
    automation. A failed or malformed read is fatal instead of silently
    accepting a receipt that only names an older commit.
    """
    if not pr_number:
        return []
    proc = _gh(
        [
            "gh", "api", "--paginate", "--slurp",
            "repos/{}/pulls/{}/commits?per_page=100".format(repo, pr_number),
        ],
        "read commits for implementation PR #{}; refusing incomplete "
        "publication continuity".format(pr_number),
    )
    try:
        pages = json.loads(proc.stdout)
    except ValueError:
        raise SystemExit(
            "implementation PR #{} commit history could not be parsed".format(
                pr_number
            )
        )
    commits = []
    for page in pages if isinstance(pages, list) else [pages]:
        if isinstance(page, list):
            commits.extend(page)
        elif isinstance(page, dict):
            commits.append(page)
    for commit in commits:
        sha = str(commit.get("sha") or "").strip()
        if not sha:
            continue
        statuses = _gh(
            [
                "gh", "api", "--paginate", "--slurp",
                "repos/{}/commits/{}/statuses?per_page=100".format(repo, sha),
            ],
            "read commit statuses for {}; refusing incomplete publication "
            "continuity".format(sha),
        )
        try:
            pages = json.loads(statuses.stdout)
        except ValueError:
            raise SystemExit(
                "commit statuses for {} could not be parsed".format(sha)
            )
        parsed_statuses = []
        for page in pages if isinstance(pages, list) else [pages]:
            if isinstance(page, list):
                parsed_statuses.extend(page)
            elif isinstance(page, dict):
                parsed_statuses.append(page)
        for status in parsed_statuses:
            if not isinstance(status, dict):
                continue
            if str(status.get("context") or "") != CANARY_REPAIR_STATUS_CONTEXT:
                continue
            run_id = repair_status_run_id(status, repo)
            if run_id is None:
                continue
            status["publication"] = repair_publication(
                repo, run_id, commit, pr_number, branch
            )
        commit["statuses"] = parsed_statuses
    return commits


def worker_publication(repo: str, run_id: int) -> Optional[Mapping[str, Any]]:
    """Return the run-scoped publication receipt, or None when absent.

    A failed read is fatal: a stale snapshot must never stand in for the run's
    own publication evidence.
    """
    listing = _artifact_listing(repo, run_id)
    candidates = []
    for line in listing.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or not fields[0].isdigit():
            continue
        artifact_id, name, expired = fields
        if str(expired).strip().lower() == "true":
            continue
        if not name.startswith(CANARY_PUBLICATION_ARTIFACT_PREFIX):
            continue
        candidates.append((int(artifact_id), name))
    if not candidates:
        return None
    _, name = max(candidates)
    target = tempfile.mkdtemp(
        prefix="continuum-publication-",
        dir=os.environ.get("RUNNER_TEMP") or None,
    )
    _gh(
        [
            "gh", "run", "download", str(run_id),
            "--repo", repo, "--name", name, "--dir", target,
        ],
        "download publication receipt {} from worker run {}".format(
            name, run_id
        ),
    )
    try:
        with open(os.path.join(target, "publication.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        raise SystemExit(
            "publication receipt {} from worker run {} is unreadable".format(
                name, run_id
            )
        )


def repair_publication(
    repo: str,
    run_id: int,
    commit: Mapping[str, Any],
    pr_number: object,
    branch: str,
) -> Optional[Mapping[str, Any]]:
    """Return the receipt that attests this status's exact commit.

    A rerun of one repair workflow keeps its run id while it can push another
    commit, so the newest receipt may describe a later commit than the status
    being checked. Read every candidate receipt and let the module select the
    one binding this run, PR, branch, direct parent, and pushed commit. A
    failed read is fatal. An absent match leaves the status unverified instead
    of falling back to URL alone.
    """
    listing = _artifact_listing(repo, run_id)
    candidates = publication_artifacts(
        listing,
        CANARY_REPAIR_PUBLICATION_ARTIFACT_PREFIX,
    )
    if not candidates:
        return None
    parents = commit.get("parents")
    parent = parents[0] if isinstance(parents, list) and parents else {}
    base = (
        str(parent.get("sha") or "").strip()
        if isinstance(parent, dict)
        else ""
    )
    receipts = []
    for artifact_id, name in sorted(candidates, reverse=True):
        # Fetch each receipt by artifact id. Downloading by name can resolve to
        # another attempt's same-named artifact, which would lose the receipt
        # for an earlier repair commit.
        proc = _gh(
            [
                "gh", "api",
                "repos/{}/actions/artifacts/{}/zip".format(repo, artifact_id),
            ],
            "download repair publication receipt {} from run {}".format(
                name, run_id
            ),
            text=False,
        )
        try:
            with zipfile.ZipFile(io.BytesIO(proc.stdout or b"")) as archive:
                with archive.open("publication.json") as handle:
                    receipts.append(json.loads(handle.read().decode("utf-8")))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            raise SystemExit(
                "repair publication receipt {} from run {} cannot be read".format(
                    name, run_id
                )
            )
    return select_repair_publication(
        receipts,
        run_id,
        base,
        str(commit.get("sha") or "").strip(),
        pr_number,
        repo,
        branch,
    )
