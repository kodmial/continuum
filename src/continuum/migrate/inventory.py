"""What the consumer repository is *now*, read before anything is changed.

The cutover gate in :mod:`continuum.shadow.cutover` judges evidence. This module
supplies the one fact no shadow window can: the shape of the repository the
evidence was gathered from. Without it a controller would be planning a change
against an assumption, and an assumption about which files implement which writer
is exactly the thing a cutover must not make.

Four things are read, and each one exists because a later stage would otherwise
be guessing:

* **The default-branch HEAD**, its workflow inventory and its open pull requests.
  Reused from :mod:`continuum.shadow.baseline` rather than reimplemented, so the
  controller and the gate cannot end up with two different ideas of "now". The
  HEAD is also the rollback target: it is the last revision at which the legacy
  writers are the sole writers, and a cutover with no way back is refused.

* **Every ``kodmial/continuum/...@<ref>`` reference in the consumer.** Their
  ``ref`` is recorded, and whether it is a full commit SHA is recorded with it.
  A consumer already calling Continuum from ``@main`` is running code nobody
  reviewed, and the inventory has to be able to say so rather than have the
  planner quietly fix it.

* **Repository variables and secret *names*.** #84 is explicit that the
  migration must not require new repository variables, that the existing
  values must be preserved, and that behaviour must not change silently. So the
  inventory carries the current values forward into the plan. Secrets are
  recorded as names and a boolean. GitHub's API never returns a secret value,
  so demanding one would be asking for something that cannot be provided and
  storing one would be a new leak; the boolean is the whole of what exists.

* **The reviewed writer roles**, from the parity ledger. Never inferred from a
  filename -- see the package docstring.

Every read is attempted even after one fails, because a migration report that
names the first error is less use than one that names every gap. What it will
never do is paper over a gap: an unread read sets :attr:`Inventory.complete` to
false, and no preflight may report READY on an incomplete inventory.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..shadow import baseline

INVENTORY_SCHEMA = "continuum.migration-inventory/v1"

#: The consumer-facing configuration document: two booleans, per
#: ``docs/continuum-mvp-contract.md``. Distinct from ``.continuum.yml``, which is
#: Continuum's own engine configuration and is not a consumer artifact.
CONSUMER_CONFIG_PATH = ".github/continuum.yml"

#: The repository whose reusable workflows and engine are the implementation
#: source. A plan that generated a caller into any other repository would be a
#: second implementation source, which is the failure the whole architecture
#: exists to prevent, so it is a constant and not a parameter.
CONTINUUM_REPOSITORY = "kodmial/continuum"

#: The client methods the inventory reads. Listed rather than discovered so that
#: adding a *write* method to the client cannot make this read-write by
#: accident: a method that is not named here is not called here.
#:
#: ``file_at_ref`` is inherited from the review client and is deliberately *not*
#: on this list. It returns ``None`` for a 403 and a 500 as readily as for a 404,
#: and an inventory that cannot tell "absent" from "unknown" is not an inventory:
#: it would plan on top of a reading it never took.
READ_METHODS: Tuple[str, ...] = (
    "default_branch",
    "ref_sha",
    "workflow_inventory",
    "list_pulls",
    "list_pull_files",
    "list_repo_variables",
    "list_repo_secret_names",
    "list_labels",
    "read_file_at_ref",
)

#: What a workflow is, with respect to this cutover. ``kind`` is derived from the
#: reviewed ledger and the file's own references, never from its name alone.
KIND_CONTINUUM_CALLER = "continuum-caller"
KIND_LEGACY_WRITER = "legacy-writer"
KIND_SHADOW_BRIDGE = "shadow-bridge"
KIND_PRODUCT = "product"

_COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: A reusable-workflow reference, wherever it appears. Anchored on the repository
#: so a consumer's reference to some *other* project's workflow is not mistaken
#: for a Continuum pin.
_CONTINUUM_REF = re.compile(
    r"{}/(?P<path>[A-Za-z0-9._/-]+)@(?P<ref>[A-Za-z0-9._/-]+)".format(
        re.escape(CONTINUUM_REPOSITORY)
    )
)

#: The prefix of every generated Continuum caller. Continuum owns these files: a
#: product repository never hand-edits one, because the next reconciliation
#: would revert the edit and the revert would look like a product regression.
GENERATED_PREFIX = ".github/workflows/continuum-"

#: The one file Continuum owns that is *not* under ``workflows/``, because it is
#: configuration rather than a generated caller.
GENERATED_CONFIG_PATH = CONSUMER_CONFIG_PATH

#: The record a cutover leaves naming the revision it can be undone to. Named
#: here rather than imported from the rollback module so that the inventory has a
#: single, dependency-free statement of which paths Continuum owns; the rollback
#: module reads the constant from here for the same reason.
CUTOVER_RECORD_PATH = ".github/continuum-cutover.json"

#: The bridge is generated too, but it is read-only and survives the cutover, so
#: it is named separately rather than swept up with the writers.
SHADOW_BRIDGE_PATH = ".github/workflows/continuum-shadow-bridge.yml"


def git_blob_sha(content: str) -> str:
    """The git object name for ``content``, computed the way git computes it.

    Lives here rather than in :mod:`continuum.migrate.plan` because both halves
    need it and neither may disagree about it: a reconciliation that compared a
    rendered file against a recorded blob using a different hash function would
    rewrite a correct file on every run and call that progress.
    """

    payload = content.encode("utf-8")
    header = "blob {}\0".format(len(payload)).encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


class MigrationError(ValueError):
    """The migration cannot proceed from what it was given."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class ContinuumPin:
    """One reference from the consumer into Continuum, and how exact it is."""

    path: str
    ref: str

    @property
    def immutable(self) -> bool:
        """Whether this reference names code that cannot move underneath it."""

        return bool(_COMMIT.match(self.ref))

    def describe(self) -> Dict[str, Any]:
        return {"path": self.path, "ref": self.ref, "immutable": self.immutable}


