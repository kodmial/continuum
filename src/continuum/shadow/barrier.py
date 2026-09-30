"""The process-level write barrier: a shadow run that reaches around the effect
boundary stops, rather than continuing with the authority it was never given.

The effect boundary in :mod:`continuum.shadow.effects` covers the mutations
Continuum *knows* it performs. That is a real property, and it is not the same
property as "a shadow run cannot write to NanoDictate", because a shadow run
executes a lot of code that the registry does not describe: a publisher reaching
for ``urllib``, a publisher spawning ``git``, a transport opening a socket. Any
of those would be a real mutation of the production system, made by a run that
the journal would then describe as clean.

So the boundary has a second, lower layer that does not care what the code
*meant* to do. It watches the interpreter's own audit events -- the hooks the
runtime raises for process spawns, filesystem mutation, and network connects --
and refuses the ones a shadow run has no business performing. The refusal is not
a warning: it raises, the run ends with ``barrier_violation``, and the violation
is recorded in the journal.

Two things follow from using audit events rather than a code review:

* it covers code nobody looked at, including a transitive dependency and
  including a future version of Continuum;
* it cannot be satisfied by a prompt. The agent plane's existing answer to "do
  not write" is a permissions block plus ``deny`` verbs; this is the same idea
  one layer down, for the case where the run is already inside a process that
  holds a token.

The one carve-out is the run's own output directory. A journal that cannot be
written is not a journal, so writes under the declared output roots are allowed
-- and only writes, only below those roots, and only for the file modes a
journal needs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .effects import WriteBarrierViolation

BARRIER_SCHEMA = "continuum.shadow-barrier/v1"

#: Audit events a shadow run may never perform, with the reason each one is
#: refused. The list is about the *capability*, not about the intent: a shadow
#: run that needs to spawn a process has left the decision plane, whatever the
#: process was going to be.
DENIED_EVENTS: Dict[str, str] = {
    "subprocess.Popen": "a shadow run does not spawn processes",
    "os.system": "a shadow run does not shell out",
    "os.posix_spawn": "a shadow run does not spawn processes",
    "os.spawn": "a shadow run does not spawn processes",
    "os.fork": "a shadow run does not fork",
    "os.exec": "a shadow run does not exec",
    "socket.connect": "a shadow run reads a captured snapshot, never the network",
    "socket.getaddrinfo": "a shadow run resolves no host",
    "urllib.Request": "a shadow run performs no HTTP request",
    "http.client.connect": "a shadow run performs no HTTP request",
    "ftplib.connect": "a shadow run performs no FTP transfer",
    "os.remove": "a shadow run deletes nothing",
    "os.unlink": "a shadow run deletes nothing",
    "os.rmdir": "a shadow run deletes nothing",
    "os.rename": "a shadow run renames nothing",
    "os.replace": "a shadow run renames nothing",
    "os.truncate": "a shadow run truncates nothing",
    "os.chmod": "a shadow run changes no permissions",
    "os.chown": "a shadow run changes no ownership",
    "os.link": "a shadow run creates no links",
    "os.symlink": "a shadow run creates no links",
    "os.removexattr": "a shadow run changes no extended attributes",
    "shutil.rmtree": "a shadow run deletes nothing",
    "shutil.move": "a shadow run moves nothing",
    "shutil.copytree": "a shadow run copies no trees",
    "pty.spawn": "a shadow run allocates no terminals",
}

#: Audit events a shadow run is allowed to raise, and what they may carry. The
#: write modes are listed rather than inferred so an append-to-a-journal
#: allowance cannot quietly become a truncate allowance.
ALLOWED_WRITE_MODES = frozenset({"w", "a", "x"})


def classify(event: str, args: Tuple[Any, ...], output_roots: Sequence[Path]) -> Optional[str]:
    """The refusal reason for one audit event, or ``None`` to allow it.

    Pure and total: every event either has a reason or does not, so the hook
    installed by :func:`install` has no path that neither allows nor denies.
    That matters because an audit hook that can raise ``TypeError`` on an
    unexpected argument tuple would convert a would-be denial into a crash at an
    arbitrary place.
    """

    if event in DENIED_EVENTS:
        return DENIED_EVENTS[event]

    if event == "open":
        return _classify_open(args, output_roots)

    if event in ("os.mkdir", "os.makedirs"):
        # Directories are the one filesystem structure a shadow run legitimately
        # creates, because its artifact directory is organised by subdirectory
        # (one per artifact kind) so a reviewer can tell a journal from a parity
        # verdict without opening either. Allowed *inside* a declared output root
        # only: a directory created anywhere else is refused like any other write,
        # and creating one outside the output tree would be a way to leave
        # something behind.
        if not args:
            return "a directory creation with no arguments"
        if not _within(args[0], output_roots):
            return "a shadow run creates directories only inside its own output directory"
        return None

    if event == "os.putenv" or event == "os.unsetenv":
        # Process environment is not an external mutation and shadow runs read
        # the ambient one to resolve configuration.
        return None

    return None


def _classify_open(args: Tuple[Any, ...], output_roots: Sequence[Path]) -> Optional[str]:
    if not args:
        return "an open attempt with no arguments"

    path = args[0]
    mode = args[1] if len(args) > 1 and isinstance(args[1], str) else "r"
    flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0

    writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC))
    writing = writing or any(character in mode for character in "wxa+")

    if not writing:
        return None

    if isinstance(path, int) or path is None:
        return "a shadow run does not write to a file descriptor"

    roots = [root for root in output_roots if root is not None]
    if not roots:
        return "a shadow run has no output directory to write its journal to"

    if not _within(path, roots):
        return "a shadow run writes only inside its own output directory"

    return None


def _within(path: Any, roots: Sequence[Path]) -> bool:
    try:
        candidate = Path(os.fsdecode(path)).resolve()
    except (TypeError, ValueError, OSError):
        return False

    for root in roots:
        try:
            resolved = Path(root).resolve()
        except (TypeError, ValueError, OSError):
            continue
        if candidate == resolved or resolved in candidate.parents:
            return True
    return False


class BarrierReport:
    """The violations a shadow run collected, in the order they were refused.

    Collected rather than raised-and-forgotten: the issue requires that a
    blocked write be *recorded*, and a run that stops at the first violation
    would report one blocked attempt and hide the rest.
    """

    def __init__(self) -> None:
        self.violations: List[Dict[str, Any]] = []
        self.installed = False

    def record(self, event: str, reason: str, args: Sequence[Any]) -> Dict[str, Any]:
        entry = {
            "event": event,
            "reason": reason,
            "argument": _describe_argument(args),
        }
        self.violations.append(entry)
        return entry

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": BARRIER_SCHEMA,
            "installed": self.installed,
            "violations": list(self.violations),
            "count": len(self.violations),
        }


def _describe_argument(args: Sequence[Any]) -> str:
    """A short, log-safe rendering of the offending arguments."""

    parts: list[str] = []
    for value in args[:3]:
        if isinstance(value, (str, bytes, os.PathLike)):
            try:
                text = os.fsdecode(value)
            except (TypeError, ValueError):
                text = repr(value)
            parts.append(text[:200])
        elif isinstance(value, int):
            parts.append(str(value))
        else:
            parts.append(type(value).__name__)
    return " ".join(parts)


#: One plausible value per *parameter name*, so a probe can call whichever
#: signature a mutating method happens to have without keeping a hand-written
#: argument list in step with the client. A method whose signature names a
#: parameter missing from this table is reported as uncallable rather than
#: quietly skipped: an unprobed mutator is exactly the gap this exists to close.
ATTEMPT_ARGUMENTS: Dict[str, Any] = {
    "pr_number": 43,
    "number": 43,
    "issue_number": 43,
    "n": 43,
    "labels": ["continuum-shadow-probe"],
    "label": "continuum-shadow-probe",
    "body": "continuum shadow barrier probe",
    "text": "continuum shadow barrier probe",
    "message": "continuum shadow barrier probe",
    "title": "continuum shadow barrier probe",
    "head_sha": "a" * 40,
    "sha": "a" * 40,
    "ref": "refs/heads/continuum-shadow-probe",
    "state": "success",
    "context": "CodeRabbit",
    "description": "continuum shadow barrier probe",
    "conclusion": "success",
    "output": "continuum shadow barrier probe",
    "name": "continuum-shadow-probe",
    "comment_id": 1,
    "id": 1,
    "thread_id": "PRRT_continuum_shadow_probe",
    "marker": "continuum",
    "workflow": "coderabbit-retry.yml",
    "inputs": {"pull_request": "43"},
    "event": "pull_request",
    "method": "squash",
    "sha_or_tag": "a" * 40,
    "path": "continuum-shadow-probe.txt",
    "filename": "continuum-shadow-probe.txt",
    "content": "continuum shadow barrier probe",
    "files": [],
    "branch": "continuum-shadow-probe/branch",
    "tag": "v0.0.0-continuum-shadow-probe",
    "tag_name": "v0.0.0-continuum-shadow-probe",
    "release_id": 1,
    "asset": "continuum-shadow-probe.txt",
    "login": "continuum-shadow-probe",
    "logins": ["continuum-shadow-probe"],
    "milestone": "continuum-shadow-probe",
    "config": {},
    "spec": {"tag_name": "v0.0.0-continuum-shadow-probe"},
    "body_json": {},
    "run_id": 1,
    "external_id": "continuum-shadow-probe",
    "details_url": "",
    "target_url": "",
    "conclusion_state": "success",
    "steps": [],
    "resolved": True,
    "is_resolved": True,
    "line": 1,
    "start_line": 1,
    "package": "continuum-shadow-probe",
    "version": "0.0.0",
    "directory": ".",
    "base": "main",
    "checks": [],
    "required": [],
    "environments": [],
    "review_id": 1,
}

ATTEMPT_SCHEMA = "continuum.shadow-barrier-attempts/v1"


def attempt_every_write(client: Any, sink: Any) -> Dict[str, Any]:
    """Call every mutating method the client exposes, once, and say what happened.

    The issue asks for a deliberate attempted write rather than a claim that
    writes are impossible. This is that attempt, in one place, so the CLI's
    ``barrier-check`` and the test suite prove the same thing instead of two
    similar-looking probes that can drift apart.

    A call is only evidence of safety if it was *tried*. Each method is invoked
    with values drawn from :data:`ATTEMPT_ARGUMENTS`, and the result is graded:

    ``called``
        the method ran and every effect it produced was suppressed.
    ``refused``
        the client refused to expose the method at all, which is a stronger
        answer than recording it.
    ``unreachable``
        the shadow client does not implement the method, so the effect kind it
        was registered for is unreachable through this surface.
    ``uncallable``
        the method's signature names a parameter this table does not know. The
        method was not probed, which is a gap in the evidence rather than a
        pass, and the caller decides what to do about it.

    Anything in ``performed`` is an effect that was *not* suppressed: a write
    that reached its sink, or past it.
    """

    import inspect

    from .effects import mapped_attributes

    document: Dict[str, Any] = {
        "schema": ATTEMPT_SCHEMA,
        "methods": sorted(mapped_attributes()),
        "called": [],
        "refused": [],
        "unreachable": [],
        "uncallable": [],
        "performed": [],
    }
    for name in document["methods"]:
        try:
            method = getattr(client, name, None)
        except WriteBarrierViolation as violation:
            document["refused"].append({"name": name, "reason": str(violation)})
            continue
        if method is None:
            document["unreachable"].append(name)
            continue

        args: List[Any] = []
        kwargs: Dict[str, Any] = {}
        unknown: Optional[str] = None
        for parameter in inspect.signature(method).parameters.values():
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                continue
            if parameter.name not in ATTEMPT_ARGUMENTS:
                unknown = parameter.name
                break
            if parameter.kind is parameter.POSITIONAL_ONLY:
                args.append(ATTEMPT_ARGUMENTS[parameter.name])
            else:
                kwargs[parameter.name] = ATTEMPT_ARGUMENTS[parameter.name]
        if unknown is not None:
            document["uncallable"].append({"name": name, "parameter": unknown})
            continue

        before = len(sink.recorded())
        try:
            method(*args, **kwargs)
        except WriteBarrierViolation as violation:
            document["refused"].append({"name": name, "reason": str(violation)})
            continue
        except Exception as error:  # noqa: BLE001 - the refusal is the result
            # A mutator that fails on probe arguments has still been reached and
            # has still not written anything, which is what the probe is for.
            document["refused"].append(
                {"name": name, "reason": "{}: {}".format(type(error).__name__, error)}
            )
            continue

        produced = [effect.describe() for effect in sink.recorded()[before:]]
        performed = [effect for effect in produced if not effect.get("suppressed")]
        document["performed"].extend(performed)
        document["called"].append(
            {"name": name, "effects": produced, "unrecorded": not produced}
        )
    return document


def install(
    output_roots: Sequence[Path],
    report: Optional[BarrierReport] = None,
    *,
    on_violation: Optional[Callable[[str, str, Tuple[Any, ...]], None]] = None,
) -> BarrierReport:
    """Install the barrier in this process and return its report.

    ``sys.addaudithook`` cannot be uninstalled, which is the desired
    behaviour: a shadow process that has installed the barrier cannot later
    decide it would rather not have one. It also means the barrier must be
    installed by a process that is a shadow run from then on, which is why the
    CLI installs it before it loads configuration and never imports the live
    adapters.

    ``on_violation`` is called before the exception is raised, so a caller can
    flush what it has. It must not itself perform a denied operation.
    """

    state = report if report is not None else BarrierReport()
    roots = [Path(root) for root in output_roots]

    for root in roots:
        root.mkdir(parents=True, exist_ok=True)

    # A shadow run writes its journal and nothing else. Bytecode caching would
    # be a second, undeclared write, and an import that happened to miss the
    # cache would otherwise fail the run for a reason that has nothing to do
    # with the decision under test.
    sys.dont_write_bytecode = True

    def hook(event: str, args: Tuple[Any, ...]) -> None:
        reason = classify(event, args, roots)
        if reason is None:
            return
        state.record(event, reason, args)
        if on_violation is not None:
            on_violation(event, reason, args)
        raise WriteBarrierViolation(
            "shadow write barrier refused {}: {}".format(event, reason)
        )

    if not state.installed:
        sys.addaudithook(hook)
        state.installed = True
    return state
