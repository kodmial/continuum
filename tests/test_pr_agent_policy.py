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

    def test_clean_review_with_qualifying_improve_routes_to_repair(self):
        qualifying = (
            '{"payload": {"code_suggestions": ['
            '{"relevant_file": "src/app.py", "score": 9}]}}'
        )
        result = run_js(
            "disposition",
            review=make_review([]),
            raw=qualifying,
        )
        self.assertEqual(result["action"], "repair")
        self.assertEqual(result["qualifyingSuggestionCount"], 1)

    def test_clean_review_with_sub_threshold_improve_reaches_merge(self):
        low = (
            '{"payload": {"code_suggestions": ['
            '{"relevant_file": "src/app.py", "score": 3}]}}'
        )
        result = run_js(
            "disposition",
            review=make_review([]),
            raw=low,
        )
        self.assertEqual(result["action"], "merge")
        self.assertEqual(result["qualifyingSuggestionCount"], 0)


class SignalMatrixTests(unittest.TestCase):
    def test_clean_prose_carries_no_blocking_signal(self):
        clean_security = make_review([], extra={"security_concerns": "No security concerns found"})
        self.assertFalse(run_js("security", review=clean_security))
        ordinary_errors = make_review([], extra={"errors": "2 lint errors noted in diff"})
        self.assertFalse(run_js("tool", review=ordinary_errors))
        no_coverage_keys = make_review([])
        self.assertFalse(run_js("coverage", review=no_coverage_keys))

    def test_blocking_prose_carries_signal(self):
        blocking_security = make_review([], extra={"security_concerns": "hardcoded credential"})
        self.assertTrue(run_js("security", review=blocking_security))
        tool_failure = make_review([], extra={"errors": "upstream tool failed"})
        self.assertTrue(run_js("tool", review=tool_failure))
        empty_coverage = make_review([], extra={"coverage_complete": ""})
        self.assertTrue(run_js("coverage", review=empty_coverage))


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

    def test_active_persistent_finding_never_skips(self):
        state = make_persistent(
            [{"id": "pra-1", "state": "ACTIVE"}], head_sha="abc1234")
        result = run_js(
            "skip",
            review=make_review([]),
            state=state,
            options=dict(CLEAN_OPTS),
        )
        self.assertFalse(result["skip"])
        self.assertIn("ACTIVE", result["reason"])

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

    def test_missing_reviewed_head_runs_improve_fail_closed(self):
        result = run_skip_raw(
            json.dumps(make_review([])),
            json.dumps(make_persistent([], head_sha="abc1234")),
            {"headMatches": True, "reviewCoverageComplete": True},
        )
        self.assertFalse(result["skip"])
        self.assertNotIn("threw", result)

    def test_missing_key_issues_runs_improve_fail_closed(self):
        result = run_skip_raw(
            json.dumps({"merge_recommendation": "safe_to_merge"}),
            json.dumps(make_persistent([], head_sha="abc1234")),
            dict(CLEAN_OPTS),
        )
        self.assertFalse(result["skip"])
        self.assertNotIn("threw", result)

    def test_missing_merge_recommendation_runs_improve_fail_closed(self):
        result = run_skip_raw(
            json.dumps({"key_issues_to_review": []}),
            json.dumps(make_persistent([], head_sha="abc1234")),
            dict(CLEAN_OPTS),
        )
        self.assertFalse(result["skip"])
        self.assertNotIn("threw", result)

    def test_invalid_review_payload_runs_improve_fail_closed(self):
        for bad in ('{"nope": true}', 'null', '"not-an-object"'):
            with self.subTest(bad=bad):
                result = run_skip_raw(
                    bad,
                    json.dumps(make_persistent([], head_sha="abc1234")),
                    dict(CLEAN_OPTS),
                )
                self.assertFalse(result["skip"])
                self.assertNotIn("threw", result)


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

    def test_outer_and_nested_findings_concatenate_fail_closed(self):
        split = {
            "key_issues_to_review": [issue_entry(n=0)],
            "merge_recommendation": "safe_to_merge",
            "review": make_review([issue_entry(n=1)]),
        }
        merged = run_js("unwrap", review=split)
        self.assertEqual(len(merged["key_issues_to_review"]), 2)

    def test_conflicting_recommendation_merges_most_restrictive(self):
        split = {
            "merge_recommendation": "safe_to_merge",
            "key_issues_to_review": [],
            "review": make_review([], recommendation="changes_required"),
        }
        merged = run_js("unwrap", review=split)
        self.assertEqual(merged["merge_recommendation"], "changes_required")

    def test_split_envelope_never_skips_with_actionable_side(self):
        split = {
            "coverage_complete": True,
            "review": make_review([issue_entry(n=0)]),
        }
        result = run_js(
            "skip",
            review=split,
            state=make_persistent([], head_sha="abc1234"),
            options=dict(CLEAN_OPTS),
        )
        self.assertFalse(result["skip"])

    def test_skipped_clean_marker_is_machine_readable(self):
        skipped = ('{"payload": {"code_suggestions": []}, '
                   '"continuum": {"improve_skipped_clean": true}}')
        plain = '{"payload": {"code_suggestions": []}}'
        self.assertTrue(run_js("skipped", raw=skipped))
        self.assertFalse(run_js("skipped", raw=plain))
        self.assertFalse(run_js("skipped", raw="not json"))

    def test_skipped_marker_with_unparseable_line_fails_closed(self):
        skipped = ('{"payload": {"code_suggestions": []}, '
                   '"continuum": {"improve_skipped_clean": true}}\nnot json')
        self.assertFalse(run_js("skipped", raw=skipped))