def parse_pins(text: str) -> Tuple[ContinuumPin, ...]:
    """Every Continuum reference in a file, in order, with duplicates collapsed.

    Order-preserving and deterministic because a plan rendered from these has to
    be byte-stable: re-running a completed cutover must produce no change at all,
    and "no change" is only provable if the same inputs render the same bytes.
    """

    seen: Dict[Tuple[str, str], ContinuumPin] = {}
    for match in _CONTINUUM_REF.finditer(text or ""):
        key = (match.group("path"), match.group("ref"))
        seen.setdefault(key, ContinuumPin(path=key[0], ref=key[1]))
    return tuple(seen[key] for key in sorted(seen))


@dataclasses.dataclass(frozen=True)
class Workflow:
    """One active workflow, and what the reviewed audit says it implements."""

    path: str
    blob_sha: str = ""
    #: From the parity ledger. Empty when the audit classifies no writer for this
    #: path, which is a finding rather than a default: an unclassified path has
    #: no role, so nothing may be planned against it.
    writer: str = ""
    classification: str = ""
    kind: str = KIND_PRODUCT
    #: The workflow's ``name:``, which is not its filename. GitHub matches
    #: ``workflow_run.workflows`` against the display name, so a generated caller
    #: that has to react to a consumer's CI run must name the display name, and
    #: guessing from the filename would silently never fire.
    display_name: str = ""
    pins: Tuple[ContinuumPin, ...] = ()

    @property
    def floating_pins(self) -> Tuple[ContinuumPin, ...]:
        return tuple(pin for pin in self.pins if not pin.immutable)

    def describe(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "blob_sha": self.blob_sha,
            "writer": self.writer,
            "classification": self.classification,
            "kind": self.kind,
            "display_name": self.display_name,
            "pins": [pin.describe() for pin in self.pins],
        }


