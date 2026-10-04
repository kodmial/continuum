"""Contract tests for the #260 commit and review provenance contract.

Every workflow-owned commit must use the github-actions[bot] identity and
carry the stable ``Continuum-Component: <component>`` trailer; no
automated review path may manufacture a human-looking approval; status
and merge gates must stay pinned to the exact HEAD; and provenance
trailers must never leak secrets or private child repository identity.
"""

from __future__ import annotations

import os
import re
import unittest

from continuum import commit_provenance as provenance
from continuum import credential_matrix as matrix

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_DIR = os.path.join(ROOT, ".github", "workflows")
SCRIPTS_DIR = os.path.join(ROOT, ".github", "scripts")
SRC_DIR = os.path.join(ROOT, "src", "continuum")

BOT_NAME_LITERAL = 'github-actions[bot]'
BOT_EMAIL_LITERAL = '41898282+github-actions[bot]@users.noreply.github.com'
TRAILER = 'Continuum-Component:'

SECRET_SHAPES = (
    re.compile(r"ghp_[A-Za-z0-9]+"),
    re.compile(r"gho_[A-Za-z0-9_-]+"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"\brnd_[A-Za-z0-9]+\b"),
)


def read_workflow(name):
    with open(os.path.join(WORKFLOWS_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def step_window(text, step):
    """Return the owning step block for ``step`` (mirrors matrix tests)."""
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if step in line),
        None,
    )
    assert start is not None, f"step marker gone: {step}"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.startswith("      - name: ") or line.startswith("      - uses: "):
            end = i
            break
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line) or re.match(r"^jobs:\s*$", line):
            end = i
            break
    return "\n".join(lines[start:end])


class ProvenanceShapeTests(unittest.TestCase):
    def test_matrix_is_valid(self):
        self.assertTrue(provenance.validate())

    def test_bot_identity_is_stable(self):
        self.assertEqual(provenance.BOT_NAME, BOT_NAME_LITERAL)
        self.assertEqual(provenance.BOT_EMAIL, BOT_EMAIL_LITERAL)

    def test_trailer_format(self):
        line = provenance.trailer_line("opencode")
        self.assertEqual(line, "Continuum-Component: opencode")
        msg = provenance.format_commit_message("fix: something", "opencode")
        self.assertIn("fix: something", msg)
        self.assertIn("Continuum-Component: opencode", msg)

    def test_components_reject_repository_identity(self):
        for bad in ("owner/repo", "ChildRepo", "UPPER", "has space", "", "a/b"):
            with self.subTest(component=bad):
                with self.assertRaises(ValueError):
                    provenance.require_component(bad)


class CommitIdentityTests(unittest.TestCase):
    def test_every_commit_site_sets_bot_identity_and_trailer(self):
        for site in provenance.COMMIT_SITES:
            with self.subTest(
                workflow=site["workflow"], step=site["step"],
                subject=site["subject"],
            ):
                text = read_workflow(site["workflow"])
                window = step_window(text, site["step"])
                # Bot identity: `git-c` sites carry inline `-c user.name=`
                # flags in the commit step itself; `git-config` sites rely on
                # the job-level `Configure Git identity` step (bot name+email
                # set once per job before any commit), so the workflow as a
                # whole must carry the pair while the step carries the commit.
                if site["identity"] == "git-config":
                    self.assertIn('git config user.name "github-actions[bot]"', text)
                    self.assertIn(
                        'git config user.email "41898282+github-actions[bot]@users.noreply.github.com"',
                        text,
                    )
                else:
                    self.assertIn('user.name="github-actions[bot]"', window)
                    self.assertIn(
                        'user.email="41898282+github-actions[bot]@users.noreply.github.com"',
                        window,
                    )
                # Subject and trailer travel together in the commit command.
                self.assertIn(site["subject"], window)
                self.assertIn(TRAILER, window)
                self.assertIn(
                    f"{TRAILER} {site['component']}", window,
                    f"{site['workflow']}::{site['step']}: commit lacks "
                    f"the {site['component']} trailer",
                )

    def test_no_commit_rewrites_developer_history(self):
        forbidden = (
            "git commit --amend",
            "git rebase",
            "git filter-branch",
            "git filter-repo",
        )
        for name in sorted(os.listdir(WORKFLOWS_DIR)):
            if not name.startswith("continuum-") or not name.endswith(".yml"):
                continue
            text = read_workflow(name)
            for pattern in forbidden:
                with self.subTest(workflow=name, pattern=pattern):
                    self.assertNotIn(
                        pattern, text,
                        f"{name}: workflow must never rewrite developer commits ({pattern})",
                    )

    def test_tool_prompts_forbid_direct_commits(self):
        # Deterministic ownership: the workflow owns the final commit; the
        # agent leaves working-tree changes. A prompt that still invites the
        # tool to commit breaks the trailer guarantee.
        text = read_workflow("continuum-opencode.yml")
        self.assertNotIn("Commit and push fixes", text)
        self.assertNotIn("Commit and push any required fix", text)
        self.assertNotIn("Commit the merge and push", text)
        for name in (
            "continuum-consumer-child-worker.yml",
            "continuum-consumer-child-review.yml",
            "continuum-consumer-child-pr-review.yml",
        ):
            body = read_workflow(name)
            self.assertNotIn("You may make local commits", body)
            self.assertNotIn("including commits. Do not push", body)

    def test_commit_subjects_carry_no_child_repo_identity(self):
        for site in provenance.COMMIT_SITES:
            with self.subTest(component=site["component"]):
                self.assertNotIn("/", site["subject"])
                self.assertNotIn("child_repo", site["subject"].lower())
                self.assertIsNone(provenance.contains_secret(site["subject"]))


