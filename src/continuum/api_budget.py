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
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple


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
    if status == 429 or (isinstance(status, int) and 500 <= status <= 599):
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
# REST failure classification + Retry-After (kodmial/continuum#326)
# ---------------------------------------------------------------------------

#: A rejected credential or a genuine permission denial.  Retrying cannot
#: help, so discovery fails closed immediately.
AUTH_FAILURE = "auth"

#: Primary/secondary rate limiting (HTTP 429, or 403 carrying a limit
#: signal).  Retryable, but only inside the header-derived bounded window.
RATE_LIMIT_FAILURE = "rate_limited"

#: Server-side 5xx, gateway or timeout/network failures.  Retryable with the
#: bounded exponential schedule.
TRANSIENT_FAILURE = "transient"

#: A clean 404 (for example an absent relationship file).  Never retried; it
#: is a definitive "no such resource" answer.
NOT_FOUND_FAILURE = "not_found"

#: Any other definitive client error.  Never retried.
PERMANENT_FAILURE = "permanent"

#: Hard cap on any header-derived retry delay.  Respecting ``Retry-After``
#: must never park a scheduler pass for minutes: Parent-local work runs
#: regardless of delegated discovery, so the delay stays short.
DEFAULT_MAX_RETRY_AFTER = 15.0

_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "secondary rate",
    "abuse",
    "too many requests",
)

_TRANSIENT_MARKERS = (
    "try again",
    "temporarily",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "connection refused",
    "network is unreachable",
    "broken pipe",
    "socket",
    "eof",
)


def _marker_re(markers: Tuple[str, ...]):
    # Word-boundary anchored so short tokens (``eof``, ``socket``) never match
    # inside an unrelated word such as ``thereof``.
    return re.compile(r"\b(?:" + "|".join(re.escape(m) for m in markers) + r")\b")


_RATE_LIMIT_RE = _marker_re(_RATE_LIMIT_MARKERS)
_TRANSIENT_RE = _marker_re(_TRANSIENT_MARKERS)

_STATUS_LINE_RE = re.compile(r"^HTTP/\d(?:\.\d)?\s+(\d{3})", re.IGNORECASE)
_HTTP_CODE_RE = re.compile(r"\bHTTP[/ ](\d{3})\b", re.IGNORECASE)
_HEADER_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s?(.*)$")
_DIGITS_RE = re.compile(r"^\d+$")


def _headers_lower(headers: Any) -> Dict[str, str]:
    """Normalize a header mapping (or iterable of pairs) to lowercase keys."""
    out: Dict[str, str] = {}
    if headers is None:
        return out
    items = headers.items() if hasattr(headers, "items") else headers
    try:
        for key, value in items:
            out[str(key).strip().lower()] = str(value).strip()
    except (TypeError, ValueError):
        return out
    return out


def classify_rest_failure(status: Any, headers: Any = None,
                          message: str = "") -> str:
    """Classify one failed GitHub REST response into a retry policy bucket.

    Distinguishes an authentication/permission rejection (401, or 403 without
    a limit signal) from primary/secondary rate limiting (429, or 403 while
    the remaining budget is zero or the body names a limit) from 5xx/timeout
    transient failures.  Unknown input fails closed as ``PERMANENT_FAILURE``
    so an unrecognized error is never retried as transient forever.
    """
    lowered = (message or "").lower()
    status_value: Optional[int]
    try:
        status_value = int(status) if status is not None and str(status) != "" else None
    except (TypeError, ValueError):
        status_value = None
    if status_value is None:
        # gh reports failures as ``<message> (HTTP <code>)``; recover the code
        # so a message-only stream is classified as precisely as a raw one.
        code_match = _HTTP_CODE_RE.search(message or "")
        if code_match:
            status_value = int(code_match.group(1))

    header_map = _headers_lower(headers)
    remaining = header_map.get("x-ratelimit-remaining")
    limit_signal = (
        _RATE_LIMIT_RE.search(lowered) is not None
        or (remaining is not None and remaining.strip() == "0")
    )

    if status_value == 401:
        return AUTH_FAILURE
    if status_value == 403:
        return RATE_LIMIT_FAILURE if limit_signal else AUTH_FAILURE
    if status_value == 429:
        return RATE_LIMIT_FAILURE
    if status_value is not None and 500 <= status_value <= 599:
        return TRANSIENT_FAILURE
    if status_value == 404:
        return NOT_FOUND_FAILURE

    if limit_signal:
        return RATE_LIMIT_FAILURE
    if _TRANSIENT_RE.search(lowered) is not None:
        return TRANSIENT_FAILURE
    return PERMANENT_FAILURE


def is_retryable_failure(verdict: str) -> bool:
    """Only rate limiting and transient server failures are retryable."""
    return verdict in (RATE_LIMIT_FAILURE, TRANSIENT_FAILURE)


