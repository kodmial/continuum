"""Executable contract for CodeRabbit-owned UNRESOLVED batching and canonical OpenCode."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
BATCHER = ROOT / ".github/workflows/continuum-coderabbit-unresolved.yml"
OPENCODE = ROOT / ".github/workflows/continuum-opencode.yml"
LEGACY = ROOT / ".github/workflows/continuum-opencode-unresolved.yml"
INSTALLER = ROOT / "install.sh"
STUB = ROOT / ".github/caller-stubs/continuum-opencode.yml"


def js_for_step(workflow, step_name):
    text = workflow.read_text(encoding="utf-8")
    marker = "      - name: " + step_name + "\n"
    assert text.count(marker) == 1, "expected one " + step_name
    section = text.split(marker, 1)[1].split("\n      - name: ", 1)[0]
    match = re.search(r"(?ms)^          script: \|\n(.*)", section)
    assert match, "missing JS for " + step_name
    return textwrap.dedent(match.group(1))


class CodeRabbitBatchContractTests(unittest.TestCase):
    def test_one_provider_owner_and_one_model_executor(self):
        installer = INSTALLER.read_text(encoding="utf-8")
        core = installer.split("CORE_STUBS=(", 1)[1].split(")", 1)[0]
        self.assertIn("\n  continuum-coderabbit-unresolved.yml\n", core)
        self.assertNotIn("\n  continuum-opencode-unresolved.yml\n", core)
        batcher = BATCHER.read_text(encoding="utf-8")
        canonical = OPENCODE.read_text(encoding="utf-8")
        self.assertIn("Collect all unresolved findings", batcher)
        self.assertIn("Dispatch one batched repair to canonical OpenCode", batcher)
        self.assertIn("unresolved_items_json: batch", batcher)
        self.assertNotIn("opencode run --auto", batcher)
        self.assertNotIn("1.18.34", batcher)
        self.assertNotIn("git push ", batcher)
        self.assertNotIn("git commit ", batcher)
        self.assertIn("steps.model_route.outputs.model", canonical)
        self.assertIn("steps.cached_opencode.outputs.runtime-version", canonical)
        self.assertIn("Qualify current CodeRabbit unresolved batch", canonical)
        self.assertIn("Fix CodeRabbit review findings", canonical)
        self.assertIn("Ask CodeRabbit to verify every original finding", canonical)
        self.assertIn("UNRESOLVED_ITEMS_JSON", canonical)
        self.assertIn("unresolved_items_json:", STUB.read_text(encoding="utf-8"))

    def test_old_handler_is_a_migration_shim_not_a_competing_owner(self):
        legacy = LEGACY.read_text(encoding="utf-8")
        self.assertIn("CANONICAL_CALLER", legacy)
        self.assertIn('echo "dispatch=false"', legacy)
        self.assertIn("(vars.CONTINUUM_REVIEW_PROVIDER || 'none') == 'coderabbit'", legacy)

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_all_github_script_steps_parse_as_javascript(self):
        sources = [
            (BATCHER, "Collect all unresolved findings"),
            (BATCHER, "Dispatch one batched repair to canonical OpenCode"),
            (OPENCODE, "Qualify current CodeRabbit unresolved batch"),
            (OPENCODE, "Ask CodeRabbit to verify every original finding"),
        ]
        for workflow, step in sources:
            with self.subTest(workflow=workflow.name, step=step):
                code = "async function testWorkflow() {\n" + js_for_step(workflow, step) + "\n}\n"
                check = subprocess.run(
                    ["node", "--check", "-"], input=code, text=True,
                    capture_output=True, check=False,
                )
                self.assertEqual(check.returncode, 0, check.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_original_batch_collector_keeps_multiple_latest_unresolved(self):
        # Exercise the actual production JS collector with GitHub API fixtures:
        # two CodeRabbit findings from different reviews; a newer RESOLVED
        # supersedes one UNRESOLVED; an unrelated author never participates.
        node_fixture = """
const head = 'a'.repeat(40);
const comments = [
 {id: 100, user:{login:'coderabbitai[bot]'}, pull_request_review_id:11, body:'issue one'},
 {id: 200, user:{login:'coderabbitai[bot]'}, pull_request_review_id:22, body:'issue two'},
 {id: 101, in_reply_to_id:100, user:{login:'kodmial'},
  body:'<!-- opencode-coderabbit-verification review=11 finding=100 head='+head+' -->'},
 {id: 201, in_reply_to_id:200, user:{login:'kodmial'},
  body:'<!-- auto-merge-coderabbit-verification finding=200 head='+head+' -->'},
 {id: 102, in_reply_to_id:100, user:{login:'coderabbitai[bot]'}, body:'**UNRESOLVED**'},
 {id: 202, in_reply_to_id:200, user:{login:'coderabbitai[bot]'}, body:'**UNRESOLVED**'},
 {id: 103, in_reply_to_id:100, user:{login:'coderabbitai[bot]'}, body:'**RESOLVED**'},
 {id: 104, in_reply_to_id:100, user:{login:'random'}, body:'**UNRESOLVED**'}
];
const out = {};
const core = {
 setOutput: (k,v) => {out[k]=String(v);}, notice: () => {}, warning: () => {}
};
const context = {repo:{owner:'kodmial', repo:'nanodictate'},
 payload:{pull_request:{number:226}}};
const github = {rest:{
 pulls:{
   get: async () => ({data:{review_comments:comments.length,
     head:{sha:head, ref:'opencode/task', repo:{full_name:'kodmial/nanodictate'}}}}),
   listReviewComments: async () => ({data:comments}),
   getReviewComment: async ({comment_id}) => ({data:comments.find(c=>c.id===comment_id)})
 },
 git:{getRef:async()=>({data:{object:{sha:head}}})}
}};
"""
        production = js_for_step(BATCHER, "Collect all unresolved findings")
        script = node_fixture + "\nasync function run(){\n" + production + "\n}\nrun().then(() => console.log(JSON.stringify(out))).catch(e=>{console.error(e);process.exit(1)})"
        result = subprocess.run(
            ["node", "-"], input=script, text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertEqual(values["should_run"], "true")
        active = json.loads(values["items_json"])
        self.assertEqual([item["finding_id"] for item in active], [200])
        self.assertEqual(active[0]["review_id"], 22)
        self.assertEqual(active[0]["unresolved_id"], 202)


if __name__ == "__main__":
    unittest.main()
