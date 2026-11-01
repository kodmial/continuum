"""Executable contract for CodeRabbit-owned UNRESOLVED batching and canonical OpenCode."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
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
        self.assertIn("if (supportsBatch) inputs.unresolved_items_json = batch", batcher)
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

    def test_429_checkpoint_preserves_the_batched_finding_identity(self):
        executor = OPENCODE.read_text(encoding="utf-8")
        watchdog = (ROOT / ".github/workflows/continuum-opencode-watchdog.yml").read_text(encoding="utf-8")
        self.assertIn('"unresolved_items_json": os.environ.get("UNRESOLVED_ITEMS_JSON", "")', executor)
        self.assertIn("unresolved_items_json: String(identity.unresolved_items_json || '')", watchdog)
        self.assertIn('unresolved_items_json: "${{ inputs.unresolved_items_json }}"',
                      STUB.read_text(encoding="utf-8"))
        self.assertIn("steps.unresolved_batch.outputs.items_json", executor)

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
process.env.COMMENT_PAT = 'test-pat';
const comments = [
 {id: 100, user:{login:'coderabbitai[bot]'}, pull_request_review_id:11, body:'issue one'},
 {id: 200, user:{login:'coderabbitai[bot]'}, pull_request_review_id:22, body:'issue two'},
 {id: 101, in_reply_to_id:100, user:{login:'kodmial'},
  body:'<!-- opencode-coderabbit-verification review=11 finding=100 head='+head+' -->'},
 {id: 201, in_reply_to_id:200, user:{login:'kodmial'},
  body:'<!-- auto-merge-coderabbit-verification finding=200 head='+head+' -->'},
 {id: 102, in_reply_to_id:100, user:{login:'coderabbitai[bot]'}, body:'**UNRESOLVED**'},
 {id: 202, in_reply_to_id:200, user:{login:'coderabbitai[bot]'}, body:'The finding remains valid and requires a code fix.'},
 {id: 103, in_reply_to_id:100, user:{login:'coderabbitai[bot]'}, body:'**RESOLVED**'},
 {id: 104, in_reply_to_id:100, user:{login:'random'}, body:'**UNRESOLVED**'}
];
const out = {};
const core = {
 setOutput: (k,v) => {out[k]=String(v);}, notice: () => {}, warning: () => {}
};
const context = {repo:{owner:'kodmial', repo:'nanodictate'},
 payload:{pull_request:{number:226}}};
const github = {
 request: {endpoint: {DEFAULTS: {baseUrl: 'https://api.github.com'}}},
 constructor: function () {
   this.rest = {users:{getAuthenticated:async()=>({data:{login:'kodmial'}})}};
 },
 graphql: async () => ({repository:{pullRequest:{reviewThreads:{
  nodes:[
   {isResolved:false,comments:{nodes:[{databaseId:100,author:{login:'coderabbitai[bot]'}}]}},
   {isResolved:false,comments:{nodes:[{databaseId:200,author:{login:'coderabbitai[bot]'}}]}}
  ],
  pageInfo:{hasNextPage:false,endCursor:null}
 }}}}),
 rest:{
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

        # The same original CodeRabbit collector must still batch two
        # simultaneously actionable findings across distinct review IDs.
        both = node_fixture.replace(
            "{id: 103, in_reply_to_id:100, user:{login:'coderabbitai[bot]'}, body:'**RESOLVED**'}",
            "{id: 103, in_reply_to_id:100, user:{login:'random'}, body:'not a verdict'}",
        )
        result = subprocess.run(
            ["node", "-"],
            input=both + "\nasync function run(){\n" + production +
                  "\n}\nrun().then(() => console.log(JSON.stringify(out))).catch(e=>{console.error(e);process.exit(1)})",
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        active = json.loads(json.loads(result.stdout)["items_json"])
        self.assertEqual([item["finding_id"] for item in active], [100, 200])

        # Marker trust needs the PAT identity: without it the collector must
        # fail closed rather than accept a copied marker from any commenter.
        missing_pat = node_fixture.replace(
            "process.env.COMMENT_PAT = 'test-pat';",
            "delete process.env.COMMENT_PAT;",
        )
        result = subprocess.run(
            ["node", "-"],
            input=missing_pat + "\nasync function run(){\n" + production +
                  "\n}\nrun().then(() => console.log(JSON.stringify(out))).catch(e=>{console.error(e);process.exit(1)})",
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("COMMENT_PAT is required", result.stderr)


class DispatchTargetResolutionTests(unittest.TestCase):
    """The canonical OpenCode target is configurable without changing defaults."""

    def test_batcher_targets_the_canonical_caller_with_legacy_default(self):
        batcher = BATCHER.read_text(encoding="utf-8")
        self.assertIn(
            "CONTINUUM_OPENCODE_WORKFLOW: "
            "${{ inputs.opencode_workflow || "
            "vars.CONTINUUM_OPENCODE_WORKFLOW || 'continuum-opencode.yml' }}",
            batcher,
        )
        self.assertIn("const opencodeWorkflow = resolveOpencodeWorkflow();", batcher)
        self.assertIn("workflow_id: opencodeWorkflow", batcher)
        self.assertIn("path: `.github/workflows/${opencodeWorkflow}`", batcher)
        self.assertNotIn("workflow_id: 'continuum-opencode.yml'", batcher)

    def test_legacy_shim_dispatches_the_configured_caller(self):
        legacy = LEGACY.read_text(encoding="utf-8")
        self.assertIn(
            "OPENCODE_WORKFLOW: "
            "${{ inputs.opencode_workflow || "
            "vars.CONTINUUM_OPENCODE_WORKFLOW || 'continuum-opencode.yml' }}",
            legacy,
        )
        self.assertIn("actions/workflows/${OPENCODE_WORKFLOW}/dispatches", legacy)
        self.assertNotIn("actions/workflows/continuum-opencode.yml/dispatches", legacy)


def run_for_step(workflow, step_name):
    text = workflow.read_text(encoding="utf-8")
    marker = "      - name: " + step_name + "\n"
    assert text.count(marker) == 1, "expected one " + step_name
    section = text.split(marker, 1)[1].split("\n      - name: ", 1)[0]
    match = re.search(r"(?ms)^        run: \|\n(.*)", section)
    assert match, "missing shell for " + step_name
    return textwrap.dedent(match.group(1))


class LegacyShimDispatchGuardTests(unittest.TestCase):
    """The legacy shim guards the workflow name before it reaches the URL."""

    def setUp(self):
        ROOT.joinpath(".opencode-tmp").mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="legacy-dispatch-", dir=str(ROOT / ".opencode-tmp"))
        )
        bin_dir = self.workdir / "bin"
        bin_dir.mkdir()
        self.argv_file = self.workdir / "gh-argv"
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$GH_ARGS_FILE\"\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        self.env = {
            "PATH": str(bin_dir) + ":/usr/bin:/bin",
            "GH_ARGS_FILE": str(self.argv_file),
            "GITHUB_REPOSITORY": "owner/repo",
            "PR_NUMBER": "226",
            "HEAD_REF": "opencode/issue1-a",
            "REVIEW_ID": "11",
            "OPENCODE_WORKFLOW": "",
        }

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def dispatch(self, configured):
        env = dict(self.env, OPENCODE_WORKFLOW=configured)
        completed = subprocess.run(
            ["bash", "-c", run_for_step(LEGACY, "Dispatch OpenCode retry")],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv = self.argv_file.read_text(encoding="utf-8").splitlines()
        url = next(
            (item for item in argv if "/dispatches" in item),
            "",
        )
        self.assertTrue(url, argv)
        return completed, url

    def test_invalid_values_fall_back_to_the_legacy_literal(self):
        for configured in (
            "nested/opencode.yml",
            "nested\\opencode.yml",
            "opencode..yml",
            "opencode.yaml",
        ):
            with self.subTest(configured=configured):
                completed, url = self.dispatch(configured)
                self.assertIn(
                    "repos/owner/repo/actions/workflows/continuum-opencode.yml/dispatches",
                    url,
                )
                self.assertIn("Invalid OpenCode caller workflow", completed.stderr)

    def test_a_valid_value_is_dispatched_unchanged(self):
        _, url = self.dispatch("opencode.yml")
        self.assertIn(
            "repos/owner/repo/actions/workflows/opencode.yml/dispatches", url
        )

    def test_empty_value_uses_the_legacy_literal(self):
        _, url = self.dispatch("")
        self.assertIn(
            "repos/owner/repo/actions/workflows/continuum-opencode.yml/dispatches",
            url,
        )


if __name__ == "__main__":
    unittest.main()
