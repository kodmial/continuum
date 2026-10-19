"""Parent-local scheduling survives delegated discovery outages (#326).

Pins the split between the optional delegated Child inventory and the
Parent-local issue queue: an unavailable discovery (exit 4) degrades Child
dispatch only, is reported distinctly, never becomes a fabricated empty
success, and lets eligible Parent-local work progress.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from continuum.delegation_discovery import (
    DELEGATION_DEGRADED,
    DELEGATION_NONE,
    DELEGATION_OK,
    resolve_child_inventory,
)
from continuum.scheduler_reconcile import (
    IssueState,
    SchedulerConfig,
    reconcile,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"
RESOLVER = ROOT / ".github" / "scripts" / "delegation_repository.sh"


def scheduler_body() -> str:
    return SCHEDULER.read_text(encoding="utf-8")


class WorkflowDecouplingTest(unittest.TestCase):
    def test_child_queue_degraded_state_is_explicit(self):
        body = scheduler_body()
        for sentinel in (
            'if [[ "$plan_rc" -eq 4 ]]',
            'echo "delegation_state=degraded" >> "$GITHUB_OUTPUT"',
            'echo "delegation_state=ok" >> "$GITHUB_OUTPUT"',
            'Delegated child discovery is unavailable (exit 4)',
        ):
            self.assertIn(sentinel, body)

    def test_reconcile_gates_children_on_delegation_state(self):
        body = scheduler_body()
        for sentinel in (
            "DELEGATION_STATE: ${{ steps.child_queue.outputs.delegation_state }}",
            "const delegationState = (",
            "if (delegationState === 'ok') {",
            "} else if (delegationState === 'degraded') {",
            "Delegated child discovery is unavailable; Child dispatch is ",
            'Delegation state is "ok" but the delegated child candidate file is missing.',
        ):
            self.assertIn(sentinel, body)

    def test_resolver_classifies_and_bounds_discovery_retries(self):
        body = RESOLVER.read_text(encoding="utf-8")
        for sentinel in (
            "classify-response",
            "verdict",
            "rate_limited",
            "transient",
            "CONTINUUM_DISCOVERY_MAX_ATTEMPTS",
        ):
            self.assertIn(sentinel, body)


class ChildInventoryDecisionTest(unittest.TestCase):
    def test_degraded_never_fabricates_and_disables_children(self):
        decision = resolve_child_inventory(
            DELEGATION_DEGRADED, [{"id": "alpha", "repository": "owner/child"}]
        )
        self.assertFalse(decision.enabled)
        self.assertTrue(decision.degraded)
        self.assertNotIn("owner/child", decision.reason)

    def test_ok_empty_is_distinct_from_degraded(self):
        ok = resolve_child_inventory(DELEGATION_OK, [])
        degraded = resolve_child_inventory(DELEGATION_DEGRADED, [])
        self.assertTrue(ok.enabled)
        self.assertFalse(ok.degraded)
        self.assertFalse(degraded.enabled)
        self.assertTrue(degraded.degraded)
        self.assertNotEqual(ok.reason, degraded.reason)

    def test_none_disables_without_degrading(self):
        decision = resolve_child_inventory(DELEGATION_NONE)
        self.assertFalse(decision.enabled)
        self.assertFalse(decision.degraded)


class LocalProgressDuringOutageTest(unittest.TestCase):
    """A ready Parent-local issue dispatches even when children are disabled."""

    def test_local_issue_dispatches_with_no_children(self):
        config = SchedulerConfig(ready_label="automation:ready")
        issues = {
            229: IssueState(
                number=229,
                title="P0: unrelated local task",
                body="Local Parent issue.",
                labels={"priority:p0", "automation:ready"},
                is_open=True,
            )
        }
        decision = resolve_child_inventory(DELEGATION_DEGRADED, [])
        self.assertFalse(decision.enabled)
        result = reconcile(
            datetime.now(timezone.utc),
            issues,
            set(),
            set(),
            {},
            {},
            {},
            set(),
            config,
        )
        self.assertEqual(result.dispatched, [229])
        self.assertIn(config.in_progress_label, issues[229].labels)
        self.assertFalse(result.failed)


if __name__ == "__main__":
    unittest.main()
