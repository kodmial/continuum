"""Regression tests for full-PR evidence validation of PR-Agent findings.

The incident behind these tests is a review chunk being mistaken for the whole
pull request, which produced endless "file is missing from the PR" repairs on
files that were present in the full PR diff or the current tree.
"""

from __future__ import annotations

import copy
import unittest

from continuum.pr_agent_evidence import (
    EvidenceError,
    build_manifest,
    classify_finding,
    manifest_from_dict,
    manifest_to_dict,
    render_prompt,
    validate_items,
    validate_review,
)

HEAD = "a" * 40

INCIDENT_FILES = [
    ".env.example",
    ".github/workflows/aa-self-proving-qualification.yml",
    "src/aa/time_policy.py",
    "src/aa/conversation/retrieval_node.py",
    "src/aa/conversation/turn_budget.py",
    "src/aa/conversation/turn_pipeline.py",
    "src/aa/planner_node.py",
    "src/aa/verifier.py",
    "src/aa/semantic_work_plan.py",
]

TREE_FILES = INCIDENT_FILES + [
    "README.md",
    "src/aa/unchanged_helper.py",
    "src/other/config.py",
]


def manifest(*, files=None, tree=None, complete=True, truncated=False):
    entries = [{"filename": path, "status": "modified"} for path in (files or INCIDENT_FILES)]
    return build_manifest(
        HEAD,
        entries,
        tree if tree is not None else TREE_FILES,
        complete=complete,
        truncated=truncated,
        pages=1,
    )


def finding(title, content, path=""):
    return {
        "relevant_file": path,
        "issue_header": title,
        "issue_content": content,
        "start_line": 1,
        "end_line": 2,
    }