class ReviewProvenanceTests(unittest.TestCase):
    def _all_prose_sources(self):
        sources = {}
        for name in sorted(os.listdir(WORKFLOWS_DIR)):
            if not name.endswith(".yml"):
                continue
            with open(os.path.join(WORKFLOWS_DIR, name), encoding="utf-8") as handle:
                sources[f"workflows/{name}"] = handle.read()
        for name in sorted(os.listdir(SCRIPTS_DIR)):
            path = os.path.join(SCRIPTS_DIR, name)
            if not os.path.isfile(path):
                continue
            if name.startswith("test_"):
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                sources[f"scripts/{name}"] = handle.read()
        for name in sorted(os.listdir(SRC_DIR)):
            if not name.endswith(".py"):
                continue
            if name in ("commit_provenance.py",):
                # The contract module itself names the forbidden APIs to
                # forbid them; scanning it would be self-incriminating.
                continue
            with open(os.path.join(SRC_DIR, name), encoding="utf-8") as handle:
                sources[f"src/{name}"] = handle.read()
        return sources

    def test_no_automated_path_manufactures_an_approval(self):
        sources = self._all_prose_sources()
        for path, text in sources.items():
            for api in provenance.FORBIDDEN_REVIEW_APIS:
                with self.subTest(path=path, api=api):
                    self.assertNotIn(
                        api, text,
                        f"{path}: automated review creation API {api!r} "
                        "would manufacture a human-looking approval",
                    )

    def test_no_gh_pr_review_approval_shim(self):
        for name in sorted(os.listdir(WORKFLOWS_DIR)):
            if not name.startswith("continuum-"):
                continue
            text = read_workflow(name)
            self.assertNotIn(
                "gh pr review", text,
                f"{name}: must not emit review decisions via gh",
            )

    def test_merge_gating_uses_exact_head_sha(self):
        for name in ("continuum-auto-merge.yml", "continuum-pr-agent-auto-merge.yml"):
            text = read_workflow(name)
            # Every pulls.merge call pins the reviewed HEAD explicitly.
            self.assertIn("pulls.merge", text)
            for match in re.finditer(r"pulls\.merge\(\{([^}]*)\}", text, re.DOTALL):
                block = match.group(1)
                self.assertIn(
                    "sha:", block,
                    f"{name}: pulls.merge without an exact sha: would merge a moving head",
                )

    def test_status_gates_stay_exact_head(self):
        for name in (
            "continuum-pr-agent.yml",
            "continuum-pr-agent-repair.yml",
            "continuum-validation.yml",
        ):
            text = read_workflow(name)
            self.assertIn("createCommitStatus", text)
            for match in re.finditer(
                r"createCommitStatus\(\{([^}]*)\}", text, re.DOTALL
            ):
                block = match.group(1)
                self.assertTrue(
                    any(sha in block for sha in provenance.ALLOWED_STATUS_SHAS),
                    f"{name}: commit status must target the exact HEAD SHA, got: {block[:120]!r}",
                )

    def test_residual_pat_ops_are_documented_app_candidates(self):
        self.assertTrue(provenance.RESIDUAL_PAT_OPS)
        for entry in provenance.RESIDUAL_PAT_OPS:
            with self.subTest(operation=entry["operation"]):
                self.assertTrue(entry["why_pat"].strip())
                candidate = entry["app_candidate"]
                self.assertTrue(candidate.strip())
                self.assertIn("App", candidate)


class TrailerHygieneTests(unittest.TestCase):
    def test_trailers_carry_no_secret_or_repo_identity(self):
        for site in provenance.COMMIT_SITES:
            with self.subTest(component=site["component"]):
                line = provenance.trailer_line(site["component"])
                self.assertNotIn("/", line)
                for shape in SECRET_SHAPES:
                    self.assertIsNone(
                        shape.search(line),
                        f"trailer leaks secret-shaped material: {line}",
                    )

    def test_workflow_trailers_match_canonical_components(self):
        for site in provenance.COMMIT_SITES:
            with self.subTest(
                workflow=site["workflow"], component=site["component"]
            ):
                text = read_workflow(site["workflow"])
                window = step_window(text, site["step"])
                trailers = re.findall(
                    r"Continuum-Component:\s*([^\s\"']+)", window
                )
                self.assertTrue(trailers, "no trailer in owning step")
                for component in trailers:
                    with self.subTest(component=component):
                        self.assertRegex(component, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
                        self.assertNotIn("/", component)

    def test_no_new_consumer_secret_for_baseline(self):
        allowed = set(matrix.ALLOWED_SECRETS) | {"GITHUB_TOKEN"}
        secret_re = re.compile(r"secrets\.([A-Za-z0-9_]+)")
        for name in sorted(os.listdir(WORKFLOWS_DIR)):
            if not name.startswith("continuum-") or not name.endswith(".yml"):
                continue
            text = read_workflow(name)
            for secret in secret_re.findall(text):
                with self.subTest(workflow=name, secret=secret):
                    self.assertIn(
                        secret, allowed,
                        f"{name}: baseline provenance must not require a new secret secrets.{secret}",
                    )


if __name__ == "__main__":
    unittest.main()
