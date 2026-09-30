"""The release pin is the consumer's one dependency, so its rules are the rules.

ADR-0002 makes a single exact literal reference the whole selection mechanism:
one pin has to cover the entire Continuum graph, an upgrade has to be that one
value, and publishing a newer release has to leave a consumer untouched. Each of
those is a property of text in a committed file, which is exactly what unit
tests of the reconcilers could not see.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
ENGINE = SCRIPTS / "continuum_engine.py"
WORKFLOWS = ROOT / ".github" / "workflows"
FIXTURES = ROOT / "fixtures"

sys.path.insert(0, str(SCRIPTS))

import continuum_engine as engine  # noqa: E402


def ingress(*pins: str, workflow: str = "consumer.yml") -> str:
    """A minimal ingress declaring one call per pin."""
    jobs = "".join(
        "  job{0}:\n    uses: {1}/.github/workflows/{2}@{3}\n".format(
            index, engine.ENGINE_REPOSITORY, workflow, pin
        )
        for index, pin in enumerate(pins)
    )
    return "name: Continuum\n\non:\n  workflow_dispatch:\n\njobs:\n" + jobs


class PinShapeTests(unittest.TestCase):
    def test_one_exact_release_is_the_whole_dependency(self):
        # The same pin repeated across the calls a consumer needs, which is the
        # only shape that keeps one job per surface -- and therefore one grant
        # per surface -- without giving each surface its own version.
        text = ingress("v0.3.0", "v0.3.0")
        self.assertEqual(engine.assert_single_pin(text, "consumer.yml"), "v0.3.0")
        self.assertEqual(len(engine.consumer_pins(text)), 2)

    def test_a_full_commit_sha_is_the_stricter_allowed_reference(self):
        sha = "a" * 40
        self.assertEqual(engine.assert_single_pin(ingress(sha), "consumer.yml"), sha)

    def test_two_different_pins_are_two_dependencies(self):
        # Each value on its own is exact, so nothing here is invalid in
        # isolation. What fails is that the repository now has to be upgraded in
        # step, which is the multi-file coordination ADR-0002 rejects.
        with self.assertRaises(engine.EngineError) as raised:
            engine.assert_single_pin(ingress("v0.3.0", "v0.3.1"), "consumer.yml")
        self.assertIn("one dependency", str(raised.exception))

    def test_a_named_surface_is_a_per_module_pin(self):
        # Pinning `consumer-opencode.yml` directly selects one implementation and
        # leaves the rest of the graph to whatever else the repository happens to
        # reference.
        with self.assertRaises(engine.EngineError) as raised:
            engine.assert_single_pin(
                ingress("v0.3.0", workflow="consumer-opencode.yml"), "consumer.yml"
            )
        self.assertIn(engine.RELEASE_ENTRYPOINT, str(raised.exception))

    def test_no_reference_at_all_is_not_a_selection(self):
        with self.assertRaises(engine.EngineError):
            engine.assert_single_pin("name: Continuum\njobs: {}\n", "consumer.yml")

    def test_a_floating_ref_is_refused(self):
        for ref in ("main", "master", "v1", "v1.2", "HEAD", "latest"):
            with self.subTest(ref=ref):
                with self.assertRaises(engine.EngineError) as raised:
                    engine.assert_single_pin(ingress(ref), "consumer.yml")
                self.assertIn("exact release", str(raised.exception))

    def test_only_a_full_sha_or_an_exact_release_is_pinnable(self):
        self.assertTrue(engine.is_pinnable("v0.3.0"))
        self.assertTrue(engine.is_pinnable("v0.3.0-rc.1"))
        self.assertTrue(engine.is_pinnable("b" * 40))
        for ref in ("v0.3", "v0", "a" * 39, "a" * 41, "main", "V0.3.0", ""):
            with self.subTest(ref=ref):
                self.assertFalse(engine.is_pinnable(ref))
                self.assertTrue(engine.is_floating(ref))


class UpgradeTests(unittest.TestCase):
    def test_an_upgrade_touches_the_pin_and_nothing_else(self):
        # The whole determinism claim: a rewrite is a substitution, so the diff a
        # reviewer reads after an upgrade is the pin and the pin.
        before = ingress("v0.3.0", "v0.3.0")
        after = engine.rewrite_pin(before, "v0.3.1")
        self.assertEqual(
            engine.assert_single_pin(after, "consumer.yml"), "v0.3.1"
        )
        self.assertEqual(
            after, before.replace("consumer.yml@v0.3.0", "consumer.yml@v0.3.1")
        )

    def test_a_rollback_is_the_same_operation(self):
        upgraded = engine.rewrite_pin(ingress("v0.3.0"), "v0.3.1")
        self.assertEqual(
            engine.rewrite_pin(upgraded, "v0.3.0"), ingress("v0.3.0")
        )

    def test_a_rewrite_refuses_a_ref_that_is_not_exact(self):
        with self.assertRaises(engine.EngineError):
            engine.rewrite_pin(ingress("v0.3.0"), "main")

    def test_the_cli_reports_and_rewrites_the_pin(self):
        target = FIXTURES / "consumer-repo" / ".github" / "workflows" / "continuum.yml"
        reported = subprocess.run(
            [sys.executable, str(ENGINE), "pin", "--ingress", str(target)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            reported.stdout.strip(), "{}@{}".format(engine.RELEASE_REFERENCE, "v0.1.0")
        )
        rewritten = subprocess.run(
            [
                sys.executable,
                str(ENGINE),
                "rewrite",
                "--ingress",
                str(target),
                "--pin",
                "v9.9.9",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            rewritten.stdout, target.read_text(encoding="utf-8").replace("v0.1.0", "v9.9.9")
        )


class RepositoryUpgradeTests(unittest.TestCase):
    """A consumer's dependency is the repository, not one file.

    A provider entrypoint and a dispatch target are separate files in the same
    consumer repository, and they are one dependency. Asserting each file on its
    own cannot catch a repository that is half upgraded -- every file holds one
    pin, and the repository still has two. So the value is resolved across the
    repository and an upgrade moves all of it together.
    """

    def setUp(self):
        self.work = pathlib.Path(
            tempfile.mkdtemp(prefix="continuum-engine-test-")
        )
        self.addCleanup(shutil.rmtree, self.work, True)

    def consumer(self):
        """A throwaway copy of the consumer fixture, so a rewrite can be real."""
        target = self.work / "consumer-repo"
        shutil.copytree(FIXTURES / "consumer-repo", target)
        for cache in target.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        return target

    def pin_file(self, repo, name, pin):
        path = repo / ".github" / "workflows" / name
        path.write_text(
            re.sub(
                r"(consumer\.yml@)\S+", r"\g<1>" + pin, path.read_text(encoding="utf-8")
            ),
            encoding="utf-8",
        )

    def test_one_value_is_resolved_across_every_referencing_file(self):
        repo = self.consumer()
        self.assertEqual(engine.repository_pin(repo), "v0.1.0")

    def test_a_file_that_references_nothing_is_not_an_error(self):
        # The consumer's own release internals are Continuum-free on purpose, so
        # a repository-wide check has to skip them rather than fail on them.
        repo = self.consumer()
        self.assertFalse(
            engine.consumer_pins(
                (repo / ".github" / "workflows" / "continuum-release.yml").read_text(
                    encoding="utf-8"
                )
            )
        )
        self.assertEqual(engine.repository_pin(repo), "v0.1.0")

    def test_an_upgrade_moves_every_file_and_a_rollback_restores_them(self):
        repo = self.consumer()
        before = {
            path: path.read_bytes() for path, _text in engine.ingresses(repo)
        }
        changed = engine.rewrite_repository(repo, "v9.9.9")
        self.assertEqual(len(changed), 3, "every referencing file moves together")
        self.assertEqual(engine.repository_pin(repo), "v9.9.9")
        engine.rewrite_repository(repo, "v0.1.0")
        self.assertEqual(engine.repository_pin(repo), "v0.1.0")
        for path, original in before.items():
            with self.subTest(workflow=path.name):
                self.assertEqual(path.read_bytes(), original)

    def test_a_rewrite_to_the_current_pin_changes_nothing(self):
        # Idempotence matters because the upgrade is expected to be a reviewable
        # diff: running the tool twice must not produce a second change to read.
        repo = self.consumer()
        self.assertEqual(engine.rewrite_repository(repo, "v0.1.0"), [])

    def test_a_half_upgraded_repository_is_reported_and_left_alone(self):
        # Guessing is the drift this exists to prevent: an operator who runs the
        # upgrade on a repository that is already split would otherwise be told
        # it succeeded and left believing all of it moved.
        repo = self.consumer()
        self.pin_file(repo, "continuum-agent.yml", "v0.2.0")
        with self.assertRaises(engine.EngineError) as raised:
            engine.repository_pin(repo)
        self.assertIn("2 different Continuum releases", str(raised.exception))
        with self.assertRaises(engine.EngineError):
            engine.rewrite_repository(repo, "v9.9.9")
        # Untouched: still the two releases it was, not silently completed into
        # one by the tool that was asked to move it.
        self.assertEqual(
            {
                pin
                for _path, text in engine.ingresses(repo)
                for _name, pin in engine.consumer_pins(text)
            },
            {"v0.1.0", "v0.2.0"},
        )
        self.pin_file(repo, "continuum-agent.yml", "v0.1.0")
        self.assertEqual(engine.repository_pin(repo), "v0.1.0")

    def test_a_repository_that_selects_nothing_is_reported(self):
        empty = self.work / "empty"
        (empty / ".github" / "workflows").mkdir(parents=True)
        (empty / ".github" / "workflows" / "unrelated.yml").write_text(
            "name: Not Continuum\non: workflow_dispatch:\njobs: {}\n", encoding="utf-8"
        )
        with self.assertRaises(engine.EngineError) as raised:
            engine.repository_pin(empty)
        self.assertIn("selects no Continuum release", str(raised.exception))

    def test_the_repository_mode_rewrites_on_disk(self):
        repo = self.consumer()
        result = subprocess.run(
            [
                sys.executable,
                str(ENGINE),
                "rewrite",
                "--repo",
                str(repo),
                "--pin",
                "v0.9.9",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(engine.repository_pin(repo), "v0.9.9")
        self.assertIn("continuum-agent.yml", result.stderr)

    def test_passing_both_or_neither_target_is_refused(self):
        # An ambiguous invocation is not something to resolve by preferring one
        # flag: the two modes answer different questions about different scopes.
        for arguments in (
            ["pin"],
            ["pin", "--repo", str(self.consumer()), "--ingress", "x.yml"],
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, str(ENGINE)] + arguments,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--ingress", result.stderr)


class EngineCheckoutTests(unittest.TestCase):
    """A checkout that is not the pinned commit, or is partial, is refused."""

    def test_this_repository_is_a_complete_engine(self):
        # The required list is read out of the module and checked against the
        # working tree, so a path that does not exist cannot sit in it.
        self.assertEqual(engine.missing_paths(ROOT), [])
        self.assertIn(
            ".github/agents/AGENTS.md", engine.REQUIRED_PATHS
        )

    def test_a_missing_file_fails_the_assertion(self):
        incomplete = ROOT / ".github" / "tests" / "fixtures"
        self.assertTrue(incomplete.is_dir())
        with self.assertRaises(engine.EngineError) as raised:
            engine.assert_engine(incomplete)
        self.assertIn("incomplete", str(raised.exception))

    def test_a_missing_checkout_fails_the_assertion(self):
        with self.assertRaises(engine.EngineError):
            engine.assert_engine(ROOT / ".github" / "scripts" / "no-such-engine")

    def test_the_wrong_commit_fails_the_assertion(self):
        # Completeness is not enough: a checkout at a different revision is the
        # exact failure one release pin exists to make impossible, because the
        # workflow graph and the code it runs would come from two releases.
        with self.assertRaises(engine.EngineError) as raised:
            engine.assert_engine(ROOT, expect_sha="a" * 40)
        self.assertIn("expected", str(raised.exception))


class RepositoryWiringTests(unittest.TestCase):
    """The engine's own workflows have to hold to the rule they enforce."""

    def workflow_texts(self):
        return {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(WORKFLOWS.glob("*.yml"))
        }

    def test_no_workflow_lets_a_caller_choose_the_engine_revision(self):
        # `engine_ref` and friends are the whole thing ADR-0002 removes: a
        # revision chosen at call time rather than by the pin. The shadow harness
        # keeps one on purpose, because a human dispatching it by hand has no
        # caller pin to inherit from.
        for name, text in self.workflow_texts().items():
            if name == "continuum-shadow.yml":
                continue
            with self.subTest(workflow=name):
                self.assertNotIn("engine_ref", text)

    def test_no_workflow_references_continuum_by_a_moving_ref(self):
        pattern = re.compile(
            r"uses:\s*"
            + re.escape(engine.ENGINE_REPOSITORY)
            + r"/\.github/workflows/[^\s@]+@(\S+)"
        )
        for name, text in self.workflow_texts().items():
            for ref in pattern.findall(text):
                with self.subTest(workflow=name, ref=ref):
                    self.assertTrue(engine.is_pinnable(ref))

    def test_no_fixture_file_is_reachable_at_a_moving_ref(self):
        # The delegation fixtures used to call `@main`, which made a consumer's
        # child relationships follow whatever the branch happened to hold.
        for path in sorted(FIXTURES.glob("*/.github/workflows/*.yml")):
            text = path.read_text(encoding="utf-8")
            for _, ref in engine.consumer_pins(text):
                with self.subTest(fixture=path.parent.parent.parent.name, ref=ref):
                    self.assertTrue(engine.is_pinnable(ref))


if __name__ == "__main__":
    unittest.main()