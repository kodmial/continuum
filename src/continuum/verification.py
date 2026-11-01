"""Repository-authoritative implementation verification (kodmial/continuum#168).

Issue mode must establish a repository baseline before the primary coding
session, run an independent verifier/fix session on the resulting checkout,
and then repeat exactly the same verification commands. This module owns the
pure contract and evidence handling. Workflows own the agent lifecycle and
Git/API state.

The resolver never guesses a project stack. It reads the consumer's existing
Continuum validation contract:

* the ``prepare_command`` / ``build_command`` / ``test_command`` /
  ``validation_command`` inputs of the consumer's primary ``CI`` caller for
  ``continuum-validation.yml``;
* the matching documented ``CONTINUUM_*_COMMAND`` repository variables when the
  caller does not supply a literal command.

At least one build, test, or validation hook must resolve. No hook means an
explicit ``unconfigured`` state, never a silent green or a substitute command.

Standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

SCHEMA_VERSION = 1

#: Workflow-owned evidence. It is retained on a 429 checkpoint and removed at
#: the publish boundary (see ``continuum_pr_staging.sh``); it is never product
#: code and is never committed to an implementation PR.
EVIDENCE_FILE_NAME = ".continuum-verification.json"

#: Runner-controlled key that attests the trusted evidence sections. The
#: evidence file lives in the worktree, where the coding agent can rewrite it;
#: a signature keyed by a workflow secret makes a forged contract, baseline,
#: verifier result, or accepted ``final`` record fail closed instead of being
#: reused. Without the key the evidence is never trusted for reuse: the final
#: phase re-resolves the contract from the task base commit and re-establishes
#: the baseline on the current runner.
ATTESTATION_KEY_ENV = "CONTINUUM_VERIFICATION_ATTESTATION_KEY"

#: The key is delivered through a private file outside the task worktree. Its
#: path is read from this environment variable, never the key itself: a
#: repository-controlled command can read the engine's inherited environment
#: through ``/proc/$PPID/environ``, so the value must not be in it. Every
#: process that reads the file removes the on-disk copy as soon as it has the
#: value in memory, and never writes the copy back. A later invocation
#: re-provisions the file from its authoritative source with
#: :func:`provision_attestation_key` (the ``provision-key`` CLI command), and
#: fails closed with a clear error when the file is absent.
ATTESTATION_KEY_FILE_ENV = "CONTINUUM_VERIFICATION_ATTESTATION_KEY_FILE"

#: Keys already read from a file in this process, keyed by resolved path. The
#: file is removed as soon as it is read, so later signing within the same
#: invocation uses this cache instead of re-reading a file that is gone.
_ATTESTATION_KEY_CACHE: Dict[str, bytes] = {}

#: Resolved key-file paths already removed by this process. Reading removes the
#: file; this set makes the removal idempotent and lets the command-set path
#: re-check after an in-process re-provisioning.
_ATTESTATION_KEY_CONSUMED: Set[str] = set()

ATTESTATION_ALGORITHM = "hmac-sha256"

#: Evidence sections covered by the attestation signature.
ATTESTED_SECTIONS = ("contract", "baseline", "verifier", "final")

#: Ordered validation hooks, mirroring ``continuum-validation.yml``. The order
#: is the repository contract; baseline and final use the exact same tuple.
HOOKS: Tuple[Tuple[str, str], ...] = (
    ("prepare", "CONTINUUM_PREPARE_COMMAND"),
    ("build", "CONTINUUM_BUILD_COMMAND"),
    ("test", "CONTINUUM_TEST_COMMAND"),
    ("validation", "CONTINUUM_VALIDATION_COMMAND"),
)

#: A contract is only a *verification* contract when it executes at least one
#: of these hooks. A prepare-only configuration is still unconfigured.
VERIFICATION_HOOKS = frozenset({"build", "test", "validation"})

CI_CALLER_CANDIDATES = (
    ".github/workflows/ci.yml",
    ".github/workflows/ci.yaml",
)

#: Environment components pinned so baseline and final are comparable.
ENVIRONMENT_KEYS = (
    "RUNNER_OS",
    "RUNNER_ARCH",
    "ImageOS",
    "ImageVersion",
    "LANG",
    "LC_ALL",
    "SHELL",
    "PATH",
    "GITHUB_ACTIONS",
)

#: The verification engine is invoked with this directory on ``PYTHONPATH``.
#: Consumer commands must not inherit it: that would let a checkout of
#: Continuum on the runner shadow the task worktree's own ``continuum``
#: package. Strip exactly this entry while preserving the caller's path.
_ENGINE_SOURCE = Path(__file__).resolve().parents[1]

#: Workflow-owned paths excluded from task-change detection. They are never
#: part of an implementation result, so their presence must not make a
#: no-change run look like a task implementation.
WORKFLOW_OWNED_PATHS = (
    EVIDENCE_FILE_NAME,
    ".continuum-429-stage",
    ".continuum-429-recovery.json",
    ".continuum-cancel-engine",
    ".continuum-429-engine",
    ".continuum-admission-engine",
)

_VARS_EXPRESSION = re.compile(
    r"\A\$\{\{\s*vars\.([A-Z][A-Z0-9_]{0,63})\s*\}\}\Z"
)


class VerificationError(RuntimeError):
    """A verification contract or evidence error that must fail closed."""


@dataclass(frozen=True)
class VerificationCommand:
    """One resolved repository command and the contract that supplied it."""

    name: str
    command: str
    source: str

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "command": self.command, "source": self.source}


@dataclass(frozen=True)
class VerificationContract:
    """Resolved command set plus its explicit state."""

    state: str
    source: str
    commands: Tuple[VerificationCommand, ...]
    errors: Tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return self.state == "configured" and any(
            command.name in VERIFICATION_HOOKS for command in self.commands
        )

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            [[command.name, command.command] for command in self.commands],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "source": self.source,
            "commands": [command.as_dict() for command in self.commands],
            "fingerprint": self.fingerprint,
            "errors": list(self.errors),
        }


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _resolve_literal(
    value: Any, environ: Mapping[str, str]
) -> Tuple[str, str]:
    """Resolve one CI caller ``with:`` value to a literal command.

    Repository variables referenced with the simple ``${{ vars.NAME }}`` form
    resolve from the same environment the workflow passes down. Any other
    expression is intentionally unresolved: Continuum never evaluates arbitrary
    GitHub expressions and never embeds an expression as a shell command.
    Returns ``(command, error)``; an unsupported expression is an error rather
    than an empty command so it cannot silently fall back to a different check.
    """

    if value is None:
        return "", ""
    if isinstance(value, bool):
        return ("true" if value else "false"), ""
    if isinstance(value, (int, float)):
        return str(value), ""
    if not isinstance(value, str):
        return "", ""
    text = value.strip()
    if not text:
        return "", ""
    if "${{" not in text:
        return text, ""
    match = _VARS_EXPRESSION.match(text)
    if match is None:
        return "", (
            "unsupported GitHub expression in a verification command; "
            "only ${{ vars.NAME }} or a literal command is accepted"
        )
    return str(environ.get(match.group(1), "") or "").strip(), ""


_HOOK_KEYS = {hook + "_command": hook for hook, _ in HOOKS}
_BLOCK_SCALAR_HEADER = re.compile(
    r"\A[|>](?:[+-]?[1-9]?|[1-9][+-]?)\Z"
)
_DOUBLE_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\u0085",
    "_": "\u00a0",
    "L": "\u2028",
    "P": "\u2029",
}


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_comment(raw: str) -> str:
    """Strip a YAML comment while preserving quoted ``#`` characters."""

    quote: Optional[str] = None
    for index, char in enumerate(raw):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "#" and (index == 0 or raw[index - 1] in " \t"):
            return raw[:index]
    return raw


