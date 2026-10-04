"""Deterministic tests for the isolated upstream PR-Agent lifecycle (issue #196).

Behavioral parity only where upstream PR-Agent v0.46.0 documents a
primitive. No CodeRabbit emulation, no CodeRabbit modification. Live
qualification remains in #184.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")

# Intentional P0 baseline advance: 87d139b completes kodmial/continuum#214
# mandatory qualification execution mode in continuum-opencode.yml
# (immutable capability/qualification/SHA run identity, exact-SHA fetch,
# product-change forbid, evidence-gated success without pause, plus the
# trust hardening required by scripts/test-continuum.rb: trusted dispatch
# identity via isTrustedDispatchComment, automation reads via
# .user.login == "github-actions[bot]", and untracked-dropping verdict via
# --untracked-files=no). The authoritative task contract and
# scripts/test-continuum.rb require those strings in
# continuum-opencode.yml, so the pre-#214 zero-diff assertion is stale.
# Keeping the immutable commit baseline means any later protected-file
# drift still fails.
BASELINE_SHA = "87d139b49786ca1c9b6a5a413022ccf0e90b741a"

PROTECTED_FILES = [
    ".github/workflows/continuum-opencode.yml",
    ".github/workflows/opencode.yml",
]

PR_AGENT_WORKFLOWS = [
    ".github/workflows/continuum-pr-agent.yml",
    ".github/workflows/continuum-pr-agent-repair.yml",
    ".github/workflows/continuum-pr-agent-auto-merge.yml",
]

LIFECYCLE_MODULE = os.path.join(SRC, "continuum", "pr_agent_lifecycle.py")
POLICY_MODULE = os.path.join(ROOT, ".github", "scripts", "pr_agent_policy.js")


def read_repo(path: str) -> str:
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
        return handle.read()


def lifecycle_source() -> str:
    with open(LIFECYCLE_MODULE, "r", encoding="utf-8") as handle:
        return handle.read()

def run_policy(op: str, payload: dict | list | None = None):
    env = os.environ.copy()
    env["POLICY_MODULE"] = POLICY_MODULE
    env["POLICY_OP"] = op
    env["POLICY_PAYLOAD"] = json.dumps(payload)
    code = r"""
const policy = require(process.env.POLICY_MODULE);
const payload = JSON.parse(process.env.POLICY_PAYLOAD || 'null');
let result;
switch (process.env.POLICY_OP) {
  case 'threshold':
    result = policy.IMPROVE_REPAIR_THRESHOLD;
    break;
  case 'qualifying':
    result = policy.qualifyingImproveSuggestions(payload || []);
    break;
  case 'build':
    result = policy.buildRepairBatch(payload.review, payload.improve_jsonl || '');
    break;
  case 'same':
    result = policy.sameLogicalDefect(payload.review, payload.improve);
    break;
  default:
    throw new Error('unknown policy op');
}
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)



def make_review(
    issues=None,
    recommendation: str = "safe_to_merge",
    extra=None,
) -> dict:
    review = {
        "key_issues_to_review": list(issues or []),
        "merge_recommendation": recommendation,
    }
    if extra:
        review.update(extra)
    return review


def make_persistent(findings=None, head_sha="head") -> dict:
    return {
        "schema_version": 1,
        "findings": list(findings or []),
        "last_run": {
            "complete": True,
            "excluded_files": [],
            "head_sha": head_sha,
            "kind": "full",
            "run_id": "test",
        },
    }


def issue_entry(relevant_file="src/app.py", header="Possible Bug", n: int = 0) -> dict:
    return {
        "relevant_file": relevant_file,
        "issue_header": f"{header} {n}",
        "issue_content": f"concrete failure scenario {n}",
        "start_line": 10 + n,
        "end_line": 12 + n,
    }


