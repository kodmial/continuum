"""Secure continuation of an unattested legacy OpenCode checkpoint.

An interruption checkpoint can contain a real, unfinished agent tree while its
``.continuum-verification.json`` was written before the checkout advanced.  The
verification engine must never trust that writable evidence on resume: it was
not attested by the runner-held key, and the coding agent could have rewritten
it.  Simply discarding the checkpoint is not acceptable either, because the
saved tree is the user's work.

This module owns the pure identity/evidence checks for that continuation.  It
does not execute the checkpoint's commands and it does not mutate Git state.
A caller validates the exact checkpoint first, then asks
:mod:`continuum.verification` to establish a fresh baseline in an isolated
worktree at the original task base.

The persisted failure identity used by the scheduler/watchdog is deliberately
small and stable so a deterministic resume rejection is not redispatched
forever with identical inputs.  A new checkpoint SHA, task base, or pinned task
snapshot is a material premise change and allows a new attempt.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

METADATA_FILE_NAME = ".continuum-429-recovery.json"
EVIDENCE_FILE_NAME = ".continuum-verification.json"

#: Durable marker posted by the workflow when a resume identity or isolated
#: baseline cannot be proven.  The scheduler/watchdog hold the same
#: checkpoint/base/snapshot until a material premise changes.
FAILURE_MARKER_PREFIX = "<!-- continuum-checkpoint-resume-blocked"

_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_LEGACY_CHECKPOINT_RE = re.compile(r"\Aopencode/checkpoint-issue([1-9][0-9]*)\Z")
_429_CHECKPOINT_RE = re.compile(
    r"\Aopencode/(?:429|infra)-checkpoint-(.+)-gen-(\d+)\Z"
)
_REPOSITORY_RE = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_OPERATION_RE = re.compile(r"\Aopencode-[A-Za-z0-9-]+(?:-[A-Za-z0-9-]+)*\Z")

_FAILURE_MARKER_RE = re.compile(
    r"<!--\s*continuum-checkpoint-resume-blocked\s+"
    r"repo=(\S+)\s+issue=(\d+)\s+generation=(\d+)\s+"
    r"operation=(\S+)\s+checkpoint=(\S+)\s+"
    r"checkpoint_sha=([0-9a-f]{40})\s+base_ref=(\S+)\s+"
    r"base_sha=([0-9a-f]{40})\s+snapshot=([0-9a-f]{64})\s+"
    r"reason=(\S+)\s*-->"
)


class ResumeError(RuntimeError):
    """A typed, deterministic checkpoint-resume rejection."""

    def __init__(self, reason: str, detail: str, record: Optional[Mapping[str, Any]] = None):
        self.reason = str(reason or "resume-rejected")
        self.detail = str(detail or self.reason)
        self.record = dict(record or {})
        self.record.setdefault("reason", self.reason)
        self.record.setdefault("detail", self.detail)
        super().__init__("resume-{}: {}".format(self.reason, self.detail))


#: Reasons caused by the runner/Git environment rather than the pinned
#: checkpoint identity or contract. They stay on the ordinary retry path so a
#: transient failure never becomes a durable, automatic-dispatch hold.
TRANSIENT_REASONS = frozenset({"git-unavailable", "worktree-unavailable"})


def should_hold(error: ResumeError) -> bool:
    """Whether this rejection may persist a deterministic failure marker."""

    return error.reason not in TRANSIENT_REASONS


@dataclass(frozen=True)
class ResumeRequest:
    """The identity the current run expects before touching the checkpoint."""

    repository: str
    issue: int
    generation: int
    checkpoint_ref: str
    checkpoint_sha: str
    base_sha: str
    snapshot_spec: str
    operation: str = ""
    mode: str = "issue"

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ResumeRequest":
        def text(key: str) -> str:
            return str(values.get(key) or "").strip()

        def integer(key: str, default: int = 0) -> int:
            raw = text(key)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ResumeError(
                    "invalid-request",
                    "resume {} must be an integer".format(key),
                ) from exc

        return cls(
            repository=text("repository"),
            issue=integer("issue"),
            generation=integer("generation"),
            checkpoint_ref=text("checkpoint_ref"),
            checkpoint_sha=text("checkpoint_sha").lower(),
            base_sha=text("base_sha").lower(),
            snapshot_spec=text("snapshot_spec").lower(),
            operation=text("operation"),
            mode=text("mode") or "issue",
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "repository": self.repository,
            "issue": self.issue,
            "generation": self.generation,
            "operation": self.operation,
            "checkpoint_ref": self.checkpoint_ref,
            "checkpoint_sha": self.checkpoint_sha,
            "base_sha": self.base_sha,
            "snapshot_spec": self.snapshot_spec,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class ValidatedCheckpoint:
    """A checkpoint whose identity, ancestry and saved evidence were checked."""

    request: ResumeRequest
    metadata: Mapping[str, Any]
    metadata_sha256: str
    evidence: Optional[Mapping[str, Any]]
    evidence_sha256: str
    base_sha: str
    base_ref: str
    operation: str
    snapshot_bound: bool
    ignored_paths: Tuple[str, ...] = field(default=())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request": self.request.as_dict(),
            "checkpoint_ref": self.request.checkpoint_ref,
            "checkpoint_sha": self.request.checkpoint_sha,
            "metadata_file": METADATA_FILE_NAME,
            "metadata_sha256": self.metadata_sha256,
            "evidence_file": EVIDENCE_FILE_NAME if self.evidence is not None else "",
            "evidence_sha256": self.evidence_sha256,
            "base_sha": self.base_sha,
            "base_ref": self.base_ref,
            "operation": self.operation,
            "snapshot_spec": self.request.snapshot_spec,
            "snapshot_bound_to_checkpoint": self.snapshot_bound,
            "legacy_evidence_untrusted": self.evidence is not None,
        }


def _completed(repo: Path | str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
        check=False,
    )


def _git_text(repo: Path | str, *args: str) -> str:
    completed = _completed(repo, *args)
    if completed.returncode != 0:
        raise ResumeError(
            "git-unavailable",
            "git {} failed: {}".format(
                " ".join(args), (completed.stderr or "").strip()[-1000:]
            ),
        )
    return (completed.stdout or "").strip()


def _show_file(repo: Path | str, sha: str, name: str) -> Optional[str]:
    completed = _completed(repo, "show", "{}:{}".format(sha, name))
    if completed.returncode != 0:
        return None
    return completed.stdout


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_workflow_owned(path: str, ignored_paths: Sequence[str]) -> bool:
    return path in ignored_paths or any(
        path == owned or path.startswith(owned + "/")
        for owned in ignored_paths
    )


def dirty_worktree(
    repo: Path | str, ignored_paths: Iterable[str] = ()
) -> str:
    """Return the first non-workflow-owned dirty path, or ``""``.

    A checkpoint is an exact saved commit.  Uncommitted changes cannot be
    proven to belong to that same generation, so continuation fails closed and
    leaves every file in place.  Workflow-owned recovery metadata is ignored:
    it is not product code and is re-created by the workflow.
    """

    completed = _completed(
        repo, "status", "--porcelain=v1", "--untracked-files=normal"
    )
    if completed.returncode != 0:
        raise ResumeError("worktree-unavailable", "could not inspect the checkpoint worktree")
    ignored = tuple(ignored_paths)
    for line in (completed.stdout or "").splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().split(" -> ")[-1]
        if not _is_workflow_owned(path, ignored):
            return path
    return ""


def _validate_request(repo: Path | str, request: ResumeRequest) -> None:
    if not _REPOSITORY_RE.fullmatch(request.repository or ""):
        raise ResumeError("invalid-request", "resume repository is malformed")
    if request.issue < 1:
        raise ResumeError("invalid-request", "resume issue number is malformed")
    if request.generation < 1:
        raise ResumeError("invalid-request", "resume generation is malformed")
    if not request.checkpoint_ref:
        raise ResumeError("invalid-request", "resume checkpoint ref is empty")
    if not _SHA_RE.fullmatch(request.checkpoint_sha or ""):
        raise ResumeError("invalid-request", "resume checkpoint SHA is malformed")
    if not _SHA_RE.fullmatch(request.base_sha or ""):
        raise ResumeError("invalid-request", "resume base SHA is malformed")
    if not _DIGEST_RE.fullmatch(request.snapshot_spec or ""):
        raise ResumeError("invalid-request", "resume snapshot digest is malformed")
    if request.mode != "issue":
        raise ResumeError(
            "unsupported-mode",
            "secure legacy checkpoint continuation is only defined for issue mode",
        )
    if request.operation and not _OPERATION_RE.fullmatch(request.operation):
        raise ResumeError("invalid-request", "resume operation identity is malformed")

    resolved = _completed(
        repo, "rev-parse", "--verify", request.checkpoint_sha + "^{commit}"
    )
    if resolved.returncode != 0:
        raise ResumeError(
            "wrong-sha", "the pinned checkpoint commit cannot be resolved"
        )
    actual = (resolved.stdout or "").strip().lower()
    if actual != request.checkpoint_sha:
        raise ResumeError("wrong-sha", "the pinned checkpoint commit cannot be resolved")
    head = _git_text(repo, "rev-parse", "HEAD").lower()
    if head != request.checkpoint_sha:
        raise ResumeError(
            "checkpoint-mismatch",
            "the worktree HEAD {} is not the pinned checkpoint {}".format(
                head, request.checkpoint_sha
            ),
        )


def _check_repository(
    repo: Path | str, request: ResumeRequest, metadata: Mapping[str, Any]
) -> None:
    recorded = str(
        metadata.get("repository") or metadata.get("repo") or ""
    ).strip()
    if recorded and recorded.lower() != request.repository.lower():
        raise ResumeError(
            "wrong-repository",
            "the checkpoint repository does not match this task",
        )
    if recorded:
        return
    # Legacy metadata has no repository field.  When the checkout has an
    # origin, use it as an additional binding; a bare test repository has no
    # remote and is bound by issue/generation/operation below instead.
    completed = _completed(repo, "remote", "get-url", "origin")
    if completed.returncode != 0:
        return
    origin = (completed.stdout or "").strip().lower().rstrip("/")
    if origin.endswith(".git"):
        origin = origin[:-4]
    wanted = request.repository.lower()
    # Bind the exact owner/repository slug at the end of the remote path.
    # A plain substring test would accept ``evil/owner/repo-extra``.
    if not (origin.endswith("/" + wanted) or origin.endswith(":" + wanted)):
        raise ResumeError(
            "wrong-repository",
            "the checkpoint's origin is not the expected repository",
        )


def _check_checkpoint_ref(
    request: ResumeRequest, metadata_checkpoint: str, operation: str
) -> None:
    """Validate that the pinned ref names the same durable checkpoint.

    The durable 429 ref is the authoritative shape. Legacy interruption
    checkpoints may record the historical ``opencode/checkpoint-issueN`` ref
    directly, so that shape is accepted for issue generation 1 as well. Any
    other cross-ref combination is drift and fails closed.
    """

    expected = request.checkpoint_ref
    parsed = _429_CHECKPOINT_RE.match(metadata_checkpoint or "")
    if parsed is not None:
        recorded_operation, recorded_generation = parsed.group(1), int(
            parsed.group(2)
        )
        if operation and recorded_operation != operation:
            raise ResumeError(
                "wrong-operation",
                "checkpoint recovery metadata belongs to a different operation",
            )
        if recorded_generation != request.generation:
            raise ResumeError(
                "wrong-generation",
                "checkpoint recovery metadata belongs to a different generation",
            )
        if request.operation and recorded_operation != request.operation:
            raise ResumeError(
                "wrong-operation",
                "checkpoint recovery metadata belongs to a different operation",
            )
    else:
        legacy_metadata = _LEGACY_CHECKPOINT_RE.match(metadata_checkpoint or "")
        if legacy_metadata is None:
            raise ResumeError(
                "metadata-checkpoint-ref",
                "checkpoint recovery metadata has no valid durable checkpoint ref",
            )
        if int(legacy_metadata.group(1)) != request.issue or request.generation != 1:
            raise ResumeError(
                "wrong-ref",
                "the checkpoint metadata ref belongs to a different issue or generation",
            )

    if expected == metadata_checkpoint:
        return
    legacy_expected = _LEGACY_CHECKPOINT_RE.match(expected)
    if legacy_expected is None or int(legacy_expected.group(1)) != request.issue:
        raise ResumeError(
            "wrong-ref",
            "the resumed ref is neither the durable checkpoint nor the issue's legacy checkpoint",
        )
    if request.generation != 1:
        raise ResumeError(
            "wrong-generation",
            "the legacy issue checkpoint only represents generation 1",
        )


def _check_snapshot(
    request: ResumeRequest, metadata: Mapping[str, Any]
) -> bool:
    for key in ("snapshot_spec", "snapshot_sha256", "task_snapshot_sha256"):
        if key not in metadata or metadata.get(key) in (None, ""):
            continue
        recorded = str(metadata.get(key) or "").strip().lower()
        if recorded != request.snapshot_spec:
            raise ResumeError(
                "snapshot-drift",
                "the checkpoint was created for a different pinned task snapshot",
            )
        return True
    return False


def _metadata_record(
    request: ResumeRequest, *, base_ref: str = "", base_sha: str = "",
    operation: str = "", reason: str = "",
) -> Dict[str, Any]:
    return {
        "repository": request.repository,
        "issue": request.issue,
        "generation": request.generation,
        "operation": operation or request.operation or "unknown",
        "checkpoint_ref": request.checkpoint_ref,
        "checkpoint_sha": request.checkpoint_sha,
        "base_ref": base_ref or "main",
        "base_sha": base_sha or request.base_sha,
        "snapshot_spec": request.snapshot_spec,
        "reason": reason or "resume-rejected",
    }


def _validate_metadata(
    repo: Path | str, request: ResumeRequest, metadata: Mapping[str, Any]
) -> Tuple[str, str, str]:
    if not isinstance(metadata, Mapping):
        raise ResumeError(
            "metadata-malformed",
            "checkpoint recovery metadata is not an object",
            _metadata_record(request, reason="metadata-malformed"),
        )
    try:
        version = int(metadata.get("schema_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != 1:
        raise ResumeError(
            "metadata-schema",
            "checkpoint recovery metadata schema is unsupported",
            _metadata_record(request, reason="metadata-schema"),
        )

    mode = str(metadata.get("mode") or "").strip().lower()
    if mode and mode != "issue":
        raise ResumeError(
            "wrong-mode",
            "checkpoint recovery metadata is not for issue mode",
            _metadata_record(request, reason="wrong-mode"),
        )
    issue_text = str(metadata.get("issue_number") or "").strip()
    if not issue_text.isdigit() or int(issue_text) != request.issue:
        raise ResumeError(
            "wrong-issue",
            "checkpoint recovery metadata belongs to a different issue",
            _metadata_record(request, reason="wrong-issue"),
        )
    try:
        generation = int(metadata.get("generation"))
    except (TypeError, ValueError) as exc:
        raise ResumeError(
            "metadata-malformed",
            "checkpoint recovery metadata generation is malformed",
            _metadata_record(request, reason="metadata-malformed"),
        ) from exc
    if generation != request.generation:
        raise ResumeError(
            "wrong-generation",
            "checkpoint recovery metadata belongs to a different generation",
            _metadata_record(request, reason="wrong-generation"),
        )

    operation = str(metadata.get("operation_id") or "").strip()
    if not operation or not _OPERATION_RE.fullmatch(operation):
        raise ResumeError(
            "metadata-malformed",
            "checkpoint recovery metadata operation is malformed",
            _metadata_record(request, reason="metadata-malformed"),
        )
    if request.operation and operation != request.operation:
        raise ResumeError(
            "wrong-operation",
            "checkpoint recovery metadata belongs to a different operation",
            _metadata_record(request, operation=operation, reason="wrong-operation"),
        )
    if "issue{}".format(request.issue) not in operation.split("-"):
        raise ResumeError(
            "wrong-operation",
            "checkpoint operation is not scoped to this issue",
            _metadata_record(request, operation=operation, reason="wrong-operation"),
        )

    base_ref = str(metadata.get("base_ref") or "").strip() or "main"
    base_sha = str(metadata.get("base_sha") or "").strip().lower()
    if not _SHA_RE.fullmatch(base_sha):
        raise ResumeError(
            "base-malformed",
            "checkpoint recovery metadata has no valid original base SHA",
            _metadata_record(request, operation=operation, base_ref=base_ref, reason="base-malformed"),
        )
    head_sha = str(metadata.get("head_sha") or "").strip().lower()
    if not _SHA_RE.fullmatch(head_sha):
        raise ResumeError(
            "metadata-malformed",
            "checkpoint recovery metadata has no valid code head SHA",
            _metadata_record(request, operation=operation, base_ref=base_ref, base_sha=base_sha, reason="metadata-malformed"),
        )
    if not str(metadata.get("branch") or "").strip():
        raise ResumeError(
            "metadata-malformed",
            "checkpoint recovery metadata has no code branch",
            _metadata_record(request, operation=operation, base_ref=base_ref, base_sha=base_sha, reason="metadata-malformed"),
        )
    if not str(metadata.get("head_ref") or "").strip():
        raise ResumeError(
            "metadata-malformed",
            "checkpoint recovery metadata has no code head ref",
            _metadata_record(request, operation=operation, base_ref=base_ref, base_sha=base_sha, reason="metadata-malformed"),
        )

    metadata_checkpoint = str(metadata.get("checkpoint_ref") or "").strip()
    _check_checkpoint_ref(request, metadata_checkpoint, operation)

    # The original base must be an ancestor of the saved checkpoint.  A
    # current base that has advanced since the checkpoint is allowed, but the
    # baseline still runs against the original immutable base.
    if _completed(repo, "merge-base", "--is-ancestor", base_sha, request.checkpoint_sha).returncode != 0:
        raise ResumeError(
            "base-not-ancestor",
            "the checkpoint's original base is not an ancestor of the saved tree",
            _metadata_record(request, operation=operation, base_ref=base_ref, base_sha=base_sha, reason="base-not-ancestor"),
        )
    if _completed(repo, "merge-base", "--is-ancestor", head_sha, request.checkpoint_sha).returncode != 0:
        raise ResumeError(
            "metadata-ancestry",
            "the checkpoint's recorded code head is outside the saved tree",
            _metadata_record(request, operation=operation, base_ref=base_ref, base_sha=base_sha, reason="metadata-ancestry"),
        )

    current_base = request.base_sha
    if current_base != base_sha and _completed(
        repo, "merge-base", "--is-ancestor", base_sha, current_base
    ).returncode != 0:
        raise ResumeError(
            "wrong-base",
            "the expected task base is not the checkpoint base or a descendant of it",
            _metadata_record(request, operation=operation, base_ref=base_ref, base_sha=base_sha, reason="wrong-base"),
        )
    return operation, base_ref, base_sha


def _load_evidence(sha: str, repo: Path | str) -> Tuple[Optional[Mapping[str, Any]], str]:
    raw = _show_file(repo, sha, EVIDENCE_FILE_NAME)
    if raw is None:
        return None, ""
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - fail closed on any malformed JSON
        raise ResumeError(
            "evidence-malformed",
            "checkpoint verification evidence is not readable JSON: {}".format(exc),
        ) from exc
    if not isinstance(data, Mapping):
        raise ResumeError(
            "evidence-malformed",
            "checkpoint verification evidence is not a JSON object",
        )
    try:
        version = int(data.get("schema_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != 1:
        raise ResumeError(
            "evidence-malformed",
            "checkpoint verification evidence schema is unsupported",
        )
    if not isinstance(data.get("baseline"), Mapping):
        raise ResumeError(
            "evidence-malformed",
            "checkpoint verification evidence has no pre-change baseline",
        )
    return data, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_checkpoint(
    repo: Path | str,
    request: ResumeRequest,
    ignored_paths: Iterable[str] = (),
) -> ValidatedCheckpoint:
    """Validate a pinned checkpoint and return its immutable saved evidence.

    The evidence is returned only for provenance comparison.  It is never
    trusted as a verification result: the caller must establish a fresh
    baseline at the original base before executing checkpoint work.
    """

    ignored = tuple(ignored_paths)
    try:
        return _validate_checkpoint(repo, request, ignored)
    except ResumeError as exc:
        # Every rejection must still carry the exact identity so the
        # workflow can persist a durable, comparable failure marker.
        record = _metadata_record(
            request, operation=request.operation, reason=exc.reason
        )
        record.update(
            {
                key: value
                for key, value in exc.record.items()
                if value not in (None, "")
            }
        )
        record["reason"] = exc.reason
        record["detail"] = exc.detail
        exc.record = record
        raise


def _validate_checkpoint(
    repo: Path | str, request: ResumeRequest, ignored: Tuple[str, ...]
) -> ValidatedCheckpoint:
    _validate_request(repo, request)

    dirty = dirty_worktree(repo, ignored)
    if dirty:
        raise ResumeError(
            "dirty-worktree",
            "the checkpoint worktree has uncommitted task changes at {}".format(dirty),
            _metadata_record(request, reason="dirty-worktree"),
        )

    raw_metadata = _show_file(repo, request.checkpoint_sha, METADATA_FILE_NAME)
    if raw_metadata is None:
        raise ResumeError(
            "metadata-missing",
            "the legacy checkpoint has no immutable recovery metadata",
            _metadata_record(request, reason="metadata-missing"),
        )
    try:
        metadata = json.loads(raw_metadata)
    except Exception as exc:  # noqa: BLE001 - fail closed on any malformed JSON
        raise ResumeError(
            "metadata-malformed",
            "checkpoint recovery metadata is not readable JSON: {}".format(exc),
            _metadata_record(request, reason="metadata-malformed"),
        ) from exc

    _check_repository(repo, request, metadata)
    operation, base_ref, base_sha = _validate_metadata(repo, request, metadata)
    snapshot_bound = _check_snapshot(request, metadata)

    evidence, evidence_sha = _load_evidence(request.checkpoint_sha, repo)
    if evidence is not None:
        baseline = evidence.get("baseline")
        recorded_base = str(
            (baseline or {}).get("base_sha") or ""
        ).strip().lower()
        if recorded_base != base_sha:
            raise ResumeError(
                "evidence-base-mismatch",
                "the saved verification evidence belongs to a different base",
                _metadata_record(
                    request, operation=operation, base_ref=base_ref,
                    base_sha=base_sha, reason="evidence-base-mismatch",
                ),
            )

    return ValidatedCheckpoint(
        request=request,
        metadata=metadata,
        metadata_sha256=hashlib.sha256(raw_metadata.encode("utf-8")).hexdigest(),
        evidence=evidence,
        evidence_sha256=evidence_sha,
        base_sha=base_sha,
        base_ref=base_ref,
        operation=operation,
        snapshot_bound=snapshot_bound,
        ignored_paths=ignored,
    )


def evidence_contract_fingerprints(
    evidence: Optional[Mapping[str, Any]],
) -> Tuple[str, ...]:
    """Fingerprints claimed by writable checkpoint evidence.

    These values are comparison inputs only.  A mismatch against the contract
    resolved from the original base is a fail-closed signal; a match does not
    make the old evidence trusted.
    """

    if not isinstance(evidence, Mapping):
        return ()
    found = []

    def add(section: Any) -> None:
        if isinstance(section, Mapping):
            fingerprint = str(section.get("fingerprint") or "").strip()
            if fingerprint:
                found.append(fingerprint)

    add(evidence.get("contract"))
    baseline = evidence.get("baseline")
    if isinstance(baseline, Mapping):
        add(baseline.get("contract"))
    return tuple(dict.fromkeys(found))


def failure_marker(record: Mapping[str, Any]) -> str:
    """Render the durable deterministic resume failure comment/marker."""

    def value(key: str, default: str = "-") -> str:
        text = str(record.get(key) or "").strip()
        return re.sub(r"\s+", "-", text) or default

    body = [
        "{} repo={} issue={} generation={} operation={} checkpoint={} "
        "checkpoint_sha={} base_ref={} base_sha={} snapshot={} reason={} -->".format(
            FAILURE_MARKER_PREFIX,
            value("repository"),
            value("issue"),
            value("generation"),
            value("operation", "unknown"),
            value("checkpoint_ref"),
            value("checkpoint_sha"),
            value("base_ref", "main"),
            value("base_sha"),
            value("snapshot_spec"),
            value("reason"),
        ),
        "⚡ **Continuum · opencode**",
        "<!-- continuum-origin role=continuum component=opencode -->",
        "",
        "Deterministic checkpoint continuation rejected ({}).".format(
            value("reason")
        ),
        "{}".format(value("detail", "the pinned checkpoint could not be proven")),
        "",
        "The saved checkpoint `{}` at `{}` is preserved; no code was discarded.".format(
            value("checkpoint_ref"), value("checkpoint_sha")
        ),
        "No PR was published, no verification result was invented, and the "
        "checkpoint was not reset or force-pushed.",
        "",
        "Automatic dispatch holds while this exact checkpoint/base/snapshot "
        "identity is unchanged. A new checkpoint SHA, task base, or pinned "
        "task snapshot is a material premise change; an operator resume or a "
        "new generation can also supersede this hold.",
    ]
    return "\n".join(body)


def failure_record(
    request: ResumeRequest, error: ResumeError
) -> Dict[str, Any]:
    """Build the durable identity record for one deterministic rejection."""

    record = _metadata_record(
        request, operation=request.operation, reason=error.reason
    )
    record.update(
        {
            key: value
            for key, value in error.record.items()
            if value not in (None, "")
        }
    )
    record["reason"] = error.reason
    record["detail"] = error.detail
    record["marker"] = failure_marker(record)
    record["marker_line"] = record["marker"].splitlines()[0]
    return record


def parse_failure_markers(body: Any) -> list[Dict[str, Any]]:
    """Parse durable failure markers from one comment body."""

    if not isinstance(body, str):
        return []
    found = []
    for match in _FAILURE_MARKER_RE.finditer(body):
        found.append(
            {
                "repository": match.group(1),
                "issue": int(match.group(2)),
                "generation": int(match.group(3)),
                "operation": match.group(4),
                "checkpoint_ref": match.group(5),
                "checkpoint_sha": match.group(6).lower(),
                "base_ref": match.group(7),
                "base_sha": match.group(8).lower(),
                "snapshot_spec": match.group(9).lower(),
                "reason": match.group(10),
            }
        )
    return found


def premise_changed(
    marker: Mapping[str, Any],
    *,
    snapshot_spec: str = "",
    checkpoint_sha: str = "",
    base_sha: str = "",
) -> bool:
    """Whether observed material state can reconsider a persisted hold."""

    observed = {
        "snapshot_spec": str(snapshot_spec or "").strip().lower(),
        "checkpoint_sha": str(checkpoint_sha or "").strip().lower(),
        "base_sha": str(base_sha or "").strip().lower(),
    }
    for key, value in observed.items():
        if value and value != str(marker.get(key) or "").strip().lower():
            return True
    return False


__all__ = [
    "EVIDENCE_FILE_NAME",
    "FAILURE_MARKER_PREFIX",
    "METADATA_FILE_NAME",
    "ResumeError",
    "ResumeRequest",
    "ValidatedCheckpoint",
    "dirty_worktree",
    "evidence_contract_fingerprints",
    "failure_marker",
    "failure_record",
    "parse_failure_markers",
    "premise_changed",
    "should_hold",
    "validate_checkpoint",
]
