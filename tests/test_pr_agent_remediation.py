"""Target-aware PR-Agent remediation disposition tests.

Covers the issue #402 truth table: recommendation strings are one input,
never the whole decision. Ticket obligations, security/tool/coverage
failures, code findings, metadata obligations and owner/policy holds each
produce a different deterministic next action.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
POLICY_MODULE = os.path.join(ROOT, ".github", "scripts", "pr_agent_policy.js")
SRC = os.path.join(ROOT, "src")


def _life():
    sys.path.insert(0, SRC)
    try:
        from continuum import pr_agent_lifecycle as life

        return life
    finally:
        sys.path.remove(SRC)


def run_js(expression, review=None, raw=None, state=None, options=None):
    env = os.environ.copy()
    env["POLICY_MODULE"] = POLICY_MODULE
    env["POLICY_REVIEW"] = json.dumps(review) if review is not None else ""
    env["POLICY_RAW"] = raw if raw is not None else ""
    env["POLICY_STATE"] = json.dumps(state) if state is not None else ""
    env["POLICY_OPTIONS"] = json.dumps(options or {})
    env["POLICY_EXPR"] = expression
    code = r"""
const policy = require(process.env.POLICY_MODULE);
const review = process.env.POLICY_REVIEW ? JSON.parse(process.env.POLICY_REVIEW) : null;
const raw = process.env.POLICY_RAW || '';
const state = process.env.POLICY_STATE ? JSON.parse(process.env.POLICY_STATE) : null;
const options = JSON.parse(process.env.POLICY_OPTIONS || '{}');
let result;
const expr = process.env.POLICY_EXPR;
if (expr === 'disposition') result = policy.reviewDisposition(review, raw);
else if (expr === 'ticket') result = policy.ticketComplianceState(review);
else if (expr === 'items') result = policy.normalizeActionableItems(review, raw).items;
else if (expr === 'target') result = policy.classifyRemediationTarget(options.text || '', { path: options.path || '' });
else if (expr === 'evidence') result = policy.buildMetadataEvidence(options.requirements || [], options.files || [], options);
else if (expr === 'replace') result = policy.replaceMetadataEvidence(options.body || '', options.evidence || '');
else if (expr === 'inputVersion') result = policy.reviewInputVersion(options.head, options.body || '');
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


def make_review(issues=None, recommendation="safe_to_merge", extra=None):
    review = {
        "key_issues_to_review": list(issues or []),
        "merge_recommendation": recommendation,
    }
    if extra:
        review.update(extra)
    return review


def issue_entry(text="Concrete code defect", path="src/app.py", n=0):
    entry = {
        "issue_header": f"Possible Bug {n}",
        "issue_content": text,
        "start_line": 10 + n,
        "end_line": 12 + n,
    }
    if path:
        entry["relevant_file"] = path
    return entry


def ticket(text):
    return {"ticket_compliance_check": [{"not_compliant_requirements": text}]}


