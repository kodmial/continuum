"""GitHub API request-budget policy for Continuum controllers (kodmial/continuum#254).

This module is the executable half of the #254 request inventory.  It owns:

- the per-scan pagination bounds every controller/workflow pass must honour
  (no ``paginate(all history)`` patterns outside the excluded #253 scope);
- single-pass memoization of same-resource reads;
- bounded backoff for transient/rate-limit failures in non-critical reads;
- the credential-class policy (repository-scoped ``GITHUB_TOKEN`` for
  unavoidable same-repo reads, shared PAT only for cross-repo access or
  mutation/actor semantics);
- the local-Git / event-payload substitution table;
- lightweight per-pass accounting (calls by credential class, endpoint
  family, and page counts, plus substitution flags) for step summaries.

The module is dependency-free and performs no I/O itself: workflows consult
the policy constants/advisors and report their own counts.  The helpers here
let unit tests prove the bounds on deterministic fixtures.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# Pagination bounds
# ---------------------------------------------------------------------------

#: Default page size used by Continuum list operations.
PER_PAGE = 100

#: Default hard cap on pages fetched by any single list scan.  With
#: ``PER_PAGE`` this bounds every newly-audited scan to at most 500 records,
#: independent of repository lifetime/history.
DEFAULT_MAX_PAGES = 5

#: Tighter cap for scans that only need the most recent decision-relevant
#: records (workflow-run gate checks filtered server-side by branch/event).
RECENT_MAX_PAGES = 2

#: Cap for scans that must enumerate a whole queue (open PRs/issues) to rank
#: candidates.  Still independent of history size.
QUEUE_MAX_PAGES = 5

#: Cap for comment/review-thread scans on a single PR/issue.
THREAD_MAX_PAGES = 5

#: Cap for file-listing scans on a single PR.
FILES_MAX_PAGES = 10


def worst_case_requests(total_records: int, per_page: int = PER_PAGE,
                        max_pages: int = DEFAULT_MAX_PAGES) -> int:
    """Return the worst-case request count for a bounded scan.

    The count never grows with ``total_records``: it is capped at
    ``max_pages`` no matter how large the underlying history is.
    """
    if total_records <= 0:
        return 0
    needed = int(math.ceil(total_records / max(1, per_page)))
    return max(1, min(needed, max_pages))


def simulate_scan(total_records: int, per_page: int = PER_PAGE,
                  max_pages: int = DEFAULT_MAX_PAGES) -> Dict[str, int]:
    """Simulate a bounded scan against a deterministic fixture.

    Returns the request count and the number of records observed, proving
    the scan stays bounded even for ``total_records`` values of 10k+.
    """
    pages = worst_case_requests(total_records, per_page, max_pages)
    observed = min(total_records, pages * per_page)
    return {"requests": pages, "observed": observed,
            "truncated": total_records > observed}


# ---------------------------------------------------------------------------
# Endpoint families and credential classes
# ---------------------------------------------------------------------------

def endpoint_family(operation: str) -> str:
    """Map a concrete operation name to its endpoint family for accounting."""
    op = (operation or "").lower()
    if "graphql" in op or "reviewthreads" in op or "review_threads" in op:
        return "graphql:reviewThreads"
    for family in ("actions", "pulls", "issues", "repos", "search"):
        if op.startswith(family + ".") or op.startswith(family + "/") or op == family:
            return family
    if "dispatch" in op:
        return "actions"
    if "comment" in op or "label" in op:
        return "issues"
    return "other"


READ_CREDENTIAL = "GITHUB_TOKEN"
PAT_CREDENTIAL = "TAP_PAT"


def credential_for(same_repo: bool, read_only: bool,
                   token_permissions_sufficient: bool = True) -> str:
    """Return the required credential class for one GitHub API call.

    Same-repository read-only calls use the repository-scoped token whenever
    permissions allow.  The shared PAT is reserved for cross-repo access and
    for mutations/actor semantics (writes must keep fanning out as
    issue/label events under the PAT identity).
    """
    if same_repo and read_only and token_permissions_sufficient:
        return READ_CREDENTIAL
    return PAT_CREDENTIAL


# ---------------------------------------------------------------------------
# Local-Git / event-payload substitution table
# ---------------------------------------------------------------------------

#: Repository-state questions Git already answers when the repo is checked
#: out.  Each entry names the canonical local command.
GIT_EQUIVALENT_QUESTIONS: Mapping[str, str] = {
    "head_sha": "git rev-parse HEAD",
    "branch_head_sha": "git rev-parse <ref>",
    "merge_base": "git merge-base <a> <b>",
    "is_ancestor": "git merge-base --is-ancestor <a> <b>",
    "changed_files": "git diff --name-only <base>...<head>",
    "commit_history": "git log --format=%H <range>",
    "file_content_at_ref": "git show <ref>:<path>",
    "ref_exists": "git cat-file -e <ref>",
    "behind_by": "git rev-list --count <head>.. <upstream>",
}

#: GitHub-only state that must never be inferred from local Git.
GITHUB_ONLY_QUESTIONS = frozenset({
    "reviews", "review_decision", "issue_labels", "workflow_status",
    "check_runs", "permissions", "mergeability", "rate_limit",
    "comments", "review_threads",
})


def substitution_for(question: str, checkout_present: bool,
                     event_has_value: bool) -> str:
    """Return the cheapest sufficient data source for one question.

    Returns one of ``"event"``, ``"git"``, or ``"api"``.  GitHub-only state
    always resolves to ``"api"``; repository-state questions prefer the
    event payload, then local Git, and only then the API.
    """
    q = (question or "").strip().lower()
    if q in GITHUB_ONLY_QUESTIONS:
        return "api"
    if event_has_value:
        return "event"
    if q in GIT_EQUIVALENT_QUESTIONS and checkout_present:
        return "git"
    return "api"


# ---------------------------------------------------------------------------
# Single-pass memoization
# ---------------------------------------------------------------------------

class MemoCache:
    """Coalesce duplicate same-resource reads within one controller pass."""

    def __init__(self) -> None:
        self._store: Dict[str, Any] = {}
        self.hits = 0
        self.misses = 0

    def get_or_fetch(self, key: str, fetch: Callable[[], Any]) -> Any:
        """Return the cached value for ``key``, fetching once on first use."""
        if key in self._store:
            self.hits += 1
            return self._store[key]
        self.misses += 1
        value = fetch()
        self._store[key] = value
        return value

    @property
    def request_count(self) -> int:
        """Number of backing fetches performed (one per distinct key)."""
        return self.misses


# ---------------------------------------------------------------------------
# Bounded retry for non-critical reads
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryPolicy:
    """Bounded backoff for transient/rate-limit failures (non-critical reads).

    Non-critical reads return ``fallback`` after the budget is exhausted so
    they can neither retry forever nor create retry storms; critical paths
    fail closed via ``reraise=True`` instead.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0
    reraise: bool = False

    def delays(self, seed: int = 0) -> Sequence[float]:
        """Return the bounded, jittered inter-attempt delays."""
        rng = random.Random(seed)
        out = []
        for attempt in range(max(0, self.max_attempts - 1)):
            capped = min(self.base_delay_seconds * (2 ** attempt),
                         self.max_delay_seconds)
            out.append(round(rng.uniform(0, capped), 3))
        return out


