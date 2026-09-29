"""Google Play publication: upload the bundle, hold a draft release, then promote it.

This is the module that knows about the Google Play Developer API. Nothing else
in Continuum does, and the Android adapter in `android.py` knows nothing about
it: a project that ships a signed APK to a GitHub Release and never enrols in
Play configures no publisher at all, and the release chain's `publish` stage then
records a green no-op rather than a missing component.

Play's release model is not GitHub's, and pretending otherwise is how a release
goes wrong quietly. Three properties of the destination drive the whole design:

**Play has no unpublished release, but it has a draft release.** An upload is
consumed the moment the edit is committed — the `versionCode` is spent and can
never be used again, and no device sees the bytes. So the two stages of the
release chain map onto Play's own two states rather than onto a local draft this
module has to remember: `draft()` creates an edit, uploads the bundle, assigns the
`versionCode` to the track as a **draft release**, and commits. `publish()` opens
a new edit and changes that draft to a **completed** release, optionally staged
across a user fraction. Nothing is held in memory between the stages, so a job
that dies after drafting resumes by promoting a release that already exists.

**One open edit per app.** Play rejects a second concurrent edit, so a run that
opened an edit and then failed would block every later run until the edit
expired. An edit this module opens is deleted on the way out whatever happens, and
an edit that Play has already invalidated — expired, deleted, or conflicted — is
recreated rather than retried against, because the handle is the thing that is
stale, not the request.

**A `versionCode` is spent and strictly increasing.** Play refuses an upload
whose code is not greater than every code already in use, and refuses to reuse a
code even for the same bytes. That makes three failure modes distinct rather than
one "upload failed": a code already on *this* track is a duplicate (green, and
the reason a re-run is quiet), a code already used on *another* track is a
conflict that only a human resolves, and a code Play rejects for being too low is
a version-binding bug. Each is reported with its own code and its own answer to
"would a retry help".

Two further checks exist because they are the ones that catch a bad release
before a user does. The `versionCode` is derived with the *same* function the
adapter used to stamp the bundle, because a publisher that computed it
differently would upload a bundle Play rejects and, if it did not, would ship an
app whose reported version is not the one that was released. And the digest Play
echoes back for the uploaded bytes is compared against the manifest's, so a
truncated or substituted upload is caught at the boundary rather than by the
first crash report.

The destination is injected as a transport, not opened here, so the whole
publisher — including every retry, every skip, and every refusal — is exercised
against an in-memory implementation of Play with no network and no credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .android import version_code_for
from .contract import (
    TYPE_PACKAGE,
    Artifact,
    ContractError,
    PublishRequest,
    PublisherResult,
    failed,
    published,
    skipped,
)

PUBLISHER_NAME = "google-play"

#: The destination string that appears in a publication result. A component name
#: rather than a URL or a package name, so the recorded identity of a
#: destination is the same whichever app the same publisher is pointed at.
DESTINATION = "google play"

DRY_RUN_CODE = "dry-run"

# The two writable tracks every Play app has. `alpha`, `beta`, and `closed` are
# accepted because an app created before Play split them still exposes them, and
# refusing a track a destination actually has would be a configuration error
# reported as a missing one.
TRACK_INTERNAL = "internal"
TRACK_PRODUCTION = "production"
SUPPORTED_TRACKS: Tuple[str, ...] = (
    TRACK_INTERNAL,
    TRACK_PRODUCTION,
    "beta",
    "alpha",
    "closed",
)

# The release statuses Play stores on a track. `draft` is invisible to users and
# is where the `draft` stage leaves the release; `completed` is the only status
# that serves the bundle; `inProgress` is a staged rollout; `halted` is a rollout
# that was stopped and cannot be resumed.
STATUS_DRAFT = "draft"
STATUS_COMPLETED = "completed"
STATUS_IN_PROGRESS = "inProgress"
STATUS_HALTED = "halted"
SUPPORTED_STATUSES: Tuple[str, ...] = (
    STATUS_DRAFT,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_HALTED,
)

#: The statuses that make a release visible to some user. A code in one of them is
#: spent: it cannot be uploaded again on any track.
LIVE_STATUSES: Tuple[str, ...] = (STATUS_COMPLETED, STATUS_IN_PROGRESS, STATUS_HALTED)

#: The two stages. They read the same tracks and mean different things by a
#: release found there, so the stage travels with the question.
STAGE_DRAFT = "draft"
STAGE_PUBLISH = "publish"

API_ROOT = "https://androidpublisher.googleapis.com/androidpublisher/v3"
UPLOAD_API_ROOT = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3"

DEFAULT_ATTEMPTS = 3
DEFAULT_TIMEOUT = 120

#: Backoff between retries, in seconds: the first retry waits this long and each
#: one after it doubles, up to the cap. Long enough to be a request rather than a
#: retry storm, short enough that a release is not held up for minutes.
RETRY_BACKOFF_BASE = 1.0
RETRY_BACKOFF_CAP = 30.0

_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")


class PlayError(RuntimeError):
    """The publisher cannot honestly report a result.

    Carries a stable `code`, a message safe to print, and whether a second
    attempt could plausibly succeed — the same three things every other release
    component reports, and the reason a caller can branch on the kind of failure
    instead of on prose.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        remediation: str = "",
        details: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.remediation = remediation
        self.details: Dict[str, str] = dict(details or {})

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.remediation:
            payload["remediation"] = self.remediation
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class PlayApiError(PlayError):
    """A call to Play failed, classified by what the response said.

    The distinction that matters most is `invalidated_edit`: an edit handle Play
    has expired, deleted, or conflicted with is not a failed request, it is a
    stale handle, and the correct response is a new handle rather than a second
    try with the old one. Everything else is reported as it came.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 0,
        retryable: bool = False,
        invalidated_edit: bool = False,
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(code, message, retryable=retryable)
        self.status = status
        self.invalidated_edit = invalidated_edit
        #: Seconds Play asked to wait, from `Retry-After`. Zero means Play said
        #: nothing, so the caller backs off on its own schedule.
        self.retry_after = retry_after


#: Failure codes whose fix is a fresh edit. Ordered as the transport reports
#: them, and shared with the retry loop so the two cannot disagree.
INVALIDATED_EDIT_CODES: Tuple[str, ...] = (
    "edit-not-found",
    "edit-expired",
    "edit-conflict",
)

RETRYABLE_CODES: Tuple[str, ...] = (
    "play-unavailable",
    "edit-not-found",
    "edit-expired",
    "edit-conflict",
    "upload-interrupted",
)


# -- configuration ----------------------------------------------------------


def _mapping(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PlayError(
            "configuration-invalid", f"{where} must be a mapping, got {type(value).__name__}"
        )
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise PlayError(
            "configuration-invalid",
            f"{where} has unsupported key(s): {', '.join(unknown)}; allowed: "
            f"{', '.join(allowed)}",
        )


def _text(value: Any, where: str, default: str = "", *, required: bool = False) -> str:
    if value is None:
        if required:
            raise PlayError("configuration-invalid", f"{where} is required")
        return default
    if not isinstance(value, str) or not value.strip():
        raise PlayError("configuration-invalid", f"{where} must be a non-empty string")
    return value.strip()


def _number(value: Any, where: str, default: float, *, low: float, high: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlayError("configuration-invalid", f"{where} must be a number")
    if not low <= float(value) <= high:
        raise PlayError(
            "configuration-invalid", f"{where} must be between {low} and {high}, got {value!r}"
        )
    return round(float(value), 2)


def _int(value: Any, where: str, default: int, *, low: int, high: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise PlayError("configuration-invalid", f"{where} must be an integer")
    if not low <= value <= high:
        raise PlayError(
            "configuration-invalid", f"{where} must be between {low} and {high}, got {value!r}"
        )
    return value


def _bool(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PlayError("configuration-invalid", f"{where} must be true or false")
    return value


def _secret_name(value: Any, where: str) -> str:
    text = _text(value, where, required=True)
    if not re.match(r"^[A-Z][A-Z0-9_]{0,63}$", text):
        raise PlayError(
            "configuration-invalid",
            f"{where} must be an upper-case repository secret name; a Play "
            f"credential is referenced by name, never inlined; got {text!r}",
        )
    return text


@dataclass(frozen=True)
class PlaySettings:
    """One Play destination: which app, which track, and how far to roll out.

    The rollout fraction is a *publishing* decision, not a build one, so it lives
    here rather than in the adapter's options. A project that ships a build to
    `internal` at 100% and a production release to 20% is two destinations, not
    two targets.
    """

    package_name: str
    track: str = TRACK_INTERNAL
    rollout: float = 1.0
    release_name: str = ""
    release_notes: str = ""
    access_token_secret: str = ""
    version_code: int = 0
    version_code_offset: int = 0
    attempts: int = DEFAULT_ATTEMPTS
    timeout: int = DEFAULT_TIMEOUT
    require_bundle: bool = True

    def __post_init__(self) -> None:
        if not _PACKAGE_RE.match(self.package_name or ""):
            raise PlayError(
                "configuration-invalid",
                f"package_name {self.package_name!r} is not an Android application id; "
                "Play identifies an app by its package name and there is no way to "
                "derive one",
            )
        if self.track not in SUPPORTED_TRACKS:
            raise PlayError(
                "configuration-invalid",
                f"track {self.track!r} is not writable; supported: "
                f"{', '.join(SUPPORTED_TRACKS)}",
            )
        if not 0.0 < self.rollout <= 1.0:
            raise PlayError(
                "configuration-invalid",
                f"rollout must be greater than 0 and at most 1; got {self.rollout!r}. A "
                "rollout of 0 serves nobody, and Play has no other way to hold a "
                "release back.",
            )
        if self.version_code < 0 or self.version_code > 2_100_000_000:
            raise PlayError(
                "configuration-invalid",
                f"version_code must be between 0 and 2100000000; got {self.version_code}",
            )
        if self.version_code and self.version_code_offset:
            raise PlayError(
                "configuration-invalid",
                "version_code is an explicit override, so version_code_offset would "
                "never apply; set one or the other",
            )
        if not 1 <= self.attempts <= 5:
            raise PlayError(
                "configuration-invalid", f"attempts must be between 1 and 5; got {self.attempts}"
            )
        if not 1 <= self.timeout <= 900:
            raise PlayError(
                "configuration-invalid",
                f"timeout must be between 1 and 900 seconds; got {self.timeout}",
            )

    @property
    def staged(self) -> bool:
        """Whether publishing this is a staged rollout rather than a full release."""

        return self.rollout < 1.0

    def version_binding(self, version: str) -> Tuple[str, int]:
        """The `versionName` and `versionCode` this release is published as.

        The code comes from the same function the adapter stamped the bundle
        with, and that is the entire point: the code Play records for a release
        is read out of the bundle, and a publisher that disagreed with the
        adapter would either be rejected as a duplicate or ship an app whose
        reported version is not the version that was released.
        """

        name = (version or "").strip().lstrip("vV")
        code = self.version_code or version_code_for(version, offset=self.version_code_offset)
        return name, code

    @property
    def destination(self) -> str:
        return DESTINATION

    def describe(self) -> Dict[str, Any]:
        return {
            "package_name": self.package_name,
            "track": self.track,
            "rollout": self.rollout,
            "release_name": self.release_name,
            "access_token_secret": self.access_token_secret,
            "version_code": self.version_code,
            "version_code_offset": self.version_code_offset,
            "attempts": self.attempts,
            "require_bundle": self.require_bundle,
        }


_ALLOWED_SETTING_KEYS = (
    "package_name",
    "track",
    "rollout",
    "release_name",
    "release_notes",
    "access_token_secret",
    "version_code",
    "version_code_offset",
    "attempts",
    "timeout",
    "require_bundle",
)


def parse_settings(value: Any, where: str = "google play") -> PlaySettings:
    """Validate one Play destination's options.

    Kept here rather than in the release configuration schema for the same
    reason the Android adapter keeps its own: a track name and a package name are
    this publisher's business, and a consumer-facing schema is not the place a
    new platform's vocabulary is allowed to arrive.
    """

    mapping = _mapping(value, where)
    _reject_unknown(mapping, _ALLOWED_SETTING_KEYS, where)
    token = mapping.get("access_token_secret")
    return PlaySettings(
        package_name=_text(mapping.get("package_name"), f"{where}.package_name", required=True),
        track=_text(mapping.get("track"), f"{where}.track", TRACK_INTERNAL),
        rollout=_number(mapping.get("rollout"), f"{where}.rollout", 1.0, low=0.01, high=1.0),
        release_name=_text(mapping.get("release_name"), f"{where}.release_name", ""),
        release_notes=_text(mapping.get("release_notes"), f"{where}.release_notes", ""),
        access_token_secret=_secret_name(token, f"{where}.access_token_secret")
        if token is not None
        else "",
        version_code=_int(
            mapping.get("version_code"), f"{where}.version_code", 0, low=0, high=2_100_000_000
        ),
        version_code_offset=_int(
            mapping.get("version_code_offset"), f"{where}.version_code_offset", 0, low=0, high=1000
        ),
        attempts=_int(mapping.get("attempts"), f"{where}.attempts", DEFAULT_ATTEMPTS, low=1, high=5),
        timeout=_int(mapping.get("timeout"), f"{where}.timeout", DEFAULT_TIMEOUT, low=1, high=900),
        require_bundle=_bool(mapping.get("require_bundle"), f"{where}.require_bundle", True),
    )


# -- the destination --------------------------------------------------------


@dataclass(frozen=True)
class TrackState:
    """A track as Play currently holds it.

    `version_code` is the code of the release currently *on* the track, and it is
    the fact this publisher checks before it does anything: whether a code is
    free, already spent, or already drafted is a question only Play can answer,
    and asking it first is what makes an upload safe to retry.
    """

    track: str
    status: str = ""
    version_code: int = 0
    user_fraction: float = 0.0
    name: str = ""

    def __post_init__(self) -> None:
        if not self.track:
            raise PlayError("configuration-invalid", "a track state must name its track")
        if self.status and self.status not in SUPPORTED_STATUSES:
            raise PlayError(
                "configuration-invalid",
                f"track {self.track!r} holds unknown release status {self.status!r}; "
                f"supported: {', '.join(SUPPORTED_STATUSES)}",
            )

    @property
    def live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def editable(self) -> bool:
        """Whether this release may still be moved to `completed`.

        Play does not resume a halted rollout: a halted release can only be
        replaced by a new upload with a higher code. Reporting that here rather
        than letting the API answer 400 is the difference between "you must cut a
        new version" and "the request was malformed".
        """

        return self.status in (STATUS_DRAFT, STATUS_IN_PROGRESS, "")

    def describe(self) -> Dict[str, Any]:
        return {
            "track": self.track,
            "status": self.status,
            "version_code": self.version_code,
            "user_fraction": self.user_fraction,
            "name": self.name,
        }


@dataclass(frozen=True)
class UploadReceipt:
    """What Play says about the bytes it received.

    Play echoes a digest of the uploaded bundle. Comparing it with the manifest
    turns "the upload returned 200" into "the upload returned 200 with these
    bytes", which is the difference between a release that is confirmed shipped
    and one that is assumed shipped.
    """

    size: int = 0
    sha1: str = ""
    sha256: str = ""


class PlayTransport:
    """The calls this publisher makes against Play.

    Declared as an explicit attribute list rather than a `Protocol` so that a
    mis-wired destination is refused at construction, by the same `conform()`
    discipline every other port in the release contract follows, and so this
    works with an object written against a different Continuum.
    """

    SUPPORTS_DRY_RUN = True
    name = "play-transport"

    def insert_edit(self, package_name: str) -> str:
        raise NotImplementedError

    def list_tracks(self, package_name: str, edit_id: str) -> Tuple[TrackState, ...]:
        raise NotImplementedError

    def upload_bundle(self, package_name: str, edit_id: str, path: str) -> UploadReceipt:
        raise NotImplementedError

    def update_track(
        self,
        package_name: str,
        edit_id: str,
        track: str,
        status: str,
        version_code: int,
        *,
        name: str = "",
        user_fraction: float = 0.0,
        notes: str = "",
    ) -> None:
        raise NotImplementedError

    def commit_edit(self, package_name: str, edit_id: str) -> None:
        raise NotImplementedError

    def delete_edit(self, package_name: str, edit_id: str) -> None:
        raise NotImplementedError


TRANSPORT_SURFACE: Tuple[str, ...] = (
    "insert_edit",
    "list_tracks",
    "upload_bundle",
    "update_track",
    "commit_edit",
    "delete_edit",
)


def require_transport(transport: Any) -> Any:
    """Refuse a destination that cannot answer every call this publisher makes.

    A port that is missing `commit_edit` is not a smaller Play API, it is a
    release that uploads a bundle and leaves it in an uncommitted edit Play will
    discard — the most expensive way to discover the mistake.
    """

    missing = [member for member in TRANSPORT_SURFACE if not callable(getattr(transport, member, None))]
    if missing:
        raise PlayError(
            "transport-incomplete",
            f"the Play transport is missing {', '.join(missing)}. Every call this "
            "publisher makes has to be answered, including the one that commits "
            "the edit.",
        )
    return transport


# -- the in-memory destination ----------------------------------------------


class InMemoryPlayTransport(PlayTransport):
    """A Play that keeps its state in a dict, so the publisher can be tested.

    It enforces the properties the real destination enforces, because a fake
    that enforced none of them would prove nothing: only one open edit at a time,
    an expired edit, a spent `versionCode` that cannot be uploaded twice, a
    strictly increasing code, a digest echoed back from the bytes, and a staged
    rollout that may only widen.

    `failures` is a queue of `PlayApiError`s, one consumed per call, which is how
    a test states "the first attempt is refused and the second is not" without
    this class knowing anything about retries.
    """

    name = "play-in-memory"

    def __init__(
        self,
        *,
        spent_codes: Sequence[int] = (),
        max_attempts_before_success: int = 0,
        echo_digest: bool = True,
    ) -> None:
        self.tracks: Dict[str, TrackState] = {}
        self.uploads: List[Dict[str, Any]] = []
        self.calls: List[str] = []
        self.edits: Dict[str, Dict[str, Any]] = {}
        self.open_edits: List[str] = []
        self.spent: set = set(int(code) for code in spent_codes)
        self.committed: List[str] = []
        self.expired: set = set()
        self.failures: List[PlayApiError] = []
        self.echo_digest = echo_digest
        self._counter = 0
        self._attempts_left = max_attempts_before_success
        self._call_index = 0

    # -- test controls ----------------------------------------------------
    def fail_next(self, *errors: PlayApiError) -> None:
        """Queue failures to be returned by the next calls, in order."""

        self.failures.extend(errors)

    def expire(self, edit_id: str) -> None:
        """Make an edit handle stale, as Play does when it expires.

        The slot it held is freed, because that is what Play does once a handle
        has expired: a run that discovers its edit is stale has to be able to
        insert a new one rather than colliding with its own dead handle.
        """

        self.expired.add(edit_id)
        if edit_id in self.open_edits:
            self.open_edits.remove(edit_id)

    # -- transport --------------------------------------------------------
    def _next_failure(self) -> Optional[PlayApiError]:
        if self.failures:
            return self.failures.pop(0)
        if self._attempts_left > 0:
            self._attempts_left -= 1
            return PlayApiError(
                "play-unavailable",
                "the backend is not responding (simulated)",
                status=503,
                retryable=True,
            )
        return None

    def _require_edit(self, edit_id: str) -> Dict[str, Any]:
        if edit_id in self.expired or edit_id not in self.edits:
            raise PlayApiError(
                "edit-expired",
                f"edit {edit_id!r} is no longer valid",
                status=410,
                retryable=True,
                invalidated_edit=True,
            )
        return self.edits[edit_id]

    def insert_edit(self, package_name: str) -> str:
        self.calls.append("insert_edit")
        failure = self._next_failure()
        if failure is not None:
            raise failure
        if self.open_edits:
            raise PlayApiError(
                "edit-conflict",
                f"an edit is already open ({', '.join(self.open_edits)}); Play allows "
                "one at a time",
                status=409,
                retryable=True,
                invalidated_edit=True,
            )
        self._counter += 1
        edit_id = f"edit-{self._counter:03d}"
        self.edits[edit_id] = {"package": package_name, "tracks": {}}
        self.open_edits.append(edit_id)
        return edit_id

    def list_tracks(self, package_name: str, edit_id: str) -> Tuple[TrackState, ...]:
        self.calls.append("list_tracks")
        failure = self._next_failure()
        if failure is not None:
            raise failure
        self._require_edit(edit_id)
        return tuple(self.tracks.values())

    def upload_bundle(self, package_name: str, edit_id: str, path: str) -> UploadReceipt:
        self.calls.append("upload_bundle")
        failure = self._next_failure()
        if failure is not None:
            raise failure
        self._require_edit(edit_id)
        if not os.path.isfile(path):
            raise PlayError(
                "bundle-absent",
                f"the bundle at {path!r} is not a readable file; uploading it would "
                "record a versionCode with no bytes behind it",
            )
        with open(path, "rb") as handle:
            data = handle.read()
        sha256 = hashlib.sha256(data).hexdigest()
        self.uploads.append({"path": path, "size": len(data), "sha256": sha256})
        return UploadReceipt(
            size=len(data),
            sha1=hashlib.sha1(data).hexdigest(),
            sha256=sha256 if self.echo_digest else "",
        )

    def update_track(
        self,
        package_name: str,
        edit_id: str,
        track: str,
        status: str,
        version_code: int,
        *,
        name: str = "",
        user_fraction: float = 0.0,
        notes: str = "",
    ) -> None:
        self.calls.append("update_track")
        failure = self._next_failure()
        if failure is not None:
            raise failure
        self._require_edit(edit_id)
        if status not in SUPPORTED_STATUSES:
            raise PlayApiError(
                "request-rejected", f"unknown release status {status!r}", status=400
            )
        if track not in SUPPORTED_TRACKS:
            raise PlayApiError(
                "track-unknown", f"track {track!r} does not exist for this app", status=400
            )
        previous = self.tracks.get(track)
        # A release already on this track is being moved, not uploaded: promoting
        # a draft to completed spends no second code, and Play accepts exactly
        # that. Anything else with this code is a second use of it, including a
        # code that is only drafted elsewhere.
        moving = previous is not None and previous.version_code == version_code
        if not moving and version_code in self.spent:
            raise PlayApiError(
                "version-code-used",
                f"Version code {version_code} has already been used. Each version "
                "code may only be used once.",
                status=400,
            )
        if status == STATUS_IN_PROGRESS and not 0.0 < user_fraction <= 1.0:
            raise PlayApiError(
                "request-rejected",
                "a staged rollout requires a user fraction greater than 0 and at "
                "most 1",
                status=400,
            )
        if user_fraction:
            existing = self.tracks.get(track)
            if (
                existing is not None
                and existing.live
                and user_fraction < existing.user_fraction
            ):
                raise PlayApiError(
                    "rollout-not-widened",
                    "A staged rollout can only be widened, not narrowed. Halt the "
                    "rollout to stop it.",
                    status=400,
                )
        if not moving:
            # A code must exceed every code already used anywhere in the app, not
            # just the ones on this track. That is why the publisher asks about
            # all the tracks before it writes rather than only about its own.
            highest = max(
                self.spent | {state.version_code for state in self.tracks.values()},
                default=0,
            )
            if version_code <= highest:
                raise PlayApiError(
                    "version-code-not-greater",
                    f"Version code {version_code} is not greater than the highest "
                    f"previously used version code ({highest}).",
                    status=400,
                )
        self.tracks[track] = TrackState(
            track=track,
            status=status,
            version_code=version_code,
            user_fraction=user_fraction,
            name=name,
        )

    def commit_edit(self, package_name: str, edit_id: str) -> None:
        self.calls.append("commit_edit")
        failure = self._next_failure()
        if failure is not None:
            raise failure
        self._require_edit(edit_id)
        # Committing spends the code, draft included: Play reserves a drafted
        # release's `versionCode`, so a second upload with it is refused even
        # though nobody is being served yet. That reservation is what makes a
        # re-run a skip rather than a second upload.
        for state in self.tracks.values():
            if state.live or state.status == STATUS_DRAFT:
                self.spent.add(state.version_code)
        self.committed.append(edit_id)
        if edit_id in self.open_edits:
            self.open_edits.remove(edit_id)
        self.expired.discard(edit_id)

    def delete_edit(self, package_name: str, edit_id: str) -> None:
        self.calls.append("delete_edit")
        if edit_id in self.open_edits:
            self.open_edits.remove(edit_id)


# -- the HTTP destination ---------------------------------------------------


_RETRY_AFTER_RE = re.compile(r"^\d+$")


def _classify(status: int, body: str) -> PlayApiError:
    """Turn a Play HTTP response into a classified failure.

    The classification is by the fact being reported rather than by the status
    alone, because two 400s mean opposite things to a release: "your request was
    malformed" is a configuration bug, and "this version code is not greater than
    the last one" is a version-binding bug that no retry fixes.
    """

    lowered = (body or "").lower()
    if status in (401, 403):
        return PlayApiError(
            "credential-rejected",
            f"Play refused the credential (HTTP {status}): {body[:200]}",
            status=status,
        )
    if status == 404:
        return PlayApiError(
            "edit-not-found",
            f"the edit no longer exists (HTTP {status}); Play expires an edit that has "
            "been open too long",
            status=status,
            retryable=True,
            invalidated_edit=True,
        )
    if status == 410:
        return PlayApiError(
            "edit-expired",
            f"the edit has expired (HTTP {status})",
            status=status,
            retryable=True,
            invalidated_edit=True,
        )
    if status == 409:
        return PlayApiError(
            "edit-conflict",
            f"another edit is open for this app (HTTP {status})",
            status=status,
            retryable=True,
            invalidated_edit=True,
        )
    if "version code" in lowered and (
        "already been used" in lowered or "not greater" in lowered
    ):
        return PlayApiError(
            "version-code-used",
            f"Play refused the version code (HTTP {status}): {body[:200]}",
            status=status,
        )
    if status == 400:
        return PlayApiError(
            "request-rejected", f"Play rejected the request (HTTP 400): {body[:200]}", status=400
        )
    if status in (429, 500, 502, 503, 504):
        return PlayApiError(
            "play-unavailable",
            f"Play is temporarily unavailable (HTTP {status})",
            status=status,
            retryable=True,
        )
    return PlayApiError(
        "play-call-failed", f"Play returned HTTP {status}: {body[:200]}", status=status
    )


class HttpPlayTransport(PlayTransport):
    """The real Google Play Developer API, over the standard library.

    The access token is read from a repository secret *by name* and held only in
    the request header, so a Play credential never reaches a manifest, a journal,
    or a log. Bundles are uploaded with the resumable protocol because an AAB is
    routinely tens of megabytes and a single-request upload is neither supported
    at that size nor resumable if the runner drops.
    """

    name = "play-http"

    def __init__(
        self,
        environment: Optional[Mapping[str, str]] = None,
        *,
        token_secret: str = "",
        api_root: str = API_ROOT,
        upload_api_root: str = UPLOAD_API_ROOT,
        timeout: int = DEFAULT_TIMEOUT,
        sleep: Any = time.sleep,
    ) -> None:
        self.environment: Dict[str, str] = dict(environment or {})
        self.token_secret = token_secret
        self.api_root = api_root.rstrip("/")
        self.upload_api_root = upload_api_root.rstrip("/")
        self.timeout = timeout
        self._sleep = sleep

    # -- plumbing ---------------------------------------------------------
    def _token(self) -> str:
        token = (self.environment.get(self.token_secret) or "").strip()
        if not token:
            raise PlayError(
                "credential-missing",
                f"the Play access token is not available in this job. The repository "
                f"secret {self.token_secret!r} is empty or unset; a fork receives it "
                "with nothing in it, so a check for the name alone would pass and "
                "authenticate as nobody.",
                remediation=(
                    "expose the service-account access token as a repository secret "
                    "named by access_token_secret"
                ),
            )
        return token

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: Optional[bytes] = None,
        content_type: str = "application/json",
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Tuple[int, bytes, Dict[str, str]]:
        import urllib.error
        import urllib.request

        headers = {
            "Authorization": f"Bearer {self._token()}",
            "User-Agent": "continuum-release",
        }
        if body is not None:
            headers["Content-Type"] = content_type
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            payload = exc.read() or b""
            headers = dict(getattr(exc, "headers", {}) or {})
            error = _classify(exc.code, payload.decode("utf-8", "replace"))
            # Play's own pacing wins over the backoff schedule: a 429 with
            # `Retry-After` is an instruction, not a hint.
            retry_after = _RETRY_AFTER_RE.match(headers.get("Retry-After", "").strip() or "")
            if retry_after is not None:
                error.retry_after = float(retry_after.group(0))
            raise error from None
        except (urllib.error.URLError, OSError) as exc:
            raise PlayApiError(
                "play-unavailable", f"Play could not be reached: {exc}", retryable=True
            ) from None

    def _json_request(
        self, method: str, url: str, payload: Optional[Mapping[str, Any]] = None
    ) -> Tuple[bytes, Dict[str, str]]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        _, data, headers = self._request(method, url, body=body)
        return data, headers

    def _app_url(self, *parts: str) -> str:
        return "/".join([self.api_root, "applications", *parts])

    # -- transport --------------------------------------------------------
    def insert_edit(self, package_name: str) -> str:
        data, _ = self._json_request("POST", self._app_url(package_name, "edits"), {})
        edit_id = str(json.loads(data.decode("utf-8")).get("id") or "")
        if not edit_id:
            raise PlayError(
                "play-call-failed",
                "Play created an edit but returned no id, so there is nothing to "
                "upload into",
            )
        return edit_id

    def list_tracks(self, package_name: str, edit_id: str) -> Tuple[TrackState, ...]:
        data, _ = self._json_request("GET", self._app_url(package_name, "edits", edit_id, "tracks"))
        states: List[TrackState] = []
        for track in json.loads(data.decode("utf-8")).get("tracks", []):
            releases = track.get("releases") or [{}]
            release = releases[0] if releases else {}
            codes = release.get("versionCodes") or []
            states.append(
                TrackState(
                    track=str(track.get("track") or ""),
                    status=str(release.get("status") or ""),
                    version_code=int(codes[0]) if codes else 0,
                    user_fraction=float(release.get("userFraction") or 0.0),
                    name=str(release.get("name") or ""),
                )
            )
        return tuple(states)

    def upload_bundle(self, package_name: str, edit_id: str, path: str) -> UploadReceipt:
        if not os.path.isfile(path):
            raise PlayError(
                "bundle-absent",
                f"the bundle at {path!r} is not a readable file on this runner",
            )
        url = (
            f"{self.upload_api_root}/applications/{package_name}/edits/{edit_id}"
            "/bundles?uploadType=resumable"
        )
        # A resumable upload is two requests: one that reserves a session and one
        # that sends the bytes to the URL Play hands back. The session URL is
        # where the bytes go, and it is a different host path from the API.
        _, _, headers = self._request("POST", url, body=b"{}")
        location = headers.get("Location") or headers.get("location") or ""
        if not location:
            raise PlayError(
                "upload-interrupted",
                "Play accepted the upload but returned no resumable session URL, so "
                "there is nowhere to send the bundle",
                retryable=True,
            )
        with open(path, "rb") as handle:
            data = handle.read()
        received, _ = self._request(
            "PUT",
            location,
            body=data,
            content_type="application/octet-stream",
        )
        payload = json.loads(received.decode("utf-8")) if received else {}
        return UploadReceipt(
            size=int(payload.get("size") or len(data)),
            sha1=str(payload.get("sha1") or ""),
            sha256=str(payload.get("sha256") or ""),
        )

    def update_track(
        self,
        package_name: str,
        edit_id: str,
        track: str,
        status: str,
        version_code: int,
        *,
        name: str = "",
        user_fraction: float = 0.0,
        notes: str = "",
    ) -> None:
        release: Dict[str, Any] = {"versionCodes": [str(version_code)], "status": status}
        if name:
            release["name"] = name
        if user_fraction:
            release["userFraction"] = user_fraction
        if notes:
            release["releaseNotes"] = [{"language": "en-US", "text": notes}]
        self._json_request(
            "PUT",
            self._app_url(package_name, "edits", edit_id, "tracks", track),
            {"track": track, "releases": [release]},
        )

    def commit_edit(self, package_name: str, edit_id: str) -> None:
        self._json_request("POST", self._app_url(package_name, "edits", edit_id, ":commit"), {})

    def delete_edit(self, package_name: str, edit_id: str) -> None:
        try:
            self._request("DELETE", self._app_url(package_name, "edits", edit_id))
        except PlayApiError:
            # Deleting an already-expired edit is the outcome we wanted anyway,
            # and a failure here must not mask the failure that got us here.
            return


# -- the publisher ----------------------------------------------------------


def _refuse_unpublishable(request: PublishRequest) -> Optional[PublisherResult]:
    """The last gate before anything is uploaded to a store.

    Every manifest is re-checked against the approved source and version here as
    well as in the state machine, because this is the last code between a build
    and the outside world and a check that exists only further up is a check a
    future caller can reach past. A plan is exempt: its artifacts are declared
    rather than built, so the invariants a real run insists on are exactly the
    ones it is reporting are not yet established.
    """

    if request.dry_run:
        return None
    for manifest in request.manifests:
        try:
            manifest.assert_publishable(request.source_sha, request.version)
        except ContractError as exc:
            return failed(request.destination, str(exc), code="manifest-not-publishable")
    return None


class GooglePlayPublisher:
    """Publishes a signed Android bundle to one Google Play track.

    The transport is injected, so the whole publisher runs against an in-memory
    Play in a test with no network and no credentials, and a consumer's own
    Play access path can be substituted without this class learning anything
    about it.
    """

    SUPPORTS_DRY_RUN = True
    name = PUBLISHER_NAME

    def __init__(
        self,
        settings: PlaySettings,
        transport: Any,
        *,
        name: str = PUBLISHER_NAME,
        destination: str = DESTINATION,
        sleeper: Any = time.sleep,
    ) -> None:
        if not isinstance(settings, PlaySettings):
            raise PlayError(
                "configuration-invalid",
                f"settings must be PlaySettings, got {type(settings).__name__}",
            )
        if not destination:
            raise PlayError(
                "configuration-invalid",
                "a publisher must name its destination; a result that does not say "
                "where it went cannot be compared with anything",
            )
        self.settings = settings
        self.transport = require_transport(transport)
        self.name = name
        self._destination = destination
        #: Injected so a test can assert the backoff without waiting for it.
        self._sleeper = sleeper

    # -- introspection ----------------------------------------------------
    @property
    def destination(self) -> str:
        return self._destination

    def intent(self) -> str:
        rollout = (
            f"and roll it out to {self.settings.rollout:.0%} of users on the "
            f"{self.settings.track} track"
            if self.settings.staged
            else f"and release it to all users on the {self.settings.track} track"
        )
        return (
            f"upload the signed app bundle for {self.settings.package_name} to the "
            f"{self.settings.track} track of Google Play as a draft release, then "
            f"promote that draft to a completed release {rollout}"
        )

    def _identity(self, version_code: int, *, track: Optional[str] = None) -> str:
        """The identity of a Play release, as something a re-run can recognise.

        A Play release is a `versionCode` on a track: that pair is what Play will
        refuse a second upload against, and therefore what a duplicate event has
        to be able to name. The version alone is not enough, because the same
        code is a legitimate release on a different track.
        """

        return f"{self.settings.package_name}:{track or self.settings.track}:{version_code}"

    # -- inputs -----------------------------------------------------------
    def _bundle(self, request: PublishRequest) -> Tuple[Optional[Artifact], Optional[PublisherResult]]:
        """The one artifact Play will accept, or why there is not one.

        Play accepts an app bundle and nothing else: an APK is not rejected with
        a helpful message, it is rejected as the wrong format for an upload, and
        a release that shipped both and let the publisher choose would be
        choosing by accident. So the bundle is looked up by type and the absence
        of one is reported here rather than discovered as a 400 from the API.
        """

        bundles = [item for item in request.artifacts if item.type == TYPE_PACKAGE]
        if not bundles:
            if not self.settings.require_bundle:
                return None, None
            return None, failed(
                self._destination,
                f"{request.tag} has no app bundle, and Google Play accepts an app "
                "bundle and nothing else. A release configured to publish an APK to a "
                "GitHub Release and a bundle to Play needs both targets declared; one "
                "that has only an APK has no Play release to make.",
                code="bundle-absent",
                details={"artifacts": ",".join(item.name for item in request.artifacts) or "-"},
            )
        if len(bundles) > 1:
            return None, failed(
                self._destination,
                "this release holds "
                + ", ".join(item.name for item in bundles)
                + ". Google Play takes exactly one app bundle per release, so a release "
                "with two cannot be published to it without choosing between them.",
                code="bundle-ambiguous",
                details={"bundles": ",".join(item.name for item in bundles)},
            )
        return bundles[0], None

    def _planned(self, request: PublishRequest, version_code: int, bundle: Optional[Artifact]) -> PublisherResult:
        return skipped(
            self._destination,
            f"would upload {bundle.name if bundle else 'the app bundle'} as version "
            f"code {version_code} to the {self.settings.track} track of "
            f"{self.settings.package_name} and hold it as a draft release"
            + (f", then release it to {self.settings.rollout:.0%} of users" if self.settings.staged else ""),
            identity=self._identity(version_code),
            external_id=f"versionCode={version_code}",
            external_version=request.version,
            code=DRY_RUN_CODE,
            details={
                "track": self.settings.track,
                "version_code": str(version_code),
                "package": self.settings.package_name,
            },
        )

    # -- edit lifecycle ---------------------------------------------------
    def _in_edit(self, operation: Any) -> Any:
        """Run one operation against a fresh edit, recreating the edit if it goes stale.

        Play allows one open edit per app, so an edit left open by a failed run
        blocks the next one. Every edit this method opens is therefore deleted on
        the way out, and an edit Play has invalidated is replaced rather than
        retried: the handle is what is stale, and repeating the request against
        the same dead handle produces the same 410 for as long as the retry budget
        lasts.

        A retry waits before it goes out. Play returns 429 and 503 under load, and
        a tight retry loop turns a blip into a rate-limit ban of the credential it
        is using; the wait is Play's `Retry-After` when it gave one and a doubling
        backoff otherwise. A stale *handle* is the one failure that does not wait,
        because nothing about the new handle is likely to be busier than the old
        one.
        """

        package = self.settings.package_name
        last: Optional[PlayApiError] = None
        for attempt in range(1, self.settings.attempts + 1):
            try:
                edit_id = self.transport.insert_edit(package)
            except PlayApiError as exc:
                if exc.retryable and attempt < self.settings.attempts:
                    last = exc
                    self._wait(exc, attempt)
                    continue
                raise
            try:
                return operation(edit_id)
            except PlayApiError as exc:
                self._discard(edit_id)
                if exc.invalidated_edit and attempt < self.settings.attempts:
                    last = exc
                    continue
                raise
            except Exception:
                self._discard(edit_id)
                raise
        raise last or PlayError("play-call-failed", "the Play edit could not be opened")

    def _wait(self, error: PlayApiError, attempt: int) -> None:
        if error.invalidated_edit:
            return
        seconds = error.retry_after or min(
            RETRY_BACKOFF_CAP, RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
        )
        if seconds > 0:
            self._sleeper(seconds)

    def _discard(self, edit_id: str) -> None:
        try:
            self.transport.delete_edit(self.settings.package_name, edit_id)
        except Exception:  # noqa: BLE001 - cleanup must not mask the real failure
            return

    # -- track inspection -------------------------------------------------
    def _survey(
        self, states: Tuple[TrackState, ...], version_code: int, stage: str = STAGE_DRAFT
    ) -> Optional[PublisherResult]:
        """What the tracks already say about this `versionCode`, if anything.

        Asked before anything is written, because the answers are different
        outcomes and only some of them are failures worth reporting: a live
        release with this code on our own track is a duplicate, the same code on a
        *different* track is a conflict, and no release with this code anywhere
        means the write is safe to attempt. Reading this first is what makes a
        re-run quiet instead of a second upload Play rejects.

        The two stages read the same track differently, because a draft is a
        duplicate to the stage that would upload and the *target* to the stage
        that would promote it. Without the stage, a run that drafted and then
        published would be told at the publish stage that it had nothing to do.
        """

        promoting = stage == STAGE_PUBLISH
        for state in states:
            if state.version_code != version_code:
                continue
            if state.track != self.settings.track:
                if state.live:
                    return failed(
                        self._destination,
                        f"version code {version_code} is already in use on the "
                        f"{state.track} track. A version code may only be used once, so "
                        "this release cannot also go to "
                        f"{self.settings.track} without a higher code.",
                        code="version-code-conflict",
                        identity=self._identity(version_code, track=state.track),
                        external_id=f"versionCode={version_code}",
                        details={"track": state.track, "status": state.status},
                    )
                continue
            if state.status == STATUS_HALTED:
                return failed(
                    self._destination,
                    f"version code {version_code} on the {self.settings.track} track is a "
                    "halted rollout. Play does not resume a halted release; the next build "
                    "has to carry a higher version code.",
                    code="rollout-halted",
                    identity=self._identity(version_code),
                    external_id=f"versionCode={version_code}",
                    details={"track": state.track, "status": state.status},
                )
            if state.status == STATUS_DRAFT:
                if promoting:
                    return None
                return skipped(
                    self._destination,
                    f"version code {version_code} is already drafted on the "
                    f"{self.settings.track} track; there is nothing to upload",
                    identity=self._identity(version_code),
                    external_id=f"versionCode={version_code}",
                    code="already-drafted",
                    details={"track": state.track, "status": state.status},
                )
            if state.status in (STATUS_COMPLETED, STATUS_IN_PROGRESS):
                if (
                    promoting
                    and state.status == STATUS_IN_PROGRESS
                    and state.user_fraction
                    and self.settings.staged
                ):
                    if self.settings.rollout > state.user_fraction:
                        # Play widens a staged rollout on the release already on
                        # the track, so this is a release, not a duplicate.
                        return None
                    return failed(
                        self._destination,
                        f"version code {version_code} is already rolled out to "
                        f"{state.user_fraction:.0%} of users and this destination is "
                        f"configured for {self.settings.rollout:.0%}. Play only widens a "
                        "staged rollout, so releasing to a smaller fraction is refused "
                        "rather than silently ignored; to stop the rollout, halt it.",
                        code="rollout-not-widened",
                        identity=self._identity(version_code),
                        external_id=f"versionCode={version_code}",
                        details={
                            "track": state.track,
                            "current": f"{state.user_fraction:.2f}",
                            "requested": f"{self.settings.rollout:.2f}",
                        },
                    )
                return skipped(
                    self._destination,
                    f"version code {version_code} is already live on the "
                    f"{self.settings.track} track"
                    + (
                        f" to {state.user_fraction:.0%} of users"
                        if state.user_fraction
                        else ""
                    ),
                    identity=self._identity(version_code),
                    external_id=f"versionCode={version_code}",
                    external_version=self.settings.release_name or str(version_code),
                    code="already-published",
                    details={"track": state.track, "status": state.status},
                )
        return None

    def _confirm_upload(self, bundle: Artifact, receipt: UploadReceipt) -> Optional[PublisherResult]:
        """Check that the bytes Play received are the bytes that were built.

        Play echoes the digest and the size of an uploaded bundle. A publisher
        that ignores them has proved only that a request returned 200; a
        truncated upload, or bytes that were substituted between the manifest
        being written and the file being read, both return 200. The digest is
        compared only when Play reports one, because the field was added after
        the original API and an older account legitimately does not have it.
        """

        if receipt.size and receipt.size != bundle.size:
            return failed(
                self._destination,
                f"Play reports {receipt.size} bytes for {bundle.name}, but the manifest "
                f"holds {bundle.size}. The upload was truncated or the file changed "
                "after the manifest was written; the version code is not spent by a "
                "failed upload, so this is safe to re-run.",
                code="upload-size-mismatch",
                retryable=True,
                details={"artifact": bundle.name, "received": str(receipt.size)},
            )
        if receipt.sha256 and receipt.sha256 != bundle.digest:
            return failed(
                self._destination,
                f"Play hashed {bundle.name} to {receipt.sha256}, but the manifest holds "
                f"{bundle.digest}. The bytes that reached the store are not the bytes "
                "this release was approved for, so the upload is abandoned rather "
                "than committed.",
                code="upload-digest-mismatch",
                details={"artifact": bundle.name, "received": receipt.sha256},
            )
        return None

    # -- draft ------------------------------------------------------------
    def draft(self, request: PublishRequest) -> PublisherResult:
        """Upload the bundle and leave it on the track as a draft release.

        This is the stage that spends the `versionCode`, which is why everything
        that has to be true before it is spent is checked here first: the bundle
        exists and is unambiguous, its bytes are the ones the manifest recorded,
        and the code is not already in use. A draft release is invisible to every
        user, so the commit at the end of this stage is a statement that the
        upload is complete rather than a statement that anything was released.
        """

        refusal = _refuse_unpublishable(request)
        if refusal is not None:
            return refusal
        try:
            _, version_code = self.settings.version_binding(request.version)
        except PlayError as exc:
            return failed(self._destination, exc.message, code=exc.code)

        bundle, problem = self._bundle(request)
        if problem is not None:
            return problem
        if request.dry_run:
            return self._planned(request, version_code, bundle)
        if bundle is None:
            return failed(
                self._destination,
                "require_bundle is disabled and this release has no app bundle, so there "
                "is nothing for Play to publish",
                code="bundle-absent",
            )
        if not os.path.isfile(bundle.path):
            return failed(
                self._destination,
                f"{bundle.name} is in the manifest at {bundle.path!r}, which is not a "
                "readable file on this runner. Uploading it would spend a version code "
                "with no bundle behind it.",
                code="bundle-absent",
                retryable=True,
                details={"artifact": bundle.name},
            )

        return self._with_error_mapping(
            self._destination,
            lambda: self._in_edit(
                lambda edit_id: self._draft_in_edit(edit_id, bundle, version_code, request)
            ),
        )

    def _draft_in_edit(
        self, edit_id: str, bundle: Artifact, version_code: int, request: PublishRequest
    ) -> PublisherResult:
        identity = self._identity(version_code)
        duplicate = self._survey(
            self.transport.list_tracks(self.settings.package_name, edit_id),
            version_code,
            STAGE_DRAFT,
        )
        if duplicate is not None:
            # The edit was opened to ask the question and the question is
            # answered; committing an empty edit would only record a no-op.
            self._discard(edit_id)
            return duplicate

        receipt = self.transport.upload_bundle(
            self.settings.package_name, edit_id, bundle.path
        )
        mismatch = self._confirm_upload(bundle, receipt)
        if mismatch is not None:
            self._discard(edit_id)
            return mismatch

        self.transport.update_track(
            self.settings.package_name,
            edit_id,
            self.settings.track,
            STATUS_DRAFT,
            version_code,
            name=self._release_name(request),
            notes=self.settings.release_notes,
        )
        self.transport.commit_edit(self.settings.package_name, edit_id)
        return published(
            self._destination,
            identity,
            external_id=f"versionCode={version_code}",
            external_version=request.version,
            code="drafted",
            details={
                "track": self.settings.track,
                "version_code": str(version_code),
                "bundle": bundle.name,
                "status": STATUS_DRAFT,
                "edit": edit_id,
            },
        )

    def _release_name(self, request: PublishRequest) -> str:
        return self.settings.release_name or f"{self.settings.package_name} {request.version}"

    # -- publish ----------------------------------------------------------
    def publish(self, request: PublishRequest) -> PublisherResult:
        """Promote the drafted release to users, staged or all at once.

        Nothing is uploaded here. The bytes were committed in the draft stage and
        the `versionCode` is already spent, so this stage is a pure state change
        on the track — which is what makes it safe to run twice, safe to resume
        after a failure, and safe to refuse when the draft is not there.
        """

        refusal = _refuse_unpublishable(request)
        if refusal is not None:
            return refusal
        try:
            _, version_code = self.settings.version_binding(request.version)
        except PlayError as exc:
            return failed(self._destination, exc.message, code=exc.code)

        bundle, problem = self._bundle(request)
        if problem is not None:
            return problem
        if request.dry_run:
            return skipped(
                self._destination,
                f"would promote version code {version_code} from a draft release to a "
                f"completed release on the {self.settings.track} track of "
                f"{self.settings.package_name}"
                + (
                    f", rolled out to {self.settings.rollout:.0%} of users"
                    if self.settings.staged
                    else ", released to all users"
                ),
                identity=self._identity(version_code),
                external_id=f"versionCode={version_code}",
                external_version=request.version,
                code=DRY_RUN_CODE,
                details={"track": self.settings.track, "version_code": str(version_code)},
            )

        return self._with_error_mapping(
            self._destination,
            lambda: self._in_edit(
                lambda edit_id: self._promote_in_edit(edit_id, version_code, request)
            ),
        )

    def _promote_in_edit(
        self, edit_id: str, version_code: int, request: PublishRequest
    ) -> PublisherResult:
        identity = self._identity(version_code)
        states = self.transport.list_tracks(self.settings.package_name, edit_id)
        # Read the tracks as the *publishing* stage: a draft with this code is the
        # release being promoted here, and a live one at a wider fraction is a
        # refusal rather than a duplicate to report.
        duplicate = self._survey(states, version_code, STAGE_PUBLISH)
        if duplicate is not None:
            self._discard(edit_id)
            return duplicate

        current = next((state for state in states if state.track == self.settings.track), None)
        if current is None or current.version_code != version_code:
            self._discard(edit_id)
            return failed(
                self._destination,
                f"the {self.settings.track} track holds "
                + (
                    f"version code {current.version_code}"
                    if current is not None
                    else "no release at all"
                )
                + f", not the draft for version code {version_code} this release "
                "drafted. The draft stage has to run first, and nothing has been "
                "released; this is safe to retry.",
                code="draft-absent",
                retryable=True,
                identity=identity,
                external_id=f"versionCode={version_code}",
                details={"track": self.settings.track},
            )

        status = STATUS_IN_PROGRESS if self.settings.staged else STATUS_COMPLETED
        self.transport.update_track(
            self.settings.package_name,
            edit_id,
            self.settings.track,
            status,
            version_code,
            name=self._release_name(request),
            user_fraction=self.settings.rollout if self.settings.staged else 0.0,
        )
        self.transport.commit_edit(self.settings.package_name, edit_id)
        return published(
            self._destination,
            identity,
            external_id=f"versionCode={version_code}",
            external_version=request.version,
            code="published",
            details={
                "track": self.settings.track,
                "version_code": str(version_code),
                "status": status,
                "rollout": f"{self.settings.rollout:.2f}" if self.settings.staged else "1.00",
                "edit": edit_id,
            },
        )

    # -- error mapping ----------------------------------------------------
    def _with_error_mapping(self, destination: str, call: Any) -> PublisherResult:
        """Turn a classified Play failure into a publication result.

        The retryable flag is not a guess here: it is the fact the transport
        reported, so a credential Play rejected and a 503 from the same endpoint
        produce different answers to "would running this again help", which is
        the only question a workflow has time to ask.
        """

        try:
            result = call()
        except PlayError as exc:
            return failed(
                destination,
                exc.message,
                code=exc.code,
                retryable=exc.retryable,
                details=exc.details or None,
            )
        if not isinstance(result, PublisherResult):
            return failed(
                destination,
                f"the publisher produced {type(result).__name__} rather than a "
                "publication result",
                code="result-not-returned",
            )
        return result


__all__ = [
    "API_ROOT",
    "DESTINATION",
    "DRY_RUN_CODE",
    "INVALIDATED_EDIT_CODES",
    "PUBLISHER_NAME",
    "RETRYABLE_CODES",
    "RETRY_BACKOFF_BASE",
    "RETRY_BACKOFF_CAP",
    "STATUS_COMPLETED",
    "STATUS_DRAFT",
    "STATUS_HALTED",
    "STATUS_IN_PROGRESS",
    "SUPPORTED_STATUSES",
    "SUPPORTED_TRACKS",
    "TRACK_INTERNAL",
    "TRACK_PRODUCTION",
    "TRANSPORT_SURFACE",
    "UPLOAD_API_ROOT",
    "GooglePlayPublisher",
    "HttpPlayTransport",
    "InMemoryPlayTransport",
    "PlayApiError",
    "PlayError",
    "PlaySettings",
    "PlayTransport",
    "TrackState",
    "UploadReceipt",
    "parse_settings",
    "require_transport",
]