class ManifestTests(unittest.TestCase):
    def test_large_pr_manifest_is_complete_and_digest_stable(self):
        files = [f"src/runtime/file_{index}.py" for index in range(41)]
        value = manifest(files=files, tree=files)
        self.assertEqual(len(value.files), 41)
        self.assertTrue(value.complete)
        self.assertFalse(value.truncated)
        self.assertEqual(value.digest, manifest(files=files, tree=files).digest)
        payload = manifest_to_dict(value)
        self.assertEqual(payload["file_count"], 41)
        restored = manifest_from_dict(payload)
        self.assertEqual(restored.digest, value.digest)
        self.assertEqual(restored.head_sha, HEAD)

    def test_manifest_digest_mismatch_fails_closed(self):
        payload = manifest_to_dict(manifest())
        payload["digest"] = "0" * 64
        with self.assertRaises(EvidenceError):
            manifest_from_dict(payload)

    def test_unparseable_or_inconsistent_manifests_fail_closed(self):
        base = manifest_to_dict(manifest())
        cases = [
            {},
            {"schema_version": 999, "files": [], "tree_paths": []},
            {**base, "files": "not-a-list"},
            {**base, "tree_paths": "not-a-list"},
            {**base, "file_count": len(base["files"]) + 1},
            {**base, "head_sha": "not-a-sha"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(EvidenceError):
                    manifest_from_dict(payload)

    def test_prompt_distinguishes_full_pr_chunk_and_repair_diff(self):
        prompt = render_prompt(manifest())
        self.assertIn("FULL PR DIFF", prompt)
        self.assertIn("REVIEW CHUNK", prompt)
        self.assertIn("REPAIR-ONLY DIFF", prompt)
        self.assertIn("src/aa/time_policy.py", prompt)
        self.assertIn(".env.example", prompt)
        self.assertNotIn("INCOMPLETE OR TRUNCATED", prompt)

    def test_truncated_manifest_requires_verification_and_never_filters(self):
        truncated = manifest(complete=False, truncated=True)
        self.assertIn("INCOMPLETE OR TRUNCATED", render_prompt(truncated))
        with self.assertRaises(EvidenceError):
            validate_review({"key_issues_to_review": []}, truncated)

    def test_rename_previous_path_is_part_of_the_pr(self):
        value = build_manifest(
            HEAD,
            [{
                "filename": "src/aa/new_time_policy.py",
                "previous_filename": "src/aa/time_policy.py",
                "status": "renamed",
            }],
            ["src/aa/new_time_policy.py"],
        )
        assessment = classify_finding(
            finding(
                "Missing Core Implementation",
                "src/aa/time_policy.py is not included in the PR diff.",
            ),
            value,
        )
        self.assertTrue(assessment.drop)
        self.assertEqual(assessment.reason, "contradicted_full_pr_manifest")


class AbsenceClassificationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = manifest()

    def test_missing_implementation_for_file_in_manifest_is_dropped(self):
        cases = [
            finding(
                "Missing Core Implementation",
                "The PR does not include `src/aa/time_policy.py` in the diff, so the "
                "time policy implementation cannot be verified.",
            ),
            finding(
                "Missing Runtime Changes",
                "src/aa/conversation/turn_budget.py and "
                "src/aa/conversation/turn_pipeline.py are absent from the PR.",
            ),
            finding(
                "Missing Runtime Changes",
                "`src/aa/conversation/retrieval_node.py` is not part of the pull request.",
            ),
        ]
        for item in cases:
            with self.subTest(title=item["issue_header"]):
                assessment = classify_finding(item, self.manifest)
                self.assertTrue(assessment.drop, assessment)
                self.assertEqual(assessment.reason, "contradicted_full_pr_manifest")

    def test_env_example_missing_from_repair_diff_is_dropped(self):
        # The repair-only diff is not the full base..head PR diff. An unchanged
        # previously corrected file is still current and still part of the PR.
        item = finding(
            "Excessive Transport Timeout",
            ".env.example is missing from the repair diff, so the transport timeout "
            "was not corrected. It should include a longer timeout.",
        )
        assessment = classify_finding(item, self.manifest)
        self.assertTrue(assessment.drop)
        self.assertEqual(assessment.scope, "repair")

    def test_multiline_chunk_scope_claim_is_dropped(self):
        item = finding(
            "Missing Runtime Changes",
            "The review cannot verify the implementation.\n\n"
            "- src/aa/time_policy.py is not visible in this review chunk\n"
            "- planner_node.py is absent from the current context\n"
            "- verifier.py was not shown in the provided diff portion",
        )
        assessment = classify_finding(item, self.manifest)
        self.assertTrue(assessment.drop)
        self.assertEqual(assessment.scope, "chunk")

    def test_genuine_missing_file_still_blocks(self):
        item = finding(
            "Missing Core Implementation",
            "src/aa/not_in_the_pr.py is missing from the PR and does not exist in the tree.",
        )
        assessment = classify_finding(item, self.manifest)
        self.assertFalse(assessment.drop)
        self.assertEqual(assessment.reason, "absent_or_unverifiable_path")

    def test_in_file_content_absence_remains_actionable(self):
        cases = [
            finding(
                "Missing lock",
                "`src/aa/time_policy.py` is missing the lock around the clock read.",
            ),
            finding(
                "Unimplemented branch",
                "src/aa/time_policy.py is missing validation for the failure path.",
            ),
            finding(
                "Not implemented",
                "The retry handling in src/aa/time_policy.py is not implemented.",
            ),
        ]
        for item in cases:
            with self.subTest(title=item["issue_header"]):
                assessment = classify_finding(item, self.manifest)
                self.assertFalse(assessment.drop, assessment)
                self.assertEqual(
                    assessment.reason, "changed_file_content_claim"
                )

    def test_bare_path_absence_claim_is_structural(self):
        for content in (
            "src/aa/time_policy.py is missing.",
            "src/aa/time_policy.py is not in the PR diff.",
        ):
            with self.subTest(content=content):
                item = finding("Missing runtime file", content)
                assessment = classify_finding(item, self.manifest)
                self.assertTrue(assessment.drop)
                self.assertEqual(
                    assessment.reason, "contradicted_full_pr_manifest"
                )

    def test_real_security_defect_is_never_filtered(self):
        item = finding(
            "Credential leak",
            "A hardcoded credential is missing from src/aa/time_policy.py; the file "
            "is not included in the PR diff and the secret may leak.",
        )
        assessment = classify_finding(item, self.manifest)
        self.assertFalse(assessment.drop)
        self.assertEqual(assessment.reason, "security_protected")

    def test_finding_without_absence_claim_is_kept(self):
        item = finding(
            "Race condition",
            "src/aa/time_policy.py reads the clock before acquiring the lock, so two "
            "concurrent turns can exceed the budget.",
        )
        assessment = classify_finding(item, self.manifest)
        self.assertFalse(assessment.drop)
        self.assertEqual(assessment.reason, "no_absence_claim")

    def test_ambiguous_basename_requires_targeted_verification(self):
        value = build_manifest(
            HEAD,
            [
                {"filename": "src/one/config.py"},
                {"filename": "src/two/config.py"},
            ],
            ["src/one/config.py", "src/two/config.py"],
        )
        item = finding(
            "Missing configuration",
            "config.py is not included in the PR diff.",
        )
        assessment = classify_finding(item, value)
        self.assertFalse(assessment.drop)
        self.assertTrue(assessment.ambiguous)
        self.assertEqual(assessment.reason, "ambiguous_path_alias")

    def test_tree_only_existence_claim_is_contradicted(self):
        value = manifest(
            files=["src/aa/time_policy.py"],
            tree=TREE_FILES + ["src/aa/unchanged_helper.py"],
        )
        item = finding(
            "Missing implementation",
            "src/aa/unchanged_helper.py is missing from the repository.",
        )
        assessment = classify_finding(item, value)
        self.assertTrue(assessment.drop)
        self.assertEqual(assessment.reason, "contradicted_current_tree")

    def test_tree_only_diff_claim_remains_for_targeted_verification(self):
        value = manifest(files=["src/aa/time_policy.py"], tree=TREE_FILES)
        item = finding(
            "Unchanged helper not in diff",
            "src/aa/unchanged_helper.py is not in the PR diff.",
        )
        assessment = classify_finding(item, value)
        self.assertFalse(assessment.drop)
        self.assertEqual(assessment.reason, "tree_only_diff_scope")

    def test_mixed_present_and_absent_paths_keeps_actionable_finding(self):
        item = finding(
            "Missing runtime",
            "src/aa/time_policy.py is in the diff, but src/aa/really_missing.py is "
            "missing from the PR.",
        )
        assessment = classify_finding(item, self.manifest)
        self.assertFalse(assessment.drop)


class ReviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = manifest()

    def test_review_filters_only_contradicted_structural_claims(self):
        review = {
            "key_issues_to_review": [
                finding(
                    "Missing Core Implementation",
                    "src/aa/time_policy.py is not included in the PR diff.",
                ),
                finding(
                    "Race condition",
                    "src/aa/time_policy.py updates the budget without a lock.",
                ),
            ],
            "merge_recommendation": "changes_required",
            "security_concerns": "No security concerns identified",
        }
        sanitized, report = validate_review(review, self.manifest, expected_head=HEAD)
        self.assertEqual(len(sanitized["key_issues_to_review"]), 1)
        self.assertIn("lock", sanitized["key_issues_to_review"][0]["issue_content"])
        self.assertEqual(report["unsupported_count"], 1)
        self.assertEqual(report["max_key_issues_count"], 2)
        self.assertFalse(report["ambiguous"])
        self.assertEqual(report["dropped"][0]["reason"], "contradicted_full_pr_manifest")

    def test_review_split_envelope_is_filtered_on_both_sides(self):
        review = {
            "review": {
                "key_issues_to_review": [
                    finding("Missing", "planner_node.py is missing from this chunk."),
                ]
            },
            "key_issues_to_review": [
                finding("Missing", "verifier.py is absent from the repair diff."),
            ],
            "coverage_complete": True,
        }
        sanitized, report = validate_review(review, self.manifest)
        self.assertEqual(sanitized["review"]["key_issues_to_review"], [])
        self.assertEqual(sanitized["key_issues_to_review"], [])
        self.assertEqual(report["unsupported_count"], 2)
        self.assertEqual(report["max_key_issues_count"], 1)

    def test_stale_manifest_head_fails_closed(self):
        with self.assertRaises(EvidenceError):
            validate_review(
                {"key_issues_to_review": []},
                self.manifest,
                expected_head="b" * 40,
            )

    def test_incomplete_review_coverage_is_not_changed_by_validation(self):
        review = {
            "key_issues_to_review": [],
            "coverage_complete": False,
            "merge_recommendation": "safe_to_merge",
        }
        sanitized, report = validate_review(review, self.manifest)
        self.assertFalse(sanitized["coverage_complete"])
        self.assertEqual(report["unsupported_count"], 0)

    def test_repair_batch_items_are_filtered(self):
        items = [
            {
                "source": "review",
                "finding": finding(
                    "Missing Core Implementation",
                    "src/aa/time_policy.py is missing from the repair diff.",
                ),
            },
            {
                "source": "review",
                "finding": finding(
                    "Genuine defect",
                    "src/aa/time_policy.py leaks a lock when the clock read fails.",
                ),
            },
        ]
        kept, report = validate_items(items, self.manifest)
        self.assertEqual(len(kept), 1)
        self.assertEqual(report["unsupported_count"], 1)
        self.assertIn("leaks a lock", kept[0]["finding"]["issue_content"])

    def test_deep_copy_does_not_mutate_input(self):
        review = {
            "key_issues_to_review": [
                finding(
                    "Missing",
                    "src/aa/time_policy.py is not included in the PR diff.",
                )
            ]
        }
        original = copy.deepcopy(review)
        sanitized, _ = validate_review(review, self.manifest)
        self.assertEqual(review, original)
        self.assertIsNot(sanitized, review)


if __name__ == "__main__":
    unittest.main()
