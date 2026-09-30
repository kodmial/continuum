#!/usr/bin/env python3
"""The engine-root verifier is the only thing standing between a consumer's pin
and a checkout that is not it.

`job.workflow_sha` describes the workflow *file* GitHub loaded. It says nothing
about which repository the engine was fetched from, and nothing about whether the
fetch resolved to that commit. Both are separate facts, both have to be checked,
and both are invisible in a workflow review -- `ref: ${{ job.workflow_sha }}` and
`repository: <some fork>` read exactly alike.

These tests build real git checkouts rather than mocking `git`, because the
failure this guards against is a mismatch between what the verifier believes a
git URL looks like and what one actually is. A mocked remote would have accepted
the wrong shape.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "engine-path"


def load_verifier():
    """Import `verify.py` from the action directory.

    It travels with the action rather than living under `src/` because the action
    has to be usable from the engine checkout alone, before anything is known
    about the consumer's repository.
    """

    spec = importlib.util.spec_from_file_location("engine_path_verify", ACTION_DIR / "verify.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify_module = load_verifier()

A_COMMIT = "a" * 40
B_COMMIT = "b" * 40


def git(repository: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class NormalizeOriginTests(unittest.TestCase):
    """`actions/checkout` writes the URL it authenticated with.

    The scheme, the credentials, and the host are the runner's choice, so a
    verifier that compares whole URLs accepts the SSH form and fails every HTTPS
    run -- a check that is wrong on every real invocation is a check nobody
    notices is wrong. What has to hold is the repository path.
    """

    def test_the_https_form_actions_checkout_actually_writes(self):
        self.assertEqual(
            verify_module.normalize_origin("https://github.com/kodmial/continuum"),
            "kodmial/continuum",
        )

    def test_the_https_form_with_a_git_suffix(self):
        self.assertEqual(
            verify_module.normalize_origin("https://github.com/kodmial/continuum.git"),
            "kodmial/continuum",
        )

    def test_the_ssh_forms(self):
        for url in (
            "git@github.com:kodmial/continuum.git",
            "ssh://git@github.com/kodmial/continuum.git",
            "ssh://git@github.com:22/kodmial/continuum.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    verify_module.normalize_origin(url), "kodmial/continuum"
                )

    def test_a_host_on_another_service_is_still_reduced_to_its_path(self):
        self.assertEqual(
            verify_module.normalize_origin("https://git.example.com/kodmial/continuum"),
            "kodmial/continuum",
        )

    def test_a_two_component_path_is_a_path_not_a_host_and_a_path(self):
        # `owner/repo` has two components and `host/owner/repo` has three. The
        # count is what distinguishes them; testing for a dot would misread a
        # repository named `foo.bar`.
        self.assertEqual(verify_module.normalize_origin("kodmial/continuum"), "kodmial/continuum")
        self.assertEqual(
            verify_module.normalize_origin("github.com/kodmial/continuum"),
            "kodmial/continuum",
        )

    def test_a_fork_is_not_normalized_away(self):
        # The whole point: a checkout out of a fork reduces to *its* path, which
        # must not compare equal to Continuum's.
        self.assertEqual(
            verify_module.normalize_origin("https://github.com/someone/continuum"),
            "someone/continuum",
        )

    def test_an_absent_remote_is_an_error_rather_than_an_empty_path(self):
        with self.assertRaises(verify_module.EngineError):
            verify_module.normalize_origin("")


class EngineCheckoutFixture(unittest.TestCase):
    def setUp(self):
        self._workspace = REPO_ROOT / ".github" / "tests" / "_engine_fixtures"
        self.addCleanup(self._remove)
        self.root = self.make_checkout("continuum")

    def make_checkout(self, name: str) -> pathlib.Path:
        root = self._workspace / name
        root.mkdir(parents=True, exist_ok=True)
        git(root, "init", "--quiet")
        git(root, "config", "user.email", "tests@example.invalid")
        git(root, "config", "user.name", "Continuum tests")
        git(root, "remote", "add", "origin", "https://github.com/kodmial/continuum")
        # The engine files the verifier requires, so a matching checkout verifies
        # and only a *partial* one is refused. Their contents do not matter --
        # the verifier checks that they are present, not what they say.
        for required in verify_module.REQUIRED_ENGINE_FILES:
            path = root / required
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {required}\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "--quiet", "-m", "engine")
        return root

    def head(self, root: pathlib.Path | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(root or self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _remove(self):
        if not self._workspace.exists():
            return
        subprocess.run(
            ["rm", "-rf", str(self._workspace)], check=True, capture_output=True
        )


class VerifyTests(EngineCheckoutFixture):
    def test_a_matching_checkout_verifies(self):
        verify_module.verify(self.root, self.head())

    def test_a_checkout_at_another_commit_is_refused(self):
        # The regression this exists for: the caller asked for one release, and
        # something else is on disk. Failing loudly beats running a different
        # implementation than the consumer selected.
        with self.assertRaises(verify_module.EngineError) as raised:
            verify_module.verify(self.root, A_COMMIT)
        self.assertIn("not the selected release", str(raised.exception))

    def test_a_moving_reference_is_refused_before_anything_is_read(self):
        for ref in ("main", "v0.1", "v0.1.0", "", A_COMMIT[:12], A_COMMIT.upper(), "refs/heads/main"):
            with self.subTest(ref=ref):
                with self.assertRaises(verify_module.EngineError) as raised:
                    verify_module.verify(self.root, ref)
                self.assertIn("full commit sha", str(raised.exception))

    def test_a_checkout_from_another_repository_is_refused(self):
        # `ref: ${{ job.workflow_sha }}` reads like a pin, and this is the shape
        # that proves it is not one: the same commit, from somebody else's fork.
        git(self.root, "remote", "set-url", "origin", "https://github.com/someone/continuum")
        with self.assertRaises(verify_module.EngineError) as raised:
            verify_module.verify(self.root, self.head())
        self.assertIn("not 'kodmial/continuum'", str(raised.exception))

    def test_a_checkout_from_a_fork_of_the_right_commit_is_still_refused(self):
        # The same commit, from somebody else's fork, satisfies every commit
        # check. This is why the origin is verified too, and why verifying only
        # `ref:` would be verifying nothing.
        fork = self.make_checkout("fork")
        git(fork, "remote", "set-url", "origin", "https://github.com/someone/continuum")
        with self.assertRaises(verify_module.EngineError) as raised:
            verify_module.verify(fork, self.head(fork))
        self.assertIn("not 'kodmial/continuum'", str(raised.exception))

    def test_a_directory_that_is_not_a_checkout_is_refused(self):
        empty = self._workspace / "not-a-repo"
        empty.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(verify_module.EngineError) as raised:
            verify_module.verify(empty, A_COMMIT)
        self.assertIn("not a git repository", str(raised.exception))

    def test_a_partial_engine_checkout_is_refused_by_name(self):
        for name in verify_module.REQUIRED_ENGINE_FILES:
            with self.subTest(required=name):
                self.assertTrue(
                    (REPO_ROOT / name).is_file(),
                    f"{name} is asserted as required but does not exist in this repository",
                )
        # Refused, and by name -- rather than letting a bare `test -f` fail with
        # no explanation several privileged steps into an agent run.
        (self.root / "src" / "continuum" / "cli.py").unlink()
        with self.assertRaises(verify_module.EngineError) as raised:
            verify_module.verify(self.root, self.head())
        self.assertIn("missing src/continuum/cli.py", str(raised.exception))
        self.assertIn("not a release", str(raised.exception))


class ExcludeTests(EngineCheckoutFixture):
    def test_the_engine_checkout_is_excluded_from_the_consumers_commits(self):
        # The agent commits with `git add -A`, so without this the selected
        # implementation lands in the consumer's history on every agent run.
        consumer = self._workspace / "consumer"
        consumer.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(consumer), "init", "--quiet"], check=True, capture_output=True)
        verify_module.exclude_from_consumer_commits(consumer, ".continuum-engine", self.root)
        exclude = (consumer / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn("/.continuum-engine/", exclude.splitlines())

    def test_the_exclusion_is_not_duplicated_across_jobs(self):
        # Every job in a run calls the action, and they share a workspace.
        consumer = self._workspace / "consumer"
        consumer.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(consumer), "init", "--quiet"], check=True, capture_output=True)
        for _ in range(3):
            verify_module.exclude_from_consumer_commits(consumer, ".continuum-engine", self.root)
        exclude = (consumer / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(exclude.splitlines().count("/.continuum-engine/"), 1)

    def test_an_existing_exclude_file_is_not_mangled(self):
        consumer = self._workspace / "consumer"
        consumer.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(consumer), "init", "--quiet"], check=True, capture_output=True)
        exclude = consumer / ".git" / "info" / "exclude"
        exclude.write_text("*.log", encoding="utf-8")
        verify_module.exclude_from_consumer_commits(consumer, ".continuum-engine", self.root)
        self.assertEqual(exclude.read_text(encoding="utf-8"), "*.log\n/.continuum-engine/\n")

    def test_a_workspace_that_is_not_a_repository_is_not_an_error(self):
        # A job that never checked the consumer out has no history to protect.
        plain = self._workspace / "plain"
        plain.mkdir(parents=True, exist_ok=True)
        verify_module.exclude_from_consumer_commits(plain, ".continuum-engine", self.root)
        self.assertFalse((plain / ".git").exists())

    def test_the_engine_repository_does_not_exclude_itself(self):
        # Running from Continuum's own checkout there is no second history to
        # protect, and writing the entry would only litter the engine repository.
        verify_module.exclude_from_consumer_commits(self.root, ".continuum-engine", self.root)
        exclude = self.root / ".git" / "info" / "exclude"
        written = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        self.assertNotIn(".continuum-engine", written)


class CliTests(EngineCheckoutFixture):
    def test_a_verified_root_is_reported_as_a_github_environment_line(self):
        # The action's only job is to append this, so the exact shape is the
        # contract with every consumer workflow.
        result = subprocess.run(
            [
                sys.executable,
                str(ACTION_DIR / "verify.py"),
                "--root",
                str(self.root),
                "--release-sha",
                self.head(),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), f"CONTINUUM_ENGINE_ROOT={self.root.resolve()}"
        )

    def test_a_refused_root_exits_nonzero_with_a_workflow_annotation(self):
        # `::error::` is what puts the reason into the run summary, which is
        # where someone debugging a failed consumer run will look. And nothing
        # is written to stdout that could be mistaken for the environment line.
        result = subprocess.run(
            [
                sys.executable,
                str(ACTION_DIR / "verify.py"),
                "--root",
                str(self.root),
                "--release-sha",
                A_COMMIT,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::", result.stdout)
        self.assertNotIn("CONTINUUM_ENGINE_ROOT", result.stdout)


if __name__ == "__main__":
    unittest.main()
