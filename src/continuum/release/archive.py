"""Byte-identical release archives.

A release archive is not a build intermediate: its bytes are what a consumer
downloads, what a checksum file names, and what a re-run of the same revision
has to reproduce exactly. So the two properties that make an archive
reproducible are enforced here rather than hoped for:

* **A fixed timestamp for every member.** Taken from `SOURCE_DATE_EPOCH` when the
  job sets one, and otherwise from a constant. Not "now": a run whose members
  carry the wall clock cannot reproduce the archive it published an hour ago,
  and an archive that cannot be reproduced is an archive whose checksum changes
  every time anyone re-runs the release.
* **A fixed order, owner, and mode.** Members are sorted by their name in the
  archive, ownership is 0/0 with no owner names, and a member's mode is reduced
  to the one bit that matters — whether it is executable — so a umask
  difference or a checkout that preserved group-write cannot change the bytes.

Both containers are written with Python's own writers rather than by shelling
out to `tar` and `zip`. The system tools differ between a macOS runner's BSD
versions and a Linux image's GNU ones, they carry their own metadata decisions
(`zip` ignores `SOURCE_DATE_EPOCH`; `tar` writes extended attributes unless told
not to), and none of those differences is a property a release should depend on.
Writing the bytes here means the determinism the contract promises is a property
of this repository, checkable by a test that runs anywhere, instead of a property
of whichever toolchain the runner happened to have.
"""

from __future__ import annotations

import gzip
import os
import tarfile
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

#: The epoch used when a job sets none. 1980-01-01T00:00:00Z is the earliest
#: instant a zip entry can carry, and an archive that predates the format is a
#: constant for every machine that runs it.
DEFAULT_EPOCH = 315532800

#: zip stores local time with no zone and no epoch, so the archive records the
#: same instant as the fixed date below whatever `TZ` the job happens to have.
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)

_EXECUTABLE_MODE = 0o755
_DATA_MODE = 0o644

TAR_GZ_MEDIA_TYPE = "application/gzip"
ZIP_MEDIA_TYPE = "application/zip"