@dataclasses.dataclass(frozen=True)
class ConsumerConfig:
    """The consumer's declared capability switches, as they are today.

    Read rather than assumed because the plan writes them back. A cutover that
    guessed ``review: true`` for a repository that had ``false`` would switch on
    a review loop nobody asked for, and that is a behaviour change disguised as a
    migration.
    """

    present: bool = False
    review: Optional[bool] = None
    release: Optional[bool] = None
    #: sha256 of the bytes, so a report can say whether the document moved without
    #: carrying it.
    digest: str = ""
    #: The git blob SHA, which is what a reconciliation compares against. The
    #: sha256 above would work equally well for detecting change but would not
    #: match the blob an audit entry records, and the audit entry is the thing
    #: that has to line up.
    blob_sha: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "present": self.present,
            "review": self.review,
            "release": self.release,
            "digest": self.digest,
            "blob_sha": self.blob_sha,
        }


@dataclasses.dataclass(frozen=True)
class Inventory:
    """One reading of the consumer, with every gap named."""

    repository: str
    default_branch: str = ""
    head_sha: str = ""
    workflows: Tuple[Workflow, ...] = ()
    variables: Mapping[str, str] = dataclasses.field(default_factory=dict)
    #: Secret name -> whether the repository has it. Never a value: GitHub does
    #: not expose one, and a value that reached here would be a new leak.
    secrets: Mapping[str, bool] = dataclasses.field(default_factory=dict)
    labels: Tuple[str, ...] = ()
    consumer_config: ConsumerConfig = dataclasses.field(default_factory=ConsumerConfig)
    #: Blob SHA of the cutover record, when one is present. Read for the same
    #: reason the config is: a repository that has already been cut over has a
    #: rollback target on its default branch, and a controller that rewrites that
    #: record instead of reading it is destroying the only local copy of where the
    #: cutover can be undone to.
    record_blob_sha: str = ""
    open_pull_requests: Tuple[baseline.OpenPullRequest, ...] = ()
    complete: bool = True
    limits: Tuple[str, ...] = ()

    # -- lookups ----------------------------------------------------------- #

    @property
    def workflow_map(self) -> Dict[str, Workflow]:
        return {entry.path: entry for entry in self.workflows}

    @property
    def legacy_writers(self) -> Tuple[Workflow, ...]:
        return tuple(entry for entry in self.workflows if entry.kind == KIND_LEGACY_WRITER)

    @property
    def floating_pins(self) -> Tuple[Workflow, ...]:
        return tuple(entry for entry in self.workflows if entry.floating_pins)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": INVENTORY_SCHEMA,
            "repository": self.repository,
            "default_branch": self.default_branch,
            "head_sha": self.head_sha,
            "complete": self.complete,
            "limits": list(self.limits),
            "workflows": [entry.describe() for entry in self.workflows],
            "variables": dict(sorted(self.variables.items())),
            "secrets": dict(sorted(self.secrets.items())),
            "labels": list(self.labels),
            "consumer_config": self.consumer_config.describe(),
            "record_blob_sha": self.record_blob_sha,
            "open_pull_requests": [
                entry.describe() for entry in self.open_pull_requests
            ],
        }

    @property
    def digest(self) -> str:
        """A digest of the reading, so a plan can be bound to the state it read.

        The rollback target, the writer roles and the floating pins are all in
        it, which is the point: a plan built against one reading must not be
        applied to another.
        """

        payload = {
            "repository": self.repository,
            "default_branch": self.default_branch,
            "head_sha": self.head_sha,
            "workflows": sorted(
                (entry.path, entry.blob_sha, entry.writer, entry.kind)
                for entry in self.workflows
            ),
            "variables": sorted(self.variables.items()),
            "secrets": sorted(self.secrets.items()),
            "labels": sorted(self.labels),
            "consumer_config": self.consumer_config.describe(),
            "record_blob_sha": self.record_blob_sha,
        }
        return "inv-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()[:16]


