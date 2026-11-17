"""Race between automatic main sync and live OpenCode CI-repair."""

import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CiRepairMainSyncRace(unittest.TestCase):
    def test_reconciler_fences_before_both_branch_mutation_sites(self):
        body = (ROOT / ".github/workflows/continuum-auto-merge.yml").read_text()
        self.assertIn("async function activeOpenCodeRepairOperation(pr)", body)
        self.assertIn("process.env.OPENCODE_WORKFLOW || 'continuum-opencode.yml'", body)
        self.assertIn("const opencode = await activeOpenCodeRepairOperation(pr);", body)
        self.assertIn("if (opencode.active) return opencode;", body)
        self.assertIn("event: 'workflow_dispatch', status, per_page: 100,", body)
        self.assertEqual(body.count("activePrAgentHeadOperation(pr)"), 3)
        self.assertLess(
            body.index("const liveOperation = await activePrAgentHeadOperation(pr);"),
            body.index("PUT /repos/{owner}/{repo}/pulls/{pull_number}/update-branch"),
        )

    def test_live_run_guard_handles_other_pr_queued_completed_and_bad_api(self):
        text = (ROOT / ".github/workflows/continuum-auto-merge.yml").read_text()
        fn = "async function activeOpenCodeRepairOperation(pr) {" + text.split(
            "async function activeOpenCodeRepairOperation(pr) {", 1
        )[1].split(
            "          // A running PR-Agent review/repair is a live Work Lock", 1
        )[0]
        code = r"""
const assert = require('node:assert/strict');
const fn = SOURCE;
async function probe(status,name,opts={}) {
  const calls=[], warnings=[];
  const client={rest:{actions:{listWorkflowRuns:async p=>{
    calls.push(p);
    if(opts.error) throw Error('API 429');
    if(opts.invalid) return {data:{}};
    if(opts.truncated) return {data:{total_count:101,workflow_runs:[]}};
    return {data:{total_count:p.status===status?1:0,workflow_runs:p.status===status?
      [{id:42,event:opts.event||'workflow_dispatch',display_title:name,status}]:[]}};
  }}}};
  const f=new Function('owner','repo','withReadFallback','process','core',
    'ACTIVE_REPAIR_STATUSES',fn+';return activeOpenCodeRepairOperation;')(
      'kodmial','continuum',async cb=>cb(client),
      {env:{OPENCODE_WORKFLOW:'opencode.yml'}},
      {warning:s=>warnings.push(s)},
      new Set(['queued','in_progress','waiting','pending','requested']));
  return {state:await f({number:459}),calls,warnings};
}
(async()=>{
  for(const status of ['queued','in_progress','waiting','pending','requested']) {
    const x=await probe(status,'OpenCode repair PR #459 [ci-attempt:abc]');
    assert.equal(x.state.active,true,status);
    assert.equal(x.calls[0].workflow_id,'opencode.yml');
  }
  for(const name of ['OpenCode repair PR #4590','OpenCode repair PR #469',
                     'OpenCode issue #459']) {
    assert.equal((await probe('in_progress',name)).state.active,false,name);
  }
  assert.equal((await probe('completed','OpenCode repair PR #459')).state.active,false);
  assert.equal((await probe('in_progress','OpenCode repair PR #459',{event:'issue_comment'})).state.active,false);
  for(const opts of [{error:true},{invalid:true},{truncated:true}]) {
    const x=await probe('in_progress','OpenCode repair PR #459',opts);
    assert.equal(x.state.active,true);
    assert(x.warnings.length>0);
  }
})().catch(e=>{console.error(e);process.exitCode=1});
        """.replace("SOURCE", json.dumps(fn))
        result = subprocess.run(["node", "-e", code], capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ci_repair_push_discards_proven_stale_head_only(self):
        body = (ROOT / ".github/workflows/continuum-opencode.yml").read_text()
        script = body.split("      - name: Fix failed blocking workflow", 1)[1].split(
            "      - name: Re-run the consumer blocking CI workflow", 1
        )[0]
        self.assertLess(script.index('BEFORE_SHA=$(git rev-parse HEAD)'),
                        script.index('git fetch --no-tags origin "refs/heads/${HEAD_REF}"'))
        self.assertLess(script.index('git fetch --no-tags origin "refs/heads/${HEAD_REF}"'),
                        script.index('if ! git push origin "HEAD:${HEAD_REF}"; then'))
        self.assertIn('[[ "$REMOTE_HEAD" != "$BEFORE_SHA" ]]', script)
        self.assertIn('git rev-parse FETCH_HEAD', script)
        self.assertIn('CI-repair push failed without confirmed HEAD advance.', script)
        self.assertNotIn('git push --force', script)


if __name__ == "__main__":
    unittest.main()
