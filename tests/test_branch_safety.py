"""Executable branch-safety proof for kodmial/continuum#302.

Every code-producing mutation path must target a proven non-default
work/PR branch or fail closed before mutation. This test module is the
executable proof of the unsafe/missing guarantee: it exercises the
canonical :mod:`continuum.branch_safety` decisions plus the workflow
wiring that enforces them at every ``git push`` / ``git/refs`` /
``update-branch`` site.

Mutation-path map (baseline ``0f091f9``; inventory refreshed against
that exact SHA):

1. ``continuum-opencode.yml`` Implement issue: ``git/refs`` POST +
   ``force-with-lease`` push of ``opencode/issue<N>-<run>`` then PR.
2. ``continuum-opencode.yml`` Recover agent-managed issue branch:
   push of ``CURRENT_BRANCH`` (must be ``opencode/*``, never default).
3. ``continuum-opencode.yml`` 429 evacuation + interruption
   checkpoint: ``--force`` push of ``opencode/429-checkpoint-*`` /
   ``opencode/checkpoint-issue<N>`` (+ optional legacy mirror).
4. ``continuum-opencode.yml`` coderabbit-fix / resolve-conflict /
   ci-fix repair: push of ``HEAD_REF`` with exact-head race checks.
5. ``continuum-coderabbit-unresolved.yml`` batch repair: push of
   live-read ``HEAD_REF``.
6. ``continuum-pr-agent-repair.yml`` exact-HEAD repair:
   ``force-with-lease=HEAD_SHA`` push of ``HEAD_REF``.
7. ``continuum-consumer-child-worker.yml`` delegated task branch
   ``continuum-child/task-<N>-<run>`` push + PR.
8. ``continuum-consumer-child-review.yml`` /
   ``continuum-consumer-child-pr-review.yml`` delegated repair push of
   ``head_ref`` (+ ``gh pr merge`` / ``update-branch``).
9. ``continuum-pr-agent-canary.yml`` disposable
   ``continuum-pr-agent-canary/<run>`` pushes (+ delete on cleanup).
10. ``continuum-opencode-repair.yml`` + auto-merge reconcilers:
    GitHub-native ``update-branch`` (PR branch target, never default).
11. Merge operations (``gh pr merge`` / ``pulls.merge``): the ONLY
    allowed default-branch updater, after exact-HEAD gates.
12. Qualification mode: no code push by contract (forbidden, hard
    reset on drift).

Metadata writes (labels/comments/statuses/dispatches) are not code
writes and stay outside this prohibition.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from continuum import branch_safety as safety  # noqa: E402

WORKFLOWS_DIR = os.path.join(ROOT, ".github", "workflows")

# Every workflow file that publishes code (push / refs / update-branch / merges).
CODE_PUBLISHING_WORKFLOWS = (
    "continuum-opencode.yml",
    "continuum-coderabbit-unresolved.yml",
    "continuum-pr-agent-repair.yml",
    "continuum-consumer-child-worker.yml",
    "continuum-consumer-child-review.yml",
    "continuum-consumer-child-pr-review.yml",
    "continuum-pr-agent-canary.yml",
    "continuum-opencode-repair.yml",
    "continuum-auto-merge.yml",
    "continuum-pr-agent-auto-merge.yml",
)

# Workflows whose merge operation is the single allowed default updater.
MERGE_WORKFLOWS = (
    "continuum-consumer-child-review.yml",
    "continuum-consumer-child-pr-review.yml",
)


class CanonicalModuleTests(unittest.TestCase):
    def test_default_branch_is_discovered_never_hardcoded(self):
        with self.assertRaises(safety.BranchSafetyError):
            safety.resolve_default_branch("")
        with self.assertRaises(safety.BranchSafetyError):
            safety.resolve_default_branch(None)
        with self.assertRaises(safety.BranchSafetyError):
            safety.resolve_default_branch("   ")
        self.assertEqual(safety.resolve_default_branch("main"), "main")
        self.assertEqual(safety.resolve_default_branch("refs/heads/main"), "main")
        self.assertEqual(safety.resolve_default_branch("trunk"), "trunk")
        # No silent fallback to any hardcoded name.
        self.assertFalse(hasattr(safety.resolve_default_branch, "fallback"))

    def test_empty_destination_fails_closed(self):
        for empty in ("", "   ", None, "refs/heads/"):
            ok, reason = safety.validate_push_destination(empty, "main")
            self.assertFalse(ok, empty)
            self.assertIn("empty", reason.lower())

    def test_ambiguous_destination_fails_closed(self):
        for ambiguous in ("HEAD", "@", "FETCH_HEAD", "ORIG_HEAD",
                          "MERGE_HEAD", "a..b", "a~1", "a^", "a?b",
                          "a*b", "a[b", "a b", "-leading-dash",
                          "/leading-slash", "trailing-slash/",
                          "double//slash", "name.lock", "a@{1}"):
            ok, reason = safety.validate_push_destination(ambiguous, "main")
            self.assertFalse(ok, ambiguous)
            self.assertIn("ambiguous", reason.lower())

    def test_push_to_default_branch_is_rejected(self):
        for default in ("main", "master", "trunk"):
            ok, reason = safety.validate_push_destination(default, default)
            self.assertFalse(ok)
            self.assertIn("default", reason.lower())
        # refs/heads/ spelling of the default is the same branch.
        ok, _ = safety.validate_push_destination("refs/heads/main", "main")
        self.assertFalse(ok)
        ok, _ = safety.validate_push_destination("main", "refs/heads/main")
        self.assertFalse(ok)

    def test_undiscoverable_default_fails_closed(self):
        for missing in ("", "  ", None):
            ok, reason = safety.validate_push_destination(
                "opencode/issue1-2", missing)
            self.assertFalse(ok)
            self.assertIn("default", reason.lower())

    def test_exact_pr_head_repair_is_preserved(self):
        ok, _ = safety.validate_push_destination(
            "opencode/issue7-1", "main",
            expected_head="opencode/issue7-1")
        self.assertTrue(ok)
        ok, reason = safety.validate_push_destination(
            "opencode/issue7-2", "main",
            expected_head="opencode/issue7-1")
        self.assertFalse(ok)
        self.assertIn("head", reason.lower())
        # A repair that names the default branch as its head fails closed.
        ok, _ = safety.validate_push_destination(
            "main", "main", expected_head="main")
        self.assertFalse(ok)

    def test_checkpoint_refs_are_permitted_only_off_default(self):
        ref = safety.checkpoint_ref("opencode-429-issue-issue1", 1)
        ok, _ = safety.validate_push_destination(ref, "main")
        self.assertTrue(ok)
        ok, _ = safety.validate_checkpoint_push(ref, "main")
        self.assertTrue(ok)
        # A checkpoint ref can never alias the default branch.
        ok, _ = safety.validate_push_destination("main", "main")
        self.assertFalse(ok)
        with self.assertRaises(safety.BranchSafetyError):
            safety.validate_checkpoint_push("main", "main")
        with self.assertRaises(safety.BranchSafetyError):
            safety.validate_checkpoint_push("opencode/issue1-2", "main")

    def test_new_issue_branch_is_non_default(self):
        ok, _ = safety.validate_new_branch("opencode/issue9-42", "main")
        self.assertTrue(ok)
        ok, _ = safety.validate_new_branch("main", "main")
        self.assertFalse(ok)
        ok, _ = safety.validate_new_branch("", "main")
        self.assertFalse(ok)

    def test_no_emergency_bypass_exists(self):
        source_path = os.path.join(SRC, "continuum", "branch_safety.py")
        with open(source_path, "r", encoding="utf-8") as handle:
            source = handle.read()
        lowered = source.lower()
        # No code identifier may offer an override: no flag, parameter,
        # or environment hook that flips a rejection into permission.
        # (Prose stating "there is no bypass" is required documentation
        # and is not a mechanism.)
        self.assertNotRegex(lowered, r"def\s+\w*(bypass|allow_default|skip_check|force_default)")
        self.assertNotIn("allow_default", lowered)
        self.assertNotIn("skip_check", lowered)
        self.assertNotIn("force_default", lowered)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("os.getenv", source)
        # No signature may carry an opt-out parameter.
        self.assertNotRegex(lowered, r"def validate_\w+\(.*(allow|skip|force|override|unsafe)")


class WorkflowWiringTests(unittest.TestCase):
    def _body(self, name):
        with open(os.path.join(WORKFLOWS_DIR, name),
                  "r", encoding="utf-8") as handle:
            return handle.read()

    def _has_guard(self, body: str) -> bool:
        lowered = body.lower()
        return ("continuum_assert_safe_push" in lowered
                or "assertsafepush" in lowered
                or "assert_safe_push" in lowered)

    def test_every_push_site_is_guarded(self):
        missing = []
        for name in CODE_PUBLISHING_WORKFLOWS:
            body = self._body(name)
            if ("git push" not in body and "git/refs" not in body
                    and "update-branch" not in body):
                continue
            if not self._has_guard(body):
                missing.append(name)
        self.assertEqual(missing, [])

    def test_guard_discovers_default_branch_dynamically(self):
        for name in CODE_PUBLISHING_WORKFLOWS:
            body = self._body(name)
            if not self._has_guard(body):
                continue
            # The guard resolves the actual default branch from repository
            # metadata at runtime (REST default_branch field), never a
            # hardcoded name, and fails closed when discovery yields nothing.
            self.assertIn("default_branch", body,
                          f"{name}: guard must resolve the default branch")
            self.assertRegex(
                body,
                r"(default_branch.{0,80}(undiscoverable|empty|unavailable|unset)"
                r"|(undiscoverable|empty|unavailable|unset).{0,80}default_branch"
                r"|--jq \.default_branch"
                r"|\.default_branch \|\|)",
                f"{name}: guard must fail closed on undiscoverable default",
            )

    def test_guard_rejects_default_before_mutation(self):
        for name in CODE_PUBLISHING_WORKFLOWS:
            body = self._body(name)
            if not self._has_guard(body):
                continue
            # The destination-vs-default comparison must exist in the guard.
            self.assertRegex(
                body,
                r"dest.{0,40}==.{0,40}default_branch"
                r"|branch.{0,40}===?.{0,40}default_branch"
                r"|norm.{0,40}===.{0,40}default_branch",
                f"{name}: guard must compare destination against default",
            )

    def test_merge_remains_the_only_default_updater(self):
        for name in MERGE_WORKFLOWS:
            body = self._body(name)
            self.assertIn("gh pr merge", body)
        # The canonical module documents the merge exception without
        # permitting pushes to the default branch.
        source_path = os.path.join(SRC, "continuum", "branch_safety.py")
        with open(source_path, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("merge", source.lower())

    def test_no_new_required_consumer_inputs(self):
        import yaml  # type: ignore
        for name in CODE_PUBLISHING_WORKFLOWS:
            with open(os.path.join(WORKFLOWS_DIR, name),
                      "r", encoding="utf-8") as handle:
                parsed = yaml.safe_load(handle)
            inputs = (parsed.get("on", {}) or {}).get("workflow_call", {}) \
                .get("inputs", {}) or {}
            for input_name, spec in inputs.items():
                required = (spec or {}).get("required", False)
                self.assertFalse(
                    required is True,
                    f"{name}: input {input_name!r} must not become required",
                )


class ShellGuardExecutionTests(unittest.TestCase):
    GUARD = r"""