def read_inventory(document: Mapping[str, Any]) -> Inventory:
    """Read an inventory back from an artifact, refusing an unknown document.

    The rollout is a persistent reconciliation rather than a one-shot script, so
    a later run has to be able to pick up what an earlier one recorded. A
    document this module cannot vouch for is refused instead of coerced: a
    coerced inventory would be a plan built against values nobody recorded.
    """

    if not isinstance(document, Mapping):
        raise MigrationError("inventory_not_a_mapping", "an inventory must be an object")
    schema = document.get("schema")
    if schema != INVENTORY_SCHEMA:
        raise MigrationError(
            "unknown_inventory_schema",
            "expected {!r}, found {!r}".format(INVENTORY_SCHEMA, schema),
        )
    repository = str(document.get("repository", ""))
    if not repository.strip():
        raise MigrationError("inventory_repository_missing", "an inventory must name its repository")

    workflows: List[Workflow] = []
    for entry in document.get("workflows", []) or []:
        if not isinstance(entry, Mapping):
            raise MigrationError("inventory_workflow_not_a_mapping", "each workflow must be an object")
        workflows.append(
            Workflow(
                path=str(entry.get("path", "")),
                blob_sha=str(entry.get("blob_sha", "")),
                writer=str(entry.get("writer", "")),
                classification=str(entry.get("classification", "")),
                kind=str(entry.get("kind", KIND_PRODUCT)),
                display_name=str(entry.get("display_name", "")),
                pins=tuple(
                    ContinuumPin(path=str(pin.get("path", "")), ref=str(pin.get("ref", "")))
                    for pin in entry.get("pins", []) or []
                    if isinstance(pin, Mapping)
                ),
            )
        )

    config = document.get("consumer_config") or {}
    if not isinstance(config, Mapping):
        raise MigrationError("inventory_config_not_a_mapping", "consumer_config must be an object")

    def _optional_bool(value: Any) -> Optional[bool]:
        return None if value is None else bool(value)

    return Inventory(
        repository=repository,
        default_branch=str(document.get("default_branch", "")),
        head_sha=str(document.get("head_sha", "")),
        workflows=tuple(sorted(workflows, key=lambda item: item.path)),
        variables={str(key): str(value) for key, value in (document.get("variables") or {}).items()},
        secrets={
            str(key): bool(value) for key, value in (document.get("secrets") or {}).items()
        },
        labels=tuple(str(item) for item in document.get("labels", []) or []),
        record_blob_sha=str(document.get("record_blob_sha", "") or ""),
        consumer_config=ConsumerConfig(
            present=bool(config.get("present", False)),
            review=_optional_bool(config.get("review")),
            release=_optional_bool(config.get("release")),
            digest=str(config.get("digest", "")),
            blob_sha=str(config.get("blob_sha", "")),
        ),
        open_pull_requests=tuple(
            baseline.read_live_head(
                {
                    "open_pull_requests": document.get("open_pull_requests", []) or [],
                }
            ).open_pull_requests
        ),
        complete=bool(document.get("complete", True)),
        limits=tuple(str(item) for item in document.get("limits", []) or []),
    )


# --------------------------------------------------------------------------- #
# The reading
# --------------------------------------------------------------------------- #


