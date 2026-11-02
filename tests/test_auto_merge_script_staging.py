"""The auto-merge controller must not reach github-script through argv.

actions/github-script interpolates its ``script:`` input into a Node command
line. Linux limits one argv entry to 128 KiB, so the controller stages its
JavaScript in ``$RUNNER_TEMP`` from a quoted heredoc and leaves only a small
loader in ``script:``. These tests pin the staging contract and execute the
real loader with the production controller in a github-script-shaped scope.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTO_MERGE = ROOT / ".github" / "workflows" / "continuum-auto-merge.yml"
PR_AGENT_MERGE = (
    ROOT / ".github" / "workflows" / "continuum-pr-agent-auto-merge.yml"
)
EMBEDDER = ROOT / "scripts" / "embed_workflow_dispatch_contract.py"
TMP_PARENT = ROOT / ".opencode-tmp"
STAGE_MARKER = (
    'cat > "$RUNNER_TEMP/continuum/controller.js" <<\'CONTINUUM_JS\''
)
CONTROLLER_STEP = "      - name: Merge only fully reviewed current heads\n"
LOADER = "await eval('(async () => {' + controllerSource + '\\n})()');"

HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const loader = fs.readFileSync(process.argv[3], 'utf8');
(async () => {
  const github = {};
  const context = { repo: { owner: 'owner', repo: 'repo' } };
  let failure = null;
  const core = { setFailed: message => { failure = String(message); } };
  await eval('(async () => {' + loader + '\n})()');
  process.stdout.write(JSON.stringify({ failure }));
})().catch(err => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
"""


def load_embedder():
    spec = importlib.util.spec_from_file_location(
        "embed_workflow_dispatch_contract", EMBEDDER
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def staged_controller(text: str) -> str:
    lines = text.splitlines(keepends=True)
    start = next(
        index for index, line in enumerate(lines)
        if line.strip() == STAGE_MARKER
    )
    body: list[str] = []
    for line in lines[start + 1:]:
        if line.strip() == "CONTINUUM_JS":
            return textwrap.dedent("".join(body))
        body.append(line)
    raise AssertionError("unterminated CONTINUUM_JS heredoc")


def script_loader(text: str) -> str:
    lines = text.splitlines(keepends=True)
    start = lines.index(CONTROLLER_STEP)
    marker = "          script: |\n"
    script = lines.index(marker, start) + 1
    body: list[str] = []
    for line in lines[script:]:
        if line.strip() and len(line) - len(line.lstrip(" ")) < 12:
            break
        body.append(line[12:] if line.strip() else line)
    return "".join(body).rstrip("\n")


class AutoMergeScriptStagingTests(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="script-staging-", dir=str(TMP_PARENT))
        )

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_every_workflow_keeps_inline_scripts_below_the_argv_limit(self):
        embedder = load_embedder()
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        self.assertTrue(workflows, "expected workflow files")
        for target in workflows:
            with self.subTest(workflow=target.name):
                sizes = embedder.inline_script_sizes(
                    target.read_text(encoding="utf-8")
                )
                for line_number, size in sizes:
                    self.assertLessEqual(
                        size,
                        embedder.MAX_INLINE_SCRIPT_BYTES,
                        f"{target.name}:{line_number} would overflow argv",
                    )
        # The generated controllers are the reason the limit exists, so keep
        # proving they still carry an inline script block at all.
        for target in (AUTO_MERGE, PR_AGENT_MERGE):
            with self.subTest(generated=target.name):
                self.assertTrue(
                    embedder.inline_script_sizes(
                        target.read_text(encoding="utf-8")
                    ),
                    "expected at least one script: block",
                )

    def test_checker_scans_workflows_outside_the_generator_targets(self):
        embedder = load_embedder()
        workflows = self.workdir / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "unrelated.yml").write_text(
            "jobs:\n  x:\n    steps:\n"
            "      - uses: actions/github-script@v7\n"
            "        with:\n"
            "          script: |\n"
            "            // " + "x" * (embedder.MAX_INLINE_SCRIPT_BYTES + 1) + "\n",
            encoding="utf-8",
        )
        original_root = embedder.ROOT
        embedder.ROOT = self.workdir
        try:
            with self.assertRaises(SystemExit) as caught:
                embedder.check_every_workflow_script_size()
        finally:
            embedder.ROOT = original_root
        self.assertIn("argv-safe limit", str(caught.exception))

    def test_checker_rejects_an_oversized_inline_script(self):
        embedder = load_embedder()
        text = (
            "jobs:\n  x:\n    steps:\n"
            "      - uses: actions/github-script@v7\n"
            "        with:\n"
            "          script: |\n"
            "            // " + "x" * (embedder.MAX_INLINE_SCRIPT_BYTES + 1) + "\n"
        )
        with self.assertRaises(SystemExit) as caught:
            embedder.check_inline_script_sizes(Path("workflow.yml"), text)
        self.assertIn("argv-safe limit", str(caught.exception))

    def test_generator_refuses_to_write_an_oversized_script(self):
        embedder = load_embedder()
        target = self.workdir / "workflow.yml"
        target.write_text(
            "jobs:\n  x:\n    steps:\n"
            "      - uses: actions/github-script@v7\n"
            "        with:\n"
            "          script: |\n"
            "            // BEGIN continuum-workflow-dispatch-contract\n"
            "            // END continuum-workflow-dispatch-contract\n"
            "            // " + "x" * (embedder.MAX_INLINE_SCRIPT_BYTES + 1) + "\n",
            encoding="utf-8",
        )
        original_targets, original_argv = embedder.TARGETS, sys.argv
        embedder.TARGETS = (target,)
        sys.argv = ["embed_workflow_dispatch_contract.py", "--check"]
        try:
            with self.assertRaises(SystemExit) as caught:
                embedder.main()
        finally:
            embedder.TARGETS, sys.argv = original_targets, original_argv
        self.assertIn("argv-safe limit", str(caught.exception))

    def test_controller_is_staged_from_a_quoted_heredoc(self):
        body = AUTO_MERGE.read_text(encoding="utf-8")
        self.assertIn(STAGE_MARKER, body)
        controller = staged_controller(body)
        self.assertIn("const owner = context.repo.owner;", controller)
        self.assertNotIn("${{", controller, "staged JS must not be interpolated")
        loader = script_loader(body)
        self.assertIn("process.env.RUNNER_TEMP + '/continuum/controller.js'", loader)
        self.assertIn(LOADER, loader)
        self.assertLess(len(loader.encode("utf-8")), 1024)

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_real_loader_exposes_github_context_and_core_to_the_controller(self):
        body = AUTO_MERGE.read_text(encoding="utf-8")
        staged = self.workdir / "continuum"
        staged.mkdir()
        controller = staged / "controller.js"
        controller.write_text(staged_controller(body), encoding="utf-8")
        loader = self.workdir / "loader.js"
        loader.write_text(script_loader(body), encoding="utf-8")
        harness = self.workdir / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        env = dict(os.environ)
        env["RUNNER_TEMP"] = str(self.workdir)
        env.pop("READ_GITHUB_TOKEN", None)
        completed = subprocess.run(
            ["node", str(harness), str(controller), str(loader)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(
            "READ_GITHUB_TOKEN is empty; same-repository reads require "
            "GITHUB_TOKEN and must not fall back to TAP_PAT.",
            result["failure"],
            "the controller must execute with github, context, and core in scope",
        )


if __name__ == "__main__":
    unittest.main()