def _is_transient_default(exc: Exception) -> bool:
    """Default classifier: only transient/rate-limit signals are retryable.

    Programming errors (``ValueError``, ``TypeError``, ...) fail fast
    instead of being retried with backoff and swallowed as ``fallback``.
    """
    status = getattr(exc, "status", None)
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status", getattr(response, "status_code", status))
    if status in (429, 502, 503, 504):
        return True
    msg = str(exc).lower()
    return ("rate limit" in msg or "rate-limit" in msg or "try again" in msg
            or "timeout" in msg or "temporarily" in msg)


def read_with_budget(fetch: Callable[[], Any], policy: Optional[RetryPolicy] = None,
                     fallback: Any = None, is_rate_limited: Optional[Callable[[Exception], bool]] = None,
                     sleep: Optional[Callable[[float], None]] = None, seed: int = 0) -> Any:
    """Run one non-critical read under a bounded retry budget.

    Retries at most ``policy.max_attempts`` times total (never unbounded),
    then returns ``fallback`` (or reraises when ``policy.reraise`` is set).

    Transient/rate-limited failures back off between attempts using the
    bounded, jittered ``policy.delays()`` schedule instead of retrying
    immediately inside the rate-limit window.  When ``is_rate_limited`` is
    omitted only transient signals (HTTP 429/502/503/504 or rate-limit /
    try-again / timeout / temporarily messages) are retried; any other
    exception fails fast to ``fallback`` (or reraises) without further
    attempts.
    """
    active = policy or RetryPolicy()
    classifier = is_rate_limited if is_rate_limited is not None else _is_transient_default
    attempts = max(1, active.max_attempts)
    delays = list(active.delays(seed=seed))
    waiter = sleep if sleep is not None else time.sleep
    last: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return fetch()
        except Exception as exc:  # noqa: BLE001 - budget applies to any transient failure
            last = exc
            if not classifier(exc):
                break
            if attempt < attempts - 1 and attempt < len(delays):
                waiter(delays[attempt])
    if active.reraise and last is not None:
        raise last
    return fallback


# ---------------------------------------------------------------------------
# Per-pass accounting / observability
# ---------------------------------------------------------------------------

