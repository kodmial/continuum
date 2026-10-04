"""Focused contract tests for the PR-Agent JS policy bundle.

Covers the DoD triad directly against `.github/scripts/pr_agent_policy.js`:
clean-skip, actionable-improve, and exact-HEAD safety, plus split-envelope
merging and the skipped-clean marker. Guards fail-closed decisions so
regressions in the policy bundle are detectable without the Python parity
layer.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
POLICY_MODULE = os.path.join(ROOT, ".github", "scripts", "pr_agent_policy.js")


def run_js(expression, review=None, state=None, options=None, raw=None):
    env = os.environ.copy()
    env["POLICY_MODULE"] = POLICY_MODULE
    env["POLICY_REVIEW"] = json.dumps(review) if review is not None else ""
    env["POLICY_STATE"] = json.dumps(state) if state is not None else ""
    env["POLICY_OPTIONS"] = json.dumps(options or {})
    env["POLICY_RAW"] = raw if raw is not None else ""
    env["POLICY_EXPR"] = expression
    code = r"""
const policy = require(process.env.POLICY_MODULE);
const review = process.env.POLICY_REVIEW ? JSON.parse(process.env.POLICY_REVIEW) : null;
const state = process.env.POLICY_STATE ? JSON.parse(process.env.POLICY_STATE) : null;
const options = JSON.parse(process.env.POLICY_OPTIONS || '{}');
const raw = process.env.POLICY_RAW || '';
let result;
const expr = process.env.POLICY_EXPR;
if (expr === 'constants') {
  result = {
    threshold: policy.IMPROVE_REPAIR_THRESHOLD,
    maxFindings: policy.REVIEW_MAX_FINDINGS,
    mergeSafe: policy.REVIEW_MERGE_SAFE,
  };
} else if (expr === 'tool') result = policy.hasToolErrorSignal(review);
else if (expr === 'coverage') result = policy.hasIncompleteCoverageSignal(review);
else if (expr === 'security') result = policy.hasBlockingSecuritySignal(review);
else if (expr === 'unwrap') result = policy.unwrapReview(review);
else if (expr === 'skipped') result = policy.isSkippedCleanImprovePayload(raw);
else if (expr === 'skip') result = policy.isCleanReviewForImproveSkip(review, state, options);
else if (expr === 'disposition') result = policy.reviewDisposition(review, raw);
else throw new Error('unknown expr');
process.stdout.write(JSON.stringify(result === undefined ? null : result));
"""
    completed = subprocess.run(
        ["node", "-e", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def run_skip_raw(review_raw, state_raw, options=None):
    env = os.environ.copy()
    env["POLICY_MODULE"] = POLICY_MODULE
    env["POLICY_REVIEW"] = review_raw
    env["POLICY_STATE"] = state_raw
    env["POLICY_OPTIONS"] = json.dumps(options or {})
    code = r"""
const policy = require(process.env.POLICY_MODULE);
const review = JSON.parse(process.env.POLICY_REVIEW || 'null');
const state = JSON.parse(process.env.POLICY_STATE || 'null');
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
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def make_review(issues=None, recommendation="safe_to_merge", extra=None):
    review = {
        "key_issues_to_review": list(issues or []),
        "merge_recommendation": recommendation,
    }
    if extra:
        review.update(extra)
    return review


def make_persistent(findings=None, head_sha="abc1234"):
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


def issue_entry(n=0):
    return {
        "relevant_file": "src/app.py",
        "issue_header": f"Possible Bug {n}",
        "issue_content": f"concrete failure scenario {n}",
        "start_line": 10 + n,
        "end_line": 12 + n,
    }


CLEAN_OPTS = {
    "headMatches": True,
    "reviewCoverageComplete": True,
    "reviewedHeadSha": "abc1234",
}


class PolicyConstantsTests(unittest.TestCase):
    def test_centralized_constants_are_valid(self):
        constants = run_js("constants")
        self.assertEqual(constants["threshold"], 7)
        self.assertEqual(constants["maxFindings"], 6)
        self.assertEqual(constants["mergeSafe"], "safe_to_merge")