def retry_after_seconds(headers: Any = None, *, now: Optional[float] = None,
                        max_delay: float = DEFAULT_MAX_RETRY_AFTER) -> Optional[float]:
    """Return the bounded retry delay implied by rate-limit headers.

    Honours ``Retry-After`` (delta seconds or HTTP date) and
    ``X-RateLimit-Reset`` (UTC epoch seconds).  The result is clamped to
    ``[0, max_delay]`` so a header can never block a pass unboundedly.
    Returns ``None`` when neither header carries a usable value.
    """
    header_map = _headers_lower(headers)
    current = time.time() if now is None else float(now)
    candidates = []
    retry_after = (header_map.get("retry-after") or "").strip()
    if retry_after:
        if _DIGITS_RE.match(retry_after):
            candidates.append(float(retry_after))
        else:
            try:
                parsed = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                # HTTP-date is always UTC and recent; reject implausible years
                # so a misparsed or non-compliant date never skews the delta.
                if 2000 <= parsed.year <= 2100:
                    candidates.append(max(0.0, parsed.timestamp() - current))
    reset = (header_map.get("x-ratelimit-reset") or "").strip()
    if reset and _DIGITS_RE.match(reset):
        candidates.append(max(0.0, float(reset) - current))
    if not candidates:
        return None
    return float(min(max(candidates), max(0.0, float(max_delay))))


def parse_http_response(raw: str) -> Tuple[Optional[int], Dict[str, str], str]:
    """Parse a ``gh api --include``/HTTP stream into status, headers, body.

    Handles a single response or a concatenated paginated stream by keeping
    the last complete status line and its header block.  Body-adjacent status
    lines restart the header block so a paginated success never leaks page
    headers into the captured body.
    """
    status: Optional[int] = None
    headers: Dict[str, str] = {}
    body: list = []
    in_headers = False
    for line in (raw or "").splitlines():
        status_match = _STATUS_LINE_RE.match(line)
        if status_match:
            status = int(status_match.group(1))
            headers = {}
            in_headers = True
            continue
        if in_headers:
            if line.strip() == "":
                in_headers = False
                continue
            header_match = _HEADER_LINE_RE.match(line)
            if header_match:
                headers[header_match.group(1).strip().lower()] = header_match.group(2).strip()
                continue
            in_headers = False
        body.append(line)
    return status, headers, "\n".join(body).strip()


def strip_http_headers(raw: str) -> str:
    """Return only the response body lines from a ``--include`` stream."""
    _, _, body = parse_http_response(raw)
    return body


class RestFailure(RuntimeError):
    """A classified GitHub REST failure carrying status and headers."""

    def __init__(self, status: Any, headers: Any = None, message: str = ""):
        super().__init__(message or f"GitHub REST failure ({status})")
        self.status = status
        self.headers = _headers_lower(headers)
        self.message = message or ""


def _describe_failure(exc: Exception) -> Tuple[Any, Dict[str, str], str]:
    status = getattr(exc, "status", None)
    headers = getattr(exc, "headers", None)
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status", getattr(response, "status_code", status))
        headers = headers if headers is not None else getattr(response, "headers", None)
    return status, _headers_lower(headers), str(exc)


def read_discovery(fetch: Callable[[], Any],
                   policy: Optional[RetryPolicy] = None,
                   classify: Optional[Callable[[Exception], str]] = None,
                   sleep: Optional[Callable[[float], None]] = None,
                   now: Optional[float] = None,
                   max_retry_after: float = DEFAULT_MAX_RETRY_AFTER) -> Any:
    """Run one delegated-discovery read under a bounded, classified budget.

    Unlike :func:`read_with_budget` this fails closed: the last classified
    failure is re-raised once the budget is exhausted.  Only rate limiting
    and transient 5xx/timeout failures retry, and the wait honours
    ``Retry-After``/``X-RateLimit-Reset`` clamped to ``max_retry_after`` so a
    discovery retry can never spin or park the pass.
    """
    active = policy or RetryPolicy(max_attempts=3, base_delay_seconds=1.0,
                                   max_delay_seconds=max_retry_after,
                                   reraise=True)
    waiter = sleep if sleep is not None else time.sleep
    attempts = max(1, active.max_attempts)
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return fetch()
        except Exception as exc:  # noqa: BLE001 - classification decides retry
            last = exc
            status, headers, message = _describe_failure(exc)
            verdict = classify(exc) if classify is not None else classify_rest_failure(
                status, headers, message)
            if not is_retryable_failure(verdict) or attempt >= attempts:
                break
            delay = retry_after_seconds(headers, now=now, max_delay=max_retry_after)
            if delay is None:
                delay = min(active.base_delay_seconds * (2 ** (attempt - 1)),
                            active.max_delay_seconds)
            waiter(max(float(delay), 0.0))
    assert last is not None
    raise last


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