class GenericToolConjunctionTests(unittest.TestCase):
    def test_failure_word_without_tool_stays_clean(self):
        for text in (
            "2 checks failed in diff",
            "errors: missing timeout handling",
            "traceback noted in code comment",
            "exception handling added in diff",
        ):
            with self.subTest(text=text):
                review = make_review([], extra={"errors": text})
                self.assertFalse(run_js("tool", review=review))

    def test_tool_plus_failure_blocks(self):
        for text in (
            "upstream tool failed",
            "upstream tools failed",
            "tool timeout contacting model",
            "tool unavailable during review",
        ):
            with self.subTest(text=text):
                review = make_review([], extra={"errors": text})
                self.assertTrue(run_js("tool", review=review))

    def test_explicit_non_string_error_blocks_skip(self):
        for value in (True, 1, {"count": 1}, [True], ["upstream tool failed"]):
            with self.subTest(value=value):
                review = make_review([], extra={"errors": value})
                self.assertTrue(run_js("tool", review=review))
                result = run_js(
                    "skip",
                    review=review,
                    state=make_persistent([], head_sha="abc1234"),
                    options=dict(CLEAN_OPTS),
                )
                self.assertFalse(result["skip"])

    def test_clean_non_string_error_stays_clean(self):
        for value in (False, 0, None, [], {}):
            with self.subTest(value=value):
                review = make_review([], extra={"errors": value})
                self.assertFalse(run_js("tool", review=review))


class ReviewSignalRepairTests(unittest.TestCase):
    def test_infrastructure_tool_failure_blocks_skip(self):
        # Genuine tool failures that never use the literal word "tool"
        # (e.g. "API timeout contacting model") must fail closed instead
        # of authorizing a clean skip.
        for text in (
            "API timeout contacting model",
            "model provider unavailable",
            "upstream timeout contacting model",
        ):
            with self.subTest(text=text):
                review = make_review([], extra={"errors": text})
                self.assertTrue(run_js("tool", review=review))
                result = run_js(
                    "skip",
                    review=review,
                    state=make_persistent([], head_sha="abc1234"),
                    options=dict(CLEAN_OPTS),
                )
                self.assertFalse(result["skip"])

    def test_empty_truncation_flag_fails_closed(self):
        # An explicit truncation key with an empty value is not evidence
        # of complete coverage: `{"truncated": ""}` must never authorize
        # an improve skip, like null/undefined.
        for key in ("truncated", "partial", "incomplete"):
            for empty in ("", "   "):
                with self.subTest(key=key, empty=repr(empty)):
                    review = make_review([], extra={key: empty})
                    self.assertTrue(run_js("coverage", review=review))
                    result = run_js(
                        "skip",
                        review=review,
                        state=make_persistent([], head_sha="abc1234"),
                        options=dict(CLEAN_OPTS),
                    )
                    self.assertFalse(result["skip"])

    def test_null_sha_placeholder_never_skips(self):
        # Matching all-zero placeholders must never satisfy the exact-HEAD
        # skip check even though both sides are hex of plausible length.
        for zero in ("0000000", "0" * 40, "0" * 64):
            with self.subTest(zero=zero):
                options = dict(CLEAN_OPTS)
                options["reviewedHeadSha"] = zero
                result = run_js(
                    "skip",
                    review=make_review([]),
                    state=make_persistent([], head_sha=zero),
                    options=options,
                )
                self.assertFalse(result["skip"])
        # A genuine non-zero abbreviation still skips (no over-correction).
        result = run_js(
            "skip",
            review=make_review([]),
            state=make_persistent([], head_sha="abc1234"),
            options=dict(CLEAN_OPTS),
        )
        self.assertTrue(result["skip"])


