"""Downstream publication, and the retry policy that makes it resumable.

Homebrew and MacPorts are *downstream* of the GitHub release: they consume its
published digests, and a release is complete without them. They are still part
of the state machine, because a tap that never learns about a release is a
release nobody can install from it — but they are a separate destination with
their own idempotency key, so a tap failure cannot make the GitHub release look
unpublished and cannot cause it to be published twice.

A sync is a repository-owned command. Continuum gives it the release identity,
the manifest digests, and a stable idempotency key, and a sync command that has
already applied that key must say so by exiting successfully. That is what makes
a retry safe: the key is derived from the release, never from a run.

Retries are deliberately narrow. Exit code 75 (temporary failure) and 429 are
retried; everything else is the command saying that the input is wrong. A retry
that re-runs a command which failed because the template had a hole in it does
not fix the hole — it only delays the report.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .contract import PublishRequest, PublisherResult, failed, published, skipped
from .state import EMPTY_JOURNAL

DRY_RUN_CODE = "dry-run"
DEFAULT_ATTEMPTS = 3


class SyncError(RuntimeError):
    """A downstream command failed, classified only as far as it can be."""

    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _redact(text: str, secrets: Sequence[str]) -> str:
    cleaned = text
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in cleaned:
            cleaned = cleaned.replace(secret, "<redacted>")
    return cleaned


@dataclass(frozen=True)
class SyncResult:
    """The outcome of one command attempt, before it becomes a publication."""

    returncode: int
    output: str


class CommandSync:
    """One downstream destination, driven by a repository-owned command."""

    SUPPORTS_DRY_RUN = True

    def __init__(
        self,
        destination: str,
        command: Sequence[str],
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        retry_on_exit: Sequence[int] = (75, 429),
        workdir: str = "",
        environment: Optional[Mapping[str, str]] = None,
        secrets: Sequence[str] = (),
        timeout: int = 3600,
        runner: Optional[Callable[..., Any]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        if not destination or not str(destination).strip():
            raise SyncError(
                "a downstream sync must name its destination",
                code="sync-destination-missing",
            )
        if not command:
            raise SyncError(
                f"the {destination} sync needs a command", code="sync-command-missing"
            )
        if not 1 <= attempts <= 10:
            raise SyncError(
                f"the {destination} sync has {attempts} attempts; use 1 to 10",
                code="sync-attempts-invalid",
            )
        self.name = str(destination)
        self._destination = str(destination)
        self._argv = tuple(str(token) for token in command)
        self._attempts = attempts
        self._retry_on_exit = tuple(retry_on_exit)
        self._workdir = workdir
        self._environment = dict(environment or {})
        self._secrets = tuple(value for value in secrets if value)
        self._timeout = timeout
        self._runner = runner or subprocess.run
        self._sleep = sleep or _no_sleep

    @property
    def destination(self) -> str:
        return self._destination

    def intent(self) -> str:
        return (
            f"record the published release in {self._destination}, retrying only a "
            "failure the command classified as temporary"
        )

    def publish(self, request: PublishRequest) -> PublisherResult:
        if request.dry_run:
            return skipped(
                self._destination,
                f"would record {request.tag} in {self._destination}",
                code=DRY_RUN_CODE,
                identity=f"{self._destination}:{request.tag}",
            )
        environment = dict(self._environment)
        environment.update(
            {
                "CONTINUUM_RELEASE_VERSION": request.version,
                "CONTINUUM_RELEASE_TAG": request.tag,
                "CONTINUUM_SOURCE_SHA": request.source_sha,
                "CONTINUUM_DESTINATION": self._destination,
                "CONTINUUM_SYNC_KEY": request.key,
                "CONTINUUM_ARTIFACTS": "\n".join(
                    f"{artifact.name} {artifact.digest_algorithm}:{artifact.digest} "
                    f"{artifact.size}"
                    for artifact in request.artifacts
                ),
            }
        )
        for attempt in range(1, self._attempts + 1):
            try:
                completed = self._runner(
                    list(self._argv),
                    capture_output=True,
                    text=True,
                    shell=False,
                    cwd=self._workdir or None,
                    env=environment,
                    timeout=self._timeout,
                    check=False,
                )
            except FileNotFoundError as exc:
                return failed(
                    self._destination,
                    f"the {self._destination} sync could not start "
                    f"{self._argv[0]!r}: {exc}",
                    code="sync-tool-missing",
                    retryable=False,
                )
            except subprocess.TimeoutExpired:
                if attempt < self._attempts:
                    self._sleep(float(attempt))
                    continue
                return failed(
                    self._destination,
                    f"the {self._destination} sync did not finish in time after "
                    f"{attempt} attempt(s)",
                    code="sync-timeout",
                    retryable=True,
                )
            except OSError as exc:
                return failed(
                    self._destination,
                    f"the {self._destination} sync could not start: {exc}",
                    code="sync-not-started",
                    retryable=False,
                )
            output = _redact(
                str(getattr(completed, "stdout", "") or "")
                + str(getattr(completed, "stderr", "") or ""),
                self._secrets,
            )
            code = int(getattr(completed, "returncode", 1))
            if code == 0:
                return published(
                    self._destination,
                    f"{self._destination}:{request.tag}",
                    external_version=request.tag,
                    details={
                        "attempts": str(attempt),
                        "key": request.key,
                    },
                )
            retryable = code in self._retry_on_exit
            if retryable and attempt < self._attempts:
                self._sleep(float(attempt))
                continue
            return failed(
                self._destination,
                f"the {self._destination} sync exited with status {code}"
                + (f": {output.strip()[:1000]}" if output.strip() else ""),
                code="sync-failed",
                retryable=retryable,
                details={"attempts": str(attempt)},
            )
        # Unreachable: the loop returns on every path. Kept so a future edit that
        # changes the loop cannot fall off the end into a silent success.
        return failed(
            self._destination,
            f"the {self._destination} sync did not run",
            code="sync-not-run",
            retryable=False,
        )


def _no_sleep(seconds: float) -> None:  # pragma: no cover - timing is not under test
    import time

    time.sleep(seconds)


def destination_names(syncs: Sequence[Any]) -> Tuple[str, ...]:
    """Every destination name, refusing a duplicate.

    Two components with one destination name would race for the same downstream
    record, and the core would record both under keys that differ only by
    registration order. The refusal belongs here, before the first sync.
    """

    names: list[str] = []
    for sync in syncs:
        name = str(getattr(sync, "name", "") or "")
        if not name:
            raise SyncError(
                "a downstream sync does not name its destination",
                code="sync-destination-missing",
            )
        if name in names:
            raise SyncError(
                f"two downstream components are registered as {name!r}; one "
                "destination must have one writer",
                code="sync-destination-duplicate",
            )
        names.append(name)
    return tuple(names)


__all__ = [
    "CommandSync",
    "DEFAULT_ATTEMPTS",
    "DRY_RUN_CODE",
    "SyncError",
    "SyncResult",
    "destination_names",
]
