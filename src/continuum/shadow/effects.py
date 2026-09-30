"""The effect boundary: every external mutation is an ``Effect``, and in shadow
mode every ``Effect`` is recorded instead of performed.

Continuum's decision engines do not talk to GitHub. They compute a plan, and
somewhere between the plan and the world there is a step that acts on it. In
production that step is a ``gh`` call, a workflow dispatch, a ``git push``, a
store upload. This module is the vocabulary for that step, so the same decision
engine can be pointed at a recorder instead of at the world.

The point is not to be tidy. It is that a shadow run which "must not write" is
only a claim if there is something in the path that *cannot* write. A shadow
execution of the real ``queue_controller.reconcile`` will call
``client.create_issue_comment`` and ``client.dispatch_workflow`` because that is
what the production path does. The only question is which object receives those
two calls. Here it is :class:`RecordingEffects`, which cannot write, and a
registry that fails closed when it meets a mutating adapter it does not know.

Three properties make the boundary worth having:

* **Closed.** :data:`EFFECT_KINDS` is the whole vocabulary. A mutation that is
  not named there cannot be recorded, and therefore cannot be shadowed -- so a
  new mutating adapter has to be classified before shadow mode can run at all.
* **Exhaustive by construction.** :func:`unmapped_mutators` reads the real
  client classes and reports every mutating method the registry does not cover,
  so "shadow mode cannot invoke a mutating adapter it does not know about" is a
  computed fact rather than a review opinion.
* **Denied, not deferred.** There is no live sink in this package. Shadow mode
  has exactly one implementation of :class:`EffectSink`, and it performs no
  external call, so a shadow run cannot acquire write authority by choosing a
  different sink.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

#: The schema recorded in every journal entry, so a comparison tool can refuse
#: a document it does not understand instead of mis-reading it.
EFFECT_SCHEMA = "continuum.shadow-effect/v1"


class WriteBarrierViolation(RuntimeError):
    """A write-capable path reached for an external mutation in shadow mode.

    Raised for two structurally different situations, and both are failures:

    * code asked the effect layer to perform a mutation the shadow sink cannot
      perform (:class:`RecordingEffects` raises rather than pretending), and
    * code reached around the effect layer entirely -- see
      :mod:`continuum.shadow.barrier`.
    """


# --------------------------------------------------------------------------- #
# The closed vocabulary
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class EffectSpec:
    """One named external mutation and where in Continuum it can happen.

    ``adapters`` names the concrete call sites that perform it. They are not
    documentation: :func:`unmapped_mutators` uses them to decide whether the
    registry still describes the engine, and
    ``tests/test_shadow_effects.py`` asserts each one still exists.
    """

    kind: str
    surface: str
    adapters: Tuple[str, ...]
    #: The concrete class attribute or method that performs this effect, when
    #: the engine reaches it through a client object rather than a ``gh`` verb.
    attribute: str = ""
    #: Further attributes that can reach this same effect. A method that
    #: performs *one of several* effects depending on observed state -- an
    #: upsert, which creates when there is nothing to update and updates when
    #: there is -- names its alternatives here rather than being left
    #: uncovered, because leaving it uncovered would mean the coverage check
    #: reports a false gap and somebody would eventually silence the check.
    alt_attributes: Tuple[str, ...] = ()

    @property
    def attributes(self) -> Tuple[str, ...]:
        if self.attribute and self.alt_attributes:
            return (self.attribute,) + self.alt_attributes
        return self.attribute, self.alt_attributes

    def describe(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "surface": self.surface,
            "adapters": list(self.adapters),
            "attribute": self.attribute,
            "alt_attributes": list(self.alt_attributes),
        }


#: Every external mutation Continuum can perform, grouped by the surface that
#: owns it. The list is the issue's forbidden set plus the ones the engine
#: already had, and it is deliberately longer than the engine: a category with
#: no current call site is still classified, because "we do not do this yet" is
#: a fact that changes and the shadow boundary must not have to be rewritten
#: when it does.
EFFECT_KINDS: Tuple[EffectSpec, ...] = (
    # -- pull request / issue lifecycle -----------------------------------
    EffectSpec(
        "pull.merge",
        "repository",
        ("gh pr merge", "gh api --method PUT /pulls/{n}/merge"),
    ),
    EffectSpec("issue.close", "repository", ("gh issue close", "gh api --method PATCH /issues/{n}")),
    EffectSpec("issue.reopen", "repository", ("gh issue reopen", "gh api --method PATCH /issues/{n}")),
    EffectSpec("pr.create", "repository", ("gh pr create",)),
    # -- comments ----------------------------------------------------------
    EffectSpec("comment.create", "github", ("GitHubClient.create_issue_comment", "GitHubClient.upsert_marker_comment"), "create_issue_comment", ("upsert_marker_comment",)),
    EffectSpec("comment.update", "github", ("GitHubClient.update_issue_comment", "GitHubClient.upsert_marker_comment"), "update_issue_comment", ("upsert_marker_comment",)),
    EffectSpec("comment.delete", "github", ("GitHubClient.delete_issue_comment",), "delete_issue_comment"),
    # -- labels, assignees, milestones -------------------------------------
    EffectSpec("label.add", "github", ("GitHubClient.add_labels", "gh issue edit --add-label"), "add_labels"),
    EffectSpec("label.remove", "github", ("GitHubClient.remove_label", "gh issue edit --remove-label"), "remove_label"),
    EffectSpec("label.create", "repository", ("gh label create",)),
    EffectSpec("assignee.set", "repository", ("gh issue edit --add-assignee",)),
    EffectSpec("milestone.set", "repository", ("gh issue edit --milestone",)),
    # -- refs, releases, stores --------------------------------------------
    EffectSpec("ref.push", "git", ("GitPackageRepository.write", "git push"), "write"),
    EffectSpec("ref.create", "repository", ("git branch", "git update-ref")),
    EffectSpec("ref.delete", "git", ("GitPackageRepository.cleanup", "git push --delete"), "cleanup"),
    EffectSpec("tag.create", "git", ("git tag", "gh release create --target")),
    EffectSpec("release.create", "release", ("GitHubReleasePublisher.draft",), "draft"),
    EffectSpec("release.asset.upload", "release", ("GitHubReleasePublisher._complete_draft",), "_complete_draft"),
    EffectSpec("release.attest", "release", ("GitHubReleasePublisher._attest",), "_attest"),
    EffectSpec("release.publish", "release", ("GitHubReleasePublisher.publish",), "publish"),
    EffectSpec("store.upload", "release", ("GooglePlayPublisher.draft", "HttpPlayTransport._request")),
    EffectSpec("store.track.update", "release", ("GooglePlayPublisher.publish",)),
    EffectSpec("package.publish", "release", ("MavenCentralPublisher.publish", "GitHubPackagesPublisher.publish")),
    # -- dispatch ----------------------------------------------------------
    EffectSpec("workflow.dispatch", "github", ("GitHubClient.dispatch_workflow", "gh workflow run"), "dispatch_workflow"),
    EffectSpec("repo.dispatch", "repository", ("gh api --method POST /dispatches",)),
    # -- review surfaces ---------------------------------------------------
    EffectSpec("review.create", "github", ("GitHubClient.create_review",), "create_review"),
    EffectSpec("status.create", "github", ("GitHubClient.create_status",), "create_status"),
    EffectSpec("thread.resolve", "github", ("GitHubClient.resolve_review_thread",), "resolve_review_thread"),
    # -- settings and secrets ----------------------------------------------
    EffectSpec("secret.set", "settings", ("gh secret set",)),
    EffectSpec("variable.set", "settings", ("gh variable set",)),
    EffectSpec("settings.update", "settings", ("gh api --method PATCH /repos/{owner}/{repo}",)),
    EffectSpec("branch_protection.set", "settings", ("gh api --method PUT /branches/{b}/protection",)),
    # -- the runner itself --------------------------------------------------
    EffectSpec("process.spawn", "runner", ("subprocess.run", "subprocess.Popen", "os.system")),
    EffectSpec("workspace.delete", "runner", ("shutil.rmtree", "os.remove")),
)

#: ``kind -> spec``, built once so a lookup cannot half-succeed.
EFFECT_REGISTRY: Dict[str, EffectSpec] = {spec.kind: spec for spec in EFFECT_KINDS}

#: The kinds in a stable order. Reports and safety tests iterate this, so the
#: order must not depend on dict iteration or on the registry's source order
#: changing meaning.
EFFECT_ORDER: Tuple[str, ...] = tuple(sorted(EFFECT_REGISTRY))


def blocked_effect_kinds() -> Tuple[str, ...]:
    """Every effect kind a shadow run must refuse to perform.

    The safety test asserts this is the whole registry, because a kind missing
    from this list is a kind a shadow run would have performed.
    """

    return EFFECT_ORDER


def effect_spec(kind: str) -> EffectSpec:
    """The spec for ``kind``.

    Raises :class:`WriteBarrierViolation` for an unknown kind rather than
    returning a default: an unclassified mutation is a hole in the boundary,
    and the only safe response to a hole is to stop.
    """

    try:
        return EFFECT_REGISTRY[kind]
    except KeyError:
        raise WriteBarrierViolation(
            "{!r} is not a classified external mutation; shadow mode refuses to "
            "run with an unknown effect kind.".format(kind)
        ) from None


# --------------------------------------------------------------------------- #
# The recorded effect
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Effect:
    """One mutation the decision engine decided to make.

    ``target`` is the thing acted on (``pr:12``, ``issue:87``, ``ref:main``,
    ``store:play/internal``) and ``detail`` holds the deterministic fields that
    identify *which* mutation it is. Prose -- a comment body, a commit message
    -- goes in ``detail["text"]`` and is excluded from parity comparison by
    :mod:`continuum.shadow.parity`, because two implementations writing
    differently-worded comments have still made the same decision.
    """

    kind: str
    target: str = ""
    detail: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    #: Set by the recorder. True means "the decision engine wanted this and
    #: shadow mode performed none of it".
    suppressed: bool = True
    reason: str = "shadow mode performs no external mutation"

    def __post_init__(self) -> None:
        # Validating in the constructor means an unclassified effect cannot
        # even be constructed, let alone recorded.
        effect_spec(self.kind)

    @property
    def signature(self) -> Tuple[str, str, Tuple[Tuple[str, str], ...]]:
        """The identity used for parity: kind, target, and semantic detail.

        ``text`` is excluded on purpose. A comment body is prose, and prose is
        where two correct implementations differ; the decision is "comment on
        PR 12", not "comment on PR 12 with these exact words".
        """

        semantic = tuple(
            sorted(
                (str(key), _scalar(value))
                for key, value in self.detail.items()
                if key not in _PROSE_DETAIL_KEYS
            )
        )
        return (self.kind, self.target, semantic)

    def describe(self) -> Dict[str, Any]:
        """The artifact form, which is *not* the comparison form.

        Detail values keep their structure here, because the artifact has to
        round-trip: a capture read back from this document has to rebuild the
        same effect, and flattening a list of labels into ``"a,b"`` on the way
        out would mean the document could not be distinguished from a label
        whose name contains a comma. The flattened, order-stable form is
        :attr:`signature`, and that is what parity compares.
        """

        return {
            "schema": EFFECT_SCHEMA,
            "kind": self.kind,
            "surface": effect_spec(self.kind).surface,
            "target": self.target,
            "detail": {str(key): _jsonable(value) for key, value in sorted(self.detail.items())},
            "suppressed": self.suppressed,
            "reason": self.reason,
        }


def effect_from_payload(payload: Mapping[str, Any]) -> Effect:
    """Rebuild an effect from its artifact form.

    The counterpart of :meth:`Effect.describe`. It exists because the comparison
    and replay planes read journals that a *different process* wrote: without
    this, a journal read back from disk would arrive with no actions, and every
    artifact-based parity comparison would report the same missing merge,
    dispatch, and comment -- a plane that only ever finds differences because it
    cannot see its own evidence.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("effect_not_a_mapping: an effect must be an object")
    schema = payload.get("schema")
    if schema != EFFECT_SCHEMA:
        raise ValueError(
            "unknown_effect_schema: expected {!r}, found {!r}".format(EFFECT_SCHEMA, schema)
        )
    kind = str(payload.get("kind", ""))
    detail = payload.get("detail", {})
    return Effect(
        kind=kind,
        target=str(payload.get("target", "")),
        detail={str(key): value for key, value in (detail or {}).items()}
        if isinstance(detail, Mapping)
        else {},
        suppressed=bool(payload.get("suppressed", True)),
        reason=str(payload.get("reason", "")),
    )


