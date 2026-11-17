"""The durable receipt: what a release has already done, kept outside the job.

A journal is a value the core can be handed; a receipt is where that value
lives between runs. The distinction matters because the failure this module
exists to prevent is not "the process forgot" — a process always forgets — it
is "two runs disagreed about what the first one did". So a receipt carries the
journal, the manifests, and the one identity that must never move:

    one repository + one version/tag -> one source commit, one asset set

A receipt that would let a resumed run attach different bytes under a name an
earlier run already recorded is refused here, before an upload. A receipt whose
source commit disagrees with the event that found it is refused for the same
reason: a tag names one commit, and resuming must not be able to re-point it.

The store is deliberately boring. A file store writes one JSON document
atomically; a caller can put the same document on a release asset (see
`github_api.ReleaseAssetReceiptStore`) without this module learning about HTTP.
Composition is a list of stores, because a run wants both: the local copy makes
a retry cheap, and the release-attached copy is what survives a different
runner.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contract import ArtifactManifest, ContractError, require_source_sha
from .state import (
    COMPLETED,
    EMPTY_JOURNAL,
    Journal,
    NOOP,
    ReleaseStateError,
    SKIPPED,
    StageOutcome,
)

RECEIPT_SCHEMA = "continuum.release-receipt/v1"


class ReceiptError(ValueError):
    """Raised when a receipt cannot be trusted as the record of a release."""

    def __init__(self, message: str, *, code: str = "receipt-invalid") -> None:
        super().__init__(message)
        self.code = code


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip())
    if not cleaned or cleaned in (".", ".."):
        raise ReceiptError(
            f"{value!r} cannot be part of a receipt name; it has no usable characters",
            code="receipt-name-invalid",
        )
    return cleaned[:120]


def receipt_name(repository: str, version: str) -> str:
    """The file name one release's receipt is stored under."""

    if "/" not in (repository or ""):
        raise ReceiptError(
            f"receipt repository {repository!r} must be an owner/name pair",
            code="receipt-name-invalid",
        )
    return "receipt-{}-{}.json".format(_slug(repository), _slug(version))