class ProtectedBaselineTests(unittest.TestCase):
    def test_every_protected_coderabbit_file_has_zero_diff_from_baseline(self):
        # The validation workflow checks out with fetch-depth 1, so the
        # baseline object may be absent ("fatal: bad object"). Fetch it on
        # demand; the baseline commit is an ancestor on origin so a shallow
        # fetch of that single object is sufficient for the diff below.
        present = subprocess.run(
            ["git", "cat-file", "-e", BASELINE_SHA],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if present.returncode != 0:
            fetched = subprocess.run(
                ["git", "fetch", "--depth", "1", "origin", BASELINE_SHA],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(fetched.returncode, 0, fetched.stderr)
        for path in PROTECTED_FILES:
            with self.subTest(path=path):
                out = subprocess.run(
                    ["git", "diff", BASELINE_SHA, "HEAD", "--", path],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(
                    out.stdout.strip(),
                    "",
                    f"{path} differs from baseline {BASELINE_SHA}",
                )

    def test_no_coderabbit_protected_file_is_modified_in_worktree(self):
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        changed = out.stdout
        for path in PROTECTED_FILES:
            self.assertNotIn(path, changed, f"{path} is modified")


class UpstreamVersionTests(unittest.TestCase):
    def test_lifecycle_pins_v0460(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertEqual(life.PR_AGENT_VERSION, "0.46.0")
        self.assertEqual(life.UPSTREAM_FINDING_STATE_VERSION, "0.46.0")
        self.assertIn("0.46.0", life.UPSTREAM_ACTION_REF)

    def test_review_workflow_installs_pinned_upstream_release(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("0.46.0", body)
        self.assertIn("pr-agent==", body)
        self.assertIn('pr-agent --pr_url "$PR_URL" review', body)
        self.assertIn('pr-agent --pr_url "$PR_URL" improve', body)

    def test_upstream_action_ref_is_documented(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        # Pinned per installation/github.md for stability (v0.46.0).
        self.assertTrue(life.UPSTREAM_ACTION_REF.endswith("0.46.0-github_action"))


class ReviewOutputTests(unittest.TestCase):
    def test_machine_state_comes_from_step_outputs_review(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("steps.pragent.outputs.review", body)
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertEqual(life.REVIEW_OUTPUT_REF, "steps.pragent.outputs.review")

    def test_invalid_or_missing_review_json_blocks(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        for bad in ("", "   ", "not-json", "[]", "{}", '{"review": {}}',
                    '{"other": 1}', '{"review": {"key_issues_to_review": null}}'):
            with self.subTest(payload=bad):
                with self.assertRaises(life.LifecycleError):
                    life.parse_review_json(bad)
        good = life.parse_review_json({"review": make_review()})
        self.assertEqual(good["key_issues_to_review"], [])

    def test_key_issues_are_canonical_repair_payload(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        review = make_review([issue_entry(n=0), issue_entry(n=1)])
        self.assertEqual(len(life.current_key_issues(review)), 2)


class ImproveChannelTests(unittest.TestCase):
    def test_improve_uses_runner_local_push_outputs_file(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("pr-agent-outputs/continuum.jsonl", body)
        self.assertIn("push_outputs", body.lower())
        self.assertIn("payload.code_suggestions", body)
        toml = read_repo(".pr_agent.toml")
        self.assertIn('file_path = "pr-agent-outputs/continuum.jsonl"', toml)

    def test_improve_payload_parsed_from_file_never_comments(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        text = (
            '{"payload": {"code_suggestions": [{"file": "a.py", "score": 3}]}}\n'
            '{"code_suggestions": [{"file": "b.py"}]}\n'
        )
        parsed = life.parse_improve_push_outputs(text)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(life.parse_improve_push_outputs(""), [])
        with self.assertRaises(life.LifecycleError):
            life.parse_improve_push_outputs('{"payload":')
        # Repair workflow consumes the file payload; it never scrapes prose.
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("pr-agent-outputs/continuum.jsonl", repair)
        self.assertIn("payload.code_suggestions", repair)

    def test_suggestions_threshold_is_high_signal_and_unscored_is_non_actionable(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertEqual(life.SUGGESTIONS_SCORE_THRESHOLD, 7)
        self.assertEqual(run_policy("threshold"), 7)
        toml = read_repo(".pr_agent.toml")
        self.assertIn("suggestions_score_threshold = 7", toml)
        suggestions = [
            {"file": "low.py", "score": 6},
            {"file": "high.py", "score": 7},
            {"file": "very-high.py", "score": 9},
            {"file": "unscored.py"},
            {"file": "invalid.py", "score": "unknown"},
        ]
        self.assertEqual(
            [item["file"] for item in life.qualifying_suggestions(suggestions)],
            ["high.py", "very-high.py"],
        )
        self.assertEqual(
            [item["file"] for item in run_policy("qualifying", suggestions)],
            ["high.py", "very-high.py"],
        )


class RepairBatchTests(unittest.TestCase):
    def test_all_findings_and_suggestions_in_one_bounded_batch(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        review = make_review([issue_entry(n=0), issue_entry(n=1), issue_entry(n=2)])
        suggestions = [{"file": "a.py", "score": 7}, {"file": "b.py", "score": 9}]
        batch = life.build_repair_batch(review, suggestions, head_sha="abc123")
        self.assertTrue(batch.bounded)
        self.assertEqual(len(batch.items), 5)
        sources = [item["source"] for item in batch.items]
        self.assertEqual(sources.count("review"), 3)
        self.assertEqual(sources.count("improve"), 2)

    def test_review_improve_duplicate_becomes_one_repair_item(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        finding = {
            "relevant_file": "src/app.py",
            "issue_header": "Force push race can overwrite concurrent branch updates",
            "issue_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "start_line": 20,
            "end_line": 25,
        }
        suggestion = {
            "relevant_file": "src/app.py",
            "one_sentence_summary": "Force push race can overwrite concurrent branch updates",
            "suggestion_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "relevant_lines_start": 22,
            "relevant_lines_end": 24,
            "score": 9,
        }
        py_batch = life.build_repair_batch(
            make_review([finding]), [suggestion], head_sha="head"
        )
        self.assertEqual(len(py_batch.items), 1)
        js_batch = run_policy(
            "build",
            {
                "review": make_review([finding]),
                "improve_jsonl": json.dumps(
                    {"payload": {"code_suggestions": [suggestion]}}
                ),
            },
        )
        self.assertEqual(len(js_batch["items"]), 1)
        self.assertEqual(js_batch["deduplicatedSuggestions"], 1)

    def test_distinct_same_file_findings_are_not_deduplicated(self):
        finding = {
            "relevant_file": "src/app.py",
            "issue_header": "Force push race can overwrite concurrent branch updates",
            "issue_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "start_line": 20,
            "end_line": 25,
        }
        suggestion = {
            "relevant_file": "src/app.py",
            "one_sentence_summary": "Force push race can overwrite concurrent branch updates",
            "suggestion_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "relevant_lines_start": 80,
            "relevant_lines_end": 84,
            "score": 9,
        }
        js_batch = run_policy(
            "build",
            {
                "review": make_review([finding]),
                "improve_jsonl": json.dumps(
                    {"payload": {"code_suggestions": [suggestion]}}
                ),
            },
        )
        self.assertEqual(len(js_batch["items"]), 2)
        self.assertEqual(js_batch["deduplicatedSuggestions"], 0)

    def test_no_progress_fingerprint_uses_deduplicated_logical_set(self):
        finding = {
            "relevant_file": "src/app.py",
            "issue_header": "Force push race can overwrite concurrent branch updates",
            "issue_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "start_line": 20,
            "end_line": 25,
        }
        duplicate = {
            "relevant_file": "src/app.py",
            "one_sentence_summary": "Force push race can overwrite concurrent branch updates",
            "suggestion_content": "Use force-with-lease so a concurrent branch update cannot be silently overwritten.",
            "relevant_lines_start": 21,
            "relevant_lines_end": 23,
            "score": 10,
        }
        base = run_policy(
            "build",
            {"review": make_review([finding]), "improve_jsonl": ""},
        )
        with_duplicate = run_policy(
            "build",
            {
                "review": make_review([finding]),
                "improve_jsonl": json.dumps(
                    {"payload": {"code_suggestions": [duplicate]}}
                ),
            },
        )
        self.assertEqual(base["fingerprint"], with_duplicate["fingerprint"])
        self.assertEqual(base["items"], with_duplicate["items"])

    def test_multi_finding_review_includes_every_item_not_only_first(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        findings = [issue_entry(n=i) for i in range(4)]
        review = make_review(findings)
        batch = life.build_repair_batch(review, [], head_sha="sha1")
        review_items = [i for i in batch.items if i["source"] == "review"]
        self.assertEqual(len(review_items), 4)
        headers = {i["finding"]["issue_header"] for i in review_items}
        self.assertEqual(len(headers), 4)

    def test_one_review_dispatches_at_most_one_repair(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertTrue(life.should_dispatch_repair("HEAD1", []))
        self.assertFalse(life.should_dispatch_repair("HEAD1", ["head1"]))
        self.assertTrue(life.should_dispatch_repair("HEAD2", ["head1"]))

    def test_no_same_head_repair_storm(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        dispatched = ["aaa"]
        self.assertFalse(life.should_dispatch_repair("AAA", dispatched))
        # A changed HEAD is a new repair key.
        self.assertTrue(life.should_dispatch_repair("bbb", dispatched))

    def test_still_reported_enters_next_batch_resolved_does_not(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        # Previous review had findings 0 and 1. After repair, the fresh full
        # review still reports 0 (still ACTIVE) but no longer reports 1
        # (RESOLVED upstream). The next batch contains only the current one.
        current = make_review([issue_entry(n=0)])
        batch = life.build_repair_batch(current, [], head_sha="newhead")
        headers = [i["finding"]["issue_header"] for i in batch.items]
        self.assertTrue(any("0" in h for h in headers))
        self.assertFalse(any("1" in h for h in headers))

    def test_fresh_review_new_finding_enters_same_loop(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        current = make_review([issue_entry(n=9)])
        batch = life.build_repair_batch(current, [], head_sha="h2")
        self.assertEqual(len(batch.items), 1)


class NoCustomProtocolTests(unittest.TestCase):
    def test_no_custom_verify_path_in_lifecycle_or_workflows(self):
        source = lifecycle_source()
        self.assertNotIn("/verify", source)
        for path in PR_AGENT_WORKFLOWS:
            with self.subTest(path=path):
                self.assertNotIn("/verify", read_repo(path))

    def test_lifecycle_implements_no_finding_identity_or_state_parser(self):
        source = lifecycle_source()
        for needle in ("hashlib", "sha256", "pra-", "fingerprint",
                       "inline_comment_finding_id", "finding_id",
                       "track_findings", "parse_findings", "decide_review"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)

    def test_lifecycle_uses_pinned_upstream_state_implementation(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertEqual(life.UPSTREAM_FINDING_STATE_VERSION, "0.46.0")
        # v0.46.0 persistent findings use the native `state` field.
        self.assertTrue(
            life.upstream_state_has_active(make_persistent([{"state": "ACTIVE"}]))
        )
        self.assertFalse(
            life.upstream_state_has_active(make_persistent([{"state": "RESOLVED"}]))
        )
        with self.assertRaises(life.LifecycleError):
            life.upstream_state_has_active({"findings": [{"status": "ACTIVE"}]})

    def test_large_pr_behavior_is_upstream_chunking_not_continuum(self):
        source = lifecycle_source()
        self.assertNotIn("chunk_diff", source)
        self.assertNotIn("Chunk", source.replace("chunking", ""))
        toml = read_repo(".pr_agent.toml")
        self.assertIn("enable_large_pr_chunking = true", toml)
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("PR_REVIEWER__ENABLE_LARGE_PR_CHUNKING", body)

    def test_no_custom_approval_or_severity_semantics(self):
        source = lifecycle_source()
        for needle in ("CHANGES_REQUESTED", "APPROVED", "BLOCKING_SEVERITIES",
                       "decide_review"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)


class ConfigurationTests(unittest.TestCase):
    def test_required_toml_values(self):
        toml = read_repo(".pr_agent.toml")
        required = [
            "persistent_comment = true",
            "persistent_finding_state = true",
            "inline_key_issues = true",
            "enable_large_pr_chunking = true",
            "max_number_of_calls = 3",
            "require_tests_review = true",
            "require_security_review = true",
            "require_risk_assessment = true",
            "require_merge_recommendation = true",
            "require_ticket_analysis_review = true",
            "enable_review_coverage_footer = true",
            "num_max_findings = 6",
            "persistent_inline_comments = true",
            "propagate_tool_errors = true",
            "focus_only_on_problems = true",
            "final_update_message = false",
            "publish_output_no_suggestions = false",
            "suggestions_score_threshold = 7",
            "max_suggestions_per_file = 0",
            "enable_suggestions_coverage_footer = true",
            "enable = true",
            'channels = ["file"]',
            'file_path = "pr-agent-outputs/continuum.jsonl"',
            "publish_as_check_run = false",
            "enable_output = true",
            "fail_on_tool_errors = true",
        ]
        for needle in required:
            with self.subTest(needle=needle):
                self.assertIn(needle, toml)

    def test_workflow_enforces_runner_behavior(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        for needle in [
            "PR_REVIEWER__PERSISTENT_FINDING_STATE",
            "PR_REVIEWER__INLINE_KEY_ISSUES",
            "PR_REVIEWER__REQUIRE_MERGE_RECOMMENDATION",
            "PR_REVIEWER__REQUIRE_TICKET_ANALYSIS_REVIEW",
            "CONFIG__PERSISTENT_INLINE_COMMENTS",
            "CONFIG__PROPAGATE_TOOL_ERRORS",
            "GITHUB_ACTION_CONFIG__ENABLE_OUTPUT",
            "GITHUB_ACTION_CONFIG__FAIL_ON_TOOL_ERRORS",
            "GITHUB__PUBLISH_AS_CHECK_RUN",
            "PR_REVIEWER__FINAL_UPDATE_MESSAGE",
            "PR_CODE_SUGGESTIONS__PUBLISH_OUTPUT_NO_SUGGESTIONS",
            '--pr_code_suggestions.suggestions_score_threshold="$IMPROVE_REPAIR_THRESHOLD"',
            "pr_agent_policy.js",
            "PUSH_OUTPUTS__FILE_PATH",
            "steps.pragent.outputs.review",
            "pr-agent-outputs/continuum.jsonl",
        ]:
            with self.subTest(needle=needle):
                self.assertIn(needle, body)

    def test_check_run_disabled_while_finding_state_enabled(self):
        toml = read_repo(".pr_agent.toml")
        self.assertIn("publish_as_check_run = false", toml)
        self.assertIn("persistent_finding_state = true", toml)
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("GITHUB__PUBLISH_AS_CHECK_RUN: 'false'", body)
        self.assertIn("PR_REVIEWER__PERSISTENT_FINDING_STATE: 'true'", body)

    def test_ticket_review_enabled_and_issue_context_required(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        review = make_review([], extra={"ticket_compliance_check": [{"ticket_url": "x"}]})
        self.assertTrue(life.review_has_ticket_compliance(review))
        self.assertFalse(life.review_has_ticket_compliance(make_review([])))
        self.assertTrue(life.ticket_compliance_ok("no issue link", make_review([])))
        self.assertTrue(life.ticket_compliance_ok("Fixes #123", review))
        self.assertFalse(life.ticket_compliance_ok("Fixes #123", make_review([])))

    def test_inline_dedup_enabled(self):
        toml = read_repo(".pr_agent.toml")
        self.assertIn("persistent_inline_comments = true", toml)
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("CONFIG__PERSISTENT_INLINE_COMMENTS: 'true'", body)

    def test_truncation_at_cap_never_clean(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        full = make_review([issue_entry(n=i) for i in range(6)])
        self.assertTrue(life.is_potentially_truncated(full))
        partial = make_review([issue_entry(n=i) for i in range(5)])
        self.assertFalse(life.is_potentially_truncated(partial))
        gate = life.evaluate_gate(life.GateInputs(
            review=full, qualifying_improve=[], persistent_state=make_persistent(),
            ci_green_on_exact_head=True, head_matches=True,
            review_coverage_complete=True, improve_coverage_complete=True,
        ))
        self.assertFalse(gate["green"])


class GateTests(unittest.TestCase):
    def _gate(self, **overrides):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        base = dict(
            review=make_review([]),
            qualifying_improve=[],
            persistent_state=make_persistent(),
            ci_green_on_exact_head=True,
            head_matches=True,
            review_coverage_complete=True,
            improve_coverage_complete=True,
            tool_error=False,
        )
        base.update(overrides)
        return life.evaluate_gate(life.GateInputs(**base))

    def test_only_safe_to_merge_with_empty_findings_greens(self):
        gate = self._gate()
        self.assertTrue(gate["green"])

    def test_caution_and_changes_required_block(self):
        for value in ("merge_with_caution", "changes_required"):
            with self.subTest(value=value):
                gate = self._gate(review=make_review([], recommendation=value))
                self.assertFalse(gate["green"])

    def test_active_persistent_state_blocks(self):
        gate = self._gate(persistent_state=make_persistent([{"state": "ACTIVE"}]))
        self.assertFalse(gate["green"])
        gate = self._gate(persistent_state=make_persistent([{"state": "RESOLVED"}]))
        self.assertTrue(gate["green"])

    def test_stale_head_blocks(self):
        gate = self._gate(head_matches=False)
        self.assertFalse(gate["green"])

    def test_ci_must_be_green_before_review_and_merge(self):
        gate = self._gate(ci_green_on_exact_head=False)
        self.assertFalse(gate["green"])
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("listWorkflowRunsForRepo", body)
        self.assertIn("CI_WORKFLOW_NAME", body)
        self.assertNotIn("getCombinedStatusForRef", body)
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("Current-head CI", merge)

    def test_incomplete_coverage_never_green(self):
        self.assertFalse(self._gate(review_coverage_complete=False)["green"])
        self.assertFalse(self._gate(improve_coverage_complete=False)["green"])
        self.assertFalse(self._gate(tool_error=True)["green"])

    def test_qualifying_suggestions_block(self):
        gate = self._gate(qualifying_improve=[{"file": "a.py", "score": 2}])
        self.assertFalse(gate["green"])

    def test_inline_absence_cannot_produce_green_with_findings(self):
        # inline_key_issues is presentation only: a review with findings
        # blocks even when no inline comment count is consulted.
        gate = self._gate(review=make_review([issue_entry(n=0)]))
        self.assertFalse(gate["green"])

    def test_head_capture_before_and_revalidation_after(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("Revalidate the admitted exact HEAD immediately before review", body)
        self.assertIn("steps.admit.outputs.head_sha", body)
        self.assertIn("Revalidate the PR head and native review output after review", body)
        self.assertIn("moved during review", body)
        self.assertIn("discarding", body)

    def test_changed_head_requires_complete_review_and_improve_after_ci(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertTrue(life.needs_fresh_review("aaa", "bbb"))
        self.assertFalse(life.needs_fresh_review("aaa", "AAA"))
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("complete upstream", body)
        self.assertIn("GITHUB_ACTION_CONFIG__HANDLE_PUSH_TRIGGER: 'false'", body)

    def test_authoritative_rereview_does_not_depend_on_push_handling(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("GITHUB_ACTION_CONFIG__HANDLE_PUSH_TRIGGER: 'false'", body)
        self.assertNotIn("handle_push_trigger: 'true'", body.lower())

    def test_bot_commit_filter_cannot_skip_opencode_repairs(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        # Push handling is disabled, so the default bot-commit ignore cannot
        # skip the CI-gated re-review of OpenCode-authored repair commits.
        self.assertIn("GITHUB_ACTION_CONFIG__HANDLE_PUSH_TRIGGER: 'false'", body)
        self.assertIn("PUSH_TRIGGER_IGNORE_BOT_COMMITS", body)

    def test_main_sync_does_not_carry_approval(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertTrue(life.needs_fresh_review("old", "new"))
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("refusing to merge a stale result", merge)


class AdmissionTests(unittest.TestCase):
    def test_admission_requires_open_nondraft_samerepo_green_ci(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        good = life.admission_allowed(
            pr_state="open", is_draft=False, head_repo="o/r", base_repo="o/r",
            ci_green_on_exact_head=True,
        )
        self.assertTrue(good["allowed"])
        for kwargs in (
            dict(pr_state="closed", is_draft=False, head_repo="o/r", base_repo="o/r",
                 ci_green_on_exact_head=True),
            dict(pr_state="open", is_draft=True, head_repo="o/r", base_repo="o/r",
                 ci_green_on_exact_head=True),
            dict(pr_state="open", is_draft=False, head_repo="fork/r", base_repo="o/r",
                 ci_green_on_exact_head=True),
            dict(pr_state="open", is_draft=False, head_repo="o/r", base_repo="o/r",
                 ci_green_on_exact_head=False),
        ):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(life.admission_allowed(**kwargs)["allowed"])


class IsolationTests(unittest.TestCase):
    def test_consumer_mode_contract_and_fail_closed(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertEqual(life.resolve_consumer_mode({})["stack"], "none")
        self.assertEqual(
            life.resolve_consumer_mode({"CONTINUUM_REVIEW_PROVIDER": "coderabbit"})["stack"],
            "coderabbit",
        )
        mode = life.resolve_consumer_mode({"CONTINUUM_REVIEW_PROVIDER": "pr-agent"})
        self.assertEqual(mode["stack"], "pr-agent")
        self.assertTrue(mode["pr_agent_enabled"])
        self.assertFalse(mode["require_coderabbit"])
        with self.assertRaises(life.LifecycleError):
            life.resolve_consumer_mode({"CONTINUUM_REVIEW_PROVIDER": "both"})

    def test_pr_agent_workflows_use_unified_review_provider_selector(self):
        for path in (
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
            ".github/workflows/continuum-pr-agent-auto-merge.yml",
        ):
            with self.subTest(path=path):
                body = read_repo(path)
                self.assertIn("CONTINUUM_REVIEW_PROVIDER", body)
                self.assertNotIn("CONTINUUM_PR_AGENT_ENABLED", body)
                self.assertNotIn("CONTINUUM_REQUIRE_CODERABBIT", body)

    def test_pr_agent_permission_ceiling_allows_nested_repair_and_merge(self):
        engine = read_repo(".github/workflows/continuum-pr-agent.yml")
        caller = read_repo(".github/caller-stubs/continuum-pr-agent.yml")
        self_caller = read_repo(".github/workflows/pr-agent.yml")
        for body in (engine, caller, self_caller):
            self.assertIn("contents: write", body)
            self.assertIn("pull-requests: write", body)
        self.assertIn("statuses: read", engine)

    def test_improve_uses_env_for_push_outputs_not_forbidden_cli_args(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("PUSH_OUTPUTS__ENABLE: 'true'", body)
        self.assertIn("PUSH_OUTPUTS__CHANNELS:", body)
        self.assertIn("PUSH_OUTPUTS__FILE_PATH:", body)
        self.assertNotIn("--push_outputs.enable", body)
        self.assertNotIn("--push_outputs.channels", body)
        self.assertNotIn("--push_outputs.file_path", body)

    def test_pr_agent_bridge_runtime_bundle_includes_python_dependency(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("Resolve the Continuum-owned PR-Agent runtime bundle", body)
        self.assertIn("contents/src/continuum/pr_agent.py", body)
        self.assertIn("continuum-pr-agent-runtime", body)

    def test_pre_ci_skip_does_not_run_checkout_integrity_guard(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn(
            "if: always() && steps.stack.outputs.enabled == 'true' && steps.admit.outputs.admitted == 'true'",
            body,
        )

    def test_automatic_pr_agent_review_has_one_authoritative_wakeup(self):
        caller = read_repo(".github/caller-stubs/continuum-pr-agent.yml")
        recovery = read_repo(
            ".github/caller-stubs/continuum-pr-agent-recovery.yml"
        )
        router_stub = read_repo(
            ".github/caller-stubs/continuum-pr-agent-router.yml"
        )
        router = read_repo(
            ".github/workflows/continuum-pr-agent-router.yml"
        )
        # The heavy caller is dispatch-only: ordinary comments must not
        # create heavy workflow runs.
        self.assertNotIn("workflow_run:", caller)
        self.assertNotIn("pull_request_target:", caller)
        self.assertNotIn("issue_comment", caller)
        self.assertNotIn("contains(github.event.comment.body, '/review')", caller)
        # Explicit `/review` is owned by the thin router, which validates
        # the event and dispatches the heavy workflow once per exact HEAD.
        self.assertIn("issue_comment", router_stub)
        self.assertIn("contains(github.event.comment.body, '/review')", router_stub)
        self.assertIn("expected_head_sha", router)
        self.assertIn("createWorkflowDispatch", router)
        self.assertIn("review:", router)
        self.assertIn("already active", router)
        self.assertIn("workflow_run:", recovery)
        self.assertIn("- CI", recovery)
        self.assertIn("pull_request_target:", recovery)
        self.assertIn("ready_for_review", recovery)
        self.assertIn("synchronize", recovery)

    def test_recovery_caller_wakes_on_successful_ci_and_review_exports_native_outputs(self):
        caller = read_repo(
            ".github/caller-stubs/continuum-pr-agent-recovery.yml"
        )
        self.assertIn("workflow_run:", caller)
        self.assertIn("- CI", caller)
        self.assertIn("- PR Agent (OpenCode backend)", caller)
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("review_json:", body)
        self.assertIn("improve_jsonl:", body)
        self.assertIn("steps.pragent.outputs.review", body)
        self.assertNotIn("CONTINUUM_PR_AGENT_ENABLED", body)
        self.assertNotIn("CONTINUUM_REQUIRE_CODERABBIT", body)
        self.assertIn("Install pinned OpenCode CLI for the PR-Agent backend", body)
        self.assertIn("Resolve the Continuum-owned PR-Agent runtime bundle", body)

    def test_review_routes_only_to_pr_agent_repair_or_merge(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("uses: ./.github/workflows/continuum-pr-agent-repair.yml", review)
        self.assertIn("uses: ./.github/workflows/continuum-pr-agent-auto-merge.yml", review)
        self.assertIn("needs.pr_agent.outputs.needs_repair == 'true'", review)
        self.assertIn("needs.pr_agent.outputs.needs_repair == 'false'", review)
        self.assertIn("Export native persistent finding state for the reviewed HEAD", review)
        self.assertIn("parse_review_state", review)

    def test_repair_revalidates_branch_immediately_before_publish(self):
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("PR branch moved before publish", repair)
        self.assertIn('git ls-remote origin "refs/heads/$HEAD_REF"', repair)
        self.assertIn('gh pr view "$PR_NUMBER"', repair)
        self.assertLess(repair.index("PR branch moved before publish"), repair.index("git push"))
        self.assertLess(repair.index('git diff --cached --quiet'), repair.index("PR branch moved before publish"))

    def test_repair_targets_real_branch_and_never_truncates_json(self):
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertNotIn("refs/pull/$PR_NUMBER/head", repair)
        self.assertNotIn(".slice(0, 60000)", repair)
        self.assertIn('refs/heads/$HEAD_REF', repair)
        self.assertIn("Resolve the writable PR source branch", repair)
        self.assertIn("Install pinned OpenCode CLI for PR-Agent repair", repair)
        self.assertIn("git add -A", repair)

    def test_merge_gate_uses_native_v046_persistent_state_schema(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("persistentState.findings", merge)
        self.assertIn("entry.state", merge)
        self.assertIn("last_run", merge)
        self.assertIn("lastRun.complete", merge)
        self.assertIn("lastRun.head_sha", merge)
        self.assertNotIn("entry.status", merge)

    def test_generic_auto_merge_is_sync_only_in_pr_agent_mode(self):
        generic = read_repo(".github/workflows/continuum-auto-merge.yml")
        self.assertIn("const prAgentSyncOnly = reviewProvider === 'pr-agent';", generic)
        self.assertIn("Main synchronization remains active; merge stays PR-Agent-owned.", generic)
        self.assertIn("await updateFromMain(pr);", generic)
        sync_guard = generic.index("if (prAgentSyncOnly) {", generic.index("await updateFromMain(pr);"))
        generic_ci = generic.index("const ci = await latestCurrentHeadCi(pr);")
        self.assertLess(sync_guard, generic_ci)

    def test_pr_agent_stack_invokes_only_pr_agent_workflows(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        workflows = life.workflows_for_stack("pr-agent")
        self.assertEqual(
            sorted(workflows),
            sorted([
                "continuum-pr-agent.yml",
                "continuum-pr-agent-repair.yml",
                "continuum-pr-agent-auto-merge.yml",
            ]),
        )
        for forbidden in (
            "continuum-coderabbit-retry.yml",
            "continuum-coderabbit-unresolved.yml",
            "continuum-add-review-label.yml",
            "continuum-auto-merge.yml",
        ):
            self.assertNotIn(forbidden, workflows)

    def test_no_coderabbit_workflow_dispatched_in_pr_agent_mode(self):
        for path in PR_AGENT_WORKFLOWS:
            with self.subTest(path=path):
                body = read_repo(path)
                self.assertNotIn("continuum-coderabbit-retry.yml", body)
                self.assertNotIn("continuum-coderabbit-unresolved.yml", body)
                self.assertNotIn("uses: ./.github/workflows/continuum-auto-merge.yml", body)
                self.assertNotIn("uses: ./.github/workflows/continuum-add-review-label.yml", body)
                self.assertNotIn("uses: kodmial/continuum/.github/workflows/continuum-auto-merge.yml", body)

    def test_no_coderabbit_fix_routing(self):
        for path in PR_AGENT_WORKFLOWS:
            with self.subTest(path=path):
                self.assertNotIn("coderabbit-fix", read_repo(path))
        self.assertNotIn("coderabbit-fix", lifecycle_source())

    def test_green_ci_without_pragent_gate_cannot_merge_elsewhere(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        # The PR-Agent-owned merge path is the only active merger in this
        # mode; it requires the review portion of the gate first.
        self.assertIn("review portion satisfied", merge)
        self.assertIn("safe_to_merge", merge)


class StabilizationParityTests(unittest.TestCase):
    def test_pr_agent_merge_reuses_mature_common_safety_contract(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        for needle in (
            "AUTO_MERGE_BLOCK_LABEL = 'no-auto-merge'",
            "CONFLICT_LOCK_LABEL = 'opencode-conflict-repair'",
            "Packaging smoke",
            "REQUIRED_WORKFLOW_GATE_LABEL",
            "requiredWorkflowGateName",
            "expected_head_sha: oldHead",
            "mode: 'resolve-conflict'",
            "sha: reviewedHead",
            "commit_title: conventionalTitle",
            "POST_MERGE_WAKEUPS",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, merge)

    def test_pr_agent_main_sync_never_carries_old_review(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("fresh CI and a fresh full PR-Agent review", merge)
        self.assertNotIn("carrying PR-Agent approval", merge)
        self.assertIn("main advanced in merge-critical files", merge)
        self.assertIn("non-merge-critical-main-delta", merge)

    def test_pr_agent_merge_is_atomic_against_reviewed_head(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("pr.head.sha.toLowerCase() !== reviewedHead", merge)
        self.assertIn("sha: reviewedHead", merge)
        self.assertNotIn('gh pr merge "$PR_NUMBER"', merge)

    def test_pr_agent_retry_is_bounded_exact_head_and_isolated(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        caller = read_repo(".github/caller-stubs/continuum-pr-agent.yml")
        for body in (review, repair):
            self.assertIn("retry_attempt", body)
            self.assertIn("15 * (1 << attempt)", body)
            self.assertIn("attempt >= 2", body)
            self.assertIn("expected_head_sha", review)
            self.assertNotIn("continuum-coderabbit-retry.yml", body)
            self.assertNotIn("continuum-coderabbit-unresolved.yml", body)
        self.assertIn("workflow_dispatch:", caller)

    def test_review_presentation_is_persistent_but_notification_noise_is_disabled(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("PR_REVIEWER__PERSISTENT_COMMENT: 'true'", review)
        self.assertIn("PR_REVIEWER__FINAL_UPDATE_MESSAGE: 'false'", review)
        self.assertIn("--pr_reviewer.final_update_message=false", review)
        self.assertIn("PR_CODE_SUGGESTIONS__PUBLISH_OUTPUT_NO_SUGGESTIONS: 'false'", review)
        self.assertIn("--pr_code_suggestions.publish_output_no_suggestions=false", review)
        self.assertIn("Normalize persistent improve presentation", review)
        self.assertIn("stale improve presentation removed", review)

    def test_improve_presentation_cleanup_runs_only_after_exact_head_revalidation(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertLess(
            review.index("Revalidate the PR head and native review output after review"),
            review.index("Normalize persistent improve presentation"),
        )
        self.assertIn("steps.result.outcome == 'success'", review)
        self.assertIn("steps.result.outputs.head_sha", review)

    def test_retry_and_no_progress_use_one_upsertable_controller_comment(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        policy = read_repo(".github/scripts/pr_agent_policy.js")
        self.assertIn("continuum-pr-agent-controller-state:v1", policy)
        for body in (review, repair):
            self.assertNotIn("gh pr comment", body)
            self.assertIn("github.rest.issues.updateComment", body)
            self.assertIn("github.rest.issues.createComment", body)
            self.assertIn("github.rest.issues.deleteComment", body)
            self.assertIn("CONTROLLER_STATE_MARKER", body)

    def test_runtime_review_repair_and_merge_share_central_policy(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        for body in (review, repair, merge):
            self.assertIn("pr_agent_policy.js", body)
        self.assertIn("IMPROVE_REPAIR_THRESHOLD", review)
        self.assertIn("policy.buildRepairBatch", repair)
        self.assertIn("policy.qualifyingImproveSuggestions", merge)

    def test_no_progress_uses_structured_fingerprint_and_head(self):
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        policy = read_repo(".github/scripts/pr_agent_policy.js")
        self.assertIn("createHash('sha256')", policy)
        self.assertIn("policy.buildRepairBatch", repair)
        self.assertIn("continuum-pr-agent-no-progress head=", repair)
        self.assertIn("fingerprint=", repair)
        self.assertIn("identical structured PR-Agent finding state", repair)
        self.assertIn("steps.convergence.outputs.held != 'true'", repair)

    def test_main_sync_classification_predicate_is_not_inverted(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        # The exact negation is the contract: only a fully non-critical
        # delta may skip the sync. An inverted `required: onlyNonCritical`
        # would sync the wrong set and merge the wrong HEAD.
        self.assertIn("required: !onlyNonCritical", merge)
        self.assertNotIn("required: onlyNonCritical", merge)
        self.assertIn(
            "files.length > 0 && criticalFiles.length === 0", merge
        )
        self.assertIn("unknown-main-delta", merge)
        self.assertIn("non-merge-critical-main-delta", merge)
        self.assertIn("merge-critical-main-delta", merge)
        # Both compare directions are required; a single direction cannot
        # distinguish behind-by from the file delta.
        self.assertIn("defaultBranch + '...' + pr.head.sha", merge)
        self.assertIn("pr.head.sha + '...' + defaultBranch", merge)

    def test_final_and_premerge_revalidation_refresh_pr_before_merge(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        # A missing final getPull would merge on a stale PR object; the
        # reconciliation must refresh before every gate window.
        self.assertGreaterEqual(merge.count("await getPull()"), 5)
        self.assertGreaterEqual(
            merge.count("pr = await getPull()"), 2
        )
        ordered = (
            "let pr = await getPull()",
            "mainSync",
            "pr = await getPull()",
            "finalSync",
            "finalGates",
            "pr = await getPull()",
            "preMergeSync",
            "preMergeGates",
            "pr.mergeable",
            "pulls.merge",
        )
        index = -1
        for needle in ordered:
            nxt = merge.index(needle, index + 1)
            self.assertGreater(
                nxt, index, f"{needle} must follow the prior gate in order"
            )
            index = nxt
        # Every revalidation window re-checks the exact reviewed HEAD.
        self.assertGreaterEqual(
            merge.count("pr.head.sha.toLowerCase() !== reviewedHead"), 3
        )
        self.assertIn("PR state changed during final PR-Agent revalidation", merge)
        self.assertIn("PR state changed immediately before merge", merge)

    def test_merge_conflict_error_routing_is_not_swapped(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        idx_409 = merge.index("err.status === 409")
        idx_422 = merge.index("err.status === 422")
        self.assertLess(
            idx_409, idx_422, "conflict (409) handling must precede 422 handling"
        )
        window_409 = merge[idx_409:idx_422]
        self.assertIn("isConflictMessage", window_409)
        self.assertIn("await dispatchConflictRepair(fresh, message)", window_409)
        window_422 = merge[idx_422 : idx_422 + 800]
        self.assertIn(
            "Atomic merge rejected with HTTP 422 without conflict evidence",
            window_422,
        )
        self.assertNotIn("dispatchConflictRepair", window_422)
        # Mergeability states: dirty dispatches repair, unknown waits.
        self.assertIn(
            "pr.mergeable === false || pr.mergeable_state === 'dirty'", merge
        )
        self.assertIn("pr.mergeable === null", merge)
        self.assertIn("still being computed", merge)

    def test_packaging_and_required_gates_block_merge(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("Packaging smoke", merge)
        self.assertIn(
            "packaging.status !== 'completed' || packaging.conclusion !== 'success'",
            merge,
        )
        self.assertIn("requiredWorkflowGateName", merge)
        self.assertIn(
            "required.status !== 'completed' ||",
            merge,
        )
        self.assertIn("required workflow ", merge)
        self.assertIn("combined status is ", merge)

    def test_finding_fingerprint_is_order_independent(self):
        first = issue_entry(n=0)
        second = issue_entry(n=1)
        forward = run_policy(
            "build",
            {"review": make_review([first, second]), "improve_jsonl": ""},
        )
        reverse = run_policy(
            "build",
            {"review": make_review([second, first]), "improve_jsonl": ""},
        )
        self.assertEqual(forward["fingerprint"], reverse["fingerprint"])
        policy = read_repo(".github/scripts/pr_agent_policy.js")
        self.assertIn("function logicalFingerprint", policy)
        self.assertIn(".sort()", policy)

    def test_default_branch_resolution_is_validated(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("function sanitizeBranch", merge)
        self.assertIn("cachedDefaultBranch = sanitizeBranch(name,", merge)
        self.assertIn("part.startsWith('.')", merge)
        self.assertIn("part.endsWith('.lock')", merge)

    def test_retry_branch_sanitizer_covers_hidden_components(self):
        for path in (
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
        ):
            with self.subTest(path=path):
                body = read_repo(path)
                self.assertIn("|-*|.*)", body)
                self.assertIn(r"\.lock($|/)", body)
                self.assertIn("(^|/)", body)

    def test_pr_agent_callers_do_not_expand_github_token_actions_permission(self):
        for path in (
            ".github/workflows/pr-agent.yml",
            ".github/caller-stubs/continuum-pr-agent.yml",
            ".github/caller-stubs/continuum-pr-agent-repair.yml",
            ".github/caller-stubs/continuum-pr-agent-auto-merge.yml",
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
            ".github/workflows/continuum-pr-agent-auto-merge.yml",
        ):
            with self.subTest(path=path):
                self.assertNotIn("actions: write", read_repo(path))

    def test_code_rabbit_workflows_are_not_referenced_by_new_pr_agent_recovery(self):
        for path in (
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
            ".github/workflows/continuum-pr-agent-auto-merge.yml",
        ):
            body = read_repo(path)
            self.assertNotIn("continuum-coderabbit-retry.yml", body)
            self.assertNotIn("continuum-coderabbit-unresolved.yml", body)


class ConflictLockRegressionTests(unittest.TestCase):
    """Behavioral regression for the conflict-repair lock freeze.

    A stranded `opencode-conflict-repair` label with no live repair run
    behind it must release, never wait forever. These tests exercise the
    decision helper directly instead of asserting workflow substrings.
    """

    def _life(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        return life

    def test_stranded_lock_with_no_active_run_is_released_not_waited(self):
        life = self._life()
        # The freeze: label present, terminal run left nothing active.
        self.assertTrue(
            life.is_conflict_lock_stranded(
                label_present=True, active_repair_runs=0
            )
        )
        decision = life.conflict_repair_action(
            label_present=True, active_repair_runs=0, attempts_for_head=0
        )
        self.assertEqual(decision["action"], "release-and-dispatch")
        self.assertTrue(decision["release_lock"])

    def test_active_repair_run_still_waits(self):
        life = self._life()
        self.assertFalse(
            life.is_conflict_lock_stranded(
                label_present=True, active_repair_runs=1
            )
        )
        decision = life.conflict_repair_action(
            label_present=True, active_repair_runs=1, attempts_for_head=0
        )
        self.assertEqual(decision["action"], "wait")
        self.assertFalse(decision["release_lock"])

    def test_no_label_without_prior_attempt_dispatches(self):
        life = self._life()
        decision = life.conflict_repair_action(
            label_present=False, active_repair_runs=0, attempts_for_head=0
        )
        self.assertEqual(decision["action"], "dispatch")

    def test_second_conflict_on_same_head_is_held_not_redispatched(self):
        life = self._life()
        # Replay-vs-redo: the same HEAD already consumed its single bounded
        # merge-scope attempt, so no full repair run is re-dispatched.
        for label in (False, True):
            with self.subTest(label_present=label):
                decision = life.conflict_repair_action(
                    label_present=label,
                    active_repair_runs=0,
                    attempts_for_head=1,
                )
                self.assertEqual(decision["action"], "hold")
                self.assertTrue(decision["release_lock"])

    def test_new_head_starts_a_new_episode(self):
        life = self._life()
        self.assertTrue(life.needs_fresh_review("oldhead", "newhead"))
        decision = life.conflict_repair_action(
            label_present=False, active_repair_runs=0, attempts_for_head=0
        )
        self.assertEqual(decision["action"], "dispatch")

    def test_invalid_lock_inputs_fail_closed(self):
        life = self._life()
        with self.assertRaises(life.LifecycleError):
            life.is_conflict_lock_stranded(
                label_present=True, active_repair_runs=-1
            )
        with self.assertRaises(life.LifecycleError):
            life.conflict_repair_action(
                label_present=False,
                active_repair_runs=0,
                attempts_for_head=-1,
            )

    def test_retry_budget_is_bounded_with_exponential_backoff(self):
        life = self._life()
        self.assertEqual(life.RETRY_MAX_ATTEMPTS, 3)
        for attempt in (0, 1):
            with self.subTest(attempt=attempt):
                self.assertTrue(life.retry_allowed(attempt))
        self.assertFalse(life.retry_allowed(2))
        self.assertFalse(life.retry_allowed(3))
        self.assertFalse(life.retry_allowed(9))
        self.assertEqual(life.retry_backoff_seconds(0), 15)
        self.assertEqual(life.retry_backoff_seconds(1), 30)
        self.assertEqual(life.retry_backoff_seconds(2), 60)
        with self.assertRaises(life.LifecycleError):
            life.retry_allowed("nope")
        with self.assertRaises(life.LifecycleError):
            life.retry_backoff_seconds(-1)

    def test_retry_backoff_threads_the_same_limit_as_allowed(self):
        life = self._life()
        # retry_allowed accepts a custom limit; backoff must bound with the
        # same limit instead of the global so limit=10 does not diverge.
        self.assertTrue(life.retry_allowed(8, limit=10))
        self.assertEqual(life.retry_backoff_seconds(9, limit=10), 15 * (1 << 9))
        self.assertFalse(life.retry_allowed(9, limit=10))
        self.assertFalse(life.retry_allowed(9, limit=9))
        with self.assertRaises(life.LifecycleError):
            life.retry_backoff_seconds(10, limit=10)
        with self.assertRaises(life.LifecycleError):
            life.retry_backoff_seconds(9, limit=9)
        with self.assertRaises(life.LifecycleError):
            life.retry_backoff_seconds(0, limit="nope")

    def test_dispatch_ref_never_hardcodes_a_branch(self):
        life = self._life()
        self.assertEqual(
            life.resolve_dispatch_ref("my-default"), "my-default"
        )
        self.assertEqual(life.resolve_dispatch_ref("", "main"), "main")
        self.assertEqual(life.resolve_dispatch_ref(None, ""), "main")
        self.assertEqual(
            life.CONFLICT_REPAIR_TIMEOUT_MINUTES, 60
        )
        self.assertEqual(
            life.CONFLICT_REPAIR_ATTEMPTS_PER_HEAD, 1
        )


class RepairWiringRegressionTests(unittest.TestCase):
    """The workflow wiring must implement the lock/timeout/retry contract."""

    def test_conflict_dispatch_is_bounded_merge_scope_on_default_branch(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        # Default-branch sync in both compare directions, never a literal.
        self.assertIn("getDefaultBranch", merge)
        self.assertIn("defaultBranch + '...' + pr.head.sha", merge)
        self.assertIn("pr.head.sha + '...' + defaultBranch", merge)
        self.assertNotIn("basehead: 'main", merge)
        self.assertNotIn("'...main'", merge)
        self.assertNotIn('"...main"', merge)
        # Bounded merge-scope repair: explicit strategy plus a timeout.
        self.assertIn("conflict_strategy: 'merge'", merge)
        self.assertIn("timeout_minutes: '60'", merge)
        # Isolation is per PR/per HEAD: a repository-global active-run probe
        # would let an unrelated PR block this PR.
        self.assertNotIn("conflictRepairRunsActive", merge)
        self.assertIn("releaseConflictLock", merge)
        self.assertIn("exact-HEAD marker decides whether this PR may dispatch", merge)
        # Bounded retry: one attempt marker per HEAD, then hold.
        self.assertIn("opencode-conflict-repair-attempt head=", merge)
        self.assertIn("already ran for this HEAD", merge)
        # Dispatch still targets the resolved default branch.
        self.assertIn("ref: defaultBranch", merge)
        # Required parity needles survive the repair.
        for needle in (
            "AUTO_MERGE_BLOCK_LABEL = 'no-auto-merge'",
            "CONFLICT_LOCK_LABEL = 'opencode-conflict-repair'",
            "mode: 'resolve-conflict'",
            "expected_head_sha: oldHead",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, merge)

    def test_merge_reconciliation_is_serialized_per_pr(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn(
            "group: pr-agent-merge-${{ inputs.pr_number || github.run_id }}",
            merge,
        )
        self.assertIn("cancel-in-progress: false", merge)
        # Per-PR serialization plus the exact-HEAD marker is the isolation
        # contract; repository-global active-run probing is forbidden.
        self.assertNotIn("conflictRepairRunsActive", merge)

    def test_active_conflict_repair_wins_over_exhausted_attempt_helper(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        decision = life.conflict_repair_action(
            label_present=True,
            active_repair_runs=1,
            attempts_for_head=1,
        )
        self.assertEqual(decision["action"], "wait")
        self.assertFalse(decision["release_lock"])

    def test_recovery_controller_is_owned_by_dedicated_38_workflows(self):
        for path in (
            ".github/workflows/pr-agent.yml",
            ".github/caller-stubs/continuum-pr-agent.yml",
        ):
            with self.subTest(path=path):
                caller = read_repo(path)
                self.assertNotIn("workflow_run:", caller)
                self.assertNotIn("pull_request_target:", caller)
                self.assertNotIn("schedule:", caller)

        for path in (
            ".github/workflows/pr-agent-recovery.yml",
            ".github/caller-stubs/continuum-pr-agent-recovery.yml",
        ):
            with self.subTest(path=path):
                recovery = read_repo(path)
                self.assertIn("workflow_run:", recovery)
                self.assertIn("schedule:", recovery)
                self.assertIn("- CI", recovery)

        engine = read_repo(
            ".github/workflows/continuum-pr-agent-recovery.yml"
        )
        self.assertIn("Reconcile PR-Agent latest state", engine)
        self.assertIn("createWorkflowDispatch", engine)

    def test_review_and_repair_publish_durable_exact_head_statuses(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("Mark PR-Agent review in flight", review)
        self.assertIn("Publish durable PR-Agent review state", review)
        self.assertIn("Publish failed PR-Agent review state", review)
        self.assertIn("continuum/pr-agent-review", review)
        self.assertIn("PR-Agent review complete: actionable", review)
        self.assertIn("PR-Agent review complete: clean", review)
        self.assertIn("Mark PR-Agent repair in flight", repair)
        self.assertIn("Publish durable PR-Agent repair state", repair)
        self.assertIn("Publish failed PR-Agent repair state", repair)
        self.assertIn("continuum/pr-agent-repair", repair)

    def test_no_caller_level_per_pr_serialization(self):
        # Issue #227: the caller must not serialize per PR. A caller-level
        # lock is acquired before admission, so no-op comment runs would
        # queue ahead of useful exact-HEAD reviews. Heavy callers are
        # dispatch-only by design (issue #229): ordinary comments must not
        # create heavy workflow runs at all. Filtering and coalescing live
        # in the thin router, so the heavy YAML carries no comment gate.
        # Every atomic condition of caller_review_event_is_actionable stays
        # mirrored in the router, not in the heavy caller.
        for path in (
            ".github/workflows/pr-agent.yml",
            ".github/caller-stubs/continuum-pr-agent.yml",
        ):
            with self.subTest(path=path):
                caller = read_repo(path)
                self.assertNotIn("pr-agent-caller-", caller)
                self.assertNotIn("concurrency:", caller)
                self.assertNotIn("cancel-in-progress", caller)
                self.assertIn("workflow_dispatch:", caller)
                self.assertIn("github.event_name == 'workflow_dispatch'", caller)
                self.assertNotIn("issue_comment", caller)
                self.assertNotIn(
                    "contains(github.event.comment.body, '/review')", caller
                )
        router_stub = read_repo(
            ".github/caller-stubs/continuum-pr-agent-router.yml"
        )
        self.assertIn("issue_comment", router_stub)
        self.assertIn(
            "contains(github.event.comment.body, '/review')", router_stub
        )

    def test_authoritative_review_serialization_is_cancellable_per_pr(self):
        # Issue #227: the reusable operation layer owns the only per-PR
        # serialization, and it covers the whole run at workflow level so
        # downstream repair/merge wrappers can never run in parallel with
        # a newer run. Preemption is HEAD-guarded, never unconditional:
        # an out-of-order old-HEAD event must not cancel newer
        # exact-HEAD work and same-HEAD duplicates coalesce.
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("group: pr-agent-${{", body)
        self.assertIn("cancel-in-progress: false", body)
        self.assertNotIn("group: pr-agent-caller-", body)
        self.assertNotIn("cancel-in-progress: true", body)
        workflow_block, _, jobs_block = body.partition("\njobs:\n")
        self.assertIn("concurrency:", workflow_block)
        self.assertNotIn("\n    concurrency:", jobs_block)
        # HEAD-guarded supersession markers implement
        # stale_review_may_be_cancelled instead of native preemption.
        self.assertIn("Stale admission ignored:", body)
        self.assertIn("PR head moved during review", body)
        self.assertIn("stale_review_may_be_cancelled", body)

    def test_repair_serialization_is_non_interruptible_per_head(self):
        # Issue #227: repair publication keeps its own non-cancellable
        # per-PR-HEAD group and is never interrupted by serialized review
        # work, so a newer event cannot interrupt a mutating publish.
        # The parent run is also non-preemptive at workflow level, so an
        # old-HEAD duplicate can never cancel newer exact-HEAD work.
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn(
            "group: pr-agent-repair-${{ inputs.pr_number || github.run_id }}-"
            "${{ inputs.head_sha || github.sha }}",
            repair,
        )
        self.assertIn("cancel-in-progress: false", repair)
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("cancel-in-progress: false", review)
        self.assertNotIn("cancel-in-progress: true", review)
        # The whole-run group is per PR; the repair group is
        # per PR plus exact HEAD, so same-HEAD repairs serialize while
        # HEAD-guarded supersession (not native cancellation) retires
        # stale reviews.
        self.assertIn("inputs.pr_number || github.run_id", review)

    def test_no_progress_marker_trust_does_not_depend_on_login(self):
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        # Stale-label regression: markers posted under the TAP_PAT machine
        # user must hold; a login allow-list strands them into a storm.
        self.assertIn("continuum-pr-agent-no-progress head=", repair)
        self.assertNotIn("login === owner", repair)
        self.assertNotIn("comment.user?.login", repair)

    def test_retry_dispatch_preserves_branch_workflow_and_guards_api(self):
        for path in (
            ".github/workflows/continuum-pr-agent.yml",
            ".github/workflows/continuum-pr-agent-repair.yml",
        ):
            with self.subTest(path=path):
                body = read_repo(path)
                self.assertIn("DEFAULT_BRANCH=", body)
                self.assertIn('"${DEFAULT_BRANCH:-main}"', body)
                self.assertIn('-f retry_workflow="$RETRY_WORKFLOW"', body)
                self.assertIn(
                    "Could not revalidate PR state after backoff", body
                )
                self.assertIn(
                    "Could not revalidate exact-HEAD CI after backoff", body
                )
                self.assertNotIn('--ref main', body)

    def test_retry_workflow_target_is_allow_listed(self):
        review = read_repo(".github/workflows/continuum-pr-agent.yml")
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn(
            "['continuum-pr-agent.yml', 'pr-agent.yml'].includes(retryWorkflow)",
            review,
        )
        self.assertIn("continuum-pr-agent.yml|pr-agent.yml", repair)

    def test_conflict_dispatch_failure_does_not_consume_head_attempt(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("attemptComment.data.id", merge)
        self.assertIn("deleteComment", merge)
        self.assertLess(
            merge.index("createComment"),
            merge.index("actions/workflows/{workflow_id}/dispatches"),
        )

    def test_final_merge_refreshes_pr_and_handles_late_conflict(self):
        merge = read_repo(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("Re-read after all asynchronous gate queries", merge)
        self.assertIn("preMergeSync", merge)
        self.assertIn("preMergeGates", merge)
        self.assertIn("err.status === 409", merge)
        self.assertIn("const isConflictMessage", merge)
        self.assertIn(
            "Atomic merge rejected with HTTP 422 without conflict evidence",
            merge,
        )
        self.assertIn("await dispatchConflictRepair(fresh, message)", merge)

    def test_pr_agent_caller_forwards_exact_head_retry_inputs(self):
        caller = read_repo(".github/workflows/pr-agent.yml")
        self.assertIn('pr_number: "${{ inputs.pr_number }}"', caller)
        self.assertIn(
            'expected_head_sha: "${{ inputs.expected_head_sha }}"', caller
        )
        self.assertIn('retry_attempt: "${{ inputs.retry_attempt }}"', caller)


def run_skip_policy(review, persistent_state, options=None):
    env = os.environ.copy()
    env["POLICY_MODULE"] = POLICY_MODULE
    env["POLICY_REVIEW"] = json.dumps(review)
    env["POLICY_STATE"] = json.dumps(persistent_state)
    env["POLICY_OPTIONS"] = json.dumps(options or {})
    code = r"""
const policy = require(process.env.POLICY_MODULE);
const review = JSON.parse(process.env.POLICY_REVIEW || 'null');
const state = JSON.parse(process.env.POLICY_STATE || 'null');
const options = JSON.parse(process.env.POLICY_OPTIONS || '{}');
process.stdout.write(JSON.stringify(policy.isCleanReviewForImproveSkip(review, state, options)));
"""
    completed = subprocess.run(
        ["node", "-e", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


def run_skip_policy_raw(review_raw, state_raw, options=None):
    env = os.environ.copy()
    env["POLICY_MODULE"] = POLICY_MODULE
    env["POLICY_REVIEW"] = review_raw
    env["POLICY_STATE"] = state_raw
    env["POLICY_OPTIONS"] = json.dumps(options or {})
    code = r"""
const policy = require(process.env.POLICY_MODULE);
let review;
try {
  review = JSON.parse(process.env.POLICY_REVIEW || 'null');
} catch (err) {
  process.stdout.write(JSON.stringify({parse_error: 'review:' + err.message}));
  process.exit(0);
}
let state;
try {
  state = JSON.parse(process.env.POLICY_STATE || 'null');
} catch (err) {
  process.stdout.write(JSON.stringify({parse_error: 'state:' + err.message}));
  process.exit(0);
}
const options = JSON.parse(process.env.POLICY_OPTIONS || '{}');
try {
  process.stdout.write(JSON.stringify(policy.isCleanReviewForImproveSkip(review, state, options)));
} catch (err) {
  process.stdout.write(JSON.stringify({threw: err.message}));
}
"""
    completed = subprocess.run(
        ["node", "-e", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


class CleanReviewImproveSkipTests(unittest.TestCase):
    """Issue #231: a clean authoritative review skips automatic improve."""

    def _life(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        return life

    def test_clean_review_skips_automatic_improve(self):
        life = self._life()
        decision = life.should_skip_improve(
            make_review([]),
            make_persistent([], head_sha="abc"),
            head_matches=True,
            reviewed_head_sha="abc",
        )
        self.assertTrue(decision["skip"])
        js = run_skip_policy(
            make_review([]),
            make_persistent([], head_sha="abc"),
            {"headMatches": True, "reviewedHeadSha": "abc"},
        )
        self.assertTrue(js["skip"])

    def test_actionable_review_still_runs_improve(self):
        life = self._life()
        cases = [
            make_review([issue_entry(n=0)]),
            make_review([], recommendation="merge_with_caution"),
            make_review([], recommendation="changes_required"),
            make_review([issue_entry(n=i) for i in range(6)]),
        ]
        for review in cases:
            with self.subTest(review=review):
                decision = life.should_skip_improve(
                    review,
                    make_persistent([], head_sha="abc"),
                    head_matches=True,
                    reviewed_head_sha="abc",
                )
                self.assertFalse(decision["skip"])
                js = run_skip_policy(
                    review,
                    make_persistent([], head_sha="abc"),
                    {"headMatches": True, "reviewedHeadSha": "abc"},
                )
                self.assertFalse(js["skip"])

    def test_active_persistent_state_still_runs_improve(self):
        life = self._life()
        state = make_persistent([{"state": "ACTIVE"}], head_sha="abc")
        decision = life.should_skip_improve(
            make_review([]), state, head_matches=True, reviewed_head_sha="abc"
        )
        self.assertFalse(decision["skip"])
        js = run_skip_policy(
            make_review([]), state, {"headMatches": True, "reviewedHeadSha": "abc"}
        )
        self.assertFalse(js["skip"])

    def test_blocking_security_signal_still_runs_improve(self):
        life = self._life()
        review = make_review(
            [], extra={"security_concerns": ["hardcoded credential"]}
        )
        decision = life.should_skip_improve(
            review, make_persistent([], head_sha="abc"), head_matches=True,
            reviewed_head_sha="abc",
        )
        self.assertFalse(decision["skip"])
        js = run_skip_policy(
            review, make_persistent([], head_sha="abc"),
            {"headMatches": True, "reviewedHeadSha": "abc"},
        )
        self.assertFalse(js["skip"])

    def test_tool_error_and_incomplete_coverage_still_run_improve(self):
        life = self._life()
        for kwargs in (
            {"tool_error": True},
            {"review_coverage_complete": False},
        ):
            with self.subTest(kwargs=kwargs):
                decision = life.should_skip_improve(
                    make_review([]),
                    make_persistent([], head_sha="abc"),
                    head_matches=True,
                    reviewed_head_sha="abc",
                    **kwargs,
                )
                self.assertFalse(decision["skip"])
        js = run_skip_policy(
            make_review([]),
            make_persistent([], head_sha="abc"),
            {"headMatches": True, "toolError": True, "reviewedHeadSha": "abc"},
        )
        self.assertFalse(js["skip"])
        js = run_skip_policy(
            make_review([]),
            make_persistent([], head_sha="abc"),
            {"headMatches": True, "reviewCoverageComplete": False, "reviewedHeadSha": "abc"},
        )
        self.assertFalse(js["skip"])

    def test_exact_head_mismatch_never_skips(self):
        life = self._life()
        decision = life.should_skip_improve(
            make_review([]),
            make_persistent([], head_sha="abc"),
            head_matches=False,
        )
        self.assertFalse(decision["skip"])
        js = run_skip_policy(
            make_review([]),
            make_persistent([], head_sha="abc"),
            {"headMatches": False},
        )
        self.assertFalse(js["skip"])

    def test_invalid_review_or_state_fails_closed_not_skip(self):
        life = self._life()
        with self.assertRaises(life.LifecycleError):
            life.should_skip_improve(
                {"merge_recommendation": "safe_to_merge"},
                make_persistent([], head_sha="abc"),
                head_matches=True,
            )
        with self.assertRaises(life.LifecycleError):
            life.should_skip_improve(
                make_review([]),
                {"findings": [], "last_run": {"complete": False, "kind": "full"}},
                head_matches=True,
            )
        with self.assertRaises(life.LifecycleError):
            life.should_skip_improve(
                make_review([]), {"not": "state"}, head_matches=True
            )
        bad_review = run_skip_policy_raw(
            '{"nope": true}', json.dumps(make_persistent([], head_sha="abc")),
            {"headMatches": True},
        )
        self.assertIn("threw", bad_review)
        bad_state = run_skip_policy_raw(
            json.dumps(make_review([])), '{"not": "state"}',
            {"headMatches": True},
        )
        self.assertIn("threw", bad_state)

    def test_wrapped_review_envelope_skips_when_clean(self):
        life = self._life()
        wrapped = {"review": make_review([])}
        decision = life.should_skip_improve(
            wrapped, make_persistent([], head_sha="abc"), head_matches=True,
            reviewed_head_sha="abc",
        )
        self.assertTrue(decision["skip"])
        js = run_skip_policy(
            wrapped, make_persistent([], head_sha="abc"),
            {"headMatches": True, "reviewedHeadSha": "abc"},
        )
        self.assertTrue(js["skip"])

    def test_full_counts_never_mask_an_incomplete_coverage_flag(self):
        life = self._life()
        for extra in (
            {"coverage": {"reviewed": 5, "total": 5, "truncated": True}},
            {"coverage": {"reviewed": 5, "total": 5, "partial": True}},
            {"coverage": {"reviewed": 5, "total": 5, "complete": False}},
            {"review_coverage": {"reviewed": 3, "total": 3, "truncated": True}},
        ):
            with self.subTest(extra=extra):
                review = make_review([], extra=extra)
                self.assertTrue(life.has_incomplete_coverage_signal(review))
                decision = life.should_skip_improve(
                    review,
                    make_persistent([], head_sha="abc"),
                    head_matches=True,
                    reviewed_head_sha="abc",
                )
                self.assertFalse(decision["skip"])
                js = run_skip_policy(
                    review,
                    make_persistent([], head_sha="abc"),
                    {"headMatches": True, "reviewedHeadSha": "abc"},
                )
                self.assertFalse(js["skip"])

    def test_missing_reviewed_head_never_skips(self):
        life = self._life()
        with self.assertRaises(life.LifecycleError):
            life.should_skip_improve(
                make_review([]),
                make_persistent([], head_sha="abc"),
                head_matches=True,
            )
        stale = life.should_skip_improve(
            make_review([]),
            make_persistent([], head_sha="abc"),
            head_matches=True,
            reviewed_head_sha="def",
        )
        self.assertFalse(stale["skip"])
        missing = run_skip_policy_raw(
            json.dumps(make_review([])),
            json.dumps(make_persistent([], head_sha="abc")),
            {"headMatches": True},
        )
        self.assertIn("threw", missing)

    def test_merge_gate_greens_for_skipped_clean_improve(self):
        life = self._life()
        gate = life.evaluate_gate(life.GateInputs(
            review=make_review([]),
            qualifying_improve=[],
            persistent_state=make_persistent([], head_sha="abc"),
            ci_green_on_exact_head=True,
            head_matches=True,
            review_coverage_complete=True,
            improve_coverage_complete=False,
            improve_skipped_clean=True,
        ))
        self.assertTrue(gate["green"])
        held = life.evaluate_gate(life.GateInputs(
            review=make_review([]),
            qualifying_improve=[{"file": "a.py", "score": 9}],
            persistent_state=make_persistent([], head_sha="abc"),
            ci_green_on_exact_head=True,
            head_matches=True,
            review_coverage_complete=True,
            improve_coverage_complete=False,
            improve_skipped_clean=True,
        ))
        self.assertFalse(held["green"])
        incomplete = life.evaluate_gate(life.GateInputs(
            review=make_review([]),
            qualifying_improve=[],
            persistent_state=make_persistent([], head_sha="abc"),
            ci_green_on_exact_head=True,
            head_matches=True,
            review_coverage_complete=True,
            improve_coverage_complete=False,
        ))
        self.assertFalse(incomplete["green"])


class ImproveSkipWorkflowTests(unittest.TestCase):
    def test_review_runs_before_improve_gate_and_improve_is_conditional(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("Run upstream full review on the exact HEAD", body)
        self.assertNotIn("Run upstream full review and full improve on the exact HEAD", body)
        self.assertIn("Decide whether automatic improve can be skipped", body)
        self.assertIn("isCleanReviewForImproveSkip", body)
        self.assertIn(
            "Run upstream improve on the exact HEAD when repair value remains", body
        )
        self.assertIn("Record skipped automatic improve for the clean HEAD", body)
        # The conditional improve still runs the pinned upstream tool in full.
        self.assertIn('pr-agent --pr_url "$PR_URL" review', body)
        self.assertIn('pr-agent --pr_url "$PR_URL" improve', body)
        gate = body.index("Decide whether automatic improve can be skipped")
        improve = body.index("Run upstream improve on the exact HEAD when repair value remains")
        skip_record = body.index("Record skipped automatic improve for the clean HEAD")
        export = body.index("Export native improve output", gate)
        normalize = body.index("Normalize persistent improve presentation")
        self.assertLess(gate, improve)
        self.assertLess(improve, skip_record)
        self.assertLess(skip_record, export)
        self.assertLess(export, normalize)
        self.assertIn("steps.improve_gate.outputs.skip_improve != 'true'", body)
        self.assertIn("steps.improve_gate.outputs.skip_improve == 'true'", body)

    def test_persistent_state_precedes_the_improve_gate(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        persistent = body.index("Export native persistent finding state for the reviewed HEAD")
        gate = body.index("Decide whether automatic improve can be skipped")
        self.assertLess(persistent, gate)
        gate_window = body[gate:gate + 6000]
        self.assertIn("PERSISTENT_STATE_JSON", gate_window)
        self.assertIn("last_run", gate_window)

    def test_improve_gate_is_exact_head_safe(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        gate = body.index("Decide whether automatic improve can be skipped")
        window = body[gate:gate + 6000]
        self.assertIn("REVIEWED_SHA", window)
        self.assertIn("HEAD_SHA", window)
        self.assertIn("headSha !== reviewedSha", window)
        self.assertIn("PR head moved before the improve gate", window)
        self.assertIn("persistent state is stale", window)
        self.assertIn("headMatches: true", window)

    def test_improve_failure_remains_retryable(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("steps.improve.outcome == 'failure'", body)
        self.assertGreaterEqual(body.count("steps.improve.outcome == 'failure'"), 3)

    def test_improve_gate_revalidates_the_live_head(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        gate = body.index("Decide whether automatic improve can be skipped")
        window = body[gate:gate + 6000]
        self.assertIn("pulls.get", window)
        self.assertIn("liveSha", window)
        self.assertIn("reviewedHeadSha", window)

    def test_skipped_improve_failure_remains_retryable(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertGreaterEqual(
            body.count("steps.improve_skipped.outcome == 'failure'"), 3
        )


class SchedulingSemanticsTests(unittest.TestCase):
    """Issue #227: latest-useful-work scheduling without double-queueing.

    No-op issue_comment events must never hold a per-PR lock, superseded
    reviews may be cancelled/coalesced, and in-flight repair publication
    must never be interrupted by a newer review event.
    """

    def _life(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        return life

    def test_noop_comment_events_are_not_actionable(self):
        life = self._life()
        # Owner /review on a PR and workflow_dispatch carry useful work.
        self.assertTrue(
            life.caller_review_event_is_actionable(
                "workflow_dispatch",
            )
        )
        self.assertTrue(
            life.caller_review_event_is_actionable(
                "issue_comment",
                is_pull_request_comment=True,
                actor_is_owner=True,
                comment_body="/review please",
            )
        )
        # Everything else is a no-op: plain comments, non-owner actors,
        # non-PR issues, and unrelated events must not invoke the operation
        # layer, so they can never delay a useful exact-HEAD review.
        self.assertFalse(
            life.caller_review_event_is_actionable(
                "issue_comment",
                is_pull_request_comment=True,
                actor_is_owner=True,
                comment_body="looks good, thanks!",
            )
        )
        self.assertFalse(
            life.caller_review_event_is_actionable(
                "issue_comment",
                is_pull_request_comment=True,
                actor_is_owner=False,
                comment_body="/review",
            )
        )
        self.assertFalse(
            life.caller_review_event_is_actionable(
                "issue_comment",
                is_pull_request_comment=False,
                actor_is_owner=True,
                comment_body="/review",
            )
        )
        self.assertFalse(
            life.caller_review_event_is_actionable(
                "issue_comment",
                is_pull_request_comment=True,
                actor_is_owner=True,
                comment_body="",
            )
        )
        self.assertFalse(life.caller_review_event_is_actionable("schedule"))
        self.assertFalse(life.caller_review_event_is_actionable(""))

    def test_superseded_review_may_be_cancelled_same_head_coalesces(self):
        life = self._life()
        old = "a" * 40
        new = "b" * 40
        # A moved HEAD supersedes older review work: cancel/coalesce it.
        self.assertTrue(life.stale_review_may_be_cancelled(old, new))
        self.assertTrue(life.needs_fresh_review(old, new))
        # Same HEAD duplicates coalesce: exact-HEAD work is idempotent.
        self.assertFalse(life.stale_review_may_be_cancelled(old, old.upper()))
        self.assertFalse(life.needs_fresh_review(old, old.upper()))
        # Missing SHAs never cancel: fail closed, never discard blindly.
        self.assertFalse(life.stale_review_may_be_cancelled("", new))
        self.assertFalse(life.stale_review_may_be_cancelled(old, ""))
        self.assertFalse(life.stale_review_may_be_cancelled(None, new))

    def test_repair_publication_is_non_interruptible(self):
        life = self._life()
        # Repair mutates/publishes under exact-HEAD revalidation plus
        # force-with-lease; an active same-HEAD repair blocks review
        # preemption, while no repair (or a different HEAD) does not.
        self.assertTrue(
            life.repair_is_protected_from_review_preemption(
                repair_active=True, repair_head_sha="a" * 40, review_head_sha="a" * 40
            )
        )
        self.assertTrue(
            life.repair_is_protected_from_review_preemption(repair_active=True)
        )
        self.assertFalse(
            life.repair_is_protected_from_review_preemption(repair_active=False)
        )
        self.assertFalse(
            life.repair_is_protected_from_review_preemption(
                repair_active=True,
                repair_head_sha="a" * 40,
                review_head_sha="b" * 40,
            )
        )
        # The scheduling decision wires all three predicates so none is
        # dead code: non-actionable events ignore, active repair waits,
        # moved HEAD supersedes, same HEAD coalesces, and a current HEAD
        # with no running review proceeds.
        old, new = "a" * 40, "b" * 40
        self.assertEqual(
            life.review_supersession_decision(
                old, new, event_name="schedule"
            )["action"],
            "ignore",
        )
        self.assertEqual(
            life.review_supersession_decision(
                old,
                new,
                repair_active=True,
                repair_head_sha=new,
                review_head_sha=new,
            )["action"],
            "wait",
        )
        self.assertEqual(
            life.review_supersession_decision(old, new)["action"], "supersede"
        )
        self.assertEqual(
            life.review_supersession_decision(old, old)["action"], "coalesce"
        )
        # First-ever review with no running SHA proceeds to admission;
        # a missing current HEAD still fails closed to wait.
        self.assertEqual(
            life.review_supersession_decision("", new)["action"], "proceed"
        )
        self.assertEqual(
            life.review_supersession_decision(None, new)["action"], "proceed"
        )
        self.assertEqual(
            life.review_supersession_decision(old, "")["action"], "wait"
        )
        # Python wiring is a real call site, not documentation-only.
        source = lifecycle_source()
        decision = source.split("def review_supersession_decision", 1)[1]
        self.assertIn("caller_review_event_is_actionable(", decision)
        self.assertIn("stale_review_may_be_cancelled(", decision)
        self.assertIn("repair_is_protected_from_review_preemption(", decision)
        repair = read_repo(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("force-with-lease", repair)
        self.assertIn("PR branch moved before publish", repair)

    def test_router_holds_no_per_pr_lock(self):
        # Issue #227: the thin router filters before any delay. A
        # workflow-level lock is acquired before the job `if` / early-exit
        # filter, so a plain non-`/review` comment run would queue ahead of
        # a useful dispatch for the same PR. Duplicates coalesce via the
        # operation-key active-run check instead.
        for path in (
            ".github/caller-stubs/continuum-pr-agent-router.yml",
            ".github/workflows/continuum-pr-agent-router.yml",
            ".github/workflows/pr-agent-router.yml",
        ):
            with self.subTest(path=path):
                body = read_repo(path)
                self.assertNotIn("concurrency:", body)
                self.assertNotIn("cancel-in-progress", body)




if __name__ == "__main__":
    unittest.main()
