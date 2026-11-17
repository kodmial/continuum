"""The watchdog must not reach github-script through argv.

``actions/github-script`` interpolates its ``script:`` input into a Node command
line, and Linux caps one argv entry at 128 KiB. The watchdog body also carries
the self-heal logic for obsolete checkpoint holds, so it grows. Stage the real
implementation in ``$RUNNER_TEMP`` from a quoted heredoc and leave only a small
loader inline, mirroring the auto-merge controller.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / ".github" / "workflows" / "continuum-opencode-watchdog.yml"
DISPATCH_HELPER = ROOT / ".github" / "scripts" / "opencode_429_dispatch.js"
TMP_PARENT = ROOT / ".opencode-tmp"
STAGE_MARKER = (
    'cat > "$RUNNER_TEMP/continuum/opencode-watchdog.js" '
    "<<'CONTINUUM_WATCHDOG_JS'"
)
LOADER = "await eval('(async () => {' + watchdogSource + '\\n})()');"

HARNESS = r"""
const fs = require('fs');
const loader = fs.readFileSync(process.argv[2], 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
(async () => {
  const github = {
    request: { endpoint: { DEFAULTS: { baseUrl: 'https://api.github.com' } } },
  };
  const context = {
    repo: { owner: 'owner', repo: 'repo' },
    payload: { workflow_run: {} },
  };
  let failure = null;
  const core = { setFailed: message => { failure = String(message); } };
  try {
    const run = new AsyncFunction(
      'github', 'context', 'core', 'require', loader
    );
    await run(github, context, core, require);
  } catch (error) {
    failure = String(error && error.message || error);
  }
  process.stdout.write(JSON.stringify({ failure }));
})().catch(error => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""


def staged_watchdog(text: str) -> str:
    lines = text.splitlines(keepends=True)
    start = next(
        index for index, line in enumerate(lines)
        if line.strip() == STAGE_MARKER
    )
    body: list[str] = []
    for line in lines[start + 1:]:
        if line.strip() == "CONTINUUM_WATCHDOG_JS":
            return textwrap.dedent("".join(body))
        body.append(line)
    raise AssertionError("unterminated CONTINUUM_WATCHDOG_JS heredoc")


def script_loader(text: str) -> str:
    lines = text.splitlines(keepends=True)
    start = lines.index("      - name: Recover failed OpenCode issue run\n")
    script = lines.index("          script: |\n", start) + 1
    body: list[str] = []
    for line in lines[script:]:
        if line.strip() and len(line) - len(line.lstrip(" ")) < 12:
            break
        body.append(line[12:] if line.strip() else line)
    return "".join(body).rstrip("\n")


class WatchdogScriptStagingTests(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="watchdog-staging-", dir=str(TMP_PARENT))
        )

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_watchdog_is_staged_from_a_quoted_heredoc(self):
        body = WATCHDOG.read_text(encoding="utf-8")
        self.assertIn(STAGE_MARKER, body)
        self.assertLess(
            body.index(STAGE_MARKER),
            body.index("      - name: Recover failed OpenCode issue run\n"),
        )
        staged = staged_watchdog(body)
        self.assertIn("const owner = context.repo.owner;", staged)
        self.assertIn("recoverFrom429OnFreshVm", staged)
        self.assertIn("continuum-checkpoint-resume-release", staged)
        self.assertNotIn("${{", staged, "staged JS must not be interpolated")

    def test_github_script_keeps_a_small_file_loader(self):
        loader = script_loader(WATCHDOG.read_text(encoding="utf-8"))
        self.assertIn("process.env.CONTINUUM_WATCHDOG_SCRIPT", loader)
        self.assertIn(LOADER, loader)
        self.assertNotIn("recoverFrom429OnFreshVm", loader)
        self.assertLess(len(loader.encode("utf-8")), 1024)

    def test_loader_step_reads_the_staged_file_it_wrote(self):
        body = WATCHDOG.read_text(encoding="utf-8")
        start = body.index("      - name: Recover failed OpenCode issue run\n")
        step = body[start:body.index("          script: |\n", start)]
        self.assertIn(
            "CONTINUUM_WATCHDOG_SCRIPT: ${{ runner.temp }}/continuum/"
            "opencode-watchdog.js",
            step,
            "the loader must read the exact staged path, never an unset env",
        )
        self.assertIn(
            'cat > "$RUNNER_TEMP/continuum/opencode-watchdog.js"',
            body,
            "the staged path and the loader path must name the same file",
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_real_loader_executes_the_staged_watchdog_body(self):
        body = WATCHDOG.read_text(encoding="utf-8")
        staged = self.workdir / "continuum"
        staged.mkdir()
        script = staged / "opencode-watchdog.js"
        script.write_text(staged_watchdog(body), encoding="utf-8")
        loader = self.workdir / "loader.js"
        loader.write_text(script_loader(body), encoding="utf-8")
        harness = self.workdir / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        env = dict(os.environ)
        env["CONTINUUM_WATCHDOG_SCRIPT"] = str(script)
        env["CONTINUUM_DISPATCH_HELPER"] = str(DISPATCH_HELPER)
        env.pop("READ_GITHUB_TOKEN", None)
        completed = subprocess.run(
            ["node", str(harness), str(loader)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(
            "READ_GITHUB_TOKEN is required for same-repository watchdog reads.",
            result["failure"],
            "the staged body must execute with github, context, and core in scope",
        )


if __name__ == "__main__":
    unittest.main()
