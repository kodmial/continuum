#!/usr/bin/env python3
"""Conflict-repair policy: repair the completed change, never redo the task.

Incident this module exists for
-------------------------------
On 2026-09-28 a completed pull request (runtime-lab #66, from issue #56) was
frozen as ``no-auto-merge`` + ``opencode-conflict-repair`` after ``main``
advanced. Conflict recovery then dispatched the agent in **issue mode**, which
re-ran the entire source task from the new ``main`` instead of resolving the
change that already existed. The replacement run spent ~35 minutes repeating
expensive research, hit the workflow timeout, published nothing, and left the
original pull request frozen with no watchdog to release it. The task was
unrecoverable even though the implementation had been finished the day before.

The fix is a decision, not a retry: a merge conflict is a request to **repair
an existing change**, and repairing a change is a bounded, mechanical-first
operation. Redoing the source issue is not on the ladder, so it cannot be
selected by any input.

Recovery ladder
---------------
``evaluate_recovery`` walks a closed, ordered list of rungs and returns the
first one the caller has not already ruled out:

1. ``update-branch``    GitHub's own merge. No agent, no token, no risk.
2. ``replay-commits``   Replay the pull request's own commits onto the current
   base head (``git rebase``). Pure git; the commits and their content are
   preserved verbatim.
3. ``resolve-hunks``    The only rung that calls a model, and only for the
   hunks that rungs 1 and 2 could not replay. Bounded by
   :data:`REPAIR_BUDGET_MINUTES`, which is deliberately unrelated to the
   duration of the source task.
4. ``failed``           The ladder is exhausted or the attempt budget is spent.
   An explicit, visible failure state — never a silent stall.

Two invariants make the incident structurally impossible to repeat:

* The rung list is a closed constant that contains no "re-run the source task"
  action, so no input can select one.
* :func:`repair_budget_minutes` caps a repair at a small fixed budget. A
  35-minute source task cannot decide how long its own conflict repair runs.

Transactional publication
-------------------------
:func:`evaluate_publication` refuses to freeze or supersede the original pull
request unless a replacement head provably exists. A repair that failed leaves
its pull request live and reconcilable, so a timeout can never strand a
finished change behind an opt-out label.

Every function is pure: no network, no filesystem, no clock. The CLI is the
only place that touches the environment, and the workflows consume its output
rather than re-deriving the ladder in shell.

CLI
---
    budget            Print ``task_timeout_minutes`` for an invocation mode.
    recovery-plan     Print the next repair rung and the lock transition.
    publication-gate  Print whether the original pull request may be frozen.
    episode           Read repair-episode markers and print the latest state.
    watchdog          Print the remediation for a stale repair state.

Environment:
    GITHUB_OUTPUT, CONTINUUM_REPAIR_MAX_ATTEMPTS,
    CONTINUUM_REPAIR_BACKOFF_SECONDS, CONTINUUM_REPAIR_STALE_MINUTES.

Standard library only, so it runs on stock ``ubuntu-latest`` runners without
dependency installation.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import sys

# --------------------------------------------------------------------------- #
# Policy constants
# --------------------------------------------------------------------------- #

DEFAULT_BASE_BRANCH = "main"

#: Repair modes operate on a change that already exists. Every other mode is
#: treated as task execution and gets the long budget.
REPAIR_MODES = ("resolve-conflict", "ci-fix")

#: A conflict repair is a bounded operation on a finished change. The source
#: task may have taken three hours; repairing its merge conflict is a few
#: minutes of git plus, only if git cannot finish, a few minutes of model time.
#: Keeping this constant is what stops a long task from setting the repair
#: deadline that stranded runtime-lab #66.
REPAIR_BUDGET_MINUTES = 25

#: Held back from the repair budget for pushing the result and recording the
#: outcome, so a slow model call cannot consume the whole job.
RESERVED_OUTCOME_SECONDS = 300

#: Task execution keeps the generous window that real coding work needs.
TASK_BUDGET_MINUTES = 180

#: Bounded retry. The budget is what separates "a conflict that needs another
#: try" from "a repair loop that would run forever".
DEFAULT_MAX_ATTEMPTS = 3

#: Exponential backoff between attempts, capped so a long outage still retries.
BACKOFF_BASE_SECONDS = 600
BACKOFF_MAX_SECONDS = 7200

#: A repair episode with no live run and no new head for this long is stale.
STALE_EPISODE_MINUTES = 90

#: Upper bound accepted from an episode marker. A real episode stops recording
#: attempts once ``DEFAULT_MAX_ATTEMPTS`` is spent, so nothing beyond a small
#: margin is genuine. Markers are comments, so this bounds what a comment can
#: claim about somebody else's pull request.
MAX_RECORDED_ATTEMPTS = 8

CONFLICT_LOCK_LABEL = "opencode-conflict-repair"
AUTO_MERGE_OPT_OUT_LABEL = "no-auto-merge"
REPAIR_FAILED_LABEL = "opencode-repair-failed"

#: Machine-readable episode record, written as a pull-request comment by the
#: repair controller and by the repair run itself. It is the only durable
#: evidence that lets the watchdog tell a live repair from an abandoned one,
#: and it attributes ``no-auto-merge`` to this controller instead of leaving
#: the watchdog to guess whether a human set it.
EPISODE_MARKER_PREFIX = "continuum-conflict-repair"
EPISODE_MARKER_RE = re.compile(
    r"<!--\s*" + re.escape(EPISODE_MARKER_PREFIX) + r":\s*(?P<fields>[^>]*?)\s*-->"
)

RUNG_REASONS = {
    "update-branch": (
        "Letting GitHub perform its own merge of the current base into the pull "
        "request; this needs no agent, no credential, and keeps the existing "
        "history."
    ),
    "replay-commits": (
        "Replaying the pull request's own commits onto the current base "
        "mechanically; no model is involved and every commit is preserved "
        "verbatim."
    ),
    "resolve-hunks": (
        "Mechanical replay could not finish; delegating only the remaining "
        "conflicting hunks to a model, within a {} minute budget that is "
        "independent of how long the source task took.".format(REPAIR_BUDGET_MINUTES)
    ),
}

#: Ordered recovery ladder. Mechanical rungs first: they need no model, no
#: token, and they preserve the existing commits exactly.
MECHANICAL_RUNGS = ("update-branch", "replay-commits")
AGENT_RUNGS = ("resolve-hunks",)
RECOVERY_RUNGS = MECHANICAL_RUNGS + AGENT_RUNGS

#: Terminal action. Reached only when the ladder is exhausted or the budget is
#: spent; it is a *visible* state, not a stall.
FAILED_ACTION = "failed"

#: States an episode marker may report.
EPISODE_STATES = ("open", "resolved", "failed", "infra_failed", "superseded")

#: Every field a marker must carry to count as a record. A marker missing any of
#: them is not one this controller wrote, so it is not a record at all.
EPISODE_MARKER_FIELDS = frozenset({"episode", "pr", "head", "attempt", "state"})

#: A full commit id, or nothing.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------------- #
# Decision type
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Decision:
    """Outcome of a conflict-repair evaluation.

    ``action`` is never inferred from a partial answer: it is the result of
    every check having passed. The lock fields are part of the decision rather
    than something the caller has to remember, because the runtime-lab #66
    failure was precisely a lock that outlived its episode.
    """

    action: str
    code: str
    reason: str
    #: Whether the caller must add/keep the ``opencode-conflict-repair`` lock.
    hold_lock: bool = False
    #: Whether the caller must remove that lock. Releasing is always safe: the
    #: attempt budget, not the lock, is what bounds retries.
    release_lock: bool = False
    #: Whether the original pull request may be frozen or superseded. Only ever
    #: true once a replacement head exists.
    freeze_original: bool = False
    #: Whether a human-visible failure state should be recorded.
    record_failure: bool = False
    #: Whether another attempt is possible after this decision.
    retryable: bool = False
    retry_after_seconds: int = 0
    task_timeout_minutes: int = 0
    #: Hard cap on a single model call inside a repair. Smaller than the job
    #: budget on purpose, so the agent is stopped while there is still time to
    #: push whatever it produced and record the outcome.
    agent_timeout_seconds: int = 0
    episode_attempts: int = 0
    head_sha: str = ""

    def as_outputs(self) -> dict:
        return {
            "action": self.action,
            "repair_code": self.code,
            "repair_reason": " ".join((self.reason or "").split()),
            "repair_action": self.action if self.action in RECOVERY_RUNGS else "",
            "hold_lock": "true" if self.hold_lock else "false",
            "release_lock": "true" if self.release_lock else "false",
            "freeze_original": "true" if self.freeze_original else "false",
            "record_failure": "true" if self.record_failure else "false",
            "retryable": "true" if self.retryable else "false",
            "retry_after_seconds": str(self.retry_after_seconds),
            "task_timeout_minutes": str(self.task_timeout_minutes),
            "repair_agent_timeout_seconds": str(self.agent_timeout_seconds),
            "episode_attempts": str(self.episode_attempts),
            "head_sha": self.head_sha,
        }


def _decide(action: str, code: str, reason: str, **kwargs) -> Decision:
    return Decision(action=action, code=code, reason=reason, **kwargs)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def one_line(text, limit: int = 480) -> str:
    """Collapse text to a single log-safe line.

    Pull-request and issue text is attacker-influenceable, and this value ends
    up in workflow logs, so control characters and workflow commands are
    removed before the text is ever printed.
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
    cleaned = re.sub(r"(?m)^\s*::", r"\\:", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def non_negative_int(value, default: int = 0) -> int:
    """Parse a non-negative integer, falling back to ``default``.

    A malformed bound must not be read as zero, because zero attempts is a
    silently different policy from the default budget.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value >= 0 else default
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value.strip()):
        return int(value.strip())
    return default


def normalize_ref(value) -> str:
    """Return a printable ref, or ``""`` for anything unusable."""
    if not isinstance(value, str):
        return ""
    ref = value.strip()
    if not ref or len(ref) > 200:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref):
        return ""
    if ".." in ref or "//" in ref:
        return ""
    return ref


def repair_branch_name(pr_number, episode: int) -> str:
    """Name the repair branch for an episode.

    The result is always inside the agent branch shape
    (``opencode/issue<N>-<slug>``) so the repaired pull request stays a
    first-class agent branch: same trust rules, same merge gates, same
    reconciliation. Repairing a change must not move it outside the plane that
    produced it.
    """
    pr = non_negative_int(pr_number)
    ep = non_negative_int(episode, 1) or 1
    if pr <= 0:
        return ""
    return "opencode/issue{}-conflict-repair-{}".format(pr, ep)


def backoff_delay_seconds(
    attempts: int, base: int = BACKOFF_BASE_SECONDS, cap: int = BACKOFF_MAX_SECONDS
) -> int:
    """Exponential, capped backoff for the attempt after ``attempts`` tries.

    Attempt 1 is followed immediately, attempt 2 after ``base`` seconds,
    attempt 3 after ``2 * base``, and so on up to ``cap``. The cap matters: an
    unbounded doubling would mean a repository that is down for a day never
    retries at all.
    """
    tries = non_negative_int(attempts)
    if tries <= 0:
        return 0
    base_seconds = non_negative_int(base, BACKOFF_BASE_SECONDS) or BACKOFF_BASE_SECONDS
    cap_seconds = max(base_seconds, non_negative_int(cap, BACKOFF_MAX_SECONDS))
    delay = base_seconds * (2 ** (tries - 1))
    return min(delay, cap_seconds)


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def repair_budget_minutes(mode, configured_total_minutes=None) -> int:
    """Return the execution budget for ``mode``.

    A repair gets :data:`REPAIR_BUDGET_MINUTES` unconditionally. That is the
    whole point of the incident fix: the length of the source task has no
    bearing on how long repairing its merge conflict may take, so the budget is
    a constant rather than a function of anything the caller can widen.
    """
    if isinstance(mode, str) and mode.strip() in REPAIR_MODES:
        return REPAIR_BUDGET_MINUTES
    configured = non_negative_int(configured_total_minutes)
    return configured if configured >= TASK_BUDGET_MINUTES else TASK_BUDGET_MINUTES


def repair_agent_timeout_seconds(budget_minutes=None) -> int:
    """Hard cap for a single model call inside a repair.

    Deliberately shorter than :func:`repair_budget_minutes` so the agent is
    stopped with time left in the job to push what it produced and record an
    outcome. A repair killed by the job timeout leaves nobody able to say
    whether it worked; a repair stopped here leaves a repairable pull request
    and a recorded attempt.
    """
    budget = non_negative_int(budget_minutes) or REPAIR_BUDGET_MINUTES
    total_seconds = max(300, budget * 60)
    return max(120, total_seconds - RESERVED_OUTCOME_SECONDS)


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #


def evaluate_plan(
    *,
    mode: str = "",
    behind: int = 0,
    ruled_out=(),
    attempts: int = 0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    seconds_since_attempt=None,
) -> Decision:
    """Produce everything the agent job needs before it is allowed to start.

    Task execution and repair execution are different kinds of work and get
    different constants, decided here rather than in shell: the budget, the
    model-call cap, and the rung to start at. A repair never inherits the
    source task's runtime, and a task never inherits a repair's.
    """
    budget = repair_budget_minutes(mode)

    if not (isinstance(mode, str) and mode.strip() in REPAIR_MODES):
        return Decision(
            action="",
            code="task_execution",
            reason="Mode {!r} executes the source task and gets a {} minute "
            "budget.".format(mode, budget),
            task_timeout_minutes=budget,
            episode_attempts=non_negative_int(attempts),
        )

    recovery = evaluate_recovery(
        behind=behind,
        ruled_out=ruled_out,
        attempts=attempts,
        max_attempts=max_attempts,
        seconds_since_attempt=seconds_since_attempt,
    )
    return dataclasses.replace(
        recovery,
        task_timeout_minutes=budget,
        agent_timeout_seconds=repair_agent_timeout_seconds(budget),
    )


# --------------------------------------------------------------------------- #
# Recovery ladder
# --------------------------------------------------------------------------- #


def evaluate_recovery(
    *,
    behind: int = 0,
    ruled_out=(),
    attempts: int = 0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    seconds_since_attempt=None,
    backoff_base_seconds: int = BACKOFF_BASE_SECONDS,
    backoff_max_seconds: int = BACKOFF_MAX_SECONDS,
) -> Decision:
    """Decide the next step of a conflict-repair episode.

    ``ruled_out`` names the rungs the caller already tried and that did not
    work (``update-branch`` when GitHub refused the merge, ``replay-commits``
    when the branch could not be replayed mechanically). The function returns
    the first rung that is still available, so the ladder advances strictly in
    order and is never re-entered.

    The returned action is one of :data:`RECOVERY_RUNGS`, ``wait``, ``none``,
    or ``failed``. There is deliberately no action that re-executes the source
    issue: the ladder is a closed constant, so no combination of inputs can
    select "start the task again".
    """
    behind_count = non_negative_int(behind)
    tried = {rung for rung in (ruled_out or ()) if rung in RECOVERY_RUNGS}
    tries = non_negative_int(attempts)
    budget = non_negative_int(max_attempts, DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS

    if behind_count == 0:
        # The branch already contains the current base. Nothing to repair, and
        # the lock must go: a lock on a healthy pull request is what made the
        # runtime-lab #66 repair invisible to every later reconciliation.
        return _decide(
            "none",
            "already_contains_base",
            "The pull request already contains the current base branch.",
            release_lock=True,
        )

    if tries >= budget:
        return _decide(
            FAILED_ACTION,
            "repair_budget_exhausted",
            "Conflict repair used all {} attempt(s); recording an explicit "
            "failure state and leaving the pull request reconcilable.".format(budget),
            release_lock=True,
            record_failure=True,
            freeze_original=False,
            retryable=False,
            episode_attempts=tries,
        )

    if tries:
        delay = backoff_delay_seconds(
            tries, base=backoff_base_seconds, cap=backoff_max_seconds
        )
        waited = None if seconds_since_attempt is None else non_negative_int(seconds_since_attempt)
        if waited is not None and waited < delay:
            return _decide(
                "wait",
                "repair_backoff",
                "Waiting {}s before conflict repair attempt {} of {}.".format(
                    delay - waited, tries + 1, budget
                ),
                retryable=True,
                retry_after_seconds=delay - waited,
                episode_attempts=tries,
            )

    for rung in RECOVERY_RUNGS:
        if rung in tried:
            continue
        return _decide(
            rung,
            "repair_rung_" + rung.replace("-", "_"),
            RUNG_REASONS[rung],
            hold_lock=True,
            retryable=True,
            episode_attempts=tries,
        )

    # Every rung was tried and none worked. This is a real, reportable failure
    # of the repair of a finished change — not a reason to start the source
    # task over.
    return _decide(
        FAILED_ACTION,
        "recovery_ladder_exhausted",
        "Every conflict-repair rung ({} ) was tried without success; recording "
        "an explicit failure state and leaving the pull request "
        "reconcilable.".format(", ".join(RECOVERY_RUNGS)),
        release_lock=True,
        record_failure=True,
        freeze_original=False,
        retryable=False,
        episode_attempts=tries,
    )


# --------------------------------------------------------------------------- #
# Lock lifecycle
# --------------------------------------------------------------------------- #


def evaluate_lock(
    *,
    lock_present: bool,
    attempts: int = 0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    repair_run_in_flight: bool = False,
) -> Decision:
    """Decide what happens to the ``opencode-conflict-repair`` lock.

    The lock is a *per-attempt* reservation, never a permanent verdict. It is
    held only while an attempt is actually running; once the attempt concludes
    — successfully, with a failure, or by timing out — the lock is released and
    the attempt budget decides whether another try happens. A lock that
    outlives its attempt is the exact condition that made runtime-lab #66
    unrecoverable, so there is no path here that keeps it.

    ``repair_run_in_flight`` is the only thing that may justify holding the
    lock, and a caller that cannot prove an attempt is running must release.
    """
    tries = non_negative_int(attempts)
    budget = non_negative_int(max_attempts, DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS

    if repair_run_in_flight:
        return _decide(
            "hold",
            "repair_run_in_flight",
            "A conflict-repair run is in flight; holding the lock for attempt {} "
            "of {}.".format(max(tries, 1), budget),
            hold_lock=lock_present,
            retryable=True,
            episode_attempts=tries,
        )

    if tries >= budget:
        return _decide(
            "release",
            "repair_budget_exhausted",
            "All {} conflict-repair attempt(s) are spent; releasing the lock so "
            "the pull request is reconcilable again.".format(budget),
            release_lock=lock_present,
            record_failure=True,
            freeze_original=False,
            retryable=False,
            episode_attempts=tries,
        )

    return _decide(
        "release",
        "attempt_concluded",
        "The conflict-repair attempt concluded; releasing the lock so "
        "reconciliation can observe the result.",
        release_lock=lock_present,
        retryable=True,
        episode_attempts=tries,
    )


# --------------------------------------------------------------------------- #
# Transactional publication
# --------------------------------------------------------------------------- #


def evaluate_publication(
    *,
    original_head_sha: str,
    replacement_head_sha: str = "",
    repair_succeeded: bool = False,
    original_frozen: bool = False,
    attempts: int = 0,
    repaired_in_place: bool = False,
) -> Decision:
    """Decide whether the original pull request may be frozen or superseded.

    Publication is transactional: the original is frozen **only after** a
    replacement pull request provably exists.

    ``repaired_in_place`` is what distinguishes a replacement from the repaired
    pull request's own new head. A repair that lands on the same branch moves
    the head, so a distinct sha is not evidence of anything: the pull request
    that was repaired *is* the pull request whose head changed. Reading that as
    a published replacement froze the pull request the repair had just fixed,
    which is the same class of failure as freezing it on a timeout -- a
    finished change made unmergeable by its own repair.

    A failed or unfinished repair never freezes anything. That is what keeps a
    timeout from stranding a finished change: the pull request stays live, the
    opt-out label does not appear, and the next reconciliation sees it.
    """
    original = original_head_sha.strip().lower()
    replacement = replacement_head_sha.strip().lower()
    tries = non_negative_int(attempts)

    if original_frozen:
        return _decide(
            "hold",
            "already_superseded",
            "The original pull request is already superseded.",
            freeze_original=True,
            episode_attempts=tries,
        )

    if repaired_in_place:
        return _decide(
            "keep-live",
            "repaired_in_place",
            "The repair landed on the pull request's own branch, so the pull "
            "request is the repaired change and there is nothing to supersede.",
            freeze_original=False,
            episode_attempts=tries,
        )

    if not repair_succeeded:
        return _decide(
            "keep-live",
            "no_replacement_exists",
            "The repair did not complete, so no replacement head exists; the "
            "original pull request must stay live and reconcilable.",
            freeze_original=False,
            record_failure=True,
            retryable=True,
            episode_attempts=tries,
        )

    if not SHA_RE.fullmatch(replacement) or not SHA_RE.fullmatch(original):
        return _decide(
            "keep-live",
            "unverifiable_replacement",
            "A replacement head could not be verified as a full commit id; the "
            "original pull request stays live rather than being frozen on an "
            "unproven assumption.",
            freeze_original=False,
            episode_attempts=tries,
        )

    if replacement == original:
        return _decide(
            "keep-live",
            "repaired_in_place",
            "The existing pull request was repaired in place; its head moved, "
            "so there is nothing to supersede.",
            freeze_original=False,
            episode_attempts=tries,
        )

    return _decide(
        "supersede",
        "replacement_published",
        "Replacement head {} exists; the original pull request may now be "
        "frozen and superseded.".format(replacement[:12]),
        freeze_original=True,
        episode_attempts=tries,
    )


# --------------------------------------------------------------------------- #
# Episode markers
# --------------------------------------------------------------------------- #


def parse_episode_marker(text) -> dict:
    """Parse the newest repair-episode marker out of ``text``.

    Returns ``{}`` when the text carries no marker. A marker is only honoured
    when *every* field is well formed: a half-valid marker is treated as no
    marker at all, not as a partial record. Anything looser would let a
    hand-written comment with one plausible field and one broken field be
    counted as a real attempt record.
    """
    if not isinstance(text, str):
        return {}
    for match in reversed(list(EPISODE_MARKER_RE.finditer(text))):
        fields = {}
        for token in match.group("fields").split():
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if key in ("episode", "pr", "attempt"):
                # All digits, or nothing. Sanitising instead (``-5`` -> ``5``)
                # turns a malformed marker into a real attempt number, which is
                # the one thing a marker must never be able to do silently.
                if not value.isdigit():
                    continue
            elif key == "head":
                value = value.lower()
                if not SHA_RE.fullmatch(value):
                    continue
            elif key == "state":
                value = value.lower()
                if value not in EPISODE_STATES:
                    continue
            if key:
                fields[key] = value
        if EPISODE_MARKER_FIELDS.issubset(fields):
            return fields
    return {}


def parse_episode_history(texts) -> list:
    """Return the parsed markers found in ``texts``, oldest first.

    Used to count how many repair attempts an episode has already consumed.
    A marker is only counted when it names both a pull request and an attempt
    number, so an unrelated or truncated comment cannot inflate the count.
    """
    episodes = []
    for text in texts or ():
        fields = parse_episode_marker(text)
        if not fields:
            continue
        if "pr" not in fields or "attempt" not in fields:
            continue
        episodes.append(fields)
    return episodes


def latest_episode(texts) -> dict:
    """Return the most recent well-formed episode marker, or ``{}``."""
    history = parse_episode_history(texts)
    if not history:
        return {}

    def sort_key(fields):
        try:
            episode = int(fields.get("episode") or 0)
        except ValueError:
            episode = 0
        try:
            attempt = int(fields.get("attempt") or 0)
        except ValueError:
            attempt = 0
        return (episode, attempt)

    return max(history, key=sort_key)


def episode_attempts(texts, pr_number=None) -> int:
    """Number of repair attempts recorded for ``pr_number``.

    Only markers naming that pull request are counted, so a busy repository
    with many pull requests does not exhaust one pull request's budget with
    another pull request's history.

    The count is also clamped to what an episode can actually have consumed.
    Markers live in pull-request comments, which anybody who can comment can
    write, so a comment claiming nine thousand attempts would otherwise be
    enough to make the controller declare a pull request's repair budget spent
    and stop repairing it -- a denial of repair, delivered with a text comment
    and no write access at all. Anything past the cap is treated as noise.
    """
    wanted = non_negative_int(pr_number)
    latest_by_attempt = {}
    for fields in parse_episode_history(texts):
        if wanted and non_negative_int(fields.get("pr")) != wanted:
            continue
        recorded = non_negative_int(fields.get("attempt"))
        if recorded <= 0 or recorded > MAX_RECORDED_ATTEMPTS:
            continue
        # An attempt is opened before dispatch so a dead runner still leaves a
        # durable trace. If that dispatch later proves to be infrastructure-only,
        # the later infra_failed marker cancels the semantic charge for the same
        # attempt number. A later open/resolved/failed marker for that number
        # makes it semantic again.
        latest_by_attempt[recorded] = fields

    attempts = 0
    for recorded, fields in latest_by_attempt.items():
        if fields.get("state") == "infra_failed":
            continue
        attempts = max(attempts, recorded)
    return attempts


def render_episode_marker(episode, pr_number, head_sha, attempt, state) -> str:
    """Render a machine-readable episode record.

    The rendered marker is interpolated into a pull-request comment, so every
    field is validated: a malformed number or an unverified head produces no
    marker at all rather than a comment the watchdog would misparse.
    """
    ep = non_negative_int(episode)
    pr = non_negative_int(pr_number)
    head = str(head_sha or "").strip().lower()
    attempt_number = non_negative_int(attempt)
    if state not in EPISODE_STATES:
        return ""
    if ep <= 0 or pr <= 0 or attempt_number <= 0 or not SHA_RE.fullmatch(head):
        return ""
    return "<!-- {}: episode={} pr={} head={} attempt={} state={} -->".format(
        EPISODE_MARKER_PREFIX, ep, pr, head, attempt_number, state
    )


# --------------------------------------------------------------------------- #
# Watchdog
# --------------------------------------------------------------------------- #


def evaluate_watchdog(
    *,
    labels=(),
    head_sha: str = "",
    locked_head_sha: str = "",
    attempts: int = 0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    repair_run_in_flight: bool = False,
    seconds_since_lock: int = 0,
    mergeable_state: str = "",
    stale_after_minutes: int = STALE_EPISODE_MINUTES,
    controller_marked_opt_out: bool = False,
) -> Decision:
    """Decide the remediation for a pull request left in a repair state.

    Two states strand a pull request, and both are handled here:

    * a conflict-repair lock with nothing running behind it, and
    * a merge opt-out the controller itself set for a conflict that is no
      longer there.

    The lock is released when no attempt is in flight and either the episode
    has outlived ``stale_after_minutes`` or the head has moved since the lock
    was taken. A moved head means the episode is over by definition: the thing
    the lock was protecting has been replaced.

    The opt-out is only ever removed when ``controller_marked_opt_out`` is
    true, i.e. when this controller's own marker accounts for it. A human's
    ``no-auto-merge`` is never touched.
    """
    held = {str(name) for name in (labels or ()) if name}
    tries = non_negative_int(attempts)
    budget = non_negative_int(max_attempts, DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS
    age = non_negative_int(seconds_since_lock)
    limit = max(1, non_negative_int(stale_after_minutes, STALE_EPISODE_MINUTES) or STALE_EPISODE_MINUTES)
    head = str(head_sha or "").strip().lower()
    locked_head = str(locked_head_sha or "").strip().lower()

    if repair_run_in_flight:
        return _decide(
            "none",
            "repair_run_in_flight",
            "A conflict-repair run is in flight; the state is not stale.",
            episode_attempts=tries,
        )

    if CONFLICT_LOCK_LABEL in held:
        head_moved = bool(head and locked_head and SHA_RE.fullmatch(head) and SHA_RE.fullmatch(locked_head) and head != locked_head)
        expired = age >= limit * 60
        if head_moved or expired:
            reason = (
                "the pull request head moved since the lock was taken"
                if head_moved
                else "no repair attempt has run for {} minutes".format(limit)
            )
            return _decide(
                "release-stale-lock",
                "stale_conflict_lock",
                "PR carries {} with no repair running because {}; releasing it so "
                "reconciliation can continue.".format(CONFLICT_LOCK_LABEL, reason),
                release_lock=True,
                record_failure=tries >= budget,
                retryable=tries < budget,
                episode_attempts=tries,
            )
        return _decide(
            "none",
            "lock_is_current",
            "PR carries {} and a current repair attempt is still within its "
            "window; leaving it alone.".format(CONFLICT_LOCK_LABEL),
            episode_attempts=tries,
        )

    if tries >= budget and REPAIR_FAILED_LABEL not in held:
        return _decide(
            "record-failure",
            "repair_failure_unreported",
            "PR exhausted {} conflict-repair attempt(s) without a recorded "
            "failure state; recording it so the outcome is visible.".format(budget),
            record_failure=True,
            freeze_original=False,
            retryable=False,
            episode_attempts=tries,
        )

    if (
        AUTO_MERGE_OPT_OUT_LABEL in held
        and controller_marked_opt_out
        and str(mergeable_state or "").strip().lower() not in ("", "dirty")
    ):
        return _decide(
            "release-stale-opt-out",
            "stale_auto_merge_opt_out",
            "PR still carries {} set by conflict repair, but its mergeable state "
            "is {!r}; the conflict is gone, so reconciliation must not keep "
            "excluding it.".format(AUTO_MERGE_OPT_OUT_LABEL, mergeable_state),
            release_lock=False,
            episode_attempts=tries,
        )

    return _decide(
        "none",
        "no_stale_state",
        "PR has no stale conflict-repair state.",
        episode_attempts=tries,
    )


# --------------------------------------------------------------------------- #
# Environment / output plumbing
# --------------------------------------------------------------------------- #


def _output_line(key: str, value) -> str:
    """Render one ``GITHUB_OUTPUT`` entry, heredoc-quoting multi-line values.

    ``GITHUB_OUTPUT`` has no escaping for a raw newline, so a value containing
    one has to use the delimiter form or the file is corrupted for every later
    step in the job.
    """
    text = "" if value is None else str(value)
    if "\n" not in text:
        return "{}={}".format(key, text)
    index = 0
    while True:
        delimiter = "continuum_episode_{}".format(index)
        if delimiter not in text:
            return "{}<<{}\n{}\n{}".format(key, delimiter, text, delimiter)
        index += 1


def write_outputs(outputs: dict) -> None:
    """Emit ``key=value`` pairs so a workflow can read them either way.

    Steps consume this policy in two different ways: some read
    ``$GITHUB_OUTPUT`` after the step, and others pipe stdout into ``sed`` or
    capture it in a command substitution. Emitting only to one of the two meant
    that half the callers silently saw nothing -- and a caller that reads an
    empty attempt count cannot tell "no attempts yet" from "this is broken",
    which is how a lock with no attempt record ended up looking like a finished
    repair. So both sinks get every value.
    """
    for key, value in outputs.items():
        print("{}={}".format(key, value))
    destination = os.environ.get("GITHUB_OUTPUT")
    if not destination:
        return
    with open(destination, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write("{}\n".format(_output_line(key, value)))


def report(decision: Decision, stream=None) -> None:
    """Log the decision for humans.

    This goes to stderr so that stdout stays a clean ``key=value`` stream: the
    workflow steps parse stdout with ``sed`` and ``grep``, and a prose line
    interleaved with the values is one refactor away from being parsed as a
    value.
    """
    out = stream or sys.stderr
    print(
        "conflict-repair [{}] {} -> {}".format(
            decision.code, one_line(decision.reason), decision.action
        ),
        file=out,
    )


def env_int(name: str, default: int) -> int:
    return non_negative_int(os.environ.get(name, ""), default)


TRUE_VALUES = frozenset({"true", "yes", "on", "1"})
FALSE_VALUES = frozenset({"false", "no", "off", "0"})


def bool_flag(value) -> bool:
    """Parse a boolean that arrived as a shell variable.

    Callers hold these as ``true``/``false`` strings, so a bare ``store_true``
    cannot express "definitely not running", and worse, a bare flag plus the
    value would be parsed as an extra positional argument and abort the step.
    That is how the lock release on the outcome step used to die: the step
    failed on the argument list and never reached the API call that frees the
    pull request.

    An unrecognised value raises instead of guessing. Silently reading "maybe"
    as true would put the policy back in charge of a value the caller never
    decided.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ValueError("expected a boolean, got {!r}".format(value))


def _add_bool(parser, name: str, dest: str) -> None:
    parser.add_argument("--" + name, dest=dest, type=bool_flag, default=False)


# --------------------------------------------------------------------------- #
# CLI commands
# --------------------------------------------------------------------------- #


def cmd_plan(args) -> int:
    decision = evaluate_plan(
        mode=args.mode,
        behind=args.behind,
        ruled_out=[r for r in (args.ruled_out or "").split(",") if r],
        attempts=args.attempts,
        max_attempts=args.max_attempts,
        seconds_since_attempt=args.seconds_since_attempt,
    )
    write_outputs(decision.as_outputs())
    report(decision)
    return 0


def cmd_budget(args) -> int:
    minutes = repair_budget_minutes(args.mode, args.configured_minutes)
    is_repair = isinstance(args.mode, str) and args.mode.strip() in REPAIR_MODES
    decision = Decision(
        action="budget",
        code="repair_budget" if is_repair else "task_budget",
        reason="Mode {!r} {} a {} minute budget: {}.".format(
            args.mode,
            "is a repair of an existing change and is capped at"
            if is_repair
            else "executes the task and gets",
            minutes,
            "the repair budget is a constant and is unaffected by how long the "
            "source task took"
            if is_repair
            else "a repair of this task will still be capped at the repair budget",
        ),
        task_timeout_minutes=minutes,
    )
    write_outputs({"task_timeout_minutes": str(decision.task_timeout_minutes)})
    report(decision)
    return 0


def cmd_recovery_plan(args) -> int:
    decision = evaluate_recovery(
        behind=args.behind,
        ruled_out=[r for r in (args.ruled_out or "").split(",") if r],
        attempts=args.attempts,
        max_attempts=args.max_attempts,
        seconds_since_attempt=args.seconds_since_attempt,
        backoff_base_seconds=env_int("CONTINUUM_REPAIR_BACKOFF_SECONDS", BACKOFF_BASE_SECONDS),
        backoff_max_seconds=BACKOFF_MAX_SECONDS,
    )
    write_outputs(decision.as_outputs())
    report(decision)
    # `failed` is a decision, not a crash: the caller has to act on it by
    # releasing the lock and recording the failure state, which it can only do
    # if this command still exits cleanly.
    return 0


def cmd_publication_gate(args) -> int:
    decision = evaluate_publication(
        original_head_sha=args.original_head,
        replacement_head_sha=args.replacement_head,
        repair_succeeded=args.repair_succeeded,
        original_frozen=args.original_frozen,
        attempts=args.attempts,
        repaired_in_place=args.repaired_in_place,
    )
    write_outputs(decision.as_outputs())
    report(decision)
    return 0


def cmd_episode(args) -> int:
    if args.file == "-":
        payload = sys.stdin.read()
    else:
        with open(args.file, encoding="utf-8") as handle:
            payload = handle.read()
    # Comments arrive one body per JSON element from `gh --jq .body`; splitting
    # on newlines would tear a marker that wraps, so the whole stream is
    # scanned and each marker is extracted independently.
    fields = latest_episode(payload.splitlines())
    write_outputs(
        {
            "episode": fields.get("episode", "0"),
            "episode_pr": fields.get("pr", "0"),
            "episode_head": fields.get("head", ""),
            "episode_attempts": str(episode_attempts(payload.splitlines(), args.pr_number)),
            "episode_state": fields.get("state", ""),
        }
    )
    return 0


def cmd_watchdog(args) -> int:
    decision = evaluate_watchdog(
        labels=[name for name in (args.labels or "").split(",") if name],
        head_sha=args.head_sha,
        locked_head_sha=args.locked_head_sha,
        attempts=args.attempts,
        max_attempts=args.max_attempts,
        repair_run_in_flight=args.repair_run_in_flight,
        seconds_since_lock=args.seconds_since_lock,
        mergeable_state=args.mergeable_state,
        stale_after_minutes=env_int("CONTINUUM_REPAIR_STALE_MINUTES", STALE_EPISODE_MINUTES),
        controller_marked_opt_out=args.controller_marked_opt_out,
    )
    write_outputs(decision.as_outputs())
    report(decision)
    return 0


def cmd_lock(args) -> int:
    decision = evaluate_lock(
        lock_present=args.lock_present,
        attempts=args.attempts,
        max_attempts=args.max_attempts,
        repair_run_in_flight=args.repair_run_in_flight,
    )
    write_outputs(decision.as_outputs())
    report(decision)
    return 0


def cmd_render_marker(args) -> int:
    marker = render_episode_marker(
        args.episode, args.pr_number, args.head_sha, args.attempt, args.state
    )
    if marker:
        print(marker)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Continuum conflict-repair policy")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Print the budget, model cap, and repair rung")
    plan.add_argument("--mode", default="")
    plan.add_argument("--behind", default="0")
    plan.add_argument("--ruled-out", dest="ruled_out", default="")
    plan.add_argument("--attempts", default="0")
    plan.add_argument(
        "--max-attempts",
        dest="max_attempts",
        default=env_int("CONTINUUM_REPAIR_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
    )
    plan.add_argument("--seconds-since-attempt", dest="seconds_since_attempt", default=None)
    plan.set_defaults(func=cmd_plan)

    budget = sub.add_parser("budget", help="Print the execution budget for a mode")
    budget.add_argument("--mode", default="")
    budget.add_argument("--configured-minutes", dest="configured_minutes", default="")
    budget.set_defaults(func=cmd_budget)

    recovery = sub.add_parser("recovery-plan", help="Print the next repair rung")
    recovery.add_argument("--behind", default="0")
    recovery.add_argument("--ruled-out", dest="ruled_out", default="")
    recovery.add_argument("--attempts", default="0")
    recovery.add_argument("--max-attempts", dest="max_attempts", default=env_int("CONTINUUM_REPAIR_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
    recovery.add_argument("--seconds-since-attempt", dest="seconds_since_attempt", default=None)
    recovery.set_defaults(func=cmd_recovery_plan)

    lock = sub.add_parser("lock", help="Decide what to do with the conflict-repair lock")
    _add_bool(lock, "lock-present", "lock_present")
    lock.add_argument("--attempts", default="0")
    lock.add_argument("--max-attempts", dest="max_attempts", default=env_int("CONTINUUM_REPAIR_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
    _add_bool(lock, "repair-run-in-flight", "repair_run_in_flight")
    lock.set_defaults(func=cmd_lock)

    publication = sub.add_parser("publication-gate", help="Decide whether the original PR may be frozen")
    publication.add_argument("--original-head", dest="original_head", default="")
    publication.add_argument("--replacement-head", dest="replacement_head", default="")
    _add_bool(publication, "repair-succeeded", "repair_succeeded")
    _add_bool(publication, "original-frozen", "original_frozen")
    _add_bool(publication, "repaired-in-place", "repaired_in_place")
    publication.add_argument("--attempts", default="0")
    publication.set_defaults(func=cmd_publication_gate)

    episode = sub.add_parser("episode", help="Read repair-episode markers from stdin")
    episode.add_argument("--file", default="-")
    episode.add_argument("--pr-number", dest="pr_number", default="0")
    episode.set_defaults(func=cmd_episode)

    watchdog = sub.add_parser("watchdog", help="Decide the remediation for a stale repair state")
    watchdog.add_argument("--labels", default="")
    watchdog.add_argument("--head-sha", dest="head_sha", default="")
    watchdog.add_argument("--locked-head-sha", dest="locked_head_sha", default="")
    watchdog.add_argument("--attempts", default="0")
    watchdog.add_argument("--max-attempts", dest="max_attempts", default=env_int("CONTINUUM_REPAIR_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
    _add_bool(watchdog, "repair-run-in-flight", "repair_run_in_flight")
    watchdog.add_argument("--seconds-since-lock", dest="seconds_since_lock", default="0")
    watchdog.add_argument("--mergeable-state", dest="mergeable_state", default="")
    _add_bool(watchdog, "controller-marked-opt-out", "controller_marked_opt_out")
    watchdog.set_defaults(func=cmd_watchdog)

    marker = sub.add_parser("render-marker", help="Render a repair-episode marker")
    marker.add_argument("--episode", default="0")
    marker.add_argument("--pr-number", dest="pr_number", default="0")
    marker.add_argument("--head-sha", dest="head_sha", default="")
    marker.add_argument("--attempt", default="0")
    marker.add_argument("--state", default="")
    marker.set_defaults(func=cmd_render_marker)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
