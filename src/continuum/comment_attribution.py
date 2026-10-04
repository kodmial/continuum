"""Shared comment-attribution contract for Continuum automation.

Every human-visible GitHub comment created or updated by Continuum must make
machine authorship unambiguous even when the underlying GitHub actor renders
as the repository owner. This module is the single canonical definition of
that presentation contract; workflow bodies and the bundled JS policy must
conform to it byte-for-byte (headers and markers), and the deterministic
suite in ``tests/test_comment_attribution.py`` enforces conformance across
every comment-mutation path.

Message-origin taxonomy (classified by *who is speaking semantically*):

* ``continuum`` -- orchestration and lifecycle automation (scheduler and
  controller state, recovery/retry/backoff, auto-merge and merge gating,
  review routing, conflict-repair orchestration). Visible prefix
  ``⚡ **Continuum · <component>**``.
* ``agent-coder`` -- messages representing the coding/repair agent itself
  (implementation summaries, "no change" explanations, repair evidence).
  The identity is deliberately engine-neutral so the backend can be
  replaced without changing the public taxonomy. Visible prefix
  ``🦾 **Agent Coder · <component>**``.
* ``project`` -- messages reporting facts produced by the consumer
  repository itself (CI/test, canary/health, packaging/release,
  qualification evidence). Visible prefix ``🛰️ **Project · <repo>**``
  where ``<repo>`` is always derived from the current repository context
  and never hard-coded. Third-party systems keep their own identity and
  are never relabeled by this module.

Every Continuum-owned message additionally carries one stable hidden
machine marker used for deterministic identification, upsert matching,
and tests::

    <!-- continuum-origin role=<role> component=<component> -->

The marker never contains secrets, tokens, private child repository names,
or internal paths: ``component`` is a lowercase kebab-case producer name
without slashes, and the project repository display name never appears in
the marker.

Layout rule
-----------
The visible header is the first *rendered* line of the body. Leading
machine lines stay first so existing machine consumers keep working:

* ``/oc`` (and ``/opencode``) command comments keep the command token on
  the first significant line -- the command gate requires it there;
* ``/review``, ``/verify <id>`` and ``@coderabbitai`` command comments
  keep the command line first for third-party parser safety;
* controller/upsert bodies keep their legacy hidden marker lines first so
  positional upsert/election matching is unchanged (HTML comments do not
  render, so the header is still the first rendered line).

Attribution is inserted into the existing body; a second "signature"
comment is never posted, and existing single-comment coalescing semantics
are unchanged.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

#: Semantic origins. Classified by who is speaking, never by credential.
ROLE_CONTINUUM = "continuum"
ROLE_AGENT_CODER = "agent-coder"
ROLE_PROJECT = "project"
ROLES = (ROLE_CONTINUUM, ROLE_AGENT_CODER, ROLE_PROJECT)

#: Visible header prefixes, one per role.
CONTINUUM_PREFIX = "⚡ **Continuum · "
AGENT_CODER_PREFIX = "🦾 **Agent Coder · "
PROJECT_PREFIX = "🛰️ **Project · "
_HEADER_SUFFIX = "**"

#: Hidden machine-marker template. Stable; matched by substring/regex.
ORIGIN_MARKER_TEMPLATE = "<!-- continuum-origin role={role} component={component} -->"

#: Canonical origin-marker pattern used by tests and upsert matchers.
ORIGIN_RE = re.compile(
    r"<!--\s*continuum-origin\s+role=(continuum|agent-coder|project)"
    r"\s+component=([a-z0-9]+(?:-[a-z0-9]+)*)\s*-->"
)

#: Producer component names must be lowercase kebab-case without slashes so
#: a private ``owner/repo`` identity can never hide inside a marker.
COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Repository display names are short names derived from context
#: (``owner/name`` -> ``name``); they never contain a slash.
REPO_DISPLAY_RE = re.compile(r"^[^/\s#@<>]+$")

#: Secret/token shapes that must never appear in an attributed body.
_SECRET_RES = (
    re.compile(r"ghp_[A-Za-z0-9]+"),
    re.compile(r"gho_[A-Za-z0-9_-]+"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"\brnd_[A-Za-z0-9]+\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"xox[abp]-"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

#: Lines that may lead a body before the attribution block: hidden HTML
#: markers, automation command tokens, and the blank lines between them.
_COMMAND_LINE_RE = re.compile(
    r"^(?:/oc(?:-cancel)?|/opencode|/review\b|/verify\b|@coderabbitai\b.*|.*@coderabbitai.*)$"
)
_HIDDEN_MARKER_LINE_RE = re.compile(r"^<!--.*?-->$")


def attribution_header(
    role: str, component: Optional[str] = None, repo: Optional[str] = None
) -> str:
    """Return the visible header line for one Continuum-owned message."""
    if role == ROLE_CONTINUUM:
        return f"{CONTINUUM_PREFIX}{require_component(component)}{_HEADER_SUFFIX}"
    if role == ROLE_AGENT_CODER:
        return f"{AGENT_CODER_PREFIX}{require_component(component)}{_HEADER_SUFFIX}"
    if role == ROLE_PROJECT:
        return f"{PROJECT_PREFIX}{require_repo_display(repo)}{_HEADER_SUFFIX}"
    raise ValueError(f"unknown attribution role: {role!r}")


def origin_marker(role: str, component: str) -> str:
    """Return the stable hidden machine marker for one producer."""
    if role not in ROLES:
        raise ValueError(f"unknown attribution role: {role!r}")
    return ORIGIN_MARKER_TEMPLATE.format(
        role=role, component=require_component(component)
    )


def require_component(component: Optional[str]) -> str:
    """Validate a producer component name (never a repository identity)."""
    text = str(component or "").strip()
    if not COMPONENT_RE.match(text):
        raise ValueError(
            f"invalid attribution component: {component!r} "
            "(lowercase kebab-case, no slashes or repository names)"
        )
    return text


def require_repo_display(repo: Optional[str]) -> str:
    """Derive a safe short display name from a repository context value."""
    text = str(repo or "").strip()
    short = text.split("/")[-1].strip()
    if not short or not REPO_DISPLAY_RE.match(short):
        raise ValueError(
            f"invalid repository display name: {repo!r} "
            "(derive from context; never hard-code a consumer name)"
        )
    return short


def contains_secret(text: str) -> Optional[str]:
    """Return the offending secret shape in text, or None when clean."""
    for pattern in _SECRET_RES:
        match = pattern.search(text or "")
        if match:
            return match.group(0)[:12]
    return None


def split_leading_machine_lines(body: str) -> Tuple[str, str]:
    """Split leading machine lines (markers/commands/blanks) from prose.

    Returns ``(head, rest)`` where ``head`` holds the leading machine
    section verbatim (including its trailing blank separator) and ``rest``
    holds the remaining human prose. A body that opens with prose yields
    an empty ``head``.
    """
    lines = str(body or "").split("\n")
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if (
            not stripped
            or _HIDDEN_MARKER_LINE_RE.match(stripped)
            or _COMMAND_LINE_RE.match(stripped)
        ):
            index += 1
        else:
            break
    head = "\n".join(lines[:index])
    rest = "\n".join(lines[index:])
    if head and rest:
        head += "\n"
    return head, rest


def with_attribution(
    body: str,
    role: str,
    component: str,
    repo: Optional[str] = None,
) -> str:
    """Insert the attribution block into an existing comment body.

    The header/marker pair is placed after any leading machine lines and
    before the human prose, and is never duplicated when already present.
    No second comment is created; callers keep their existing
    create/update/upsert call.
    """
    text = str(body or "")
    header = attribution_header(role, component, repo)
    marker = origin_marker(role, component)
    if header in text and marker in text:
        return text
    head, rest = split_leading_machine_lines(text)
    block = header + "\n" + marker
    if head:
        return head + block + "\n" + rest if rest else head + block
    return block + "\n" + text if text else block


def ensure_attribution(
    body: str,
    role: str,
    component: str,
    repo: Optional[str] = None,
) -> str:
    """Upsert-safe attribution: preserve existing markers, add what's missing.

    Update paths must never drop the origin marker or the visible header
    when rewriting a controller comment; this repair keeps both while
    leaving every other byte of the body untouched.
    """
    text = str(body or "")
    header = attribution_header(role, component, repo)
    marker = origin_marker(role, component)
    if header not in text or marker not in text:
        return with_attribution(text, role, component, repo)
    return text


def parse_origin(body: str) -> Optional[Tuple[str, str]]:
    """Return the ``(role, component)`` of the first origin marker in a body."""
    match = ORIGIN_RE.search(str(body or ""))
    if not match:
        return None
    return match.group(1), match.group(2)