def capture_inventory(
    client: Any,
    *,
    repository: str = "",
    ledger: Optional["baseline.ParityLedger"] = None,
    max_pull_requests: int = 100,
) -> Inventory:
    """Take the reading.

    ``client`` is a ``continuum.review.github.GitHubClient`` (or the
    migration client's superset of it) holding a token with **read** permission
    over the consumer. The head half is delegated to
    :func:`continuum.shadow.baseline.capture_live_head` so that the controller
    and the gate are reading the same thing the same way; the two disagreeing
    about "the current head" would be a bug that only shows up during a cutover.

    ``ledger`` is the reviewed audit. It is a parameter rather than a constant
    because the controller is a generic product and the consumer under
    installation is data; passing the wrong ledger is caught by
    :func:`continuum.migrate.preflight.evaluate`, which refuses a ledger that
    audits a different repository.
    """

    repository = repository or getattr(client, "repository", "") or ""
    live = baseline.capture_live_head(
        client, repository=repository, max_pull_requests=max_pull_requests
    )

    limits: List[str] = list(live.limits)
    variables = _read_variables(client, limits)
    secrets = _read_secret_names(client, limits)
    labels = _read_labels(client, limits)
    config = _read_consumer_config(
        client, live.default_branch or live.head_sha, limits
    )
    record_blob = _read_optional_blob(client, CUTOVER_RECORD_PATH, live.default_branch, limits)

    reviewed = ledger.workflow_map if ledger is not None else {}
    workflows = [
        _describe_workflow(
            Workflow(path=path, blob_sha=sha),
            reviewed.get(path),
            _read_text(client, path, live.default_branch, limits),
        )
        for path, sha in sorted(live.workflow_map.items())
    ]

    return Inventory(
        repository=repository,
        default_branch=live.default_branch,
        head_sha=live.head_sha,
        workflows=tuple(workflows),
        variables=variables,
        secrets=secrets,
        labels=labels,
        consumer_config=config,
        record_blob_sha=record_blob,
        open_pull_requests=live.open_pull_requests,
        complete=not limits,
        limits=tuple(limits),
    )


