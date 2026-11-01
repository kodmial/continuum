"""Parent-local vs delegated scheduling isolation (kodmial/continuum#326).

On a public Parent runner delegated Child discovery is optional: it needs
cross-repository GitHub REST reads that can fail with authentication,
rate-limit or 5xx/timeout errors.  A discovery outage must never block
unrelated Parent-local scheduling, but it also must never be reported as an
empty successful Child inventory.

This module owns the pure delegation policy the scheduler and the shell
resolver both encode, so it can be tested in isolation from GitHub:

- :data:`DELEGATION_OK` / :data:`DELEGATION_DEGRADED` / :data:`DELEGATION_NONE`
  name the delegation state a scheduler pass reports, so a degraded pass is
  distinct from "no children" (a genuine, successful empty inventory);
- :func:`resolve_child_inventory` is the executable decision: when discovery
  is degraded Child dispatch is disabled and no inventory is fabricated, while
  Parent-local work continues;
- :class:`InventoryCache` is the authenticated, strictly scoped, TTL-limited
  and privacy-preserving reuse of a Child inventory already trusted for the
  same epoch.  It never holds a credential and refuses any entry that is
  expired, tampered with, or bound to a different parent / allowlist.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

#: Discovery succeeded: the (possibly empty) Child inventory is authoritative.
DELEGATION_OK = "ok"

#: Discovery was unavailable (exit 4): Child dispatch is fail-closed for this
#: pass; Parent-local scheduling still runs.
DELEGATION_DEGRADED = "degraded"

#: This repository is not a Parent; there is no delegated inventory at all.
DELEGATION_NONE = "none"

DELEGATION_STATES = frozenset({DELEGATION_OK, DELEGATION_DEGRADED, DELEGATION_NONE})

#: Default TTL for a trusted, already-enumerated Child inventory.
DEFAULT_TTL_SECONDS = 900

#: Hard ceiling so a trusted inventory can never outlive the epoch by much.
#: Never longer than the default trust window: a cached Child binding must not
#: stay reusable for an hour when the allowlist or bindings may have changed.
MAX_TTL_SECONDS = DEFAULT_TTL_SECONDS

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ChildDispatchDecision:
    """Whether Child dispatch is enabled for one scheduler pass and why."""

    enabled: bool
    degraded: bool
    reason: str


def resolve_child_inventory(delegation_state: str,
                            children: Sequence[Dict[str, Any]] = (),
                            *,
                            parent: bool = False) -> ChildDispatchDecision:
    """Decide Child dispatch state without ever fabricating an inventory.

    ``DELEGATION_OK`` enables dispatch over the supplied ``children`` (which
    may legitimately be empty).  ``DELEGATION_DEGRADED`` disables Child
    dispatch and reports a distinct degraded reason; any supplied children are
    ignored because their provenance is no longer trusted.  ``DELEGATION_NONE``
    disables dispatch because no delegation is configured.  Any other value
    fails closed as degraded.
    """
    state = (delegation_state or "").strip()
    if state == DELEGATION_OK:
        return ChildDispatchDecision(True, False, "discovery available")
    if state == DELEGATION_NONE:
        return ChildDispatchDecision(
            False, False, "no delegated children configured for this repository")
    if state == DELEGATION_DEGRADED:
        return ChildDispatchDecision(
            False, True,
            "delegated child discovery unavailable; child dispatch disabled "
            "for this pass while parent-local scheduling continues")
    return ChildDispatchDecision(
        False, True,
        "unknown delegation state %r; failing closed for child dispatch" % state)


def allowlist_fingerprint(parent_repository: str, allowlist_json: str) -> str:
    """Stable fingerprint binding a cached inventory to its exact scope.

    Binds the parent repository and the raw ``CONTINUUM_CHILDREN`` allowlist
    (order included) so an inventory can never be reused for a different
    parent or a changed allowlist.
    """
    payload = json.dumps(
        [str(parent_repository or ""), str(allowlist_json or "")],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_plan(plan: Any) -> bool:
    if not isinstance(plan, list):
        return False
    seen = set()
    for entry in plan:
        if not isinstance(entry, dict):
            return False
        child_id = entry.get("id")
        repository = entry.get("repository")
        if not isinstance(child_id, str) or not child_id:
            return False
        if not isinstance(repository, str) or repository.count("/") != 1:
            return False
        owner, _, name = repository.partition("/")
        if not owner or not name:
            return False
        if child_id in seen:
            return False
        seen.add(child_id)
    return True


def _allowed_child_ids(allowlist_json: Any) -> Optional[set]:
    try:
        value = json.loads(allowlist_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list):
        return None
    allowed = set()
    for item in value:
        if not isinstance(item, str) or not item:
            return None
        allowed.add(item)
    return allowed


def _bounded_ttl(ttl: Any) -> int:
    try:
        value = int(ttl)
    except (TypeError, ValueError):
        value = DEFAULT_TTL_SECONDS
    return max(0, min(value, MAX_TTL_SECONDS))


class InventoryCache:
    """Authenticated, scoped, TTL-limited cache of a trusted Child inventory.

    The cache is a small JSON document written to an ephemeral, job-scoped
    path (never a repository file, artifact, Redis or external store, and
    never a credential).  :meth:`read` returns ``None`` — refusing to guess —
    unless the document matches the requested parent, the exact allowlist
    fingerprint, the schema, and the TTL window.  Any Child repository name
    therefore never escapes the runner workspace.
    """

    def __init__(self, path: str, *, ttl: int = DEFAULT_TTL_SECONDS):
        if not path:
            raise ValueError("InventoryCache requires a path")
        self.path = path
        self.ttl = _bounded_ttl(ttl)

    def write(self, *, parent_repository: str, allowlist_json: str,
              plan: Sequence[Dict[str, Any]], now: int) -> None:
        if not _valid_plan(list(plan)):
            raise ValueError("Refusing to cache a malformed child inventory")
        document = {
            "schema": SCHEMA_VERSION,
            "parent": str(parent_repository or ""),
            "scope": allowlist_fingerprint(parent_repository, allowlist_json),
            "created_at": int(now),
            "plan": [
                {"id": entry["id"], "repository": entry["repository"]}
                for entry in plan
            ],
        }
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))

    def read(self, *, parent_repository: str, allowlist_json: str,
             now: int) -> Optional[List[Dict[str, str]]]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict):
            return None
        if document.get("schema") != SCHEMA_VERSION:
            return None
        if document.get("parent") != str(parent_repository or ""):
            return None
        if document.get("scope") != allowlist_fingerprint(parent_repository, allowlist_json):
            return None
        created_at = document.get("created_at")
        if not isinstance(created_at, int):
            return None
        if not 0 <= int(now) - created_at <= self.ttl:
            return None
        plan = document.get("plan")
        if not _valid_plan(plan):
            return None
        # A cached plan may only contain children still allowed by the current
        # allowlist; a narrowed allowlist must never resurrect a removed child.
        allowed = _allowed_child_ids(allowlist_json)
        if allowed is None:
            return None
        return [
            {"id": entry["id"], "repository": entry["repository"]}
            for entry in plan
            if entry["id"] in allowed
        ]


def deletion_evidence(delegation_state: str, dispatch_enabled: bool) -> str:
    """Human-auditable one-line delegation summary (never a Child name)."""
    if delegation_state == DELEGATION_OK and dispatch_enabled:
        return "delegation=ok child_dispatch=enabled"
    if delegation_state == DELEGATION_DEGRADED:
        return "delegation=degraded child_dispatch=disabled parent_local=scheduled"
    if delegation_state == DELEGATION_NONE:
        return "delegation=none child_dispatch=disabled parent_local=scheduled"
    return "delegation=unknown child_dispatch=disabled parent_local=scheduled"