def effects_from_payload(documents: Sequence[Mapping[str, Any]]) -> Tuple[Effect, ...]:
    """Rebuild a journal's action list, skipping nothing."""

    return tuple(effect_from_payload(entry) for entry in documents or ())


#: Detail keys that carry prose rather than decision content. Excluded from
#: :attr:`Effect.signature` so wording differences classify as explainable
#: rather than as a missing or extra action.
_PROSE_DETAIL_KEYS = frozenset({"text", "body", "message", "title", "reason", "marker"})


def _jsonable(value: Any) -> Any:
    """A JSON-native rendering that keeps structure.

    Tuples become lists and everything else is passed through, so
    ``describe()`` output parses back into an equal effect.
    """

    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def _scalar(value: Any) -> str:
    """Render a detail value deterministically."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    if isinstance(value, Mapping):
        return ";".join(
            "{}={}".format(key, _scalar(value[key])) for key in sorted(value, key=str)
        )
    if value is None:
        return ""
    return str(value)


# --------------------------------------------------------------------------- #
# The sink
# --------------------------------------------------------------------------- #


class EffectSink:
    """Where a decided mutation goes.

    There is deliberately no live implementation. Adding one would mean shadow
    mode could be pointed at the world, and the entire value of the plane is
    that it cannot be.
    """

    def record(self, effect: Effect) -> Effect:  # pragma: no cover - abstract
        raise NotImplementedError

    def recorded(self) -> Sequence[Effect]:  # pragma: no cover - abstract
        raise NotImplementedError


class RecordingEffects(EffectSink):
    """Record decisions; perform nothing.

    A recording sink is not a stub that returns ``None`` and lets a caller
    carry on. It returns the same shape the real adapter returns, built locally,
    so the production path after the effect still executes -- which is the whole
    point, because the steps *after* a write are where orchestration logic lives
    (writing the in-flight slot, rolling a dispatch back when the lock could not
    be recorded, refusing to publish a plan that is not actually complete).
    """

    def __init__(self) -> None:
        self._effects: list[Effect] = []
        self._identifiers = 0

    def record(self, effect: Effect) -> Effect:
        self._effects.append(effect)
        return effect

    def recorded(self) -> Tuple[Effect, ...]:
        return tuple(self._effects)

    def kinds(self) -> Tuple[str, ...]:
        return tuple(effect.kind for effect in self._effects)

    def signatures(self) -> Tuple[Tuple[str, str, Tuple[Tuple[str, str], ...]], ...]:
        return tuple(effect.signature for effect in self._effects)

    def next_identifier(self, prefix: str = "shadow") -> int:
        """A locally unique id for an object a real adapter would have created.

        Returning a real-looking identifier rather than ``0`` keeps the
        production path honest: code that reads the id back, checks it, and
        rolls the write back when the check fails must still run.
        """

        self._identifiers += 1
        return _local_identifier(prefix, self._identifiers)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": EFFECT_SCHEMA,
            "mode": "record-only",
            "suppressed": len(self._effects),
            "effects": [effect.describe() for effect in self._effects],
        }


def _local_identifier(prefix: str, ordinal: int) -> int:
    """A stable pseudo-id.

    Negative ids are used on purpose: GitHub's ids are positive, so a value
    that could never be confused with a real comment id cannot accidentally be
    treated as one.
    """

    return -(1_000_000 + ordinal)


# --------------------------------------------------------------------------- #
# Coverage: the registry must still describe the engine
# --------------------------------------------------------------------------- #


def is_read_only(name: str) -> bool:
    """Whether ``name`` is a method name that cannot mutate anything.

    This is an allowlist, so a new method on a client is treated as mutating
    until somebody proves otherwise. The failure mode of the other default -- a
    new method assumed safe -- is a silent hole in the write boundary.
    """

    return name in READ_ONLY_METHODS


#: The read surface of ``continuum.review.github.GitHubClient``. Anything a
#: client exposes outside this set is treated as a mutation.
READ_ONLY_METHODS = frozenset(
    {
        "request",
        "paginate",
        "graphql",
        "get_pull",
        "list_pulls",
        "get_issue",
        "list_check_runs",
        "list_pull_files",
        "list_issue_comments",
        "list_review_comments",
        "list_reviews",
        "combined_status_for_ref",
        "file_at_ref",
        "default_branch",
        "ref_sha",
        "workflow_inventory",
        "review_threads",
        "unresolved_thread_comment_ids",
    }
)


def mapped_attributes() -> Dict[str, str]:
    """``client attribute name -> effect kind``.

    A method that can reach more than one effect is registered under each, with
    the first registration winning the summary label. The coverage check only
    asks whether the name is present; which of the effects a particular call
    actually produced is decided at run time by what the sink recorded, not
    from this table.
    """

    mapping: Dict[str, str] = {}
    for spec in EFFECT_KINDS:
        for name in spec.attributes:
            if name:
                mapping.setdefault(name, spec.kind)
    return mapping


def unmapped_mutators(owner: type) -> Tuple[str, ...]:
    """Public methods of ``owner`` that mutate but have no effect kind.

    The safety property the issue asks for -- "shadow mode cannot invoke every
    known mutating adapter" -- is only meaningful if the set of *known* mutating
    adapters is derived from the engine rather than from a list somebody
    remembered to update. This is that derivation: hand it
    ``GitHubClient`` and it reports any mutating method the registry forgot.
    """

    mapped = mapped_attributes()
    unmapped: list[str] = []
    for name in dir(owner):
        if name.startswith("_"):
            continue
        attribute = getattr(owner, name, None)
        if not callable(attribute):
            continue
        if is_read_only(name):
            continue
        if name in mapped:
            continue
        unmapped.append(name)
    return tuple(sorted(unmapped))


def assert_registry_covers(owner: type) -> None:
    """Fail closed when ``owner`` exposes a mutation the registry cannot record."""

    unmapped = unmapped_mutators(owner)
    if unmapped:
        raise WriteBarrierViolation(
            "{} exposes mutating method(s) with no effect kind: {}. Shadow mode "
            "refuses to run until each is classified.".format(
                owner.__name__, ", ".join(unmapped)
            )
        )


def all_adapters() -> Tuple[str, ...]:
    """Every adapter name the registry claims to cover, sorted and unique."""

    names: set[str] = set()
    for spec in EFFECT_KINDS:
        names.update(spec.adapters)
    return tuple(sorted(names))


def known_surfaces() -> Tuple[str, ...]:
    return tuple(sorted({spec.surface for spec in EFFECT_KINDS}))


def describe_registry() -> Dict[str, Any]:
    return {
        "schema": EFFECT_SCHEMA,
        "surfaces": list(known_surfaces()),
        "kinds": [EFFECT_REGISTRY[kind].describe() for kind in EFFECT_ORDER],
    }


def forbidden_in_shadow(
    effects: Iterable[Effect],
) -> Tuple[Dict[str, Any], ...]:
    """Assert every effect handed to a shadow sink was suppressed.

    A shadow run that performed one effect has violated the boundary even if the
    rest of the journal looks perfect, so this is checked over the whole
    recorded sequence rather than trusted per call.
    """

    violations: list[Dict[str, Any]] = []
    for effect in effects:
        if not effect.suppressed:
            violations.append(effect.describe())
    return tuple(violations)


def sequence_signatures(effects: Sequence[Effect]) -> Tuple[Tuple[str, str, Tuple[Tuple[str, str], ...]], ...]:
    return tuple(effect.signature for effect in effects)