def _unquote_scalar(raw: str) -> str:
    text = raw.strip()
    if len(text) < 2 or text[0] not in ("'", '"') or text[-1] != text[0]:
        return text
    if text[0] == "'":
        return text[1:-1].replace("''", "'")
    inner = text[1:-1]
    out: List[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(inner):
            return inner
        marker = inner[index + 1]
        if marker == "u" and index + 6 <= len(inner):
            try:
                out.append(chr(int(inner[index + 2 : index + 6], 16)))
            except ValueError:
                return inner
            index += 6
            continue
        out.append(_DOUBLE_ESCAPES.get(marker, marker))
        index += 2
    return "".join(out)


def _split_mapping_entry(text: str) -> Optional[Tuple[str, str]]:
    quote: Optional[str] = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == ":" and (index + 1 == len(text) or text[index + 1] in " \t"):
            key = text[:index].strip()
            if key[:1] in ("'", '"') and key[-1:] == key[:1]:
                key = _unquote_scalar(key)
            if not key:
                return None
            return key, text[index + 1 :].strip()
    return None


def _fold_block_lines(lines: Sequence[str]) -> str:
    parts: List[str] = []
    pending_blank = 0
    previous_more_indented = False
    has_previous = False
    for line in lines:
        if line == "":
            pending_blank += 1
            continue
        more_indented = line[:1] in (" ", "\t")
        if has_previous:
            if pending_blank:
                parts.append("\n" * pending_blank)
            elif previous_more_indented or more_indented:
                parts.append("\n")
            else:
                parts.append(" ")
        elif pending_blank:
            parts.append("\n" * pending_blank)
        parts.append(line)
        has_previous = True
        previous_more_indented = more_indented
        pending_blank = 0
    if pending_blank:
        parts.append("\n" * pending_blank)
    return "".join(parts)


def _parse_block_scalar(
    header: str, lines: Sequence[str], start: int, parent_indent: int
) -> Tuple[str, int]:
    """Parse a YAML ``|``/``>`` block scalar starting on ``start``."""

    style = header[0]
    chomp = ""
    indicator = 0
    for char in header[1:]:
        if char in "+-":
            chomp = char
        elif char.isdigit():
            indicator = int(char)
    if indicator:
        content_indent = parent_indent + indicator
    else:
        content_indent = parent_indent + 1
        for probe in range(start, len(lines)):
            raw = lines[probe]
            if not raw.strip():
                continue
            indent = _leading_spaces(raw)
            if indent > parent_indent:
                content_indent = indent
            break
    collected: List[str] = []
    index = start
    while index < len(lines):
        raw = lines[index]
        if not raw.strip():
            collected.append("")
            index += 1
            continue
        indent = _leading_spaces(raw)
        if indent < content_indent:
            break
        collected.append(raw[content_indent:])
        index += 1
    if style == "|":
        text = "\n".join(collected)
    else:
        text = _fold_block_lines(collected)
    if chomp == "-":
        text = text.rstrip("\n")
    elif chomp == "+":
        if text and not text.endswith("\n"):
            text += "\n"
    elif text:
        text = text.rstrip("\n") + "\n"
    return text, index


def _is_shell_comment_only(command: str) -> bool:
    has_content = False
    for line in command.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        has_content = True
        if not stripped.startswith("#"):
            return False
    return has_content


def _find_validation_caller(
    lines: Sequence[str],
) -> Optional[Tuple[int, int]]:
    """Locate the ``uses:`` entry for the primary validation caller."""

    for index, raw in enumerate(lines):
        code = _strip_comment(raw)
        if not code.strip():
            continue
        indent = _leading_spaces(raw)
        text = code.strip()
        if text.startswith("- "):
            rest = text[2:]
            key_indent = indent + 2 + (len(rest) - len(rest.lstrip()))
            text = rest.lstrip()
        else:
            key_indent = indent
        entry = _split_mapping_entry(text)
        if entry is None or entry[0] != "uses":
            continue
        value = _unquote_scalar(entry[1])
        if not value:
            continue
        base = value.split("@", 1)[0].rstrip("/")
        if base.rsplit("/", 1)[-1] == "continuum-validation.yml":
            return index, key_indent
    return None


def _find_with_entry(
    lines: Sequence[str], uses_index: int, key_indent: int
) -> Optional[Tuple[int, str]]:
    for index in range(uses_index + 1, len(lines)):
        raw = lines[index]
        code = _strip_comment(raw)
        if not code.strip():
            continue
        indent = _leading_spaces(raw)
        if indent < key_indent:
            return None
        if indent > key_indent:
            continue
        text = code.strip()
        if text.startswith("- "):
            return None
        entry = _split_mapping_entry(text)
        if entry is not None and entry[0] == "with":
            return index, entry[1]
    return None


def _with_mapping_indent(
    lines: Sequence[str], with_index: int, with_indent: int
) -> Optional[int]:
    for index in range(with_index + 1, len(lines)):
        raw = lines[index]
        if not raw.strip() or not _strip_comment(raw).strip():
            continue
        indent = _leading_spaces(raw)
        if indent <= with_indent:
            return None
        return indent
    return None


def _ci_caller_text(repo_root: Path, relative: str, ref: str) -> Optional[str]:
    """Read one CI caller from ``ref`` or from the worktree when ``ref`` is empty."""

    if ref:
        completed = _run_git(repo_root, "show", "{}:{}".format(ref, relative))
        if completed.returncode != 0:
            return None
        return completed.stdout or ""
    path = repo_root / relative
    if not path.is_file():
        return None
    return _read_text(path)


def _ci_caller_commands(
    repo_root: Path, environ: Mapping[str, str], ref: str = ""
) -> Tuple[Dict[str, Tuple[str, str]], List[str]]:
    """Read literal hook commands from the consumer's primary CI caller.

    Only the ``with:`` mapping of the job/step that calls
    ``continuum-validation.yml`` is read, so an unrelated ``uses:`` block cannot
    supply a hook. Values obey YAML scalar rules: block scalars are expanded,
    quoted scalars are unquoted, and comments do not become commands.

    ``ref`` reads the caller from that Git revision instead of the worktree, so
    a task checkout cannot rewrite its own verification contract after the
    baseline was recorded.
    """

    values: Dict[str, Tuple[str, str]] = {}
    errors: List[str] = []
    for relative in CI_CALLER_CANDIDATES:
        text = _ci_caller_text(repo_root, relative, ref)
        if text is None:
            continue
        lines = text.splitlines()
        caller = _find_validation_caller(lines)
        if caller is None:
            continue
        uses_index, key_indent = caller
        with_entry = _find_with_entry(lines, uses_index, key_indent)
        if with_entry is None:
            continue
        with_index, raw_with = with_entry
        if raw_with:
            continue
        mapping_indent = _with_mapping_indent(lines, with_index, key_indent)
        if mapping_indent is None:
            continue
        index = with_index + 1
        while index < len(lines):
            raw = lines[index]
            code = _strip_comment(raw)
            if not code.strip():
                index += 1
                continue
            indent = _leading_spaces(raw)
            if indent <= key_indent:
                break
            if indent != mapping_indent:
                index += 1
                continue
            entry = _split_mapping_entry(code.strip())
            if entry is None:
                index += 1
                continue
            key, raw_value = entry
            hook = _HOOK_KEYS.get(key)
            if hook is None or hook in values:
                index += 1
                continue
            header = raw_value if _BLOCK_SCALAR_HEADER.match(raw_value) else None
            if header is not None:
                scalar, index = _parse_block_scalar(
                    header, lines, index + 1, indent
                )
            else:
                scalar = _unquote_scalar(raw_value) if raw_value else ""
                index += 1
            literal, error = _resolve_literal(scalar, environ)
            if error:
                errors.append("{}: {}".format(relative, error))
                continue
            if not literal:
                continue
            if _is_shell_comment_only(literal):
                errors.append(
                    "{}: {}_command is a shell comment with no command; "
                    "refusing a silent no-op".format(relative, hook)
                )
                continue
            values[hook] = (
                literal,
                "{}:{}_{}".format(relative, hook, "command"),
            )
        if values:
            break
    return values, errors


def resolve_contract(
    repo_root: Path | str,
    environ: Optional[Mapping[str, str]] = None,
    ref: str = "",
) -> VerificationContract:
    """Resolve the consumer's authoritative verification commands.

    The explicit CI caller contract wins over repository variables, mirroring
    ``continuum-validation.yml``'s ``inputs.foo || vars.foo`` precedence. Only
    non-empty literal commands are returned; there is no built-in fallback.

    ``ref`` resolves the CI caller from that Git revision. The final phase
    passes the task base commit so a worktree edit cannot weaken the contract
    that was recorded at baseline time.
    """

    root = Path(repo_root)
    env = environ if environ is not None else os.environ
    ci_values, errors = _ci_caller_commands(root, env, ref)

    commands: List[VerificationCommand] = []
    sources: List[str] = []
    for hook, variable in HOOKS:
        value = ""
        source = ""
        from_ci = ci_values.get(hook)
        if from_ci is not None:
            value, source = from_ci
        if not value:
            value = str(env.get(variable, "") or "").strip()
            source = variable if value else ""
        if value:
            commands.append(
                VerificationCommand(name=hook, command=value, source=source)
            )
            sources.append(source)

    has_verification = any(
        command.name in VERIFICATION_HOOKS for command in commands
    )
    if errors:
        return VerificationContract(
            state="unconfigured",
            source="none",
            commands=tuple(commands),
            errors=tuple(errors),
        )
    if not has_verification:
        detail = (
            "No authoritative verification command was found. Configure the "
            "consumer CI caller's build/test/validation command or the matching "
            "CONTINUUM_*_COMMAND repository variable."
        )
        return VerificationContract(
            state="unconfigured",
            source="none",
            commands=tuple(commands),
            errors=(detail,),
        )

    source = sources[0] if sources else "none"
    if any(item.startswith(".github/workflows/") for item in sources):
        source = "ci-caller"
    elif any(item.startswith("CONTINUUM_") for item in sources):
        source = "repository-variables"
    return VerificationContract(
        state="configured",
        source=source,
        commands=tuple(commands),
        errors=tuple(errors),
    )


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
        check=False,
    )