class DispositionTruthTableTests(unittest.TestCase):
    def assert_action(self, review, action, raw=""):
        result = run_js("disposition", review=review, raw=raw)
        self.assertEqual(result["action"], action, result)
        return result

    def test_safe_to_merge_cannot_override_non_compliant_ticket(self):
        review = make_review(
            [],
            extra=ticket("Before/after examples are missing from the PR description"),
        )
        result = self.assert_action(review, "metadata")
        self.assertEqual(result["target"], "pr_description")
        self.assertFalse(result["ticketCompliance"]["verified"])

    def test_safe_to_merge_with_verified_compliance_merges(self):
        review = make_review(
            [],
            extra={"ticket_compliance_check": [{"not_compliant_requirements": "None"}]},
        )
        self.assert_action(review, "merge")

    def test_compliant_code_requirement_cannot_merge(self):
        review = make_review(
            [],
            extra=ticket("Implement the missing retry handling in code"),
        )
        result = self.assert_action(review, "repair")
        self.assertEqual(result["target"], "code")

    def test_owner_only_requirement_is_a_durable_hold(self):
        review = make_review(
            [],
            extra=ticket("Requires owner-only manual approval before merge"),
        )
        self.assert_action(review, "hold")

    def test_ci_requirement_waits_on_existing_path(self):
        review = make_review(
            [],
            extra=ticket("Required live E2E evidence missing"),
        )
        result = self.assert_action(review, "rereview")
        self.assertEqual(result["target"], "ci")

    def test_clean_caution_is_non_actionable_without_storm(self):
        review = make_review(
            [],
            recommendation="merge_with_caution",
            extra={
                "security_concerns": "No security concerns identified",
                "ticket_compliance_check": [{"not_compliant_requirements": "None"}],
            },
        )
        self.assert_action(review, "merge")

    def test_caution_with_code_findings_repairs(self):
        review = make_review(
            [issue_entry()],
            recommendation="merge_with_caution",
            extra={"security_concerns": "No security concerns identified"},
        )
        self.assert_action(review, "repair")

    def test_metadata_finding_without_path_corrects_description(self):
        review = make_review(
            [
                issue_entry(
                    "The PR description is missing the required before and after evidence",
                    path="",
                )
            ],
            recommendation="changes_required",
        )
        self.assert_action(review, "metadata")

    def test_policy_finding_holds_without_code_loop(self):
        review = make_review(
            [issue_entry("Requires a human owner decision for licensing", path="")],
            recommendation="changes_required",
        )
        self.assert_action(review, "hold")

    def test_security_tool_and_coverage_fail_closed(self):
        cases = [
            {"security_concerns": "hardcoded credential"},
            {"tool_errors": "upstream tool failed"},
            {"coverage_complete": False},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                result = run_js(
                    "disposition",
                    review=make_review([], extra=extra),
                )
                self.assertEqual(result["action"], "rereview")

    def test_malformed_or_conflicting_ticket_data_fails_closed(self):
        for value in ([{"unknown": "not checked"}], "unknown"):
            with self.subTest(value=value):
                result = run_js(
                    "disposition",
                    review=make_review([], extra={"ticket_compliance_check": value}),
                )
                self.assertEqual(result["action"], "rereview")
                self.assertIn("malformed", result["reason"])


class NormalizedItemTests(unittest.TestCase):
    def test_items_carry_identity_target_and_status(self):
        review = make_review(
            [
                issue_entry("Fix the null dereference", path="src/app.py", n=0),
                issue_entry(
                    "The PR description must document before and after behavior",
                    path="",
                    n=1,
                ),
            ]
        )
        items = run_js("items", review=review)
        self.assertEqual(items[0]["target"], "code")
        self.assertTrue(items[0]["blocking"])
        self.assertEqual(items[1]["target"], "pr_description")
        self.assertTrue(items[1]["id"])
        self.assertNotEqual(items[0]["id"], items[1]["id"])

    def test_advisory_improve_is_not_blocking(self):
        raw = (
            '{"payload": {"code_suggestions": ['
            '{"relevant_file": "src/app.py", "score": 9, '
            '"one_sentence_summary": "consider a nit"}]}}'
        )
        items = run_js("items", review=make_review([]), raw=raw)
        improve = [item for item in items if item["source"] == "improve"]
        self.assertTrue(improve)
        self.assertTrue(improve[0]["advisory"])
        self.assertFalse(improve[0]["blocking"])

    def test_target_classification_is_explicit(self):
        self.assertEqual(
            run_js("target", options={"text": "before/after evidence in the PR description"}),
            "pr_description",
        )
        self.assertEqual(
            run_js("target", options={"text": "requires owner decision"}),
            "policy",
        )
        self.assertEqual(
            run_js("target", options={"text": "token expired", "path": "src/a.py"}),
            "code",
        )
        self.assertEqual(
            run_js(
                "target",
                options={
                    "text": "the lock is released before and after the retry",
                    "path": "src/a.py",
                },
            ),
            "code",
        )


class MetadataEvidenceTests(unittest.TestCase):
    diff = (
        "diff --git a/docs/usage.md b/docs/usage.md\n"
        "--- a/docs/usage.md\n"
        "+++ b/docs/usage.md\n"
        "@@ -1 +1 @@\n"
        "-Before: 3 retries\n"
        "+After: 5 retries\n"
    )

    def test_before_after_comes_from_the_actual_diff(self):
        evidence = run_js(
            "evidence",
            options={
                "requirements": [
                    {"text": "The PR description needs before/after evidence"}
                ],
                "files": [
                    {
                        "filename": "docs/usage.md",
                        "patch": "@@ -1 +1 @@\n-Before: 3 retries\n+After: 5 retries",
                    }
                ],
                "head": "a" * 40,
            },
        )
        self.assertIn("Before: 3 retries", evidence)
        self.assertIn("After: 5 retries", evidence)
        self.assertIn("head=" + "a" * 40, evidence)

    def test_no_invented_examples_without_a_real_pair(self):
        evidence = run_js(
            "evidence",
            options={
                "requirements": [
                    {"text": "The PR description needs before/after evidence"}
                ],
                "files": [{"filename": "docs/usage.md", "patch": "@@ -1 +1 @@\n+new"}],
            },
        )
        self.assertNotIn("Before:", evidence)
        self.assertNotIn("After:", evidence)

    def test_replacement_preserves_protected_markers(self):
        body = (
            "<!-- continuum-task-context repo=o/r issue=7 -->\n"
            "<!-- continuum-task-snapshot-ref repo=o/r issue=7 generation=1 "
            "title-sha256=a body-sha256=b spec-sha256=c -->\n"
            "Closes #7"
        )
        evidence = run_js(
            "evidence",
            options={"requirements": [], "files": [], "head": "b" * 40},
        )
        updated = run_js("replace", options={"body": body, "evidence": evidence})
        self.assertIn("continuum-task-context", updated)
        self.assertIn("continuum-task-snapshot-ref", updated)
        self.assertIn("Closes #7", updated)
        self.assertIn("continuum-pr-body-evidence:v1", updated)

    def test_replacement_rewrites_an_existing_head_marked_block(self):
        existing = (
            "<!-- continuum-task-context repo=o/r issue=7 -->\n\n"
            "<!-- continuum-pr-body-evidence:v1 head=" + "a" * 40 + " -->\n"
            "stale evidence\n<!-- /continuum-pr-body-evidence -->\n\n"
            "Closes #7\n"
        )
        evidence = run_js(
            "evidence",
            options={"requirements": ["Show before/after evidence"], "files": [], "head": "c" * 40},
        )
        updated = run_js("replace", options={"body": existing, "evidence": evidence})
        self.assertNotIn("stale evidence", updated)
        self.assertEqual(updated.count("continuum-pr-body-evidence:v1"), 1)
        self.assertEqual(updated.count("<!-- /continuum-pr-body-evidence -->"), 1)
        self.assertIn("head=" + "c" * 40, updated)
        self.assertIn("Closes #7", updated)


class ReviewInputVersionTests(unittest.TestCase):
    def test_head_plus_body_digest_changes_with_description_only(self):
        head = "c" * 40
        first = run_js("inputVersion", options={"head": head, "body": "body A"})
        second = run_js("inputVersion", options={"head": head, "body": "body B"})
        same = run_js("inputVersion", options={"head": head, "body": "body A"})
        self.assertNotEqual(first, second)
        self.assertEqual(first, same)
        self.assertTrue(first.startswith(head + ":"))


class PythonParityTests(unittest.TestCase):
    def test_python_truth_table_matches_js(self):
        life = _life()
        review = make_review(
            [],
            extra=ticket("Before/after examples are missing from the PR description"),
        )
        self.assertEqual(life.review_disposition(review)["action"], "metadata")
        self.assertEqual(
            life.review_disposition(
                make_review([], extra=ticket("Requires owner-only approval"))
            )["action"],
            "hold",
        )
        self.assertEqual(
            life.review_disposition(
                make_review([issue_entry()], recommendation="merge_with_caution")
            )["action"],
            "repair",
        )

    def test_python_review_input_version(self):
        sys.path.insert(0, SRC)
        try:
            from continuum import pr_body
        finally:
            sys.path.remove(SRC)
        head = "d" * 40
        self.assertEqual(
            pr_body.review_input_version(head, "body A"),
            pr_body.review_input_version(head, "body A"),
        )
        self.assertNotEqual(
            pr_body.review_input_version(head, "body A"),
            pr_body.review_input_version(head, "body B"),
        )


if __name__ == "__main__":
    unittest.main()


class WorkflowWiringTests(unittest.TestCase):
    def read(self, relative):
        with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
            return handle.read()

    def test_merge_gate_requires_review_input_version(self):
        body = self.read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("continuum/pr-agent-review-input", body)
        self.assertIn("expectedInputVersion = headSha + ':' + bodyDigest", body)
        self.assertIn("createHash('sha256')", body)
        self.assertGreaterEqual(
            body.count("continuum/pr-agent-review-input"),
            2,
            "both verified-gate and recompute paths must check review input",
        )

    def test_metadata_correction_is_same_head_and_dispatches_fresh_review(self):
        body = self.read(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("Correct required PR description evidence on the same HEAD", body)
        self.assertIn("metadata_correction", body)
        self.assertIn("createWorkflowDispatch", body)
        self.assertIn("includes('<!-- continuum-pr-body-evidence:v1 head='", body)
        self.assertIn("continuum/pr-agent-review-input", body)

    def test_child_generator_uses_source_grounded_helper(self):
        body = self.read(".github/workflows/continuum-consumer-child-worker.yml")
        self.assertIn("src/continuum/pr_body.py", body)
        self.assertIn("--child-accepted", body)
        self.assertIn("continuum-task-snapshot-ref", body)
        self.assertIn("continuum-child-accepted", body)
