"""Regression for 429 fresh-runner checkout of an unpublished issue branch.

Checks the ref expression in the actual checked-in Actions workflow rather
than reimplementing the fallback in a separate Python helper.
Production examples: #357 / #373 / #374 / #382 / #395.
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".github" / "workflows" / "continuum-opencode.yml"

EVALUATE_EXPRESSION = r"""
const expression = process.argv[1];
const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const result = new Function('inputs', 'github', 'return (' + expression + ')')(
  payload.inputs, payload.github
);
process.stdout.write(String(result));
"""


class FreshRunner429CheckoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = yaml.safe_load(ENGINE.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["opencode"]["steps"]
        checkout = next(s for s in steps if s.get("name") == "Checkout repository")
        restore = next(
            s for s in steps if s.get("name") == "Restore 429 recovery checkpoint"
        )
        cls.steps = steps
        cls.restore = restore
        match = re.fullmatch(
            r"\$\{\{\s*(.*?)\s*\}\}", checkout["with"]["ref"]
        )
        if not match:
            raise AssertionError("Checkout ref must be a single Actions expression")
        cls.expression = match.group(1)

    def resolve(self, *, mode="issue", checkpoint="", head="", base="main"):
        payload = {
            "inputs": {
                "mode": mode,
                "recovery_checkpoint_ref": checkpoint,
                "head_ref": head,
                "base_ref": base,
            },
            "github": {
                "ref": "refs/heads/main",
                "event": {"repository": {"default_branch": "main"}},
            },
        }
        proc = subprocess.run(
            ["node", "-e", EVALUATE_EXPRESSION, self.expression],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_real_unpublished_head_examples_recover_from_existing_base(self):
        incidents = [
            (357, "39852307581"),
            (373, "39863508457"),
            (374, "39882889392"),
            (382, "39883956862"),
            (395, "39899690211"),
        ]
        for issue, run in incidents:
            head = "opencode/issue{}-{}".format(issue, run)
            checkpoint = (
                "opencode/429-checkpoint-opencode-429-issue-issue{}-gen-1"
                .format(issue)
            )
            with self.subTest(issue=issue):
                self.assertEqual(
                    self.resolve(checkpoint=checkpoint, head=head), "main"
                )

    def test_does_not_require_missing_remote_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            proc = subprocess.run(
                ["git", "init", "-b", "main", str(repo)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "test@example.org"],
                check=True,
            )
            (repo / "README").write_text("local fixture", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "base"],
                check=True, capture_output=True,
            )
            ephemeral = "opencode/issue395-39899690211"
            checkout_ref = self.resolve(
                checkpoint="opencode/429-checkpoint-opencode-429-issue-issue395-gen-1",
                head=ephemeral,
            )
            self.assertNotEqual(
                subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "--verify", ephemeral],
                    capture_output=True,
                ).returncode, 0,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "--verify", checkout_ref],
                    capture_output=True,
                ).returncode, 0,
            )

    def test_ordinary_issue_does_not_change(self):
        self.assertEqual(self.resolve(), "refs/heads/main")
        self.assertEqual(self.resolve(head="feature/ordinary"), "feature/ordinary")

    def test_pr_repair_and_qualification_keep_head_semantics(self):
        for mode in ("ci-fix", "coderabbit-fix", "resolve-conflict"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    self.resolve(
                        mode=mode,
                        checkpoint="opencode/429-checkpoint-a-gen-1",
                        head="actual-pr-head",
                    ),
                    "actual-pr-head",
                )
        self.assertEqual(
            self.resolve(mode="qualification", checkpoint="opencode/429-checkpoint-x"),
            "refs/heads/main",
        )

    def test_restore_still_verifies_pinned_checkpoint_before_agent(self):
        names = [s.get("name") for s in self.steps]
        self.assertLess(
            names.index("Checkout repository"),
            names.index("Restore 429 recovery checkpoint"),
        )
        self.assertEqual(
            self.restore["if"], "inputs.recovery_checkpoint_ref != ''"
        )
        restore_body = self.restore["run"]
        for marker in (
            'git fetch --no-tags origin "$RECOVERY_CHECKPOINT_REF"',
            "RECOVERY_CHECKPOINT_SHA",
            'git show "FETCH_HEAD:.continuum-429-recovery.json"',
            "git switch --detach FETCH_HEAD",
        ):
            self.assertIn(marker, restore_body)
        self.assertLess(
            names.index("Restore 429 recovery checkpoint"),
            names.index("Implement issue"),
        )


if __name__ == "__main__":
    unittest.main()