def _git_value(repo: Path, *args: str) -> str:
    completed = _run_git(repo, *args)
    if completed.returncode != 0:
        raise VerificationError(
            "git {} failed: {}".format(
                " ".join(args), (completed.stderr or "").strip()[-1000:]
            )
        )
    return (completed.stdout or "").strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str = "HEAD") -> bool:
    completed = _run_git(
        repo, "merge-base", "--is-ancestor", ancestor, descendant
    )
    return completed.returncode == 0


def _is_workflow_owned(path: str) -> bool:
    return path in WORKFLOW_OWNED_PATHS or any(
        path == owned or path.startswith(owned + "/")
        for owned in WORKFLOW_OWNED_PATHS
    )


def _child_environment(environ: Mapping[str, str]) -> Dict[str, str]:
    """Remove engine-only secrets and import paths from consumer commands."""

    env = dict(environ)
    # Repository-controlled verification commands must never receive the
    # runner-held HMAC key, or the path to the file it is read from; otherwise
    # changed tests/scripts can read the key and forge evidence.
    env.pop(ATTESTATION_KEY_ENV, None)
    env.pop(ATTESTATION_KEY_FILE_ENV, None)
    raw = str(env.get("PYTHONPATH", "") or "")
    kept: List[str] = []
    for entry in raw.split(os.pathsep):
        if not entry:
            continue
        try:
            is_engine = Path(entry).expanduser().resolve() == _ENGINE_SOURCE
        except OSError:
            is_engine = False
        if not is_engine:
            kept.append(entry)
    if kept:
        env["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        env.pop("PYTHONPATH", None)
    return env


def environment_fingerprint(repo: Path, environ: Mapping[str, str]) -> str:
    """Deterministic identity of the verification environment."""

    env = _child_environment(environ)
    payload = {key: str(env.get(key, "")) for key in ENVIRONMENT_KEYS}
    payload["cwd"] = str(Path(repo).resolve())
    payload["shell"] = "bash -c"
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _attestation_key_path(
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """The configured key-file path, never the key value itself."""

    source = os.environ if environ is None else environ
    if str(source.get(ATTESTATION_KEY_ENV, "") or "").strip():
        raise VerificationError(
            "the verification attestation key must not be set in the engine "
            "process environment: a repository command can read it through "
            "/proc/$PPID/environ. Write the key to a private file outside the "
            "task worktree and pass its path in {} instead.".format(
                ATTESTATION_KEY_FILE_ENV
            )
        )
    return str(source.get(ATTESTATION_KEY_FILE_ENV, "") or "").strip()


def _attestation_key_paths(
    environ: Optional[Mapping[str, str]] = None,
    worktree: Optional[Path | str] = None,
) -> Optional[Tuple[Path, Path]]:
    """The configured and resolved key-file paths, or ``None`` when unset.

    The configured path is what gets read, removed, and re-provisioned; the
    resolved path is the worktree-boundary and cache identity. The path must
    live outside the task worktree: a repository-controlled command can read
    anything inside the worktree it can otherwise reach.
    """

    raw_path = _attestation_key_path(environ)
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if worktree is not None:
        root = Path(worktree).resolve()
        if resolved == root or root in resolved.parents:
            raise VerificationError(
                "the verification attestation key file must live outside the "
                "task worktree"
            )
    return path, resolved


def _remove_attestation_key_files(path: Path, resolved: Path) -> None:
    """Unlink the configured key file and its resolved symlink target.

    Both names are removed: unlinking only the configured name would leave the
    key bytes readable through a symbolic link's target, and unlinking only the
    target would leave the link. A missing file is not an error because another
    consumer may have removed it first and the in-memory value is
    authoritative. Any other failure raises :class:`VerificationError` so the
    caller stops instead of continuing while a readable copy remains.
    """

    targets = [path]
    if resolved != path:
        targets.append(resolved)
    for target in targets:
        try:
            target.unlink()
        except FileNotFoundError:
            # Another consumer removed it first, or this is the same inode
            # already removed through another name.
            pass
        except OSError as exc:
            raise VerificationError(
                "the verification attestation key file cannot be removed after "
                "it was read: {}".format(exc)
            ) from exc


def _attestation_key(
    environ: Optional[Mapping[str, str]] = None,
    worktree: Optional[Path | str] = None,
) -> Optional[bytes]:
    """The runner-controlled attestation key, or ``None`` when unset.

    The key value is read once from the private file named by
    :data:`ATTESTATION_KEY_FILE_ENV`, cached in this process's memory, and the
    on-disk copy is removed immediately: the configured path and, when it
    differs, its resolved symlink target. Every key-reading command (the
    evidence summary, baseline, needs-verifier, record-verifier, and final
    commands) therefore leaves no key on disk after the process has read it.
    Nothing is restored at exit or on a signal, so a same-uid survivor process
    spawned by a repository-controlled command cannot read the key after the
    engine exits. The next verification CLI invocation that needs the key must
    re-provision the file from its authoritative source with
    :func:`provision_attestation_key` (``python -m continuum.verification
    provision-key``), and must never provision it during an agent session: an
    agent runs with the same user permissions and could read the file and sign
    forged evidence. When the file is absent, reading fails closed instead of
    silently continuing without an attestation. The value stays in this
    process's memory and is never written into the environment handed to
    repository-controlled commands, so a child cannot inherit it or its path.
    """

    location = _attestation_key_paths(environ, worktree)
    if location is None:
        return None
    path, resolved = location
    marker = str(resolved)
    cached = _ATTESTATION_KEY_CACHE.get(marker)
    if cached is not None:
        return cached
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VerificationError(
            "the verification attestation key file is unavailable: {}; "
            "re-provision it from its authoritative source with "
            "`python -m continuum.verification provision-key`".format(exc)
        ) from exc
    if not raw:
        raise VerificationError("the verification attestation key file is empty")
    key = raw.encode("utf-8")
    _ATTESTATION_KEY_CACHE[marker] = key
    _remove_attestation_key_files(path, resolved)
    _ATTESTATION_KEY_CONSUMED.add(marker)
    return key


def _consume_attestation_key(
    environ: Optional[Mapping[str, str]] = None,
    worktree: Optional[Path | str] = None,
) -> None:
    """Ensure no on-disk key remains before a repository-controlled command.

    Reading the key already removes the file. This explicit guard covers the
    command-set path even when this process re-provisioned the key after an
    earlier read: the value is cached in memory, and the on-disk copy is
    deliberately not restored at exit or on a signal. A repository-controlled
    command can spawn a same-uid survivor that outlives the engine and reads
    any file the engine can read, so leaving the key at its configured path
    after a command has run would hand the key to that survivor. A later
    invocation re-provisions the file explicitly with
    :func:`provision_attestation_key`; when it is absent, reading fails closed
    instead of silently continuing without an attestation.
    """

    key = _attestation_key(environ, worktree=worktree)
    if key is None:
        return
    location = _attestation_key_paths(environ, worktree)
    if location is None:  # pragma: no cover - guarded by _attestation_key
        return
    path, resolved = location
    marker = str(resolved)
    if marker in _ATTESTATION_KEY_CONSUMED:
        # A previous read in this process already removed the file; the cached
        # value remains authoritative.
        return
    _remove_attestation_key_files(path, resolved)
    _ATTESTATION_KEY_CONSUMED.add(marker)


def provision_attestation_key(
    data: bytes | str,
    environ: Optional[Mapping[str, str]] = None,
    worktree: Optional[Path | str] = None,
) -> Path:
    """Write the key file from its authoritative source for a later invocation.

    The caller supplies the key bytes (the ``provision-key`` CLI command reads
    them from standard input, never from an argument or the environment, so
    they do not appear in process listings or ``/proc/$PPID/environ``). The
    write is atomic, owner-only (``0600``), and refuses to replace a different
    key that is already present so a mistake cannot invalidate existing
    evidence. Re-provisioning the same key again is a no-op. The caller must
    run this outside any agent session: an agent in the same job has the same
    user permissions and could read the file and sign forged evidence.
    """

    location = _attestation_key_paths(environ, worktree)
    if location is None:
        raise VerificationError(
            "cannot provision the verification attestation key: {} is not "
            "set to a private file outside the task worktree".format(
                ATTESTATION_KEY_FILE_ENV
            )
        )
    path, resolved = location
    if isinstance(data, str):
        key = data.strip().encode("utf-8")
    else:
        key = bytes(data).strip()
    if not key:
        raise VerificationError(
            "cannot provision an empty verification attestation key"
        )
    try:
        existing = path.read_bytes().strip()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise VerificationError(
            "the existing verification attestation key file cannot be read: "
            "{}".format(exc)
        ) from exc
    if existing is not None:
        if existing != key:
            raise VerificationError(
                "refusing to replace a different verification attestation key "
                "at {}".format(path)
            )
        _ATTESTATION_KEY_CACHE[str(resolved)] = key
        _ATTESTATION_KEY_CONSUMED.discard(str(resolved))
        return path
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".provision",
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        # Hard-link rather than replace so a concurrent provisioning process
        # cannot be clobbered with a different key.
        os.link(temporary, path)
    except FileExistsError:
        try:
            existing = path.read_bytes().strip()
        except OSError as exc:  # pragma: no cover - raced file disappeared
            raise VerificationError(
                "the verification attestation key file cannot be provisioned: "
                "{}".format(exc)
            ) from exc
        if existing != key:
            raise VerificationError(
                "refusing to replace a different verification attestation key "
                "at {}".format(path)
            )
    except OSError as exc:
        raise VerificationError(
            "the verification attestation key file cannot be provisioned: "
            "{}".format(exc)
        ) from exc
    finally:
        if temporary:
            try:
                Path(temporary).unlink()
            except OSError:
                pass
    _ATTESTATION_KEY_CACHE[str(resolved)] = key
    _ATTESTATION_KEY_CONSUMED.discard(str(resolved))
    return path


def _attestation_payload(data: Mapping[str, Any]) -> bytes:
    """Canonical bytes of the trusted evidence sections."""

    trusted = {
        section: data[section]
        for section in ATTESTED_SECTIONS
        if isinstance(data.get(section), dict)
    }
    return json.dumps(
        trusted, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sign_evidence(data: Dict[str, Any], key: bytes) -> None:
    data["attestation"] = {
        "algorithm": ATTESTATION_ALGORITHM,
        "signature": hmac.new(
            key, _attestation_payload(data), hashlib.sha256
        ).hexdigest(),
    }


def _verify_evidence(data: Mapping[str, Any], key: bytes) -> None:
    record = data.get("attestation")
    if not isinstance(record, dict):
        raise VerificationError(
            "verification evidence is not attested; refusing to trust worktree evidence"
        )
    if str(record.get("algorithm") or "") != ATTESTATION_ALGORITHM:
        raise VerificationError(
            "verification evidence uses an unsupported attestation algorithm"
        )
    expected = hmac.new(
        key, _attestation_payload(data), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, str(record.get("signature") or "")):
        raise VerificationError(
            "verification evidence attestation does not match; refusing to trust worktree evidence"
        )


def _load_evidence(
    path: Path, environ: Optional[Mapping[str, str]] = None
) -> Dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION}
    try:
        data = json.loads(_read_text(path))
    except Exception as exc:
        raise VerificationError(
            "verification evidence is not readable JSON: {}".format(exc)
        ) from exc
    if not isinstance(data, dict):
        raise VerificationError("verification evidence must be a JSON object")
    if int(data.get("schema_version") or 0) != SCHEMA_VERSION:
        raise VerificationError("verification evidence schema is unsupported")
    key = _attestation_key(environ)
    if key is not None:
        _verify_evidence(data, key)
    return data


def _save_evidence(
    path: Path,
    data: Dict[str, Any],
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    key = _attestation_key(environ)
    if key is not None:
        _sign_evidence(data, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _command_result(completed: subprocess.CompletedProcess, started: float) -> Dict[str, Any]:
    output = (completed.stdout or "")
    return {
        "exit_code": int(completed.returncode),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "output_tail": output[-4000:],
    }


def run_command_set(
    contract: VerificationContract,
    repo: Path | str,
    environ: Optional[Mapping[str, str]] = None,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Execute one resolved command set exactly as recorded."""

    root = Path(repo)
    source_environ = environ if environ is not None else os.environ
    # Read the key into memory, then remove its on-disk copy before any
    # repository-controlled command runs. The copy is never restored: a command
    # can spawn a same-uid survivor that reads it after this process exits.
    # A later invocation re-provisions it from the authoritative source.
    _consume_attestation_key(source_environ, worktree=root)
    env = _child_environment(source_environ)
    timeout = timeout_seconds
    if timeout is None:
        raw = str(env.get("CONTINUUM_VERIFICATION_TIMEOUT_SECONDS", "") or "")
        try:
            timeout = float(raw) if raw else None
        except ValueError:
            raise VerificationError(
                "CONTINUUM_VERIFICATION_TIMEOUT_SECONDS must be numeric"
            ) from None
    results: List[Dict[str, Any]] = []
    for command in contract.commands:
        started = time.monotonic()
        result: Dict[str, Any]
        try:
            completed = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", command.command],
                cwd=str(root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            result = _command_result(completed, started)
        except subprocess.TimeoutExpired as exc:
            output = ""
            for stream in (exc.stdout, exc.stderr):
                if isinstance(stream, bytes):
                    output += stream.decode("utf-8", "replace")
                elif stream:
                    output += str(stream)
            result = {
                "exit_code": 124,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
                "output_tail": output[-4000:],
                "timed_out": True,
            }
        result.update(
            {
                "name": command.name,
                "command": command.command,
                "source": command.source,
            }
        )
        results.append(result)
    failed = [item["name"] for item in results if int(item["exit_code"]) != 0]
    return {
        "phase": "verification",
        "recorded_at": _now(),
        "environment_fingerprint": environment_fingerprint(root, env),
        "state": "failed" if failed else "passed",
        "commands": results,
        "failed_commands": failed,
    }


def _contract_from_evidence(data: Mapping[str, Any]) -> VerificationContract:
    section = data.get("contract")
    if not isinstance(section, dict):
        raise VerificationError("verification evidence has no resolved contract")
    commands = section.get("commands")
    if not isinstance(commands, list):
        raise VerificationError("verification evidence contract has no commands")
    resolved: List[VerificationCommand] = []
    for raw in commands:
        if not isinstance(raw, dict):
            raise VerificationError("verification evidence command is malformed")
        name = str(raw.get("name") or "").strip()
        command = str(raw.get("command") or "").strip()
        source = str(raw.get("source") or "").strip()
        if not name or not command:
            raise VerificationError("verification evidence command is incomplete")
        resolved.append(
            VerificationCommand(name=name, command=command, source=source)
        )
    contract = VerificationContract(
        state=str(section.get("state") or ""),
        source=str(section.get("source") or ""),
        commands=tuple(resolved),
    )
    expected = str(section.get("fingerprint") or "")
    if expected and contract.fingerprint != expected:
        raise VerificationError(
            "verification evidence contract fingerprint does not match its commands"
        )
    return contract


def ensure_baseline(
    repo: Path | str,
    evidence_path: Path | str,
    base_sha: str,
    environ: Optional[Mapping[str, str]] = None,
    timeout_seconds: Optional[float] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Ensure a pre-change baseline exists for ``base_sha``.

    Returns ``(evidence, reused)``. A configured command set that fails still
    produces evidence and does not abort the implementation; the final phase
    decides whether a post-change result is acceptable. An unresolved contract,
    malformed evidence, or an attempt to baseline an already-mutated checkout
    raises :class:`VerificationError` and must fail closed.
    """

    root = Path(repo)
    path = Path(evidence_path)
    env = environ if environ is not None else os.environ
    base = str(base_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise VerificationError("baseline base SHA is malformed")

    data = _load_evidence(path, env)
    contract = resolve_contract(root, env)
    if not contract.configured:
        raise VerificationError(contract.errors[0] if contract.errors else "unconfigured verification contract")

    existing = data.get("baseline")
    # Worktree evidence is only reusable when it is attested by the
    # runner-controlled key. An unattested baseline is rebuilt below instead of
    # being trusted after the coding agent could have rewritten it.
    if isinstance(existing, dict) and _attestation_key(env) is not None:
        recorded_contract = existing.get("contract")
        recorded_fingerprint = ""
        if isinstance(recorded_contract, dict):
            recorded_fingerprint = str(recorded_contract.get("fingerprint") or "")
        if recorded_fingerprint != contract.fingerprint:
            raise VerificationError(
                "the resolved verification contract changed after the baseline was recorded"
            )
        recorded_base = str(existing.get("base_sha") or "").strip().lower()
        if recorded_base != base:
            raise VerificationError(
                "the recorded baseline belongs to a different task base"
            )
        if not _is_ancestor(root, base):
            raise VerificationError(
                "the recorded baseline base is not an ancestor of the current checkout"
            )
        return data, True

    head = _git_value(root, "rev-parse", "HEAD").lower()
    if head != base:
        hint = ""
        if isinstance(existing, dict):
            hint = (
                " (an unattested baseline was recorded; set {} so a resumed "
                "run can trust it)".format(ATTESTATION_KEY_FILE_ENV)
            )
        raise VerificationError(
            "cannot establish a pre-change baseline: the checkout is already "
            "past the task base ({} != {}){}".format(head, base, hint)
        )
    if task_changes_present(root, base):
        raise VerificationError(
            "cannot establish a pre-change baseline: the checkout already "
            "contains uncommitted task changes"
        )

    result = run_command_set(contract, root, env, timeout_seconds)
    result.update(
        {
            "phase": "baseline",
            "base_sha": base,
            "head_sha": head,
            "contract": contract.as_dict(),
        }
    )
    data.update(
        {
            "schema_version": SCHEMA_VERSION,
            "contract": contract.as_dict(),
            "baseline": result,
        }
    )
    _save_evidence(path, data, env)
    if result["state"] != "passed":
        print(
            "::warning::Baseline verification failed for: {}. The failure is "
            "recorded as pre-existing and the implementation must not claim a "
            "green result unless the same command set passes after the change.".format(
                ", ".join(result["failed_commands"])
            )
        )
    else:
        print("Baseline verification passed for the resolved repository command set.")
    return data, False


def verifier_required(
    evidence_path: Path | str,
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    path = Path(evidence_path)
    data = _load_evidence(path, environ)
    baseline = data.get("baseline")
    if not isinstance(baseline, dict):
        raise VerificationError("verification evidence has no baseline")
    # The worktree can rewrite an unattested verifier record. Without the
    # runner-held key, it cannot prove that this session result is authentic.
    if _attestation_key(environ) is None:
        return True
    verifier = data.get("verifier")
    if not isinstance(verifier, dict):
        return True
    if str(verifier.get("state") or "") != "passed":
        return True
    return str(verifier.get("contract_fingerprint") or "") != str(
        (baseline.get("contract") or {}).get("fingerprint") or ""
    )


def record_verifier(
    repo: Path | str,
    evidence_path: Path | str,
    exit_code: int,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Record the distinct fresh verifier/fix session outcome."""

    root = Path(repo)
    path = Path(evidence_path)
    data = _load_evidence(path, environ)
    baseline = data.get("baseline")
    if not isinstance(baseline, dict):
        raise VerificationError("verification evidence has no baseline")
    contract = _contract_from_evidence(data)
    status = _run_git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    if status.returncode != 0:
        raise VerificationError("could not inspect the verifier worktree")
    changed_paths = [
        line[3:].strip()
        for line in (status.stdout or "").splitlines()
        if line.strip()
        and line[3:].strip() not in WORKFLOW_OWNED_PATHS
    ]
    verifier = {
        "phase": "verifier",
        "recorded_at": _now(),
        "state": "passed" if int(exit_code) == 0 else "failed",
        "exit_code": int(exit_code),
        "contract_fingerprint": contract.fingerprint,
        "head_sha": _git_value(root, "rev-parse", "HEAD").lower(),
        "changed_paths": changed_paths,
    }
    data["verifier"] = verifier
    _save_evidence(path, data, environ)
    return verifier


def _comparison(
    baseline: Mapping[str, Any], final: Mapping[str, Any]
) -> Dict[str, Any]:
    baseline_results = {
        str(item.get("name")): int(item.get("exit_code") or 0)
        for item in baseline.get("commands", [])
        if isinstance(item, dict)
    }
    final_results = {
        str(item.get("name")): int(item.get("exit_code") or 0)
        for item in final.get("commands", [])
        if isinstance(item, dict)
    }
    new_failures = [
        name
        for name, code in final_results.items()
        if code != 0 and baseline_results.get(name, 0) == 0
    ]
    preexisting_failures = [
        name
        for name, code in final_results.items()
        if code != 0 and baseline_results.get(name, 0) != 0
    ]
    resolved_failures = [
        name
        for name, code in baseline_results.items()
        if code != 0 and final_results.get(name, 0) == 0
    ]
    final_passed = str(final.get("state") or "") == "passed"
    if final_passed and str(baseline.get("state") or "") == "failed":
        result = "passed-after-preexisting-failure"
    elif final_passed:
        result = "passed"
    elif new_failures:
        result = "failed-new-regression"
    elif preexisting_failures:
        result = "failed-preexisting"
    else:
        result = "failed"
    return {
        "result": result,
        "accepted": final_passed,
        "new_failures": new_failures,
        "preexisting_failures": preexisting_failures,
        "resolved_failures": resolved_failures,
        "baseline_state": baseline.get("state"),
        "final_state": final.get("state"),
    }


def _baseline_at_base(
    root: Path,
    contract: VerificationContract,
    base: str,
    env: Mapping[str, str],
    timeout_seconds: Optional[float],
    environment: str,
    previous_environment: str = "",
) -> Dict[str, Any]:
    """Run the contract against the task base in a throwaway worktree.

    The task checkout has already moved past the base, so the baseline is
    re-derived from the trusted base commit instead of the recorded worktree
    evidence.
    """

    if not _is_ancestor(root, base):
        raise VerificationError(
            "cannot re-establish the verification baseline: the task base is "
            "not an ancestor of the current checkout"
        )
    temporary = tempfile.mkdtemp(prefix="continuum-verification-")
    try:
        added = _run_git(root, "worktree", "add", "--detach", temporary, base)
        if added.returncode != 0:
            raise VerificationError(
                "could not re-establish the verification baseline on the current "
                "runner: {}".format((added.stderr or "").strip()[-1000:])
            )
        result = run_command_set(contract, temporary, env, timeout_seconds)
    finally:
        _run_git(root, "worktree", "remove", "--force", temporary)
        shutil.rmtree(temporary, ignore_errors=True)
    result.update(
        {
            "phase": "baseline",
            "base_sha": base,
            "head_sha": base,
            "contract": contract.as_dict(),
            # The commands ran in the same environment as the final pass; only
            # the temporary worktree path differs from the task checkout.
            "environment_fingerprint": environment,
            "environment_rebaselined": True,
            "previous_environment_fingerprint": previous_environment,
        }
    )
    return result


def _reestablish_baseline(
    root: Path,
    evidence_path: Path,
    data: Dict[str, Any],
    baseline: Mapping[str, Any],
    contract: VerificationContract,
    base: str,
    env: Mapping[str, str],
    timeout_seconds: Optional[float],
    environment: str,
) -> Dict[str, Any]:
    """Re-run the baseline at the task base on a resumed, changed runner.

    A 429 evacuation moves the task onto a replacement runner whose image or
    ``PATH`` may differ. The restored baseline is then incomparable. Rebasing
    only the environment fingerprint would keep stale results, so re-establish
    a real baseline against the same task base before the final run.
    """

    result = _baseline_at_base(
        root,
        contract,
        base,
        env,
        timeout_seconds,
        environment,
        previous_environment=str(baseline.get("environment_fingerprint") or ""),
    )
    data["baseline"] = result
    data["contract"] = contract.as_dict()
    _save_evidence(evidence_path, data, env)
    print(
        "Verification environment changed on a resumed run; re-established the "
        "baseline in the current environment."
    )
    return result


def run_final(
    repo: Path | str,
    evidence_path: Path | str,
    base_sha: str,
    environ: Optional[Mapping[str, str]] = None,
    timeout_seconds: Optional[float] = None,
    resume_stage: str = "",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Repeat the exact baseline command set and decide acceptance.

    Returns ``(evidence, comparison)``. ``comparison['accepted']`` is true only
    when the post-change command set actually passed. A pre-existing failure is
    recorded distinctly but never converted into a green result.
    """

    root = Path(repo)
    path = Path(evidence_path)
    env = environ if environ is not None else os.environ
    base = str(base_sha or "").strip().lower()
    attested = _attestation_key(env) is not None
    data = _load_evidence(path, env)
    baseline = data.get("baseline")
    if not isinstance(baseline, dict):
        raise VerificationError("verification evidence has no baseline")
    if str(baseline.get("base_sha") or "").strip().lower() != base:
        raise VerificationError("verification evidence baseline base does not match")
    recorded_contract = _contract_from_evidence(data)
    if not recorded_contract.configured:
        raise VerificationError("verification evidence contract is not executable")
    if attested:
        # _load_evidence already verified the signature over this contract.
        contract = recorded_contract
    else:
        # The evidence file lives in the task worktree, where the coding agent
        # can rewrite both the commands and their unkeyed fingerprint. Resolve
        # the contract again from the immutable task base commit (repository
        # variables stay runner-controlled) and run exactly that command set.
        contract = resolve_contract(root, env, ref=base)
        if not contract.configured:
            raise VerificationError(
                contract.errors[0]
                if contract.errors
                else "verification contract is not configured at the task base"
            )
        recorded_fingerprint = str(
            (baseline.get("contract") or {}).get("fingerprint") or ""
        )
        if contract.fingerprint != recorded_fingerprint:
            raise VerificationError(
                "the verification contract resolved at the task base does not "
                "match the recorded baseline; refusing unattested evidence"
            )

    baseline_environment = str(baseline.get("environment_fingerprint") or "")
    current_environment = environment_fingerprint(root, env)

    # A checkpoint after the final pass already carries exact evidence. Reuse
    # it only for an explicit tests-passed/publish-pr resume; never for a
    # normal run that could have changed the tree. Reuse precedes the
    # environment comparison because the resumed runner is expected to differ
    # and the already-passed result does not need to be recomputed there.
    # Unattested worktree evidence is never reused: the agent could have
    # forged the accepted record.
    existing_final = data.get("final")
    if (
        attested
        and resume_stage in ("tests-passed", "publish-pr")
        and isinstance(existing_final, dict)
        and str(existing_final.get("state") or "") == "passed"
        and str(existing_final.get("contract_fingerprint") or "")
        == contract.fingerprint
    ):
        comparison = existing_final.get("comparison")
        if isinstance(comparison, dict) and bool(comparison.get("accepted")):
            print("Reusing exact post-change verification evidence from the 429 checkpoint.")
            return data, comparison

    if not attested:
        # Re-derive the baseline from the trusted base commit instead of
        # accepting the recorded results from the writable evidence file.
        baseline = _baseline_at_base(
            root,
            contract,
            base,
            env,
            timeout_seconds,
            current_environment,
            previous_environment=baseline_environment,
        )
        data["baseline"] = baseline
        data["contract"] = contract.as_dict()
    elif baseline_environment and current_environment != baseline_environment:
        if not resume_stage:
            raise VerificationError(
                "verification environment changed between baseline and final; the "
                "results are not comparable"
            )
        baseline = _reestablish_baseline(
            root,
            path,
            data,
            baseline,
            contract,
            base,
            env,
            timeout_seconds,
            current_environment,
        )

    result = run_command_set(contract, root, env, timeout_seconds)
    result["contract_fingerprint"] = contract.fingerprint
    result["base_sha"] = base
    result["head_sha"] = _git_value(root, "rev-parse", "HEAD").lower()
    comparison = _comparison(baseline, result)
    result["comparison"] = comparison
    data["final"] = result
    data["result"] = comparison
    _save_evidence(path, data, env)
    if comparison["accepted"]:
        print(
            "Post-change verification passed using the same resolved command set "
            "as the baseline ({}).".format(comparison["result"])
        )
    else:
        print(
            "::error::Post-change verification failed ({}). New failures: {}; "
            "pre-existing failures: {}.".format(
                comparison["result"],
                ", ".join(comparison["new_failures"]) or "none",
                ", ".join(comparison["preexisting_failures"]) or "none",
            )
        )
    return data, comparison


def baseline_summary(evidence_path: Path | str) -> str:
    data = _load_evidence(Path(evidence_path))
    baseline = data.get("baseline")
    if not isinstance(baseline, dict):
        return "baseline: not recorded"
    commands = [
        "{}:{}".format(item.get("name"), item.get("exit_code"))
        for item in baseline.get("commands", [])
        if isinstance(item, dict)
    ]
    return "baseline: {} ({})".format(
        baseline.get("state") or "unknown", ", ".join(commands) or "no commands"
    )


def task_changes_present(repo: Path | str, base_sha: str) -> bool:
    """Whether the checkout contains real task changes beyond workflow metadata."""

    root = Path(repo)
    base = str(base_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise VerificationError("task base SHA is malformed")
    completed = _run_git(
        root, "status", "--porcelain=v1", "--untracked-files=normal"
    )
    if completed.returncode != 0:
        raise VerificationError("could not inspect the task worktree")
    for line in (completed.stdout or "").splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().split(" -> ")[-1]
        if _is_workflow_owned(path):
            continue
        return True
    committed = _run_git(root, "diff", "--name-only", "-z", base, "HEAD")
    if committed.returncode != 0:
        raise VerificationError("could not inspect committed task changes")
    for path in (committed.stdout or "").split("\0"):
        if path and not _is_workflow_owned(path):
            return True
    return False


def _emit(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(
            "usage: python -m continuum.verification "
            "{baseline,needs-verifier,record-verifier,final,summary,changes,"
            "provision-key}",
            file=sys.stderr,
        )
        return 2
    command = args[0]
    options: Dict[str, str] = {}
    index = 1
    while index < len(args):
        item = args[index]
        if not item.startswith("--") or index + 1 >= len(args):
            print("invalid option: {}".format(item), file=sys.stderr)
            return 2
        options[item[2:].replace("-", "_")] = args[index + 1]
        index += 2

    try:
        if command == "baseline":
            data, reused = ensure_baseline(
                options.get("repo", "."),
                options.get("evidence", EVIDENCE_FILE_NAME),
                options.get("base_sha", ""),
            )
            print("baseline_reused={}".format(str(reused).lower()))
            _emit(data.get("baseline", {}))
            return 0
        if command == "needs-verifier":
            required = verifier_required(
                options.get("evidence", EVIDENCE_FILE_NAME)
            )
            print("true" if required else "false")
            return 0
        if command == "record-verifier":
            verifier = record_verifier(
                options.get("repo", "."),
                options.get("evidence", EVIDENCE_FILE_NAME),
                int(options.get("exit_code", "1")),
            )
            _emit(verifier)
            return 0
        if command == "final":
            data, comparison = run_final(
                options.get("repo", "."),
                options.get("evidence", EVIDENCE_FILE_NAME),
                options.get("base_sha", ""),
                resume_stage=options.get("resume_stage", ""),
            )
            _emit(comparison)
            return 0 if comparison.get("accepted") else 1
        if command == "summary":
            print(baseline_summary(options.get("evidence", EVIDENCE_FILE_NAME)))
            return 0
        if command == "changes":
            present = task_changes_present(
                options.get("repo", "."), options.get("base_sha", "")
            )
            print("true" if present else "false")
            return 0
        if command == "provision-key":
            # The key arrives on standard input, never in an argument or the
            # environment, so it never appears in process listings or
            # /proc/$PPID/environ. This is the explicit re-provisioning path
            # after a command-set invocation consumed the on-disk copy.
            provision_attestation_key(
                sys.stdin.buffer.read(),
                worktree=options.get("repo") or None,
            )
            return 0
        print("unknown command: {}".format(command), file=sys.stderr)
        return 2
    except VerificationError as exc:
        print("::error::{}".format(exc), file=sys.stderr)
        return 3
    except (OSError, ValueError) as exc:
        print("::error::verification helper failed: {}".format(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
