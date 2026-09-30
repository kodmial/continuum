"""Locating and importing the workflow-plane decision engine.

The decision engine Continuum shadows is split across two trees, and a shadow
run has to reach both:

* ``src/continuum`` -- the review queue contract and controller, the release
  lifecycle core, the configuration model. Imported normally, as a package.
* ``.github/scripts`` -- the trust policy, the conflict-repair ladder, and the
  agent workspace publisher. These are standalone stdlib scripts on purpose:
  they run as CLIs on a pinned checkout with no dependency install, which is
  what lets a security-sensitive step call them. They are not a package, so
  this module resolves the engine root and puts that directory on
  ``sys.path``.

The one thing worth being careful about is *which* engine is imported. A shadow
run exists to answer "would this version of Continuum have done the same thing",
so a journal that quietly recorded the decisions of a different checkout would
be worse than no journal. Every journal records the resolved engine root and
each module's ``__file__``, and :func:`describe` is written into the journal, so
a report can be checked against the SHA it claims to be about.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

#: The workflow-plane modules a shadow run must reach. Named explicitly so that
#: adding a script to ``.github/scripts`` does not silently widen what a shadow
#: run imports, and so a missing one is a startup failure rather than a skipped
#: guard.
ENGINE_MODULES: Tuple[str, ...] = ("trust_policy", "conflict_repair", "agent_workspace")

#: Environment variable a consumer workflow sets when the engine is not
#: reachable by walking up from this file (a pinned ``actions/checkout`` of the
#: engine, for instance).
ENGINE_ROOT_ENV = "CONTINUUM_ENGINE_ROOT"


class ShadowEngineUnavailable(RuntimeError):
    """The workflow-plane decision engine could not be resolved.

    Raised rather than degraded into a smaller shadow. A shadow run that
    silently skipped the trust policy would report parity for a decision path
    that is not the one production uses, which is the specific outcome this
    whole plane exists to rule out.
    """


def engine_root() -> Path:
    """The root of the Continuum checkout these modules belong to.

    Resolution order is deliberate: the explicit environment variable wins,
    because a pinned checkout is authoritative; otherwise the root is derived
    from this file's location, which is correct for every in-tree use and needs
    no configuration.
    """

    override = (os.environ.get(ENGINE_ROOT_ENV) or "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_dir():
            raise ShadowEngineUnavailable(
                "{}={!r} does not name a directory.".format(ENGINE_ROOT_ENV, override)
            )
        return candidate.resolve()

    # src/continuum/shadow/engine.py -> src/continuum/shadow -> src/continuum
    # -> src -> <root>
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".github" / "scripts" / "trust_policy.py").is_file():
            return parent
    raise ShadowEngineUnavailable(
        "Could not find the Continuum engine root above {}; set {}.".format(
            here, ENGINE_ROOT_ENV
        )
    )


def scripts_dir() -> Path:
    directory = engine_root() / ".github" / "scripts"
    if not directory.is_dir():
        raise ShadowEngineUnavailable("No decision scripts at {}.".format(directory))
    return directory


def load(name: str) -> Any:
    """Import one workflow-plane module, once.

    ``sys.path`` is extended rather than the file being loaded by path, so the
    module gets a real ``__name__`` and can be cached: two shadow decisions in
    one run must not be made by two copies of the policy.
    """

    if name not in ENGINE_MODULES:
        raise ShadowEngineUnavailable(
            "{!r} is not a shadowed decision module; expected one of {}.".format(
                name, ", ".join(ENGINE_MODULES)
            )
        )

    directory = str(scripts_dir())
    if directory not in sys.path:
        sys.path.insert(0, directory)

    existing = sys.modules.get(name)
    if existing is not None:
        return existing

    try:
        return importlib.import_module(name)
    except ImportError as error:
        raise ShadowEngineUnavailable(
            "Could not import {} from {}: {}".format(name, directory, error)
        ) from error


def trust_policy() -> Any:
    return load("trust_policy")


def conflict_repair() -> Any:
    return load("conflict_repair")


def agent_workspace() -> Any:
    return load("agent_workspace")


def load_all() -> Dict[str, Any]:
    return {name: load(name) for name in ENGINE_MODULES}


def engine_sha() -> str:
    """The commit the engine is at, or ``""`` when it cannot be determined.

    Read from ``.git`` rather than by running ``git``: a shadow run may not
    spawn a process, and the value is only ever recorded.
    """

    return _read_engine_sha(engine_root())


def _read_engine_sha(root: Path) -> str:
    head = root / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text.startswith("ref:"):
        return text if _is_sha(text) else ""
    reference = text.split(":", 1)[1].strip()
    packed = root / ".git" / "packed-refs"
    try:
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == reference:
                return parts[0] if _is_sha(parts[0]) else ""
    except OSError:
        pass
    loose = root / ".git" / reference
    try:
        value = loose.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return value if _is_sha(value) else ""


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def describe() -> Dict[str, Any]:
    """The provenance recorded in every journal.

    ``modules`` maps each shadowed decision module to the file it was loaded
    from, so a reader can tell which checkout produced a decision without
    trusting the label.
    """

    root = engine_root()
    modules: Dict[str, str] = {}
    for name in ENGINE_MODULES:
        module = load(name)
        modules[name] = str(getattr(module, "__file__", "") or "")

    try:
        from continuum import SCHEMA_VERSION as continuum_version
    except Exception:
        try:
            from . import __version__ as continuum_version
        except Exception:
            continuum_version = "unknown"

    return {
        "engine_root": str(root),
        "engine_sha": engine_sha(),
        "continuum_version": continuum_version,
        "modules": modules,
    }
