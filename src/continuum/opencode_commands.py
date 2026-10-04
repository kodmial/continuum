"""Exact parsing for manual OpenCode issue-comment commands.

A single deterministic rule decides whether an issue comment is a manual
OpenCode command. Every Continuum controller that admits owner commands --
the OpenCode entry path, the scheduler grace logic, the scheduler caller
stub, and the watchdog recovery path -- must apply exactly this rule, so an
automation-authored explanatory comment that merely mentions ``/oc`` or
``/opencode`` in prose can never be mistaken for a fresh command.

The rule
--------
A comment is a manual command if and only if its first significant line is
exactly one of the supported command tokens. Surrounding whitespace is
allowed: leading blank lines are skipped, up to three leading spaces are
tolerated, and trailing whitespace is ignored. Everything else is rejected:

* prose that mentions a token mid-sentence or mid-line;
* inline code (`` `/oc` ``), block quotes (``> /oc``), and fenced Markdown
  blocks, which never have the bare token as their first significant line;
* indented code blocks (a tab, or four or more leading spaces);
* a token with trailing arguments or prose on the same line (``/oc please``);
* lookalike tokens (``/octopus``, ``/oc-cancel`` is cancellation, not a run).

Why the first line, and not "the token appears anywhere"
---------------------------------------------------------
Scheduler dispatches and watchdog recoveries post the bare token on the
first line followed by a machine marker (``<!-- issue-scheduler-dispatch -->``
or ``<!-- opencode-watchdog-retry -->``). Owner-typed commands are the bare
token, optionally padded with whitespace. Automation-authored explanatory
comments instead open with prose and only mention the token later. Requiring
the token on the first significant line therefore preserves every intended
launch (manual, scheduler, watchdog) while rejecting every incidental
mention, which is exactly what broke the Work Lock canary: an explanatory
comment posted through the owner PAT mentioned the token in prose, the
substring gate treated it as a fresh command, the next explanatory comment
repeated the behavior, and runs chained until the canary was paused.

What this module does not decide
--------------------------------
* Authorship. The parser is body-only. Callers must still require the
  repository owner as the comment author (``github.actor ==
  github.repository_owner``); see :func:`is_owner_command`.
* The pull-request interactive path. PR issue comments additionally require
  the PR to still be open and the command token at the start of the comment
  (``startsWith`` in the workflow expression, not ``contains``), so
  agent-generated prose that mentions ``/oc`` later can never chain a
  self-triggered run. Review comments keep their existing owner gate.
* Cancellation of running work. ``/oc-cancel`` is a launch guard only:
  Continuum has no mechanism that cancels an already-running run.

Standard library only.
"""

from __future__ import annotations

#: Tokens that launch an implementation run from an issue comment.
IMPLEMENT_COMMANDS = ("/oc", "/opencode")

#: Token that suppresses an agent launch. It is a launch guard only.
CANCEL_COMMAND = "/oc-cancel"

#: Classification results of :func:`classify_issue_comment`.
RUN = "run"
CANCEL = "cancel"
IGNORE = "ignore"

#: Marker the scheduler writes into its dispatch comments.
DEFAULT_DISPATCH_MARKER = "<!-- issue-scheduler-dispatch -->"

#: Marker the watchdog writes into its recovery comments.
DEFAULT_RETRY_MARKER = "<!-- opencode-watchdog-retry -->"

#: Spaces tolerated before a command token. Four or more leading spaces (or a
#: tab) is an indented Markdown code block, which must never qualify.
_MAX_COMMAND_INDENT = 3


def first_command_line(body) -> str | None:
    """The first significant line of a comment, or None.

    Blank lines (empty or whitespace-only) are skipped. The returned line has
    surrounding horizontal whitespace removed. None is returned when the body
    is not text, holds no significant line, or its first significant line is
    indented code (a leading tab, or more than three leading spaces).
    """

    if not isinstance(body, str):
        return None
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    for raw in text.split("\n"):
        if raw.strip() == "":
            continue
        line = raw.lstrip("\ufeff")
        if line.startswith("\t"):
            return None
        leading_spaces = len(line) - len(line.lstrip(" "))
        if leading_spaces > _MAX_COMMAND_INDENT:
            return None
        return line.strip(" \t")
    return None


def parse_manual_command(body) -> str | None:
    """The manual launch token in a comment, or None.

    Returns ``"/oc"`` or ``"/opencode"`` only for the exact supported forms.
    """

    line = first_command_line(body)
    if line in IMPLEMENT_COMMANDS:
        return line
    return None


def is_manual_command(body) -> bool:
    """Whether a comment is an explicit manual implementation command."""

    return parse_manual_command(body) is not None


def parse_cancel_command(body) -> str | None:
    """The cancellation token in a comment, or None."""

    if first_command_line(body) == CANCEL_COMMAND:
        return CANCEL_COMMAND
    return None


def is_cancel_command(body) -> bool:
    """Whether a comment is an explicit cancellation command."""

    return parse_cancel_command(body) is not None


def classify_issue_comment(body) -> str:
    """Classify an issue comment as ``"run"``, ``"cancel"``, or ``"ignore"``.

    Cancellation is checked first so the ``/oc`` substring inside
    ``/oc-cancel`` can never read as a launch: under exact matching the two
    forms are disjoint, and the order makes that explicit.
    """

    if is_cancel_command(body):
        return CANCEL
    if is_manual_command(body):
        return RUN
    return IGNORE


def _logins_equal(first, second) -> bool:
    if not isinstance(first, str) or not isinstance(second, str):
        return False
    if not first.strip() or not second.strip():
        return False
    return first.strip().casefold() == second.strip().casefold()


def is_owner_command(body, author_login, owner_login) -> bool:
    """Whether a comment is an authorized manual command.

    Combines the body rule with repository-owner authorization: the comment
    author must be the repository owner. GitHub logins compare
    case-insensitively. An empty owner fails closed.
    """

    return is_manual_command(body) and _logins_equal(author_login, owner_login)


def is_scheduler_dispatch(body, dispatch_marker=DEFAULT_DISPATCH_MARKER) -> bool:
    """Whether a comment carries the scheduler's dispatch marker."""

    if not isinstance(body, str) or not isinstance(dispatch_marker, str):
        return False
    if not dispatch_marker:
        return False
    return dispatch_marker in body


def is_watchdog_recovery(body, retry_marker=DEFAULT_RETRY_MARKER) -> bool:
    """Whether a comment carries the watchdog's recovery marker."""

    if not isinstance(body, str) or not isinstance(retry_marker, str):
        return False
    if not retry_marker:
        return False
    return retry_marker in body
