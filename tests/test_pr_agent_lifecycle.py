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

BASELINE_SHA = "5833457ec6036188d1ec1a11d7c85cab8b2a7c73"

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


def read_repo(path: str) -> str:
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
        return handle.read()


def lifecycle_source() -> str:
    with open(LIFECYCLE_MODULE, "r", encoding="utf-8") as handle:
        return handle.read()


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

    def test_suggestions_threshold_is_one_and_nothing_silently_dropped(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        self.assertEqual(life.SUGGESTIONS_SCORE_THRESHOLD, 1)
        toml = read_repo(".pr_agent.toml")
        self.assertIn("suggestions_score_threshold = 1", toml)
        suggestions = [
            {"file": "a.py", "score": 1},
            {"file": "b.py", "score": 9},
            {"file": "c.py"},
        ]
        self.assertEqual(len(life.qualifying_suggestions(suggestions)), 3)
        self.assertEqual(life.qualifying_suggestions([{"score": 0}]), [])


class RepairBatchTests(unittest.TestCase):
    def test_all_findings_and_suggestions_in_one_bounded_batch(self):
        import sys

        sys.path.insert(0, SRC)
        try:
            from continuum import pr_agent_lifecycle as life
        finally:
            sys.path.remove(SRC)
        review = make_review([issue_entry(n=0), issue_entry(n=1), issue_entry(n=2)])
        suggestions = [{"file": "a.py", "score": 2}, {"file": "b.py", "score": 5}]
        batch = life.build_repair_batch(review, suggestions, head_sha="abc123")
        self.assertTrue(batch.bounded)
        self.assertEqual(len(batch.items), 5)
        sources = [item["source"] for item in batch.items]
        self.assertEqual(sources.count("review"), 3)
        self.assertEqual(sources.count("improve"), 2)

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
            "publish_output_no_suggestions = true",
            "suggestions_score_threshold = 1",
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
            "PR_CODE_SUGGESTIONS__SUGGESTIONS_SCORE_THRESHOLD",
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
        self.assertIn("getCombinedStatusForRef", body)
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

    def test_pre_ci_skip_does_not_run_checkout_integrity_guard(self):
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn(
            "if: always() && steps.stack.outputs.enabled == 'true' && steps.admit.outputs.admitted == 'true'",
            body,
        )

    def test_review_caller_wakes_on_successful_ci_and_exports_native_outputs(self):
        caller = read_repo(".github/caller-stubs/continuum-pr-agent.yml")
        self.assertIn("workflow_run:", caller)
        self.assertIn('workflows: ["CI"]', caller)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", caller)
        body = read_repo(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("review_json:", body)
        self.assertIn("improve_jsonl:", body)
        self.assertIn("steps.pragent.outputs.review", body)
        self.assertNotIn("CONTINUUM_PR_AGENT_ENABLED", body)
        self.assertNotIn("CONTINUUM_REQUIRE_CODERABBIT", body)
        self.assertIn("Install pinned OpenCode CLI for the PR-Agent backend", body)
        self.assertIn("Resolve the Continuum-owned PR-Agent bridge", body)

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

    def test_generic_auto_merge_still_refuses_pr_agent_mode(self):
        generic = read_repo(".github/workflows/continuum-auto-merge.yml")
        self.assertIn("if (reviewProvider === 'pr-agent')", generic)
        self.assertIn("Only the PR-Agent-specific merge gate may merge.", generic)

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


if __name__ == "__main__":
    unittest.main()
