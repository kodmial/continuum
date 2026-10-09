"""Parity proof for the #304 provider-adapter extraction.

Every extracted common responsibility has executable before/after parity
proof for both providers here. The CodeRabbit path
(``continuum-auto-merge.yml``) and the PR-Agent path
(``continuum-pr-agent-auto-merge.yml``) share one canonical
implementation in ``src/continuum/merge_lifecycle.py`` (mirrored at
runtime by ``.github/scripts/merge_lifecycle.js``); provider-specific
semantics stay in ``src/continuum/coderabbit_adapter.py`` and
``src/continuum/pr_agent_lifecycle.py`` respectively.

Consumer contract freeze: these tests never require stub, input,
secret, variable, label, permission, or workflow-name changes.
"""

import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from continuum import branch_safety  # noqa: E402
from continuum import coderabbit_adapter as rabbit  # noqa: E402
from continuum import conflict_repair as repair  # noqa: E402
from continuum import merge_lifecycle as life  # noqa: E402
from continuum import pr_agent_lifecycle as pragent  # noqa: E402

sys.path.remove(SRC)

GENERIC_MERGE = ".github/workflows/continuum-auto-merge.yml"
PR_AGENT_MERGE = ".github/workflows/continuum-pr-agent-auto-merge.yml"
SHARED_JS = ".github/scripts/merge_lifecycle.js"


def read_repo(path):
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
        return handle.read()


HEAD = "abcdef1234567890abcdef1234567890abcdef12"
OTHER_HEAD = "1234567890abcdef1234567890abcdef12345678"


def ci_run(status="completed", conclusion="success"):
    return {"status": status, "conclusion": conclusion}


class ProviderContractTests(unittest.TestCase):
    def test_only_known_providers_and_no_both_mode(self):
        for provider in ("none", "coderabbit", "pr-agent"):
            self.assertEqual(life.resolve_review_provider(provider), provider)
        self.assertEqual(life.resolve_review_provider(""), "none")
        with self.assertRaises(life.MergeLifecycleError):
            life.resolve_review_provider("both")
        with self.assertRaises(life.MergeLifecycleError):
            life.resolve_review_provider("coderabbit+pr-agent")

    def test_no_new_controller_authority_markers(self):
        # The common module is a pure decision library: it must not call
        # workflow dispatch, merge, or run APIs. Docstrings may name the
        # call sites; executable code must not contain them.
        source = read_repo("src/continuum/merge_lifecycle.py")
        code_lines = [
            line
            for line in source.splitlines()
            if line.strip() and not line.strip().startswith(("#", '"""', "'''" , "*", "-", ":"))
        ]
        code = "\n".join(code_lines)
        self.assertNotIn("workflow_dispatch", code)
        self.assertNotIn("github.rest", code)
        self.assertNotIn("gh api", code)
        self.assertNotIn("createWorkflowDispatch", code)


class NoAutoMergeTests(unittest.TestCase):
    def test_block_label_constant_is_shared(self):
        self.assertEqual(life.AUTO_MERGE_BLOCK_LABEL, "no-auto-merge")
        for path in (GENERIC_MERGE, PR_AGENT_MERGE):
            self.assertIn("AUTO_MERGE_BLOCK_LABEL = 'no-auto-merge'", read_repo(path))

    def test_block_label_blocks_both_providers(self):
        for labels in (["no-auto-merge"], [{"name": "no-auto-merge"}]):
            self.assertTrue(life.is_merge_blocked(labels))
        self.assertFalse(life.is_merge_blocked([]))
        self.assertFalse(life.is_merge_blocked([{"name": "other"}]))


