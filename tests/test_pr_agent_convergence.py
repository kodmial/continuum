import unittest
from pathlib import Path

from continuum.pr_agent_convergence import (
    batch_fingerprint,
    decide,
    finding_fingerprints,
    transition_marker,
)

ROOT = Path(__file__).resolve().parents[1]
A = "a" * 40
B = "b" * 40
C = "c" * 40


def item(problem, *, line=1, index=0, path="a.py"):
    return {
        "source": "review",
        "index": index,
        "finding": {
            "relevant_file": path,
            "issue_header": "Possible Bug",
            "issue_content": problem,
            "start_line": line,
            "end_line": line + 1,
        },
    }


class ConvergenceStateTests(unittest.TestCase):
    def test_same_head_batch_fingerprint_keeps_33_contract(self):
        items = [
            {
                "source": "review",
                "index": 0,
                "finding": {
                    "relevant_file": "a.py",
                    "issue_header": "Possible Bug",
                    "issue_content": "x",
                    "start_line": 10,
                    "end_line": 12,
                },
            },
            {
                "source": "review",
                "index": 1,
                "finding": {
                    "relevant_file": "b.py",
                    "issue_header": "Race",
                    "issue_content": "y",
                    "start_line": 3,
                    "end_line": 4,
                },
            },
        ]
        self.assertEqual(
            batch_fingerprint(items),
            "c8bf13ad84df9de2d261b40c6a38696890d0a9e72dc05e02db2d9a13abb20fe2",
        )

    def test_location_index_and_whitespace_are_not_logical_identity(self):
        before = [item("null   race", line=10, index=0)]
        after = [item(" null race ", line=99, index=7)]
        self.assertEqual(finding_fingerprints(before), finding_fingerprints(after))

    def test_material_change_is_new_identity(self):
        self.assertNotEqual(
            finding_fingerprints([item("null race")]),
            finding_fingerprints([item("deadlock")]),
        )

    def test_no_diff_same_head_holds_everything(self):
        items = [item("bug")]
        batch = batch_fingerprint(items)
        marker = (
            f"<!-- continuum-pr-agent-no-progress head={A} "
            f"fingerprint={batch} -->"
        )
        decision = decide(
            head_sha=A,
            batch_fp=batch,
            current_finding_ids=finding_fingerprints(items),
            comment_bodies=[marker],
        )
        self.assertTrue(decision.held)
        self.assertTrue(decision.same_head_hold)
        self.assertFalse(decision.eligible)

    def test_same_logical_finding_surviving_repair_is_held(self):
        ids = finding_fingerprints([item("bug")])
        decision = decide(
            head_sha=B,
            batch_fp="1" * 64,
            current_finding_ids=ids,
            comment_bodies=[transition_marker(A, B, ids)],
        )
        self.assertTrue(decision.held)
        self.assertEqual(decision.surviving, frozenset(ids))
        self.assertFalse(decision.eligible)

    def test_partial_survivor_does_not_block_new_finding(self):
        old = finding_fingerprints([item("one"), item("two", index=1)])
        current_items = [item("two"), item("three", index=1)]
        current = finding_fingerprints(current_items)
        decision = decide(
            head_sha=B,
            batch_fp=batch_fingerprint(current_items),
            current_finding_ids=current,
            comment_bodies=[transition_marker(A, B, old)],
        )
        self.assertFalse(decision.held)
        self.assertEqual(len(decision.surviving), 1)
        self.assertEqual(len(decision.eligible), 1)

    def test_materially_changed_finding_resumes(self):
        old = finding_fingerprints([item("one")])
        current_items = [item("two")]
        decision = decide(
            head_sha=B,
            batch_fp=batch_fingerprint(current_items),
            current_finding_ids=finding_fingerprints(current_items),
            comment_bodies=[transition_marker(A, B, old)],
        )
        self.assertFalse(decision.held)
        self.assertFalse(decision.surviving)
        self.assertEqual(len(decision.eligible), 1)

    def test_user_new_work_head_invalidates_stale_episode(self):
        ids = finding_fingerprints([item("bug")])
        decision = decide(
            head_sha=C,
            batch_fp="1" * 64,
            current_finding_ids=ids,
            comment_bodies=[transition_marker(A, B, ids)],
        )
        self.assertFalse(decision.held)
        self.assertFalse(decision.surviving)
        self.assertEqual(decision.eligible, frozenset(ids))

    def test_duplicate_wake_state_is_idempotent(self):
        ids = finding_fingerprints([item("bug")])
        marker = transition_marker(A, B, ids)
        decision = decide(
            head_sha=B,
            batch_fp="1" * 64,
            current_finding_ids=ids,
            comment_bodies=[marker, marker],
        )
        self.assertEqual(decision.surviving, frozenset(ids))

    def test_marker_never_fabricates_upstream_state(self):
        ids = finding_fingerprints([item("bug")])
        marker = transition_marker(A, B, ids)
        self.assertNotIn("RESOLVED", marker)
        self.assertNotIn("ACTIVE", marker)

    def test_same_head_same_logical_finding_holds_despite_fp_drift(self):
        from continuum.pr_agent_convergence import no_progress_marker

        items = [item("bug")]
        ids = finding_fingerprints(items)
        marker = no_progress_marker(A, "2" * 64, ids)
        decision = decide(
            head_sha=A,
            batch_fp="3" * 64,
            current_finding_ids=ids,
            comment_bodies=[marker],
        )
        self.assertFalse(decision.same_head_hold)
        self.assertTrue(decision.held)
        self.assertEqual(decision.surviving, frozenset(ids))
        self.assertFalse(decision.eligible)

    def test_same_head_partial_overlap_keeps_new_finding_eligible(self):
        from continuum.pr_agent_convergence import no_progress_marker

        old_ids = finding_fingerprints([item("bug")])
        marker = no_progress_marker(A, "2" * 64, old_ids)
        current = finding_fingerprints([item("bug"), item("totally different")])
        decision = decide(
            head_sha=A,
            batch_fp="3" * 64,
            current_finding_ids=current,
            comment_bodies=[marker],
        )
        self.assertFalse(decision.same_head_hold)
        self.assertFalse(decision.held)
        self.assertEqual(decision.surviving, frozenset(old_ids))
        self.assertEqual(decision.eligible, frozenset(current) - frozenset(old_ids))

    def test_same_head_new_logical_finding_stays_eligible(self):
        from continuum.pr_agent_convergence import no_progress_marker

        old_ids = finding_fingerprints([item("bug")])
        marker = no_progress_marker(A, "2" * 64, old_ids)
        current = finding_fingerprints([item("totally different")])
        decision = decide(
            head_sha=A,
            batch_fp="3" * 64,
            current_finding_ids=current,
            comment_bodies=[marker],
        )
        self.assertFalse(decision.same_head_hold)
        self.assertFalse(decision.held)
        self.assertEqual(decision.eligible, frozenset(current))

    def test_parse_transitions_is_bounded(self):
        from continuum.pr_agent_convergence import parse_transitions

        bodies = [transition_marker(A, B, finding_fingerprints([item("bug")]))]
        capped = parse_transitions(bodies, max_markers=0)
        self.assertEqual(capped, [])
        many = [transition_marker(A, B, finding_fingerprints([item("bug")]))] * 5
        self.assertEqual(len(parse_transitions(many, max_markers=2)), 2)
        single = parse_transitions(
            ["<!-- continuum-pr-agent-convergence from="
             + A + " to=" + B + " findings=" + "ab" * 32 + " -->"]
        )
        self.assertEqual(len(single), 1)


class WorkflowWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (
            ROOT / ".github" / "workflows" / "continuum-pr-agent-repair.yml"
        ).read_text(encoding="utf-8")

    def test_cross_head_state_is_per_finding(self):
        self.assertIn("finding_ids", self.workflow)
        self.assertIn("identified_batch", self.workflow)
        self.assertIn('FINDING_IDS_LOWER="${FINDING_IDS,,}"', self.workflow)
        self.assertIn("findings=${FINDING_IDS_LOWER}", self.workflow)
        self.assertNotIn("to=${NEW_HEAD,,} fingerprint=${BATCH_FINGERPRINT,,}", self.workflow)

    def test_partial_survivors_are_filtered_not_global_hold(self):
        self.assertIn("eligible_batch", self.workflow)
        self.assertIn(
            "BATCH_JSON: ${{ steps.convergence.outputs.eligible_batch }}",
            self.workflow,
        )
        self.assertIn("identified.filter(entry => !survivingSet.has", self.workflow)

    def test_transition_is_durable_before_branch_move(self):
        transition = self.workflow.index('continuum-pr-agent-convergence from=')
        controller = self.workflow.index(
            "CONTROLLER_MARKER='<!-- continuum-pr-agent-controller-state:v1 -->'",
            transition,
        )
        upsert = self.workflow.index('gh api --method PATCH', controller)
        create = self.workflow.index('gh api --method POST', controller)
        push = self.workflow.index(
            'git push --force-with-lease="refs/heads/$HEAD_REF:$HEAD_SHA"'
        )
        self.assertLess(transition, controller)
        self.assertLess(controller, upsert)
        self.assertLess(controller, create)
        self.assertLess(upsert, push)
        self.assertLess(create, push)
        self.assertNotIn("gh pr comment", self.workflow)

    def test_same_head_marker_format_from_33_is_preserved(self):
        self.assertIn(
            "continuum-pr-agent-no-progress head=' + headSha +",
            self.workflow,
        )
        self.assertIn("const fingerprint = batch.fingerprint;", self.workflow)
        self.assertIn("policy.buildRepairBatch(review, raw)", self.workflow)

    def test_no_upstream_resolution_is_written(self):
        self.assertNotIn("state=RESOLVED", self.workflow)
        self.assertNotIn("state=ACTIVE", self.workflow)

    def test_js_whitespace_normalization_matches_python(self):
        self.assertIn("replace(/\\s+/g, ' ')", self.workflow)
        self.assertNotIn("replace(/\\\\s+/g, ' ')", self.workflow)

    def test_js_identity_is_strict_and_fail_closed(self):
        self.assertIn("const shaRe = /^[0-9a-f]{40}$/;", self.workflow)
        self.assertIn("const fpRe = /^[0-9a-f]{64}$/;", self.workflow)
        self.assertIn("failClosed(", self.workflow)
        self.assertIn(
            "steps.convergence.outcome == 'success'", self.workflow
        )

    def test_js_cross_checks_finding_ids(self):
        self.assertIn("identifiedIds", self.workflow)
        self.assertIn(
            "PR-Agent finding IDs do not match identified batch.", self.workflow
        )

    def test_js_same_head_holds_logical_finding(self):
        self.assertIn("sameHeadFindings", self.workflow)
        self.assertIn("findings=${FINDING_IDS_LOWER}", self.workflow)
        self.assertIn("FINDING_IDS: ${{ steps.convergence.outputs.eligible_findings }}", self.workflow)


if __name__ == "__main__":
    unittest.main()