def _read(client: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Call a read method, having checked that it is on this module's allowlist.

    Same reason as :func:`continuum.shadow.capture._read`: the value of an
    allowlist is that a new client method is a *write* until it is proved
    otherwise, and it is only "new" at the moment something calls it.
    """

    if name not in READ_METHODS:
        raise MigrationError(
            "write_capable_read",
            "{} is not on the inventory read allowlist, so the migration controller "
            "may not call it during inventory.".format(name),
        )
    method = getattr(client, name, None)
    if method is None:
        raise MigrationError(
            "missing_read_method", "the client has no {}.".format(name)
        )
    return method(*args, **kwargs)


def _read_text(client: Any, path: str, ref: str, limits: List[str]) -> str:
    try:
        return str(_read(client, "read_file_at_ref", path, ref) or "")
    except Exception as error:  # noqa: BLE001 - a limit, not a crash
        limits.append("{} could not be read: {}".format(path, error))
        return ""


def _read_optional_blob(
    client: Any, path: str, ref: str, limits: List[str]
) -> str:
    """The blob SHA at ``path``, or ``""`` when the file is not there.

    An absent file is not a limit, so a first cutover is not reported as an
    incomplete inventory because nothing has been written yet. A *failed* read is
    a limit, because that is a controller that cannot tell a repository with no
    cutover record from a repository whose cutover record it failed to see -- and
    the second is a repository it might overwrite.
    """

    try:
        text = _read(client, "read_file_at_ref", path, ref)
    except Exception as error:  # noqa: BLE001 - a limit, not a crash
        limits.append("{} could not be read: {}".format(path, error))
        return ""
    if text is None:
        return ""
    return git_blob_sha(str(text))


def _read_variables(client: Any, limits: List[str]) -> Dict[str, str]:
    try:
        return {
            str(key): str(value)
            for key, value in (_read(client, "list_repo_variables") or {}).items()
        }
    except Exception as error:  # noqa: BLE001
        limits.append("repository variables could not be read: {}".format(error))
        return {}


def _read_secret_names(client: Any, limits: List[str]) -> Dict[str, bool]:
    try:
        return {str(name): True for name in (_read(client, "list_repo_secret_names") or ())}
    except Exception as error:  # noqa: BLE001
        limits.append("repository secret names could not be read: {}".format(error))
        return {}


def _read_labels(client: Any, limits: List[str]) -> Tuple[str, ...]:
    try:
        return tuple(sorted(str(name) for name in (_read(client, "list_labels") or ())))
    except Exception as error:  # noqa: BLE001
        limits.append("repository labels could not be read: {}".format(error))
        return ()


def _read_consumer_config(
    client: Any, ref: str, limits: List[str]
) -> ConsumerConfig:
    try:
        text = _read(client, "read_file_at_ref", CONSUMER_CONFIG_PATH, ref)
    except Exception as error:  # noqa: BLE001
        limits.append("{} could not be read: {}".format(CONSUMER_CONFIG_PATH, error))
        text = None

    if text is None:
        # An absent file is a legitimate reading and is not a limit. The contract
        # says a missing document resolves to both toggles off, so the plan may
        # create it; what must not happen is a controller that cannot tell an
        # absent file from an unread one.
        return ConsumerConfig(present=False)

    body = str(text)
    switches = _consumer_switches(body)
    return ConsumerConfig(
        present=True,
        review=switches[0],
        release=switches[1],
        digest="cfg-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
        blob_sha=git_blob_sha(body),
    )


def _consumer_switches(body: str) -> Tuple[Optional[bool], Optional[bool]]:
    """Read the two contract booleans out of a consumer configuration document.

    A tolerant line reader rather than a YAML load, and the reason is specific:
    the consumer contract file is allowed to carry comments explaining intent, it
    is not allowed to grow a schema, and a consumer that has written
    ``review: true`` in a way a full parser dislikes should still get a correct
    answer from the controller rather than a crash mid-cutover. Anything other
    than ``true``/``false`` on those two keys is reported as ``None`` -- unknown,
    not false -- so the preflight refuses instead of silently switching review
    off.
    """

    found: Dict[str, Optional[bool]] = {"review": None, "release": None}
    for raw in (body or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key not in found or found[key] is not None:
            continue
        value = value.strip().strip("'\"").lower()
        if value in ("true", "yes"):
            found[key] = True
        elif value in ("false", "no"):
            found[key] = False
    return found["review"], found["release"]


def _describe_workflow(
    workflow: Workflow,
    entry: Optional["baseline.LedgerEntry"],
    text: str,
) -> Workflow:
    """Classify one workflow from the reviewed audit and its own references.

    The order matters and is the reason this is a function rather than three
    comprehensions. A *generated caller* is identified by referencing Continuum,
    not by its filename, because a hand-written file that calls a Continuum
    reusable workflow is a caller for the purposes of this cutover whether or not
    anybody generated it, and a file that Continuum generates is not a legacy
    writer however much it looks like one. The bridge is identified by role, for
    the same reason: it is read-only and survives every phase.

    ``kind`` is only ever a *hint* about how to treat the path. Whether a writer
    may be retired in a given phase is decided against ``writer``, which comes
    from the ledger.
    """

    pins = parse_pins(text)
    writer = entry.writer if entry is not None else ""
    classification = entry.classification if entry is not None else ""

    if pins:
        kind = KIND_CONTINUUM_CALLER
    elif workflow.path == SHADOW_BRIDGE_PATH:
        kind = KIND_SHADOW_BRIDGE
    elif writer and writer != baseline.WRITER_OTHER:
        kind = KIND_LEGACY_WRITER
    else:
        kind = KIND_PRODUCT

    return dataclasses.replace(
        workflow,
        writer=writer,
        classification=classification,
        kind=kind,
        display_name=_display_name(text),
        pins=pins,
    )


def _display_name(text: str) -> str:
    """The workflow's ``name:``, which is what GitHub matches ``workflow_run`` on.

    A workflow with no ``name:`` reads as its filename to GitHub. Returning the
    filename in that case is therefore correct rather than a guess, and it is the
    only way a generated caller can be checked against the consumer's own CI
    workflows at all.
    """

    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0]
        # Column zero only: a job's `name:` is nested and indented, and reading
        # one of those would give a display name GitHub never matches against.
        if not line.startswith("name:"):
            continue
        return line[len("name:") :].strip().strip("'\"")
    return ""