class MainSyncTests(unittest.TestCase):
    def test_continuum_pins_are_always_merge_critical(self):
        self.assertFalse(
            life.is_non_merge_critical_main_path(
                ".github/workflows/continuum-opencode.yml"
            )
        )

    def test_docs_and_metadata_paths_are_non_critical(self):
        for path in (
            "docs/guide.md",
            "README.md",
            ".github/ISSUE_TEMPLATE.md",
            "LICENSE",
            ".gitignore",
            "CHANGELOG.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(life.is_non_merge_critical_main_path(path))

    def test_source_paths_are_critical(self):
        for path in ("src/continuum/merge_lifecycle.py", "install.sh"):
            self.assertFalse(life.is_non_merge_critical_main_path(path))

    def test_up_to_date_needs_no_sync(self):
        decision = life.decide_main_sync(0, [])
        self.assertEqual(
            (decision["required"], decision["reason"]), (False, "up-to-date")
        )

    def test_non_critical_delta_needs_no_sync(self):
        decision = life.decide_main_sync(3, ["docs/guide.md"])
        self.assertEqual(
            (decision["required"], decision["reason"]),
            (False, "non-merge-critical-main-delta"),
        )

    def test_critical_delta_requires_sync(self):
        decision = life.decide_main_sync(2, ["src/continuum/x.py"])
        self.assertEqual(
            (decision["required"], decision["reason"]),
            (True, "merge-critical-main-delta"),
        )

    def test_unknown_delta_fails_closed_to_sync(self):
        decision = life.decide_main_sync(2, [])
        self.assertEqual(
            (decision["required"], decision["reason"]), (True, "unknown-main-delta")
        )

    def test_both_workflows_share_the_same_path_table(self):
        for path in (GENERIC_MERGE, PR_AGENT_MERGE):
            body = read_repo(path)
            self.assertIn("function isNonMergeCriticalMainPath", body)
            self.assertIn(".github/workflows/continuum-", body)
            self.assertIn("non-merge-critical-main-delta", body)


class ConflictPlumbingTests(unittest.TestCase):
    def test_canonical_constants_are_shared_not_duplicated(self):
        self.assertEqual(life.CONFLICT_LOCK_LABEL, repair.CONFLICT_LOCK_LABEL)
        self.assertEqual(
            life.MAX_CONFLICT_DISPATCHES_PER_HEAD, repair.MAX_DISPATCHES_PER_HEAD
        )
        self.assertEqual(
            life.CONFLICT_DISPATCH_GRACE_SECONDS, repair.DISPATCH_GRACE_SECONDS
        )

    def test_dirty_pr_dispatches_for_both_providers(self):
        for kwargs in (
            {"mergeable": False, "mergeable_state": "dirty"},
            {
                "mergeable": False,
                "mergeable_state": "dirty",
                "has_lock": True,
                "active_repair_run": False,
                "dispatches_for_head": 1,
                "marker_age_seconds": repair.DISPATCH_GRACE_SECONDS + 1,
            },
        ):
            decision = life.conflict_decision_kwargs(**kwargs)
            self.assertEqual(decision["action"], "dispatch")

    def test_active_run_coalesces_for_both_providers(self):
        decision = life.conflict_decision_kwargs(
            mergeable=False,
            mergeable_state="dirty",
            has_lock=True,
            active_repair_run=True,
        )
        self.assertEqual(decision["action"], "wait")

    def test_budget_exhaustion_holds_for_both_providers(self):
        decision = life.conflict_decision_kwargs(
            mergeable=False,
            mergeable_state="dirty",
            dispatches_for_head=repair.MAX_DISPATCHES_PER_HEAD,
        )
        self.assertEqual(decision["action"], "hold")

    def test_repaired_head_resumes_for_both_providers(self):
        decision = life.conflict_decision_kwargs(
            mergeable=True,
            mergeable_state="clean",
            has_lock=True,
            head_changed_since_dispatch=True,
        )
        self.assertEqual(decision["action"], "resume")
        self.assertTrue(decision["reconcile_lock"])

    def test_both_workflows_carry_the_lock_label(self):
        for path in (GENERIC_MERGE, PR_AGENT_MERGE):
            self.assertIn("opencode-conflict-repair", read_repo(path))


class CurrentHeadGateTests(unittest.TestCase):
    def test_green_ci_passes_for_both_providers(self):
        ok, _ = life.evaluate_current_head_gates(ci_run=ci_run())
        self.assertTrue(ok)

    def test_missing_or_red_ci_blocks_both_providers(self):
        for run in (None, ci_run("completed", "failure"), ci_run("in_progress", None)):
            ok, reason = life.evaluate_current_head_gates(ci_run=run)
            self.assertFalse(ok, run)
            self.assertIn("CI", reason)

    def test_continuum_gates_apply_only_on_continuum(self):
        ok, _ = life.evaluate_current_head_gates(
            ci_run=ci_run(),
            repository="other/repo",
        )
        self.assertTrue(ok)
        ok, reason = life.evaluate_current_head_gates(
            ci_run=ci_run(),
            repository="kodmial/continuum",
        )
        self.assertFalse(ok)
        self.assertIn("Contract", reason)
        ok, _ = life.evaluate_current_head_gates(
            ci_run=ci_run(),
            contract_gate_run=ci_run(),
            contract_qualification_run=ci_run(),
            repository="kodmial/continuum",
        )
        self.assertTrue(ok)

    def test_delegated_skipped_ci_is_narrowly_accepted(self):
        ok, _ = life.evaluate_current_head_gates(
            ci_run=ci_run("completed", "skipped"),
            delegated_skipped_ci=True,
        )
        self.assertTrue(ok)
        ok, _ = life.evaluate_current_head_gates(
            ci_run=ci_run("completed", "skipped"),
            delegated_skipped_ci=False,
        )
        self.assertFalse(ok)


class PackagingAndRequiredGateTests(unittest.TestCase):
    def test_absent_packaging_passes_but_failed_blocks(self):
        ok, _ = life.evaluate_packaging_gate(None)
        self.assertTrue(ok)
        ok, reason = life.evaluate_packaging_gate(ci_run("completed", "failure"))
        self.assertFalse(ok)
        self.assertIn("Packaging smoke", reason)

    def test_required_gate_needs_label_and_success(self):
        ok, _ = life.evaluate_required_workflow_gate(
            labels=[], gate_label="gate", gate_name="Quality", required_run=None
        )
        self.assertTrue(ok)
        ok, reason = life.evaluate_required_workflow_gate(
            labels=["gate"], gate_label="gate", gate_name="Quality", required_run=None
        )
        self.assertFalse(ok)
        self.assertIn("Quality", reason)
        ok, _ = life.evaluate_required_workflow_gate(
            labels=["gate"],
            gate_label="gate",
            gate_name="Quality",
            required_run=ci_run(),
        )
        self.assertTrue(ok)

    def test_both_workflows_gate_packaging_and_required_workflows(self):
        for path in (GENERIC_MERGE, PR_AGENT_MERGE):
            body = read_repo(path)
            self.assertIn("Packaging smoke", body)
            self.assertIn("requiredWorkflowGate", body)
            self.assertIn("REQUIRED_WORKFLOW_GATE", body)


class ExactHeadTests(unittest.TestCase):
    def test_matching_head_validates(self):
        ok, _ = life.validate_exact_head(HEAD, HEAD.upper())
        self.assertTrue(ok)

    def test_moved_or_missing_head_fails_closed(self):
        ok, reason = life.validate_exact_head(HEAD, OTHER_HEAD)
        self.assertFalse(ok)
        self.assertIn("moved", reason)
        for reviewed, live in (("", HEAD), (HEAD, ""), (None, HEAD)):
            ok, _ = life.validate_exact_head(reviewed, live)
            self.assertFalse(ok)

    def test_both_workflows_revalidate_before_merge(self):
        generic = read_repo(GENERIC_MERGE)
        pratgent = read_repo(PR_AGENT_MERGE)
        self.assertIn("AUTO_MERGE_BLOCK_LABEL", generic)
        self.assertIn("AUTO_MERGE_BLOCK_LABEL", pratgent)
        self.assertIn("reviewedHead", pratgent)
        self.assertIn("sha:", pratgent)


class AtomicMergeTests(unittest.TestCase):
    def test_build_params_guards_exact_sha(self):
        params = life.build_merge_params(
            reviewed_head=HEAD, live_head=HEAD, title="Add feature"
        )
        self.assertEqual(params["sha"], HEAD)
        self.assertEqual(params["commit_title"], "fix: Add feature")
        with self.assertRaises(life.MergeLifecycleError):
            life.build_merge_params(
                reviewed_head=HEAD, live_head=OTHER_HEAD, title="Add feature"
            )
        with self.assertRaises(life.MergeLifecycleError):
            life.build_merge_params(
                reviewed_head=HEAD, live_head=HEAD, title="   "
            )

    def test_both_workflows_use_exact_sha_merge(self):
        for path in (GENERIC_MERGE, PR_AGENT_MERGE):
            body = read_repo(path)
            self.assertIn("pulls.merge", body)
            self.assertIn("sha:", body)
        pr_agent = read_repo(PR_AGENT_MERGE)
        self.assertIn("sha: reviewedHead", pr_agent)
        self.assertNotIn('gh pr merge "$PR_NUMBER"', pr_agent)


class MergeTitleTests(unittest.TestCase):
    def test_conventional_titles_are_kept(self):
        for title in (
            "fix: repair merge",
            "feat(api): add endpoint",
            "perf: faster queue",
            "refactor!: break api",
        ):
            self.assertEqual(life.normalize_merge_title(title), title.strip())

    def test_plain_titles_get_fix_prefix(self):
        self.assertEqual(
            life.normalize_merge_title("Add feature"), "fix: Add feature"
        )

    def test_empty_title_fails_closed(self):
        with self.assertRaises(life.MergeLifecycleError):
            life.normalize_merge_title("   ")

    def test_both_workflows_share_the_title_regex(self):
        needle = r"^(?:fix|feat|perf|refactor)"
        for path in (GENERIC_MERGE, PR_AGENT_MERGE):
            self.assertIn(needle, read_repo(path))
        self.assertIn("conventionalTitle", read_repo(GENERIC_MERGE))
        self.assertIn("conventionalTitle", read_repo(PR_AGENT_MERGE))


class PostMergeWakeupTests(unittest.TestCase):
    def test_csv_parsing(self):
        self.assertEqual(
            life.parse_post_merge_wakeups("a.yml, b.yml ,"), ["a.yml", "b.yml"]
        )
        self.assertEqual(life.parse_post_merge_wakeups(""), [])

    def test_ref_validation_falls_back_to_main(self):
        self.assertEqual(life.sanitize_wakeup_ref("main"), "main")
        self.assertEqual(life.sanitize_wakeup_ref("", "main"), "main")
        self.assertEqual(life.sanitize_wakeup_ref("../evil"), "main")
        self.assertEqual(life.sanitize_wakeup_ref("HEAD"), "main")

    def test_both_workflows_wake_consumers_only(self):
        for path in (GENERIC_MERGE, PR_AGENT_MERGE):
            body = read_repo(path)
            self.assertIn("POST_MERGE_WAKEUPS", body)
            self.assertIn("POST_MERGE_WAKEUP_REF", body)


class IdempotencyTests(unittest.TestCase):
    def test_lease_key_is_pr_and_head_scoped(self):
        self.assertEqual(life.lease_key(12, HEAD), "12:" + HEAD)
        self.assertNotEqual(life.lease_key(12, HEAD), life.lease_key(13, HEAD))
        self.assertNotEqual(life.lease_key(12, HEAD), life.lease_key(12, OTHER_HEAD))

    def test_both_workflows_serialize_without_cancelling(self):
        for path in (GENERIC_MERGE, PR_AGENT_MERGE):
            body = read_repo(path)
            self.assertIn("cancel-in-progress: false", body)
            self.assertIn("concurrency:", body)


class BranchSafetyDelegationTests(unittest.TestCase):
    def test_main_sync_destination_delegates_to_branch_safety(self):
        ok, _ = life.assert_safe_main_sync_destination("feature/x", "main")
        self.assertTrue(ok)
        ok, reason = life.assert_safe_main_sync_destination("main", "main")
        self.assertFalse(ok)
        self.assertIn("default branch", reason)
        direct = branch_safety.validate_push_destination("feature/x", "main")
        self.assertEqual(
            life.assert_safe_main_sync_destination("feature/x", "main"), direct
        )


class ProviderBoundaryTests(unittest.TestCase):
    def test_common_layer_has_no_provider_policy(self):
        # Executable surface must contain no provider-specific policy:
        # no review-JSON parsing, no persistent-state reading, no quota
        # parsing, no retry-attempt scheduling. Documentation may name the
        # adapters; code must not implement them.
        surface = set(dir(life))
        for marker in (
            "parse_review_json",
            "persistent_state",
            "merge_recommendation",
            "qualifying_suggestions",
            "parse_retry_delay",
            "retry_attempt",
            "review_disposition",
            "controller_state",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, surface)
        self.assertFalse(
            any("review" in name and "json" in name for name in surface)
        )

    def test_coderabbit_rate_limit_vectors(self):
        vectors = [
            (
                "Review rate limited. Next included review available in 13 minutes.",
                13 * 60_000,
            ),
            (
                "Review limit reached. Your next review is available after "
                "1 hour 5 minutes.",
                65 * 60_000,
            ),
            ("Review rate limited. Try again in less than a minute.", 60_000),
        ]
        for body, expected in vectors:
            with self.subTest(body=body):
                self.assertEqual(rabbit.parse_retry_delay_ms(body), expected)
        self.assertEqual(
            rabbit.parse_retry_delay_ms("Review rate limited, no countdown."),
            rabbit.QUOTA_FALLBACK_MS,
        )
        self.assertIsNone(rabbit.parse_retry_delay_ms("All good."))

    def test_coderabbit_review_commands_and_approval(self):
        self.assertTrue(
            rabbit.review_command_posted_after(
                [
                    {
                        "user": {"login": "alice"},
                        "body": "@coderabbitai review",
                        "created_at": "2026-01-02T00:00:00Z",
                    }
                ],
                0,
            )
        )
        ok, _ = rabbit.approval_basis({"state": "APPROVED"})
        self.assertTrue(ok)
        ok, reason = rabbit.approval_basis(
            {"state": "CHANGES_REQUESTED", "summary_only": True}
        )
        self.assertFalse(ok)
        self.assertIn("not terminal", reason)
        self.assertTrue(rabbit.unresolved_blocking(2))
        self.assertFalse(rabbit.unresolved_blocking(0))
        self.assertTrue(rabbit.carried_approval_valid(HEAD, HEAD))
        self.assertFalse(rabbit.carried_approval_valid(HEAD, OTHER_HEAD))

    def test_coderabbit_adapter_has_no_common_lifecycle(self):
        # The adapter must not reimplement common gates: no block-label,
        # packaging, merge, or persistent-state decisions on its surface.
        surface = set(dir(rabbit))
        for marker in (
            "is_merge_blocked",
            "evaluate_packaging",
            "evaluate_current_head",
            "build_merge",
            "persistent",
            "decide_main_sync",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, surface)

    def test_pragent_native_semantics_stay_provider_owned(self):
        review = {
            "key_issues_to_review": [],
            "merge_recommendation": "safe_to_merge",
        }
        self.assertEqual(pragent.merge_recommendation(review), "safe_to_merge")
        self.assertEqual(pragent.current_key_issues(review), [])
        state = {
            "findings": [],
            "last_run": {"head_sha": HEAD, "complete": True, "kind": "full"},
        }
        self.assertFalse(pragent.upstream_state_has_active(state))
        gate = pragent.evaluate_gate(
            pragent.GateInputs(
                review=review,
                qualifying_improve=[],
                persistent_state=state,
                ci_green_on_exact_head=True,
                head_matches=True,
                review_coverage_complete=True,
                improve_coverage_complete=True,
            )
        )
        self.assertTrue(gate["green"])
        # The common layer must not reimplement any of this.
        surface = set(dir(life))
        self.assertNotIn("merge_recommendation", surface)
        self.assertNotIn("persistent_state", surface)


class SharedJsMirrorTests(unittest.TestCase):
    def test_shared_js_bundle_exports_the_common_surface(self):
        bundle = read_repo(SHARED_JS)
        for export in (
            "AUTO_MERGE_BLOCK_LABEL",
            "isNonMergeCriticalMainPath",
            "decideMainSync",
            "sanitizeBranch",
            "normalizeMergeTitle",
            "parsePostMergeWakeups",
            "validateExactHead",
            "evaluateCurrentHeadGates",
            "leaseKey",
        ):
            with self.subTest(export=export):
                self.assertIn(export, bundle)
        # The bundle must export no provider-specific helpers.
        self.assertNotIn("parse_retry_delay", bundle)
        self.assertNotIn("reviewDisposition", bundle)
        self.assertNotIn("persistentHasActive", bundle)
        self.assertNotIn("controllerState", bundle)

    def test_shared_js_bundle_parses_and_runs_vectors(self):
        if shutil.which("node") is None:
            self.skipTest("node is not available")
        script = os.path.join(ROOT, SHARED_JS)
        probe = (
            "const m=require(%r);"
            "const assert=require('assert');"
            "assert.strictEqual(m.normalizeMergeTitle('Add feature'),'fix: Add feature');"
            "assert.strictEqual(m.normalizeMergeTitle('fix: ok'),'fix: ok');"
            "assert.deepStrictEqual(m.parsePostMergeWakeups('a.yml, b.yml'),['a.yml','b.yml']);"
            "assert.strictEqual(m.sanitizeBranch('../evil'),'main');"
            "assert.strictEqual(m.isNonMergeCriticalMainPath('docs/x.md'),true);"
            "assert.strictEqual(m.isNonMergeCriticalMainPath('src/x.py'),false);"
            "assert.strictEqual(m.validateExactHead('abc','abc').ok,true);"
            "assert.strictEqual(m.validateExactHead('abc','def').ok,false);"
            "console.log('js-mirror-ok');"
            % script
        )
        completed = subprocess.run(
            ["node", "-e", probe],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("js-mirror-ok", completed.stdout)

    def test_both_workflows_reference_the_canonical_bundle(self):
        for path in (GENERIC_MERGE, PR_AGENT_MERGE):
            body = read_repo(path)
            self.assertIn("merge_lifecycle", body)


if __name__ == "__main__":
    unittest.main()
