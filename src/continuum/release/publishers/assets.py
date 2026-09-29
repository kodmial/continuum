"""Obtaining a published asset and proving it is the one the manifest describes.

A publisher writes a checksum into a file that other people's machines will
enforce. Writing the digest it was handed without ever seeing the bytes means
the only place that check is performed is the user's install, which is the worst
possible place: it looks like a broken release, it is reported as somebody
else's bug, and nothing in CI ever said otherwise.

So fetching is a capability, and the publisher depends on the capability rather
than on a network client. That is what makes the digest check testable without
a release, and it is why the outcome of a mismatch is a *failed publish* rather
than a warning:

* the bytes are hashed and compared to the digest the release recorded;
* a mismatch reports `digest-mismatch`, which is **not** retryable, because the
  asset at an immutable URL does not change between two attempts;
* an asset that could not be obtained at all reports `asset-unreachable`, which
  **is** retryable, because a transient failure is exactly what a second
  attempt is for.

The two are kept apart on purpose. Conflating them is how a release ends up
retrying a genuinely wrong artifact until the retry budget is gone, and then
reporting the timeout rather than the mismatch.
"""

from __future__ import annotations

import hashlib
from typing import Dict, Iterable, Mapping, Optional, Protocol

from .contract import (
    ASSET_UNREACHABLE,
    DIGEST_MISMATCH,
    PublisherError,
    PublishedArtifact,
)

# A release asset is a tarball, not a memory-resident object. The bound keeps a
# malicious or misconfigured URL from turning a publish into an out-of-memory
# failure, and it is generous enough for a large application bundle.
MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024

DIGEST_ALGORITHM = "sha256"
_CHUNK = 1024 * 1024


class AssetFetcher(Protocol):
    """Something that can return the bytes behind an asset URL."""

    def fetch(self, url: str) -> bytes:  # pragma: no cover - protocol
        ...


class InMemoryAssetFetcher:
    """A fetcher over bytes the caller already has.

    This is the shape a test uses and the shape a job uses when it has already
    downloaded the release to verify it: the publisher never needs to know which
    of the two it is holding, only that what comes back is what the manifest
    claims.
    """

    def __init__(self, assets: Optional[Mapping[str, bytes]] = None) -> None:
        self._assets: Dict[str, bytes] = dict(assets or {})

    def add(self, url: str, data: bytes) -> None:
        self._assets[url] = data

    def fetch(self, url: str) -> bytes:
        try:
            return self._assets[url]
        except KeyError:
            raise PublisherError(
                ASSET_UNREACHABLE,
                f"the asset at {url!r} could not be obtained",
                remediation=(
                    "the release must still be reachable at its immutable URL when the "
                    "publisher runs; a 404 here means the tag was moved or the release "
                    "was deleted after it was cut"
                ),
            ) from None


class HttpAssetFetcher:
    """Fetch over HTTPS with the standard library.

    Redirects are followed because a release download location commonly redirects
    to a CDN object store, and the redirect target is still the same immutable
    file. The scheme is re-checked after each hop: a redirect to `file://` or to
    a plain-HTTP host would turn a published URL into a way to read something
    else entirely.
    """

    def __init__(self, *, timeout: float = 60.0, max_bytes: int = MAX_ASSET_BYTES) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes

    def fetch(self, url: str) -> bytes:  # pragma: no cover - network
        import urllib.error
        import urllib.request

        if not url.startswith("https://"):
            raise PublisherError(
                ASSET_UNREACHABLE,
                f"refusing to fetch a non-https asset URL: {url!r}",
            )
        request = urllib.request.Request(url, headers={"User-Agent": "continuum-publisher"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                final = response.geturl()
                if not final.startswith("https://"):
                    raise PublisherError(
                        ASSET_UNREACHABLE,
                        f"the asset at {url!r} redirected to a non-https location",
                    )
                data = response.read(self.max_bytes + 1)
        except PublisherError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PublisherError(
                ASSET_UNREACHABLE,
                f"the asset at {url!r} could not be obtained: {exc}",
            ) from None
        if len(data) > self.max_bytes:
            raise PublisherError(
                ASSET_UNREACHABLE,
                f"the asset at {url!r} is larger than the {self.max_bytes}-byte bound",
            )
        return data


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_artifact(artifact: PublishedArtifact, fetcher: AssetFetcher) -> bytes:
    """Fetch one asset and check it against the manifest's digest.

    Returns the bytes so a caller that has to inspect them — a cask's quarantine
    policy, a future signature check — does not download the same asset twice.
    """

    data = fetcher.fetch(artifact.url)
    actual = digest(data)
    if actual != artifact.sha256:
        raise PublisherError(
            DIGEST_MISMATCH,
            f"{artifact.name} hashes to {actual} but the release manifest records "
            f"{artifact.sha256}",
            remediation=(
                "the publisher refuses to write a checksum it could not confirm. "
                "Re-check whether the release was re-cut under the same tag, or "
                "whether the manifest describes a different asset than the one "
                "uploaded."
            ),
        )
    if artifact.size and len(data) != artifact.size:
        raise PublisherError(
            DIGEST_MISMATCH,
            f"{artifact.name} is {len(data)} bytes but the release manifest records "
            f"{artifact.size}",
            remediation=(
                "the digest and the size are both recorded from the same upload; they "
                "disagreeing means the manifest and the asset are not the same file"
            ),
        )
    return data


def verify_all(
    artifacts: Iterable[PublishedArtifact], fetcher: AssetFetcher
) -> Dict[str, bytes]:
    """Verify every artifact, failing on the first that does not hold up.

    The order is the manifest's own order, so the failure a caller sees is the
    first artifact the release pipeline produced rather than whatever a set
    happened to iterate in.
    """

    verified: Dict[str, bytes] = {}
    for artifact in artifacts:
        verified[artifact.url] = verify_artifact(artifact, fetcher)
    return verified


__all__ = [
    "DIGEST_ALGORITHM",
    "MAX_ASSET_BYTES",
    "AssetFetcher",
    "HttpAssetFetcher",
    "InMemoryAssetFetcher",
    "digest",
    "verify_all",
    "verify_artifact",
]