class CleanSkipTests(unittest.TestCase):
    def test_clean_exact_head_skips(self):
        result = run_js(
            "skip",
            review=make_review([]),
            state=make_persistent([], head_sha="abc1234"),
            options=dict(CLEAN_OPTS),
        )
        self.assertTrue(result["skip"])

    def test_actionable_review_never_skips(self):
        state = make_persistent([], head_sha="abc1234")
        cases = [
            make_review([issue_entry(n=0)]),
            make_review([], recommendation="merge_with_caution"),
            make_review([], recommendation="changes_required"),
            make_review([issue_entry(n=i) for i in range(6)]),
            make_review([], extra={"security_concerns": ["hardcoded credential"]}),
            make_review([], extra={"tool_errors": "upstream tool failed"}),
            make_review([], extra={"coverage_complete": ""}),
        ]
        for review in cases:
            with self.subTest(review=review):
                result = run_js("skip", review=review, state=state,
                                options=dict(CLEAN_OPTS))
                self.assertFalse(result["skip"])

    def test_actionable_improve_routes_to_repair(self):
        actionable = run_js(
            "disposition",
            review=make_review([issue_entry(n=0)]),
            raw="",
        )
        self.assertEqual(actionable["action"], "repair")
        clean = run_js(
            "disposition",
            review=make_review([]),
            raw="",
        )
        self.assertEqual(clean["action"], "merge")


class ExactHeadSafetyTests(unittest.TestCase):
    def test_stale_head_never_skips(self):
        result = run_js(
            "skip",
            review=make_review([]),
            state=make_persistent([], head_sha="abc1234"),
            options={"headMatches": False, "reviewCoverageComplete": True,
                     "reviewedHeadSha": "abc1234"},
        )
        self.assertFalse(result["skip"])

    def test_stale_persistent_state_never_skips(self):
        result = run_js(
            "skip",
            review=make_review([]),
            state=make_persistent([], head_sha="def5678"),
            options=dict(CLEAN_OPTS),
        )
        self.assertFalse(result["skip"])

    def test_placeholder_and_short_heads_never_skip(self):
        for bad in ("unknown", "a", "123", "abc"):
            with self.subTest(bad=bad):
                result = run_js(
                    "skip",
                    review=make_review([]),
                    state=make_persistent([], head_sha=bad),
                    options={"headMatches": True, "reviewCoverageComplete": True,
                             "reviewedHeadSha": bad},
                )
                self.assertFalse(result["skip"])

    def test_missing_reviewed_head_throws_fail_closed(self):
        result = run_skip_raw(
            json.dumps(make_review([])),
            json.dumps(make_persistent([], head_sha="abc1234")),
            {"headMatches": True, "reviewCoverageComplete": True},
        )
        self.assertIn("threw", result)

    def test_missing_key_issues_throws_fail_closed(self):
        result = run_skip_raw(
            json.dumps({"merge_recommendation": "safe_to_merge"}),
            json.dumps(make_persistent([], head_sha="abc1234")),
            dict(CLEAN_OPTS),
        )
        self.assertIn("threw", result)


class SplitEnvelopeTests(unittest.TestCase):
    def test_nested_findings_merge_fail_closed(self):
        nested = {"review": make_review([issue_entry(n=0)]),
                  "coverage_complete": True}
        merged = run_js("unwrap", review=nested)
        self.assertEqual(len(merged["key_issues_to_review"]), 1)

    def test_nested_blocking_signals_merge_fail_closed(self):
        split_security = {
            "review": make_review([], extra={"security_concerns": ["leak"]})}
        self.assertTrue(run_js("security", review=split_security))
        split_tool = {
            "review": make_review([], extra={"errors": "upstream tool failed"})}
        self.assertTrue(run_js("tool", review=split_tool))
        split_coverage = {
            "review": make_review([], extra={"coverage_complete": ""})}
        self.assertTrue(run_js("coverage", review=split_coverage))

    def test_skipped_clean_marker_is_machine_readable(self):
        skipped = ('{"payload": {"code_suggestions": []}, '
                   '"continuum": {"improve_skipped_clean": true}}')
        plain = '{"payload": {"code_suggestions": []}}'
        self.assertTrue(run_js("skipped", raw=skipped))
        self.assertFalse(run_js("skipped", raw=plain))
        self.assertFalse(run_js("skipped", raw="not json"))


if __name__ == "__main__":
    unittest.main()
