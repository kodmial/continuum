import unittest
from dataclasses import dataclass


PRIORITY_RANK = {
    "priority:p0": 0,
    "priority:p1": 1,
    "priority:p2": 2,
}


@dataclass(frozen=True)
class QueueState:
    priority: str | None = None
    review_ready: bool = True
    ci_green: bool = True
    packaging_present: bool = False
    packaging_green: bool = True
    required_gate_active: bool = False
    required_gate_green: bool = True
    unresolved_threads: int = 0
    current_head_decision: str | None = None
    current_head_inline_findings: int = 0
    prior_full_reviews: int = 0
    prior_changes_requested: bool = False
    requested_lock: bool = False
    rate_limited: bool = False
    created_at: int = 0
    issue_number: int | None = None
    pr_number: int = 1


def queue_priority(state: QueueState) -> tuple[str, int]:
    if state.priority in PRIORITY_RANK:
        return state.priority, PRIORITY_RANK[state.priority]
    # Mirrors the workflow contract: source-less/unprioritized work is an
    # explicit P2 scheduling fallback rather than an infinite rank.
    return "unprioritized:p2-fallback", PRIORITY_RANK["priority:p2"]


def review_stage(state: QueueState) -> tuple[str, int]:
    final_review = state.prior_full_reviews > 0 or state.prior_changes_requested
    return ("final-review", 0) if final_review else ("initial-review", 1)


def eligible_for_full_review(state: QueueState) -> bool:
    if not state.review_ready or not state.ci_green:
        return False
    if state.packaging_present and not state.packaging_green:
        return False
    if state.required_gate_active and not state.required_gate_green:
        return False
    if state.unresolved_threads:
        return False
    if state.current_head_decision == "APPROVED":
        return False
    if (
        state.current_head_decision == "CHANGES_REQUESTED"
        and state.current_head_inline_findings == 0
    ):
        return False
    return True


def may_emit_command(state: QueueState, shared_slot_busy: bool = False) -> bool:
    if shared_slot_busy or state.requested_lock or state.rate_limited:
        return False
    return eligible_for_full_review(state)


def order_key(state: QueueState):
    _, rank = queue_priority(state)
    _, stage_rank = review_stage(state)
    issue = state.issue_number if state.issue_number is not None else 2**63 - 1
    return (
        rank,
        stage_rank,
        state.prior_full_reviews,
        state.created_at,
        issue,
        state.pr_number,
    )


class CodeRabbitQueueLifecycleTests(unittest.TestCase):
    def test_changes_requested_waits_for_finding_verification_then_final_review(self):
        repairing = QueueState(
            priority="priority:p2",
            unresolved_threads=1,
            current_head_decision=None,
            prior_full_reviews=1,
            prior_changes_requested=True,
        )
        self.assertFalse(eligible_for_full_review(repairing))

        resolved = QueueState(
            priority="priority:p2",
            unresolved_threads=0,
            current_head_decision=None,
            prior_full_reviews=1,
            prior_changes_requested=True,
        )
        self.assertTrue(eligible_for_full_review(resolved))
        self.assertEqual(("final-review", 0), review_stage(resolved))

    def test_failed_packaging_or_required_gate_never_spends_review_slot(self):
        packaging_red = QueueState(
            packaging_present=True,
            packaging_green=False,
        )
        required_gate_red = QueueState(
            required_gate_active=True,
            required_gate_green=False,
        )
        self.assertFalse(may_emit_command(packaging_red))
        self.assertFalse(may_emit_command(required_gate_red))

    def test_policy_changes_requested_without_inline_findings_is_terminal_for_head(self):
        blocked = QueueState(
            current_head_decision="CHANGES_REQUESTED",
            current_head_inline_findings=0,
            prior_full_reviews=1,
        )
        self.assertFalse(eligible_for_full_review(blocked))

        actionable = QueueState(
            current_head_decision="CHANGES_REQUESTED",
            current_head_inline_findings=2,
            prior_full_reviews=1,
        )
        self.assertTrue(eligible_for_full_review(actionable))

    def test_exact_head_approval_stops_queue_and_new_head_without_approval_reenters(self):
        approved = QueueState(
            current_head_decision="APPROVED",
            prior_full_reviews=1,
        )
        self.assertFalse(eligible_for_full_review(approved))

        # A new HEAD has no current-head decision even if an older HEAD was
        # approved; it must receive a fresh review unless an explicit carried
        # approval path is established elsewhere.
        moved_head = QueueState(
            current_head_decision=None,
            prior_full_reviews=1,
        )
        self.assertTrue(eligible_for_full_review(moved_head))

    def test_requested_lock_and_shared_slot_make_duplicate_wakeups_idempotent(self):
        candidate = QueueState()
        self.assertTrue(may_emit_command(candidate))
        self.assertFalse(may_emit_command(QueueState(requested_lock=True)))
        self.assertFalse(may_emit_command(candidate, shared_slot_busy=True))

    def test_priority_is_primary_and_final_review_wins_inside_same_class(self):
        p0_initial = QueueState(
            priority="priority:p0",
            prior_full_reviews=0,
            pr_number=10,
        )
        p1_final = QueueState(
            priority="priority:p1",
            prior_full_reviews=1,
            prior_changes_requested=True,
            pr_number=11,
        )
        self.assertLess(order_key(p0_initial), order_key(p1_final))

        p2_initial = QueueState(
            priority="priority:p2",
            prior_full_reviews=0,
            created_at=1,
            pr_number=12,
        )
        p2_final = QueueState(
            priority="priority:p2",
            prior_full_reviews=1,
            prior_changes_requested=True,
            created_at=100,
            pr_number=13,
        )
        self.assertLess(order_key(p2_final), order_key(p2_initial))

    def test_unprioritized_is_explicit_p2_fallback_and_oldest_breaks_ties(self):
        old_unprioritized = QueueState(
            priority=None,
            created_at=1,
            pr_number=105,
        )
        newer_p2 = QueueState(
            priority="priority:p2",
            created_at=2,
            pr_number=107,
        )
        self.assertEqual(
            ("unprioritized:p2-fallback", 2),
            queue_priority(old_unprioritized),
        )
        self.assertLess(order_key(old_unprioritized), order_key(newer_p2))

    def test_rate_limit_keeps_single_slot_serialized(self):
        rate_limited = QueueState(rate_limited=True)
        self.assertFalse(may_emit_command(rate_limited))
        self.assertTrue(may_emit_command(QueueState(rate_limited=False)))


if __name__ == "__main__":
    unittest.main()
