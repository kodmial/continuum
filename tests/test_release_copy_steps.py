"""A copy step: the plan's claim about ordering, and the runner's execution.

A copy is not a command — there is nothing to execute — but it is a step, because
the ordering around it is the claim a reviewer reads: a bundle is assembled out
of binaries that have already been signed, and those copies have to happen after
the signatures and before the seal. These tests pin the two halves separately:
what a plan may say about a copy, and what the runner does with it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from typing import Dict, List, Mapping, Sequence

from continuum.release import plan as plan_module
from continuum.release import run as run_module
from continuum.release.plan import PlanStep, ReleasePlan
from continuum.release.run import CommandResult


def plan_with(*steps: PlanStep) -> ReleasePlan:
    return ReleasePlan(
        target="example",
        adapter="apple",
        platform="macos",
        build_strategy="swiftpm",
        distribution="direct",
        step_list=steps,
        signing_mode="ad-hoc",
        signature_pinned=False,
        degraded=True,
    )


class CopyStepShapeTests(unittest.TestCase):
    def test_pairs_the_paths_it_was_given(self):
        step = PlanStep(
            name="stage", kind=plan_module.STEP_COPY, argv=("a", "b", "c", "d")
        )
        self.assertEqual(step.copies(), (("a", "b"), ("c", "d")))

    def test_refuses_an_odd_number_of_paths(self):
        """The last path would have nowhere to go.

        Silently copying a prefix is how a bundle ends up holding the unsigned
        binary that a signature step covered somewhere else on disk.
        """

        with self.assertRaises(plan_module.ReleaseError) as caught:
            PlanStep(name="stage", kind=plan_module.STEP_COPY, argv=("a", "b", "c"))
        self.assertIn("alternating", str(caught.exception))

    def test_refuses_to_also_pin_output(self):
        """A copy asserts nothing; it moves bytes.

        An `expect` on a copy step would claim a check that no output can
        satisfy, and the check would never run.
        """

        with self.assertRaises(plan_module.ReleaseError):
            PlanStep(
                name="stage",
                kind=plan_module.STEP_COPY,
                argv=("a", "b"),
                expect="something",
            )

    def test_carries_no_secret_slot(self):
        """A copy step has no argument vector to substitute into.

        That is also why it cannot leak a secret into a failure message: there is
        no argv where a value could have been interpolated.
        """

        step = PlanStep(name="stage", kind=plan_module.STEP_COPY, argv=("a", "b"))
        self.assertEqual(step.uses_secrets, ())

    def test_is_a_known_kind_and_cannot_be_invented(self):
        self.assertEqual(
            PlanStep(name="s", kind=plan_module.STEP_COPY, argv=("a", "b")).kind,
            plan_module.STEP_COPY,
        )
        with self.assertRaises(plan_module.ReleaseError):
            PlanStep(name="s", kind="move", argv=("a", "b"))

    def test_appears_in_the_teardown_split_when_marked(self):
        step = PlanStep(
            name="stage", kind=plan_module.STEP_COPY, argv=("a", "b"), teardown=True
        )
        plan = plan_with(step)
        self.assertEqual(plan.step_names(), ["stage"])
        self.assertEqual(plan.steps, ())
        self.assertEqual(plan.teardown, (step,))


class CopyExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="continuum-copy-")
        self.addCleanup(shutil.rmtree, self.workdir, True)
        self.calls: List[List[str]] = []
        self.runner = run_module.Runner(
            environment={"PATH": os.environ.get("PATH", "")},
            workdir=self.workdir,
            command_runner=self.recording_runner,
        )

    def recording_runner(
        self,
        argv: Sequence[str],
        workdir: str,
        environment: Mapping[str, str],
    ) -> CommandResult:
        self.calls.append(list(argv))
        return CommandResult(code=0, output="")

    def write(self, relative: str, contents: str, *, mode: int = 0o644) -> str:
        path = os.path.join(self.workdir, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(contents)
        os.chmod(path, mode)
        return path

    def run_copy(self, *pairs: str, workdir: str = None) -> run_module.ExecutionResult:
        step = PlanStep(name="stage", kind=plan_module.STEP_COPY, argv=pairs)
        runner = (
            self.runner
            if workdir is None
            else run_module.Runner(
                environment={"PATH": os.environ.get("PATH", "")},
                workdir=workdir,
                command_runner=self.recording_runner,
            )
        )
        return runner.run(plan_with(step))

    def test_copies_the_bytes_and_starts_no_process(self):
        self.write("from/thing", "payload", mode=0o755)
        result = self.run_copy("from/thing", "to/thing")
        self.assertIsNone(result.failure)
        with open(os.path.join(self.workdir, "to/thing")) as handle:
            self.assertEqual(handle.read(), "payload")
        self.assertEqual(self.calls, [], "a copy is not a command")

    def test_keeps_the_mode_a_signature_covers(self):
        """`codesign` records the mode as part of what it signed.

        A copy that resets the executable bit produces a bundle whose seal
        describes bytes the bundle no longer holds.
        """

        self.write("from/tool", "payload", mode=0o755)
        self.run_copy("from/tool", "to/tool")
        self.assertTrue(os.access(os.path.join(self.workdir, "to/tool"), os.X_OK))

    def test_creates_the_destination_directory(self):
        self.write("from/thing", "payload")
        self.run_copy("from/thing", "deep/deeper/thing")
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, "deep/deeper/thing")))

    def test_copies_several_pairs_in_the_order_they_were_declared(self):
        self.write("a", "first")
        self.write("b", "second")
        result = self.run_copy("a", "out-a", "b", "out-b")
        self.assertEqual(result.completed, ["stage"])
        with open(os.path.join(self.workdir, "out-b")) as handle:
            self.assertEqual(handle.read(), "second")

    def test_resolves_absolute_paths_as_written(self):
        """An absolute path in a plan is absolute.

        The alternative — resolving it against the workdir twice — is how a
        release writes into a directory the repository never mentioned.
        """

        source = self.write("from/thing", "payload")
        outside = os.path.join(self.workdir, "..", "continuum-copy-outside")
        destination = os.path.normpath(os.path.join(outside, "thing"))
        result = self.run_copy(source, destination)
        self.addCleanup(shutil.rmtree, outside, True)
        self.assertIsNone(result.failure)
        self.assertTrue(os.path.isfile(destination))

    def test_names_the_pair_it_could_not_copy(self):
        self.write("from/thing", "payload")
        blocked = os.path.join(self.workdir, "blocker")
        with open(blocked, "w", encoding="utf-8") as handle:
            handle.write("a file where a directory has to be")
        result = self.run_copy("from/thing", "blocker/thing")
        self.assertIsNotNone(result.failure)
        self.assertIn("blocker/thing", result.failure.detail)

    def test_a_missing_source_fails_the_step_rather_than_skipping_it(self):
        result = self.run_copy("from/gone", "to/gone")
        self.assertIsNotNone(result.failure)
        self.assertIn("from/gone", result.failure.detail)
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "to/gone")))

    def test_a_dry_run_copies_nothing_and_succeeds(self):
        self.write("from/thing", "payload")
        result = run_module.run_plan(
            plan_with(PlanStep(name="stage", kind=plan_module.STEP_COPY, argv=("from/thing", "to/thing"))),
            workdir=self.workdir,
            dry_run=True,
            command_runner=self.recording_runner,
        )
        self.assertIsNone(result.failure)
        self.assertEqual(result.completed, ["stage"])
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "to/thing")))

    def test_the_ordering_a_plan_claims_is_the_order_the_runner_runs(self):
        """The whole point of a copy being a step.

        A signature, then a copy, then a seal — recorded in that order, with the
        copied bytes being the signed ones. An adapter that copied first would
        produce the same files and a different, unsealed release.
        """

        self.write("built/binary", "unsigned\n")
        order: List[str] = []

        def tools(
            argv: Sequence[str], workdir: str, environment: Mapping[str, str]
        ) -> CommandResult:
            order.append(os.path.basename(argv[-1]) if argv[-1] != "bundle" else "seal")
            if argv[0] == "sign":
                with open(os.path.join(self.workdir, "built/binary"), "a") as handle:
                    handle.write("signed\n")
                return CommandResult(code=0, output="signed")
            return CommandResult(code=0, output="")

        plan = plan_with(
            PlanStep(name="sign", argv=("sign", "built/binary")),
            PlanStep(name="assemble", kind=plan_module.STEP_COPY, argv=("built/binary", "App.app/Contents/MacOS/binary")),
            PlanStep(name="seal", argv=("seal", "bundle")),
        )
        result = run_module.Runner(
            environment={"PATH": ""}, workdir=self.workdir, command_runner=tools
        ).run(plan)
        self.assertIsNone(result.failure)
        self.assertEqual(result.completed, ["sign", "assemble", "seal"])
        with open(os.path.join(self.workdir, "App.app/Contents/MacOS/binary")) as handle:
            self.assertEqual(handle.read(), "unsigned\nsigned\n")
        self.assertEqual(order, ["binary", "seal"])

    def test_run_plan_passes_the_runner_through(self):
        self.write("from/thing", "payload")
        result = run_module.run_plan(
            plan_with(PlanStep(name="stage", kind=plan_module.STEP_COPY, argv=("from/thing", "to/thing"))),
            workdir=self.workdir,
            command_runner=self.recording_runner,
        )
        self.assertEqual(result.completed, ["stage"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()