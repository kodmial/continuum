"""Live CodeRabbit bot-reply admission regression test (original workflow JS)."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/continuum-opencode.yml"


def script_for_step():
    source = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Qualify current CodeRabbit unresolved batch\n"
    assert source.count(marker) == 1
    section = source.split(marker, 1)[1].split("\n      - name: ", 1)[0]
    match = re.search(r"(?ms)^          script: \|\n(.*)", section)
    assert match, "missing executable batch-validation script"
    return textwrap.dedent(match.group(1))


@unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
class CodeRabbitExecutorAdmissionTests(unittest.TestCase):
    def run_case(self, resolved=False, newer_verification=False):
        node = """
const head='a'.repeat(40);
const find=101;
const review=22;
const verification={id:201, in_reply_to_id:find, user:{login:'kodmial'},
 body:'<!-- auto-merge-coderabbit-verification finding=101 head='+head+' -->'};
const reply={id:301,in_reply_to_id:find, user:{login:'coderabbitai[bot]'},
 body:'The finding remains valid. The underlying issue is not fixed.'};
const newer={id:401,in_reply_to_id:find, user:{login:'kodmial'},
 body:'<!-- auto-merge-coderabbit-verification finding=101 head='+head+' -->'};
process.env.PR_NUMBER='226';
process.env.HEAD_REF='opencode/task';
process.env.BATCH_JSON=JSON.stringify([{review_id:review,finding_id:find,
 unresolved_id:301,verified_head:head}]);
const comments=[verification,reply];
if (process.env.TEST_NEWER==='true') comments.push(newer);
const out={};
const core={setOutput:(k,v)=>out[k]=String(v),notice:()=>{}};
const context={repo:{owner:'kodmial',repo:'nanodictate'}};
const github={
 graphql:async()=>({repository:{pullRequest:{reviewThreads:{
  nodes:[{isResolved:process.env.TEST_RESOLVED==='true',
   comments:{nodes:[{databaseId:find,author:{login:'coderabbitai'}}]}}],
  pageInfo:{hasNextPage:false,endCursor:null}
 }}}}),
 rest:{
  pulls:{
   get:async()=>({data:{state:'open',head:{sha:head,ref:'opencode/task',
      repo:{full_name:'kodmial/nanodictate'}},review_comments:3}}),
   listReviewComments:async()=>({data:comments}),
   getReviewComment:async()=>({data:{user:{login:'coderabbitai[bot]'},
     pull_request_review_id:review,
     pull_request_url:'https://api.github.com/repos/kodmial/nanodictate/pulls/226'}})
  },
  git:{getRef:async()=>({data:{object:{sha:head}}})},
  repos:{compareCommits:async()=>({data:{status:'ahead'}})}
 }
};
"""
        code = (
            node + "\nasync function run(){\n" + script_for_step() +
            "\n}\nrun().then(()=>console.log(JSON.stringify(out))).catch(e=>{console.error(e);process.exit(1)})\n"
        )
        result = subprocess.run(
            ["node", "-"], input=code, text=True, capture_output=True, check=False,
            env={**__import__("os").environ,
                 "TEST_RESOLVED": str(resolved).lower(),
                 "TEST_NEWER": str(newer_verification).lower()}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_plain_language_unresolved_is_actionable(self):
        result = self.run_case()
        self.assertEqual(result["skip"], "false")
        self.assertEqual(json.loads(result["items_json"])[0]["unresolved_id"], 301)

    def test_resolved_github_thread_must_not_repair(self):
        self.assertEqual(self.run_case(resolved=True)["skip"], "true")

    def test_newer_verification_must_not_repair_stale_answer(self):
        self.assertEqual(self.run_case(newer_verification=True)["skip"], "true")


if __name__ == "__main__":
    unittest.main()