class ConflictingNonSafeRecommendationTests(unittest.TestCase):
    def test_caution_vs_changes_required_keeps_most_restrictive(self):
        outer_caution = {
            "merge_recommendation": "merge_with_caution",
            "key_issues_to_review": [],
            "review": make_review([], recommendation="changes_required"),
        }
        self.assertEqual(
            run_js("unwrap", review=outer_caution)["merge_recommendation"],
            "changes_required",
        )
        outer_changes = {
            "merge_recommendation": "changes_required",
            "key_issues_to_review": [],
            "review": make_review([], recommendation="merge_with_caution"),
        }
        self.assertEqual(
            run_js("unwrap", review=outer_changes)["merge_recommendation"],
            "changes_required",
        )


class AutoMergeImproveCoverageLegTests(unittest.TestCase):
    def _gate(self, review, raw):
        skipped = run_js("skipped", raw=raw)
        try:
            disposition = run_js("disposition", review=review, raw=raw)
        except Exception:
            return {"green": False, "reason": "incomplete improve coverage"}
        if not raw.strip() and not skipped:
            return {"green": False, "reason": "incomplete improve coverage"}
        if skipped:
            qualifying = disposition["qualifyingSuggestionCount"]
            if qualifying > 0:
                return {
                    "green": False,
                    "reason": "improve was skipped but qualifying suggestions remain",
                }
        if disposition["action"] != "merge":
            return {"green": False, "reason": disposition["reason"]}
        return {"green": True, "reason": "review portion satisfied"}

    def test_skipped_clean_empty_payload_greens(self):
        skipped = ('{"payload": {"code_suggestions": []}, '
                   '"continuum": {"improve_skipped_clean": true}}')
        self.assertTrue(run_js("skipped", raw=skipped))
        gate = self._gate(make_review([]), skipped)
        self.assertTrue(gate["green"])

    def test_empty_improve_without_marker_fails_closed(self):
        self.assertFalse(run_js("skipped", raw=""))
        gate = self._gate(make_review([]), "")
        self.assertFalse(gate["green"])
        self.assertIn("incomplete improve coverage", gate["reason"])

    def test_skipped_marker_with_qualifying_suggestions_fails_closed(self):
        skipped_qualifying = (
            '{"payload": {"code_suggestions": ['
            '{"relevant_file": "src/app.py", "score": 9}]}, '
            '"continuum": {"improve_skipped_clean": true}}'
        )
        self.assertTrue(run_js("skipped", raw=skipped_qualifying))
        gate = self._gate(make_review([]), skipped_qualifying)
        self.assertFalse(gate["green"])
        self.assertIn("qualifying", gate["reason"])

    def test_truncated_improve_payload_fails_closed(self):
        with self.assertRaises(Exception):
            run_js("disposition", review=make_review([]), raw="not json")
        with self.assertRaises(Exception):
            run_js(
                "disposition",
                review=make_review([]),
                raw='{"payload": {"code_suggestions": [',
            )


class PrAgentWorkflowContractTests(unittest.TestCase):
    def _read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_normalize_runs_only_on_successful_improve_output(self):
        body = self._read(".github", "workflows", "continuum-pr-agent.yml")
        self.assertIn("Normalize persistent improve presentation", body)
        self.assertIn("steps.improve_output.outcome == 'success'", body)

    def test_clean_skip_producer_writes_marked_payload(self):
        body = self._read(".github", "workflows", "continuum-pr-agent.yml")
        self.assertIn("steps.improve_gate.outputs.skip_improve != 'true'", body)
        self.assertIn("steps.improve_gate.outputs.skip_improve == 'true'", body)
        self.assertIn("improve_skipped_clean", body)
        merge = self._read(
            ".github", "workflows", "continuum-pr-agent-auto-merge.yml"
        )
        self.assertIn("isSkippedCleanImprovePayload", merge)
        self.assertIn("incomplete improve coverage: failing closed", merge)
        # The merge gate consumes the skip decision itself, not just the
        # marker: a skipped-clean payload is re-validated through the
        # clean-review skip policy with the explicit review-coverage flag.
        self.assertIn("policy.isCleanReviewForImproveSkip(", merge)
        self.assertIn("reviewCoverageComplete", merge)
        self.assertIn("improve skip not justified", merge)

    def test_truncated_improve_payload_has_explicit_coverage_leg(self):
        merge = self._read(
            ".github", "workflows", "continuum-pr-agent-auto-merge.yml"
        )
        self.assertIn("truncated/invalid improve payload", merge)
        self.assertIn("incomplete improve coverage", merge)


if __name__ == "__main__":
    unittest.main()
