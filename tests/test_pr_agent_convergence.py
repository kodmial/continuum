import unittest

from continuum.pr_agent_convergence import (
    logical_fingerprint,
    should_hold,
    transition_marker,
)


A = "a" * 40
B = "b" * 40
C = "c" * 40


class ConvergenceTests(unittest.TestCase):
    def test_fingerprint_is_order_independent(self):
        x = [{"source": "review", "finding": {"path": "a.py", "problem": "bug"}},
             {"source": "review", "finding": {"path": "b.py", "problem": "other"}}]
        self.assertEqual(logical_fingerprint(x), logical_fingerprint(list(reversed(x))))

    def test_location_and_score_do_not_change_logical_identity(self):
        before = [{"source": "review", "finding": {
            "path": "a.py", "problem": "null race", "line": 10, "score": 8}}]
        after = [{"source": "review", "finding": {
            "path": "a.py", "problem": "null race", "line": 14, "score": 9}}]
        self.assertEqual(logical_fingerprint(before), logical_fingerprint(after))

    def test_material_change_changes_identity(self):
        before = [{"source": "review", "finding": {"path": "a.py", "problem": "null race"}}]
        after = [{"source": "review", "finding": {"path": "a.py", "problem": "deadlock"}}]
        self.assertNotEqual(logical_fingerprint(before), logical_fingerprint(after))

    def test_no_diff_same_head_holds(self):
        fp = logical_fingerprint([{"source": "review", "finding": {"problem": "bug"}}])
        marker = f"<!-- continuum-pr-agent-no-progress head={A} fingerprint={fp} -->"
        self.assertTrue(should_hold(head_sha=A, fingerprint=fp, comment_bodies=[marker]))

    def test_repair_generated_head_same_finding_holds(self):
        fp = logical_fingerprint([{"source": "review", "finding": {"problem": "bug"}}])
        marker = transition_marker(A, B, fp)
        self.assertTrue(should_hold(head_sha=B, fingerprint=fp, comment_bodies=[marker]))

    def test_repair_generated_head_changed_finding_resumes(self):
        old = logical_fingerprint([{"source": "review", "finding": {"problem": "bug"}}])
        new = logical_fingerprint([{"source": "review", "finding": {"problem": "different"}}])
        self.assertFalse(should_hold(head_sha=B, fingerprint=new,
                                    comment_bodies=[transition_marker(A, B, old)]))

    def test_user_new_work_head_does_not_inherit_episode(self):
        fp = logical_fingerprint([{"source": "review", "finding": {"problem": "bug"}}])
        self.assertFalse(should_hold(head_sha=C, fingerprint=fp,
                                    comment_bodies=[transition_marker(A, B, fp)]))

    def test_different_finding_set_starts_new_episode(self):
        one = logical_fingerprint([{"source": "review", "finding": {"problem": "one"}}])
        two = logical_fingerprint([{"source": "review", "finding": {"problem": "two"}}])
        self.assertFalse(should_hold(head_sha=B, fingerprint=two,
                                    comment_bodies=[transition_marker(A, B, one)]))

    def test_invalid_identity_fails_closed(self):
        with self.assertRaises(ValueError):
            should_hold(head_sha="short", fingerprint="bad", comment_bodies=[])


if __name__ == "__main__":
    unittest.main()
