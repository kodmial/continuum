"""Regress queued-versus-completed CodeRabbit repairs.

Use the actual batch dispatcher JavaScript with a mocked GitHub API. A queued
marker must be a temporary lease, not a permanent completion receipt.
"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from test_coderabbit_unresolved_dispatch import js_for_step

ROOT = Path(__file__).resolve().parents[1]
BATCHER = ROOT / ".github/workflows/continuum-coderabbit-unresolved.yml"
AUTO_MERGE = ROOT / ".github/workflows/continuum-auto-merge.yml"


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class BatchOutcomeRecoveryTests(unittest.TestCase):
    def exercise(self, marker_minutes=None, run_state="completed"):
        fixture = r"""
const crypto = require('crypto');
const head = 'a'.repeat(40);
const items = [{review_id:22, finding_id:100, unresolved_id:205, verified_head:head}];
const batch = JSON.stringify(items);
const digest = crypto.createHash('sha256').update(head+'\n'+batch).digest('hex');
const marker = '<!-- continuum-coderabbit-batch head='+head+' digest='+digest+' -->';
const minutes = Number(process.env.MINUTES);
const at = new Date(Date.now()-minutes*60_000).toISOString();
const comments = process.env.MINUTES === '-1' ? [] : [{id:1,body:marker,created_at:at}];
const emitted = [];
const out = {dispatches:emitted, messages:[]};
const core = {notice:s=>out.messages.push(s), warning:s=>out.messages.push(s)};
const context = {repo:{owner:'kodmial',repo:'nanodictate'},
 payload:{repository:{default_branch:'main'}}};
process.env.PR_NUMBER = '226';
process.env.HEAD_REF = 'opencode/issue212-123';
process.env.COLLECTED_HEAD = head;
process.env.UNRESOLVED_ITEMS_JSON = batch;
const github = {
 paginate:async()=>comments,
 rest:{
  pulls:{get:async()=>({data:{state:'open',head:{
    sha:head,ref:'opencode/issue212-123',repo:{full_name:'kodmial/nanodictate'}}}})},
  actions:{
   listWorkflowRuns:async()=>({data:{workflow_runs:[{
      display_title:'OpenCode repair PR #226',
      status:process.env.RUN_STATE,
      created_at:at
   }]}}),
   createWorkflowDispatch:async params=>{emitted.push(params);}
  },
  repos:{getContent:async()=>({data:{
    content:Buffer.from('on:\n  workflow_dispatch:\n    inputs:\n      unresolved_items_json:\n').toString('base64')
  }})},
  issues:{createComment:async()=>({})}
 }
};
"""
        source = js_for_step(BATCHER, "Dispatch one batched repair to canonical OpenCode")
        code = fixture + "\nasync function run(){\n" + source + "\n}\nrun().then(()=>console.log(JSON.stringify(out))).catch(e=>{console.error(e);process.exit(1)})\n"
        result = subprocess.run(
            ["node", "-"], input=code, capture_output=True, text=True,
            check=False, env={**__import__("os").environ,
                              "MINUTES":str(marker_minutes if marker_minutes is not None else -1),
                              "RUN_STATE":run_state},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_first_batch_is_dispatched(self):
        self.assertEqual(len(self.exercise()["dispatches"]), 1)

    def test_recent_batch_lease_suppresses_duplicate(self):
        self.assertEqual(len(self.exercise(marker_minutes=2)["dispatches"]), 0)

    def test_active_run_does_not_get_duplicate_after_lease(self):
        self.assertEqual(len(self.exercise(marker_minutes=12, run_state="in_progress")["dispatches"]), 0)

    def test_completed_no_op_batch_gets_recovered(self):
        outcome = self.exercise(marker_minutes=12)
        self.assertEqual(len(outcome["dispatches"]), 1)
        self.assertTrue(any("Re-dispatching" in m for m in outcome["messages"]))

    def test_auto_merge_wakes_batcher_when_marker_is_not_completion(self):
        source = AUTO_MERGE.read_text(encoding="utf-8")
        self.assertIn("const recentBatchLease =", source)
        self.assertIn("Date.now() - batchAt < 10 * 60_000", source)
        self.assertIn("!recentBatchLease", source)
        self.assertIn("process.env.CONTINUUM_CODERABBIT_UNRESOLVED_WORKFLOW", source)
        self.assertIn("workflow_id: codeRabbitUnresolvedWorkflow", source)
        self.assertIn(
            "CONTINUUM_CODERABBIT_UNRESOLVED_WORKFLOW: "
            "${{ inputs.coderabbit_unresolved_workflow || "
            "vars.CONTINUUM_CODERABBIT_UNRESOLVED_WORKFLOW || "
            "'continuum-coderabbit-unresolved.yml' }}",
            source,
        )


if __name__ == "__main__":
    unittest.main()