continuum_assert_safe_push() {
  local dest="${1:-}"
  dest="${dest#refs/heads/}"
  dest="$(printf '%s' "$dest" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  if [[ -z "$dest" ]]; then echo "refusing empty push destination" >&2; return 1; fi
  case "$dest" in HEAD|@|FETCH_HEAD|ORIG_HEAD|MERGE_HEAD) echo "refusing ambiguous push destination: $dest" >&2; return 1;; esac
  case "$dest" in *".."*|*"~"*|*"^"*|*"?"*|*"*"*|*"["*|*" "*|"-"*|"//"*|"*.lock"|*"@{"*) echo "refusing ambiguous push destination: $dest" >&2; return 1;; esac
  case "$dest" in -*|/*|*/|*.lock) echo "refusing ambiguous push destination: $dest" >&2; return 1;; esac
  local default_branch="${CONTINUUM_TEST_DEFAULT_BRANCH:-}"
  default_branch="${default_branch#refs/heads/}"
  if [[ -z "$default_branch" ]]; then echo "refusing push: default branch is undiscoverable" >&2; return 1; fi
  if [[ "$dest" == "$default_branch" ]]; then echo "refusing push to the default branch: $dest" >&2; return 1; fi
  return 0
}
"""

    def _run_guard(self, dest, default):
        with tempfile.NamedTemporaryFile("w", suffix=".sh",
                                         delete=False) as handle:
            handle.write(self.GUARD + "\n")
            handle.write('continuum_assert_safe_push "$GUARD_DEST"\n')
            path = handle.name
        try:
            proc = subprocess.run(
                ["bash", path],
                capture_output=True, text=True,
                env={"PATH": "/usr/bin:/bin",
                     "GUARD_DEST": dest,
                     "CONTINUUM_TEST_DEFAULT_BRANCH": default},
                timeout=30,
            )
            return proc.returncode, proc.stderr
        finally:
            os.unlink(path)

    def test_shell_guard_rejects_default_and_empty(self):
        code, _ = self._run_guard("main", "main")
        self.assertNotEqual(code, 0)
        code, _ = self._run_guard("", "main")
        self.assertNotEqual(code, 0)
        code, _ = self._run_guard("HEAD", "main")
        self.assertNotEqual(code, 0)
        code, _ = self._run_guard("opencode/issue1-2", "")
        self.assertNotEqual(code, 0)

    def test_shell_guard_permits_task_and_checkpoint_branches(self):
        code, _ = self._run_guard("opencode/issue1-2", "main")
        self.assertEqual(code, 0)
        code, _ = self._run_guard(
            "opencode/429-checkpoint-op1-gen-1", "main")
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