class ArchiveError(ValueError):
    """Raised when an archive cannot be written honestly."""

    def __init__(self, message: str, *, code: str = "archive-failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Member:
    """One file in an archive: where it is now, and what it is called inside."""

    source: str
    name: str

    def __post_init__(self) -> None:
        if not self.source:
            raise ArchiveError("an archive member needs a source path")
        if not self.name or self.name.startswith("/") or ".." in self.name.split("/"):
            raise ArchiveError(
                f"archive member name {self.name!r} must be a relative path inside the "
                "archive; an archive that can write outside its own extraction "
                "directory is not a distribution artifact"
            )


def mode_for(path: str) -> int:
    """The mode a member is recorded with: the executable bit, and nothing else.

    Reduced rather than copied, because the source tree's other mode bits come
    from whatever created the file — a umask, a checkout, a previous build — and
    none of that is a fact about the artifact a user installs.
    """

    return _EXECUTABLE_MODE if os.access(path, os.X_OK) else _DATA_MODE


def epoch_from(environment: Dict[str, str]) -> int:
    """The timestamp every member of this release's archives carries.

    `SOURCE_DATE_EPOCH` is the cross-toolchain convention for "this build is
    from this moment"; honouring it means a release job can pin the instant to
    the commit's own committer time and get the same bytes the reference flow
    produced. An unparsable value is refused rather than ignored: an archive
    built at an arbitrary time is exactly the nondeterminism this module exists
    to remove.
    """

    raw = (environment.get("SOURCE_DATE_EPOCH") or "").strip()
    if not raw:
        return DEFAULT_EPOCH
    try:
        value = int(raw)
    except ValueError:
        raise ArchiveError(
            f"SOURCE_DATE_EPOCH is {raw!r}, which is not a whole number of seconds "
            "since the epoch; an archive timestamp that cannot be read cannot be "
            "reproduced either"
        ) from None
    if value < 0:
        raise ArchiveError("SOURCE_DATE_EPOCH cannot be negative")
    return value


def members_for(
    root: str,
    names: Sequence[str],
    *,
    extensions: Sequence[str] = (),
) -> Tuple[Member, ...]:
    """The files an archive of `root` holds, sorted by their archive name.

    A name that is a directory contributes every file beneath it, so a bundle is
    packaged by naming the bundle. Extensions narrow a directory walk to the
    files that belong in an artifact, which is how a bundle carries its Mach-O
    binaries without also carrying the stray files a build leaves beside them.
    """

    collected: Dict[str, Member] = {}
    for name in names:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            collected[name] = Member(source=path, name=name)
            continue
        if not os.path.isdir(path):
            raise ArchiveError(
                f"{path!r} is neither a file nor a directory, so there is nothing to "
                "archive for it; the release was configured to ship something that "
                "the build did not produce",
                code="archive-member-missing",
            )
        for directory, _dirs, files in os.walk(path):
            relative = os.path.relpath(directory, root)
            for file_name in files:
                if extensions and not file_name.endswith(tuple(extensions)):
                    continue
                member_name = os.path.normpath(os.path.join(relative, file_name))
                collected[member_name] = Member(
                    source=os.path.join(directory, file_name), name=member_name
                )
    return tuple(collected[name] for name in sorted(collected))


def write_tar_gz(path: str, members: Sequence[Member], *, epoch: int) -> str:
    """Write a gzip-compressed tar, and return the path written.

    The gzip header carries neither a file name nor a timestamp — the `gzip -n`
    behaviour the reference flow had to ask for — because either one would make
    two archives of identical contents differ.
    """

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        with open(path, "wb") as raw:
            with gzip.GzipFile(
                fileobj=raw, mode="wb", compresslevel=9, mtime=0, filename=""
            ) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
                    for member in _ordered(members):
                        info = tar.gettarinfo(member.source, arcname=member.name)
                        info.mtime = epoch
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mode = mode_for(member.source)
                        with open(member.source, "rb") as handle:
                            tar.addfile(info, handle)
    except OSError as exc:
        raise ArchiveError(f"could not write {path}: {exc}") from None
    return path


def write_zip(path: str, members: Sequence[Member]) -> str:
    """Write a zip, and return the path written.

    Every entry carries the same fixed timestamp and the same reduced mode, and
    entries are added in sorted order, so the archive is a function of its
    contents alone.
    """

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member in _ordered(members):
                info = zipfile.ZipInfo(member.name, date_time=_ZIP_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3  # Unix, so the mode below is honoured.
                info.external_attr = mode_for(member.source) << 16
                with open(member.source, "rb") as handle:
                    archive.writestr(info, handle.read())
    except OSError as exc:
        raise ArchiveError(f"could not write {path}: {exc}") from None
    return path


def _ordered(members: Sequence[Member]) -> List[Member]:
    return sorted(members, key=lambda member: member.name)


#: The formats a repository may name, and the suffix each one is written with.
#: This is a fact about the formats rather than about any one adapter's naming
#: policy: what a file is called and which extension makes a tool recognise it
#: have to agree, so the mapping is written once here.
FORMAT_EXTENSIONS = {
    "tar.gz": ".tar.gz",
    "zip": ".zip",
}


def extension_for(artifact_format: str) -> str:
    """The file suffix an archive format is written with."""

    try:
        return FORMAT_EXTENSIONS[artifact_format]
    except KeyError:
        raise ArchiveError(
            f"unsupported archive format {artifact_format!r}; supported: "
            + ", ".join(sorted(FORMAT_EXTENSIONS)),
            code="archive-format-unsupported",
        ) from None


def combine(*groups: Sequence[Member]) -> Tuple[Member, ...]:
    """Merge member groups into one sorted archive listing.

    Two groups claiming one name is refused rather than resolved: which copy
    would win is exactly the kind of choice that has to be made by naming, and
    an archive that silently ships one of two files called the same thing is an
    archive nobody can reason about afterwards.
    """

    merged: Dict[str, Member] = {}
    for group in groups:
        for member in group:
            existing = merged.get(member.name)
            if existing is not None and existing.source != member.source:
                raise ArchiveError(
                    f"archive member {member.name!r} would be written twice, from "
                    f"{existing.source!r} and {member.source!r}",
                    code="archive-member-conflict",
                )
            merged[member.name] = member
    return tuple(merged[name] for name in sorted(merged))


def write(
    path: str,
    members: Sequence[Member],
    *,
    artifact_format: str,
    epoch: Optional[int] = None,
) -> str:
    """Write `members` to `path` in the named format.

    `epoch` is the timestamp every member is written with; left unset it comes
    from the default, which is a fixed instant rather than the current time.
    That is the whole determinism claim in one parameter: two runs over the same
    inputs produce the same bytes, so a checksum recorded for one is still true
    for the next.
    """

    if epoch is None:
        epoch = DEFAULT_EPOCH
    if artifact_format == "tar.gz":
        return write_tar_gz(path, members, epoch=epoch)
    if artifact_format == "zip":
        return write_zip(path, members)
    raise ArchiveError(
        f"artifact format {artifact_format!r} has no writer; supported: "
        + ", ".join(sorted(FORMAT_EXTENSIONS)),
        code="archive-format-unsupported",
    )


__all__ = [
    "ArchiveError",
    "DEFAULT_EPOCH",
    "FORMAT_EXTENSIONS",
    "Member",
    "TAR_GZ_MEDIA_TYPE",
    "ZIP_MEDIA_TYPE",
    "combine",
    "epoch_from",
    "extension_for",
    "members_for",
    "mode_for",
    "write",
    "write_tar_gz",
    "write_zip",
]