@dataclass(frozen=True)
class Receipt:
    """One release's durable record of itself."""

    repository: str
    version: str
    tag: str
    source_sha: str
    channel: str = "default"
    journal: Journal = EMPTY_JOURNAL
    manifests: Tuple[ArtifactManifest, ...] = ()
    release_id: str = ""
    url: str = ""
    published: bool = False

    def __post_init__(self) -> None:
        if "/" not in (self.repository or ""):
            raise ReceiptError(
                f"receipt repository {self.repository!r} must be an owner/name pair",
                code="receipt-name-invalid",
            )
        if not (self.version or "").strip():
            raise ReceiptError(
                "a receipt must name the version it is the record of",
                code="receipt-version-missing",
            )
        if not (self.tag or "").strip():
            raise ReceiptError(
                "a receipt must name the tag it is the record of",
                code="receipt-tag-missing",
            )
        require_source_sha(self.source_sha, f"receipt for {self.repository} {self.version}")
        targets: Dict[str, ArtifactManifest] = {}
        names: Dict[str, str] = {}
        for manifest in self.manifests:
            manifest.assert_release_source(self.source_sha)
            manifest.assert_release_version(self.version)
            seen = targets.get(manifest.target)
            if seen is not None and seen != manifest:
                raise ReceiptError(
                    f"receipt for {self.version} holds two different manifests for "
                    f"target {manifest.target!r}",
                    code="receipt-conflict",
                )
            targets[manifest.target] = manifest
            for artifact in manifest.artifacts:
                owner = names.get(artifact.name)
                if owner is not None and owner != artifact.identity:
                    raise ReceiptError(
                        f"receipt for {self.version} describes artifact "
                        f"{artifact.name!r} twice with different bytes",
                        code="receipt-conflict",
                    )
                names[artifact.name] = artifact.identity

    @property
    def complete(self) -> bool:
        """Whether a destination is holding this release right now."""

        return self.published

    @property
    def asset_identities(self) -> Dict[str, str]:
        """Every recorded asset name and the digest it must keep."""

        return {
            artifact.name: artifact.identity
            for manifest in self.manifests
            for artifact in manifest.artifacts
        }

    def assert_compatible(self, other: "Receipt") -> None:
        """Refuse a second receipt that describes a different release.

        The check is deliberately broader than "same version": a release is the
        tuple (repository, version, tag, source commit). Two receipts that agree
        on the version and disagree on the commit are the contradiction this
        module exists to catch, and they must be caught before the first build.
        """

        if not isinstance(other, Receipt):
            raise ReceiptError("a receipt can only be merged with a receipt")
        for name, mine, theirs in (
            ("repository", self.repository, other.repository),
            ("version", self.version, other.version),
            ("tag", self.tag, other.tag),
            ("source_sha", self.source_sha, other.source_sha),
        ):
            if mine != theirs:
                raise ReceiptError(
                    f"receipt for {self.version} records {name}={mine!r}, but another "
                    f"record says {theirs!r}. One tag is one commit and one asset set; "
                    "refusing to reconcile the two.",
                    code="receipt-conflict",
                )
        if self.channel != other.channel:
            raise ReceiptError(
                f"receipt for {self.version} belongs to channel {self.channel!r}, not "
                f"{other.channel!r}",
                code="receipt-conflict",
            )
        mine = self.asset_identities
        theirs = other.asset_identities
        for name in sorted(set(mine) & set(theirs)):
            if mine[name] != theirs[name]:
                raise ReceiptError(
                    f"receipt for {self.version} records {name!r} as {mine[name]!r}, but "
                    f"another record says {theirs[name]!r}. A published asset's bytes are "
                    "part of the release's identity.",
                    code="asset-set-conflict",
                )
        if (self.published or other.published) and set(mine) != set(theirs):
            if mine and theirs:
                raise ReceiptError(
                    f"a published release for {self.version} has an immutable asset set; "
                    f"{sorted(set(mine) ^ set(theirs))} cannot be added or removed after "
                    "publication",
                    code="asset-set-conflict",
                )

    def merge(self, other: "Receipt") -> "Receipt":
        """This record, plus everything ``other`` has already done."""

        self.assert_compatible(other)
        manifests: List[ArtifactManifest] = list(self.manifests)
        for candidate in other.manifests:
            for index, existing in enumerate(manifests):
                if existing.target == candidate.target:
                    # Compare release content, not the run-local path: the
                    # stored receipt names the published asset while a rebuilt
                    # receipt names the workspace file, and a duplicate event
                    # must reconcile those two as the same release.
                    if not existing.same_release(candidate):
                        raise ReceiptError(
                            f"two runs built different bytes for target "
                            f"{candidate.target!r} at {self.version}",
                            code="asset-set-conflict",
                        )
                    break
            else:
                manifests.append(candidate)
        return Receipt(
            repository=self.repository,
            version=self.version,
            tag=self.tag,
            source_sha=self.source_sha,
            channel=self.channel,
            journal=_merge_journals(self.journal, other.journal),
            manifests=tuple(manifests),
            release_id=self.release_id or other.release_id,
            url=self.url or other.url,
            published=self.published or other.published,
        )

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "repository": self.repository,
            "version": self.version,
            "tag": self.tag,
            "source_sha": self.source_sha,
            "channel": self.channel,
            "published": self.published,
            "journal": self.journal.describe(),
            "manifests": [manifest.describe() for manifest in self.manifests],
        }
        if self.release_id:
            payload["release_id"] = self.release_id
        if self.url:
            payload["url"] = self.url
        return payload

    def to_json(self) -> str:
        return json.dumps(self.describe(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_describe(cls, payload: Mapping[str, Any]) -> "Receipt":
        if not isinstance(payload, Mapping):
            raise ReceiptError(
                "a receipt must be a JSON object", code="malformed-receipt"
            )
        schema = str(payload.get("schema") or "")
        if schema != RECEIPT_SCHEMA:
            raise ReceiptError(
                f"unknown receipt schema {schema!r}; expected {RECEIPT_SCHEMA!r}",
                code="receipt-schema-unknown",
            )
        try:
            journal = Journal.from_describe(payload.get("journal") or {})
            manifests = tuple(
                ArtifactManifest.from_describe(item)
                for item in (payload.get("manifests") or ())
            )
        except (ReleaseStateError, ContractError, TypeError) as exc:
            raise ReceiptError(
                f"the receipt cannot be replayed: {exc}", code="malformed-receipt"
            ) from exc
        try:
            return cls(
                repository=str(payload.get("repository") or ""),
                version=str(payload.get("version") or ""),
                tag=str(payload.get("tag") or ""),
                source_sha=str(payload.get("source_sha") or ""),
                channel=str(payload.get("channel") or "default"),
                journal=journal,
                manifests=manifests,
                release_id=str(payload.get("release_id") or ""),
                url=str(payload.get("url") or ""),
                published=bool(payload.get("published", False)),
            )
        except ReceiptError:
            raise
        except (ContractError, ValueError) as exc:
            raise ReceiptError(
                f"the receipt is not a valid release record: {exc}",
                code="malformed-receipt",
            ) from exc

    @classmethod
    def from_json(cls, document: str) -> "Receipt":
        try:
            payload = json.loads(document)
        except (TypeError, ValueError) as exc:
            raise ReceiptError(
                f"the receipt is not valid JSON: {exc}", code="malformed-receipt"
            ) from exc
        return cls.from_describe(payload)


def _entry_identity(entry: StageOutcome) -> Tuple[str, ...]:
    return (
        entry.stage,
        entry.key,
        entry.outcome,
        entry.code,
        entry.summary,
        tuple(sorted(entry.details)),
    )


def _weaker_outcome(outcome: str) -> bool:
    """Outcomes that mean a later attempt found nothing left to do.

    Completion is monotonic for one idempotency key: once a stage has completed
    for a (repository, version, tag, source) release, a later run of that same
    release recording a no-op or a duplicate is agreeing that no work was left,
    not contradicting the earlier run. The no-op at a terminal destination stage
    means exactly this — "the release already holds it" — so a duplicate event
    must reconcile with the receipt the first run stored instead of being
    refused as a disagreement.
    """

    return outcome in (NOOP, SKIPPED)


def _merge_journals(first: Journal, second: Journal) -> Journal:
    """Both records, in order, with a disagreement refused.

    Entries are deduplicated by their whole value rather than by key alone: the
    same transition recorded twice is the same fact, but two different outcomes
    under one key are two runs that disagree, and a resumed run that silently
    kept the last one would forget that it had ever been told otherwise.

    The one tolerated difference is completion against a later no-op (or a
    duplicate skip). The completed record is the stronger fact and survives;
    the weaker record is not appended, so the merged journal still says the work
    was done. The reverse — a no-op that a later run contradicts by completing —
    replaces the no-op with the completion, because a stage cannot be
    un-completed by an attempt that then does the work.
    """

    merged: List[StageOutcome] = list(first.entries)
    for entry in second.entries:
        identity = _entry_identity(entry)
        if any(_entry_identity(candidate) == identity for candidate in merged):
            continue
        previous = next(
            (
                candidate
                for candidate in reversed(merged)
                if candidate.key == entry.key
                and not Journal.was_just_a_plan(candidate)
                and not Journal.was_just_a_plan(entry)
            ),
            None,
        )
        if previous is not None and previous.outcome != entry.outcome:
            if previous.outcome == COMPLETED and _weaker_outcome(entry.outcome):
                continue
            if entry.outcome == COMPLETED and _weaker_outcome(previous.outcome):
                merged[merged.index(previous)] = entry
                continue
            raise ReceiptError(
                f"two records disagree about {entry.stage} for key {entry.key}: "
                f"{previous.outcome} versus {entry.outcome}",
                code="receipt-conflict",
            )
        merged.append(entry)
    return Journal(entries=tuple(merged))


class FileReceiptStore:
    """A receipt store on disk, written atomically and read strictly."""

    def __init__(self, directory: str) -> None:
        self._directory = directory

    @property
    def directory(self) -> str:
        return self._directory

    def load(self, repository: str, version: str) -> Optional[Receipt]:
        path = os.path.join(self._directory, receipt_name(repository, version))
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                return Receipt.from_json(handle.read())
        except OSError as exc:
            raise ReceiptError(
                f"the receipt at {path} could not be read: {exc}",
                code="receipt-unreadable",
            ) from exc

    def save(self, receipt: Receipt) -> Receipt:
        os.makedirs(self._directory, exist_ok=True)
        existing = self.load(receipt.repository, receipt.version)
        merged = existing.merge(receipt) if existing is not None else receipt
        path = os.path.join(
            self._directory, receipt_name(receipt.repository, receipt.version)
        )
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self._directory,
            prefix=".receipt-",
            suffix=".tmp",
            delete=False,
        )
        temporary = handle.name
        try:
            handle.write(merged.to_json())
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(temporary, path)
        except BaseException:
            handle.close()
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
        return merged


class MemoryReceiptStore:
    """A receipt store that is a dictionary, for tests and dry runs."""

    def __init__(self) -> None:
        self.saved: Dict[str, Receipt] = {}

    def load(self, repository: str, version: str) -> Optional[Receipt]:
        return self.saved.get(receipt_name(repository, version))

    def save(self, receipt: Receipt) -> Receipt:
        key = receipt_name(receipt.repository, receipt.version)
        existing = self.saved.get(key)
        merged = existing.merge(receipt) if existing is not None else receipt
        self.saved[key] = merged
        return merged


class CompositeReceiptStore:
    """Several stores treated as one record.

    Loading merges and saving writes everywhere. The order is the caller's:
    local first makes a retry cheap; the durable copy makes it survive the
    runner. A store that refuses (a conflicting record, an unreachable release)
    fails the read rather than being skipped, because skipping the store that
    disagrees is how a run resumes from the copy that happens to be convenient.
    """

    def __init__(self, stores: Sequence[Any]) -> None:
        self._stores = tuple(stores)
        if not self._stores:
            raise ReceiptError(
                "a composite receipt store needs at least one store",
                code="receipt-store-empty",
            )

    def load(self, repository: str, version: str) -> Optional[Receipt]:
        combined: Optional[Receipt] = None
        for store in self._stores:
            found = store.load(repository, version)
            if found is None:
                continue
            combined = found if combined is None else combined.merge(found)
        return combined

    def save(self, receipt: Receipt) -> Receipt:
        saved = receipt
        for store in self._stores:
            saved = store.save(saved)
        return saved


class TagReceiptStore:
    """Adapt a tag-addressed text store to the receipt interface.

    A release asset is addressed by the release's tag rather than by the pair
    this interface takes, because that is how the destination finds things. The
    tag is fixed for the life of a run: the version stage agrees it before a
    receipt is read, and a release whose tag changes between two runs is the
    source conflict the core already refuses.
    """

    def __init__(self, store: Any, *, tag: str) -> None:
        if not (tag or "").strip():
            raise ReceiptError(
                "a tag-addressed receipt store needs the tag it is the record of",
                code="receipt-tag-missing",
            )
        self._store = store
        self._tag = tag

    def load(self, repository: str, version: str) -> Optional[Receipt]:
        document = self._store.load(self._tag)
        if document is None:
            return None
        receipt = Receipt.from_json(document)
        if receipt.repository != repository or receipt.version != version:
            raise ReceiptError(
                f"the receipt stored on {self._tag} describes "
                f"{receipt.repository} {receipt.version}, not {repository} {version}",
                code="receipt-conflict",
            )
        return receipt

    def save(self, receipt: Receipt) -> Receipt:
        if receipt.tag != self._tag:
            raise ReceiptError(
                f"this store records {self._tag}, not {receipt.tag}",
                code="receipt-conflict",
            )
        self._store.save(receipt.tag, receipt.to_json())
        return receipt


__all__ = [
    "CompositeReceiptStore",
    "FileReceiptStore",
    "MemoryReceiptStore",
    "RECEIPT_SCHEMA",
    "Receipt",
    "ReceiptError",
    "TagReceiptStore",
    "receipt_name",
]