@dataclass
class ApiBudget:
    """Lightweight per-pass request accounting (no secrets recorded)."""

    calls: int = 0
    by_credential: Dict[str, int] = field(default_factory=dict)
    by_family: Dict[str, int] = field(default_factory=dict)
    pages: int = 0
    git_substitutions: int = 0
    event_substitutions: int = 0

    def record(self, operation: str, credential: str, pages: int = 1) -> None:
        """Record one GitHub API call (never logs tokens or identifiers)."""
        self.calls += 1
        self.by_credential[credential] = self.by_credential.get(credential, 0) + 1
        family = endpoint_family(operation)
        self.by_family[family] = self.by_family.get(family, 0) + 1
        self.pages += max(0, pages)

    def record_substitution(self, source: str) -> None:
        """Record one API call avoided via local Git or the event payload."""
        if source == "git":
            self.git_substitutions += 1
        elif source == "event":
            self.event_substitutions += 1

    def pat_calls(self) -> int:
        """Number of calls drawn from the shared PAT budget."""
        return self.by_credential.get(PAT_CREDENTIAL, 0)

    def summary_lines(self) -> Sequence[str]:
        """Render step-summary lines (counts only, no secrets)."""
        lines = [
            f"continuum-api-budget: total_calls={self.calls} pages={self.pages}",
            "continuum-api-budget-by-credential: " + (
                ", ".join(f"{k}={v}" for k, v in sorted(self.by_credential.items())) or "none"),
            "continuum-api-budget-by-family: " + (
                ", ".join(f"{k}={v}" for k, v in sorted(self.by_family.items())) or "none"),
            f"continuum-api-budget-substitutions: git={self.git_substitutions} event={self.event_substitutions}",
        ]
        return lines


# ---------------------------------------------------------------------------
# Before/after measurement on deterministic fixtures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConsumerFixture:
    """Deterministic fixture representing an active Continuum consumer."""

    open_prs: int = 25
    comments_per_pr: int = 120
    reviews_per_pr: int = 40
    historical_runs: int = 12000
    threads_per_pr: int = 30
    issues_open: int = 60


def estimate_old_pass(fixture: ConsumerFixture) -> ApiBudget:
    """Estimate the pre-#254 per-pass cost on the fixture (unbounded scans).

    Models the audited hotspots before this change: a full open-PR listing
    plus per-PR unbounded comment/review scans, an Actions-history scan that
    grows with repository lifetime, and a second full reconciliation pass.
    """
    budget = ApiBudget()
    pr_pages = worst_case_requests(fixture.open_prs, PER_PAGE, 10 ** 9)
    budget.record("pulls.list", PAT_CREDENTIAL, pages=pr_pages)
    for _ in range(fixture.open_prs):
        budget.record("pulls.get", PAT_CREDENTIAL)
        budget.record("issues.listComments", PAT_CREDENTIAL,
                      pages=worst_case_requests(fixture.comments_per_pr, PER_PAGE, 10 ** 9))
        budget.record("pulls.listReviews", PAT_CREDENTIAL,
                      pages=worst_case_requests(fixture.reviews_per_pr, PER_PAGE, 10 ** 9))
        budget.record("actions.listWorkflowRunsForRepo", PAT_CREDENTIAL,
                      pages=worst_case_requests(fixture.historical_runs, PER_PAGE, 10 ** 9))
    # Second full reconciliation pass (collectState re-run).
    second = ApiBudget()
    second.calls = budget.calls
    second.pages = budget.pages
    second.by_credential = dict(budget.by_credential)
    second.by_family = dict(budget.by_family)
    budget.calls += second.calls
    budget.pages += second.pages
    for key, value in second.by_credential.items():
        budget.by_credential[key] = budget.by_credential.get(key, 0) + value
    for key, value in second.by_family.items():
        budget.by_family[key] = budget.by_family.get(key, 0) + value
    return budget


def estimate_new_pass(fixture: ConsumerFixture) -> ApiBudget:
    """Estimate the post-#254 per-pass cost on the same fixture.

    Models the optimized controller: bounded queue scan on the repository
    token, one memoized fetch per PR, bounded comment/review/run scans, a
    bounded thread scan, and a targeted single-PR revalidation instead of a
    second full pass.
    """
    budget = ApiBudget()
    budget.record("pulls.list", READ_CREDENTIAL,
                  pages=worst_case_requests(fixture.open_prs, PER_PAGE, QUEUE_MAX_PAGES))
    for _ in range(fixture.open_prs):
        budget.record("pulls.get", READ_CREDENTIAL)
        budget.record("issues.listComments", READ_CREDENTIAL,
                      pages=worst_case_requests(fixture.comments_per_pr, PER_PAGE, THREAD_MAX_PAGES))
        budget.record("pulls.listReviews", READ_CREDENTIAL,
                      pages=worst_case_requests(fixture.reviews_per_pr, PER_PAGE, THREAD_MAX_PAGES))
        budget.record("actions.listWorkflowRunsForRepo", READ_CREDENTIAL,
                      pages=worst_case_requests(fixture.historical_runs, PER_PAGE, RECENT_MAX_PAGES))
        budget.record("graphql:reviewThreads", READ_CREDENTIAL,
                      pages=worst_case_requests(fixture.threads_per_pr, 100, THREAD_MAX_PAGES))
    # Targeted revalidation of the single selected candidate.
    for operation in ("pulls.get", "actions.listWorkflowRunsForRepo",
                      "graphql:reviewThreads", "repos.getCombinedStatusForRef"):
        budget.record(operation, READ_CREDENTIAL)
    budget.record("issues.createComment", PAT_CREDENTIAL)
    budget.record_substitution("event")
    return budget
