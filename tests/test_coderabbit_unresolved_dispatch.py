"""Regression contract for the single CodeRabbit -> Continuum OpenCode repair path."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / ".github/workflows/continuum-opencode-unresolved.yml"
INSTALLER = ROOT / "install.sh"


def dispatch_shell() -> str:
    workflow = CANONICAL.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^        run: \|\n(.*?)^      - name: Dispatch OpenCode retry",
        workflow,
    )
    if not match:
        raise AssertionError("Missing canonical CodeRabbit UNRESOLVED dispatch script")
    return textwrap.dedent(match.group(1))


def verification_filter() -> str:
    shell = dispatch_shell()
    match = re.search(
        r'(?s)--arg owner "\$REPO_OWNER" \'(.*?)\' <<<"\$COMMENTS"\)',
        shell,
    )
    if not match:
        raise AssertionError("Missing production jq verification selector")
    return match.group(1)


class CodeRabbitUnresolvedDispatchContract(unittest.TestCase):
    def test_real_dispatch_shell_is_syntactically_valid(self):
        if not shutil.which("bash"):
            self.skipTest("bash unavailable")
        result = subprocess.run(
            ["bash", "-n"], input=dispatch_shell(), text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("jq"), "jq unavailable")
    def test_both_verified_sources_use_one_newest_authorized_request(self):
        sha = "a" * 40
        finding = 4325066310
        for source in (
            f"<!-- opencode-coderabbit-verification review=5585156309 finding={finding} head={sha} -->",
            f"<!-- auto-merge-coderabbit-verification finding={finding} head={sha} -->",
        ):
            with self.subTest(source=source):
                comments = [
                    {"id": 10, "in_reply_to_id": finding,
                     "user": {"login": "someone-else"}, "body": source},
                    {"id": 11, "in_reply_to_id": finding,
                     "user": {"login": "kodmial"},
                     "body": f"<!-- auto-merge-coderabbit-verification finding={finding}0 head={sha} -->"},
                    {"id": 12, "in_reply_to_id": finding,
                     "user": {"login": "kodmial"}, "body": source},
                    {"id": 14, "in_reply_to_id": finding,
                     "user": {"login": "kodmial"}, "body": source},
                ]
                result = subprocess.run(
                    ["jq", "-c", "--argjson", "finding", str(finding),
                     "--argjson", "unresolved", "13",
                     "--arg", "owner", "kodmial", verification_filter()],
                    input=json.dumps(comments), text=True,
                    capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["id"], 12)

    def test_legacy_handler_not_installed_or_invoked_by_canonical_path(self):
        installer = INSTALLER.read_text(encoding="utf-8")
        core = installer.split("CORE_STUBS=(", 1)[1].split(")", 1)[0]
        self.assertIn("continuum-opencode-unresolved.yml", core)
        self.assertNotIn("\n  continuum-coderabbit-unresolved.yml", core)
        workflow = CANONICAL.read_text(encoding="utf-8")
        self.assertEqual(workflow.count("      - name: Dispatch OpenCode retry"), 1)
        self.assertIn("continuum-opencode.yml/dispatches", workflow)
        self.assertNotIn("opencode run --auto", workflow)
        self.assertNotIn("1.18.34", workflow)

    def test_repair_stays_bound_to_original_finding_and_live_pr(self):
        shell = dispatch_shell()
        for token in (
            "pulls/comments/${FINDING_ID}",
            "ROOT_AUTHOR",
            "coderabbitai[bot]",
            "ROOT_PR",
            "ROOT_PARENT",
            ".pull_request_review_id",
            '[[ "$LATEST_RABBIT_REPLY_ID" != "$UNRESOLVED_COMMENT_ID" ]]',
            '[[ "$LIVE_REPO" != "$GITHUB_REPOSITORY" ]]',
            '[[ "$LIVE_STATE" != "open" ]]',
            '[[ "$COMPARE_STATUS" != "ahead" ]]',
        ):
            self.assertIn(token, shell)


if __name__ == "__main__":
    unittest.main()
