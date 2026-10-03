import unittest
from continuum.pr_agent_convergence import (
    batch_fingerprint, finding_fingerprints, should_hold, surviving_findings,
    transition_marker,
)
A="a"*40; B="b"*40; C="c"*40
def item(problem, line=1):
    return {"source":"review","finding":{"path":"a.py","problem":problem,"line":line},"index":0}

class ConvergenceTests(unittest.TestCase):
    def test_location_is_not_identity(self):
        self.assertEqual(finding_fingerprints([item("bug",1)]), finding_fingerprints([item("bug",99)]))
    def test_material_change_is_new_identity(self):
        self.assertNotEqual(finding_fingerprints([item("bug")]), finding_fingerprints([item("deadlock")]))
    def test_no_diff_same_head_holds(self):
        xs=[item("bug")]; batch=batch_fingerprint(xs)
        marker=f"<!-- continuum-pr-agent-no-progress head={A} fingerprint={batch} -->"
        self.assertTrue(should_hold(head_sha=A,batch_fp=batch,current_finding_ids=finding_fingerprints(xs),comment_bodies=[marker]))
    def test_repair_head_same_finding_holds(self):
        ids=finding_fingerprints([item("bug")]); marker=transition_marker(A,B,ids)
        self.assertTrue(should_hold(head_sha=B,batch_fp="1"*64,current_finding_ids=ids,comment_bodies=[marker]))
    def test_partial_survivor_holds_even_when_set_changed(self):
        old=finding_fingerprints([item("one"),item("two")])
        current=finding_fingerprints([item("two"),item("three")])
        marker=transition_marker(A,B,old)
        self.assertEqual(len(surviving_findings(head_sha=B,current_finding_ids=current,comment_bodies=[marker])),1)
        self.assertTrue(should_hold(head_sha=B,batch_fp="1"*64,current_finding_ids=current,comment_bodies=[marker]))
    def test_materially_changed_findings_resume(self):
        old=finding_fingerprints([item("one")]); current=finding_fingerprints([item("two")])
        self.assertFalse(should_hold(head_sha=B,batch_fp="1"*64,current_finding_ids=current,comment_bodies=[transition_marker(A,B,old)]))
    def test_user_new_head_invalidates_episode(self):
        ids=finding_fingerprints([item("bug")])
        self.assertFalse(should_hold(head_sha=C,batch_fp="1"*64,current_finding_ids=ids,comment_bodies=[transition_marker(A,B,ids)]))
    def test_duplicate_transition_is_idempotent(self):
        ids=finding_fingerprints([item("bug")]); marker=transition_marker(A,B,ids)
        self.assertEqual(surviving_findings(head_sha=B,current_finding_ids=ids,comment_bodies=[marker,marker]),frozenset(ids))
    def test_marker_contains_no_upstream_resolution_state(self):
        ids=finding_fingerprints([item("bug")]); marker=transition_marker(A,B,ids)
        self.assertNotIn("RESOLVED",marker); self.assertNotIn("ACTIVE",marker)

if __name__=="__main__": unittest.main()
