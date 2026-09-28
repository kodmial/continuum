"""The runner: the guarantees that hold whatever an adapter asked for.

The adapter tests check that the Apple plan says the right thing. These check
that *executing* it cannot do the wrong thing — that teardown happens after a
failure, that a secret does not reach a log, that a pin is enforced, and that
no argument is ever handed to a shell. A plan can be perfectly correct and
still leak a private key if the thing running it forgets to clean up.
"""

from __future__ import annotations

import ast
import base64
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from typing import Dict, List

from continuum.release import plan as plan_module
from continuum.release import run as run_module
from continuum.release.plan import PlanStep, ReleasePlan

# A stand-in for `codesign`/`security` that behaves the way a test tells it to.
# The point is to drive the *runner* over an argv vector without a macOS runner,
# so the script is a Python one and the argv is recorded rather than executed
# as a shell command.
FAKE_TOOL = textwrap.dedent(
    """
    import json, os, sys
    record = os.environ["FAKE_TOOL_LOG"]
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
    exit_code = int(os.environ.get("FAKE_TOOL_EXIT", "0"))
    sys.stdout.write(os.environ.get("FAKE_TOOL_STDOUT", ""))
    sys.stderr.write(os.environ.get("FAKE_TOOL_STDERR", ""))
    sys.exit(exit_code)
    """
)

# Set by `Harness.setUp` so `make_plan` can build steps that are recorded.
TOOL: List[str] = [sys.executable]


class Harness(unittest.TestCase):
    """A runner wired to a recording stand-in for the Apple toolchain."""

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="continuum-release-test-")
        self.addCleanup(shutil.rmtree, self.workdir, True)
        self.tool_dir = os.path.join(self.workdir, "bin")
        os.makedirs(self.tool_dir)
        self.tool = os.path.join(self.tool_dir, "fake-apple-tool")
        with open(self.tool, "w", encoding="utf-8") as handle:
            handle.write(FAKE_TOOL)
        os.chmod(self.tool, os.stat(self.tool).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.workdir, "argv.log")
        open(self.log, "w", encoding="utf-8").close()
        TOOL[:] = [sys.executable, self.tool]

    def environment(self, **overrides: str) -> Dict[str, str]:
        env = {
            "PATH": f"{self.tool_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_TOOL_LOG": self.log,
        }
        env.update(overrides)
        return env

    def recorded(self) -> List[List[str]]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, "r", encoding="utf-8") as handle:
            return [
                json.loads(line) for line in handle.read().splitlines() if line.strip()
            ]

    def tool_step(self, name: str, *argv: str, **kwargs) -> PlanStep:
        return PlanStep(name=name, argv=[sys.executable, self.tool, *argv], **kwargs)


def make_plan(
    steps,
    *,
    pinned="CI Signing",
    degraded=False,
    teardown=True,
    scratch=(),
    teardown_argv=("gone",),
):
    """A plan whose teardown goes through the recording stand-in, not `true`.

    Using a real `true` for teardown would hide the step from the log and make
    "did teardown run?" untestable, which is the one question these tests exist
    to answer.
    """

    all_steps = list(steps)
    if teardown:
        all_steps.append(
            PlanStep(name="delete-keychain", argv=[*TOOL, *teardown_argv], teardown=True)
        )
    return ReleasePlan(
        target="macos",
        adapter="apple",
        platform="macos",
        build_strategy="swiftpm",
        distribution="direct",
        step_list=tuple(all_steps),
        signing_mode="self-signed-stable" if not degraded else "adhoc",
        signing_identity=pinned,
        pinned_identity="" if degraded else pinned,
        signature_pinned=not degraded,
        degraded=degraded,
        scratch_paths=tuple(scratch),
    )


class RunnerBehaviourTests(Harness):
    def test_steps_run_in_order_as_argument_vectors(self):
        plan = make_plan(
            [self.tool_step("first", "one"), self.tool_step("second", "two")]
        )
        result = run_module.run_plan(plan, environment=self.environment())
        self.assertTrue(result.ok)
        self.assertEqual(result.completed, ["first", "second", "delete-keychain"])
        self.assertEqual(self.recorded(), [["one"], ["two"], ["gone"]])

    def test_a_failing_step_stops_the_plan(self):
        plan = make_plan(
            [self.tool_step("first", "one"), self.tool_step("second", "two")]
        )
        result = run_module.run_plan(plan, environment=self.environment(FAKE_TOOL_EXIT="3"))
        self.assertFalse(result.ok)
        self.assertEqual(result.completed, [])
        self.assertEqual(len(self.recorded()), 2, "teardown still ran")

    def test_teardown_runs_after_a_failure_so_material_never_outlives_the_job(self):
        scratch = os.path.join(self.workdir, "material.p12")
        plan = make_plan(
            [self.tool_step("sign", "sign")],
            scratch=(scratch,),
            teardown_argv=("delete-keychain",),
        )
        result = run_module.run_plan(plan, environment=self.environment(FAKE_TOOL_EXIT="1"))
        self.assertFalse(result.ok)
        self.assertEqual(self.recorded()[-1], ["delete-keychain"])

    def test_a_failing_teardown_does_not_mask_the_original_failure(self):
        plan = make_plan(
            [self.tool_step("sign", "sign")],
            teardown_argv=("delete-keychain",),
        )
        result = run_module.run_plan(plan, environment=self.environment(FAKE_TOOL_EXIT="1"))
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.step, "sign")

    def test_scratch_files_are_removed_whatever_happened(self):
        scratch = os.path.join(self.workdir, "material.p12")
        with open(scratch, "w", encoding="utf-8") as handle:
            handle.write("private key material")
        plan = make_plan([self.tool_step("sign", "sign")], scratch=(scratch,))
        run_module.run_plan(plan, environment=self.environment(FAKE_TOOL_EXIT="1"))
        self.assertFalse(os.path.exists(scratch))

    def test_a_missing_tool_is_reported_as_a_toolchain_problem(self):
        plan = make_plan([PlanStep(name="sign", argv=["/nonexistent/apple-tool"])])
        result = run_module.run_plan(plan, environment={})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.code, "tool-missing")
        self.assertIn("toolchain", result.failure.remediation)


class PinEnforcementTests(Harness):
    def test_an_assert_step_fails_when_its_pin_is_absent(self):
        plan = make_plan(
            [
                PlanStep(
                    name="pin",
                    kind=plan_module.STEP_ASSERT,
                    argv=[sys.executable, self.tool],
                    expect="Authority=CI Signing",
                )
            ]
        )
        result = run_module.run_plan(
            plan,
            environment=self.environment(
                FAKE_TOOL_STDOUT="Identifier=com.example.app\nAuthority=Someone Else\n"
            ),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.code, "assertion-failed")
        self.assertIn("Authority=CI Signing", result.failure.message)

    def test_an_assert_step_passes_when_its_pin_holds(self):
        plan = make_plan(
            [
                PlanStep(
                    name="pin",
                    kind=plan_module.STEP_ASSERT,
                    argv=[sys.executable, self.tool],
                    expect="Authority=CI Signing",
                )
            ]
        )
        result = run_module.run_plan(
            plan, environment=self.environment(FAKE_TOOL_STDOUT="Authority=CI Signing\n")
        )
        self.assertTrue(result.ok)

    def test_a_pinned_plan_reports_that_it_pinned(self):
        plan = make_plan([self.tool_step("sign", "sign")])
        result = run_module.run_plan(plan, environment=self.environment())
        self.assertTrue(result.describe()["signature_pinned"])


class SecretContainmentTests(Harness):
    def test_a_secret_value_never_reaches_the_failure_detail(self):
        password = "correct-horse-battery-staple"
        plan = make_plan(
            [
                PlanStep(
                    name="import",
                    argv=[sys.executable, self.tool, plan_module.secret_slot("APPLE_P12_PASSWORD")],
                    uses_secrets=("APPLE_P12_PASSWORD",),
                )
            ]
        )
        result = run_module.run_plan(
            plan,
            environment=self.environment(
                APPLE_P12_PASSWORD=password,
                # The stand-in echoes its own argv the way a real tool does when
                # it complains about what it was handed.
                FAKE_TOOL_STDERR=f"security: import failed for -P {password}",
                FAKE_TOOL_EXIT="1",
            ),
        )
        self.assertFalse(result.ok)
        self.assertNotIn(password, result.failure.detail)
        self.assertIn("<redacted>", result.failure.detail)

    def test_a_missing_secret_is_a_failure_not_an_empty_string(self):
        plan = make_plan(
            [
                PlanStep(
                    name="import",
                    argv=[sys.executable, self.tool, plan_module.secret_slot("APPLE_P12_PASSWORD")],
                )
            ]
        )
        result = run_module.run_plan(plan, environment=self.environment())
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.code, "missing-secret")

    def test_an_empty_secret_is_treated_as_missing(self):
        plan = make_plan(
            [
                PlanStep(
                    name="import",
                    argv=[sys.executable, self.tool, plan_module.secret_slot("APPLE_P12_PASSWORD")],
                )
            ]
        )
        result = run_module.run_plan(plan, environment=self.environment(APPLE_P12_PASSWORD=""))
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.code, "missing-secret")

    def test_the_job_token_is_not_handed_to_a_signing_step(self):
        # A release step has no business seeing the job's GitHub credentials.
        os.environ["GITHUB_TOKEN"] = "ghs_a_token_value"
        self.addCleanup(os.environ.pop, "GITHUB_TOKEN", None)
        self.assertNotIn("GITHUB_TOKEN", run_module.base_environment())

    def test_materialization_writes_a_private_file_and_runs_no_command(self):
        target = os.path.join(self.workdir, "material.p12")
        plan = make_plan(
            [
                PlanStep(
                    name="materialize",
                    kind=plan_module.STEP_MATERIALIZE,
                    source_env="APPLE_P12",
                    path=target,
                    encoding=plan_module.ENCODING_BASE64,
                )
            ],
            scratch=(target,),
        )
        payload = base64.b64encode(b"certificate bytes").decode("ascii")
        result = run_module.run_plan(plan, environment=self.environment(APPLE_P12=payload))
        self.assertTrue(result.ok)
        self.assertEqual(self.recorded(), [["gone"]], "materializing ran no command")
        # Teardown removed the file, which is the whole point of declaring it.
        self.assertFalse(os.path.exists(target))

    def test_the_materialized_file_is_not_world_readable(self):
        target = os.path.join(self.workdir, "material.p12")
        self.addCleanup(lambda: os.path.exists(target) and os.remove(target))
        step = PlanStep(
            name="materialize",
            kind=plan_module.STEP_MATERIALIZE,
            source_env="APPLE_P12",
            path=target,
            encoding=plan_module.ENCODING_BASE64,
        )
        payload = base64.b64encode(b"certificate bytes").decode("ascii")
        runner = run_module.Runner(environment=self.environment(APPLE_P12=payload))
        runner.run_step(step)
        mode = stat.S_IMODE(os.stat(target).st_mode)
        self.assertEqual(mode & (stat.S_IRWXG | stat.S_IRWXO), 0, "no group or other access")

    def test_undecodable_material_is_reported_before_a_tool_sees_it(self):
        target = os.path.join(self.workdir, "material.p12")
        plan = make_plan(
            [
                PlanStep(
                    name="materialize",
                    kind=plan_module.STEP_MATERIALIZE,
                    source_env="APPLE_P12",
                    path=target,
                    encoding=plan_module.ENCODING_BASE64,
                )
            ]
        )
        result = run_module.run_plan(plan, environment=self.environment(APPLE_P12="not base64!!"))
        self.assertFalse(result.ok)
        self.assertIn("base64", result.failure.detail)
        self.assertFalse(os.path.exists(target))


class NoShellTests(Harness):
    def test_a_shell_metacharacter_in_configuration_stays_an_argument(self):
        # Nothing in the plan is ever interpolated into a command line, so a
        # bundle identifier is data even when it looks like an attack.
        marker = os.path.join(self.workdir, "pwned")
        hostile = f"com.example.app; touch {marker}"
        plan = make_plan([self.tool_step("sign", hostile)])
        result = run_module.run_plan(plan, environment=self.environment())
        self.assertTrue(result.ok)
        self.assertEqual(self.recorded(), [[hostile], ["gone"]])
        self.assertFalse(os.path.exists(marker))

    def test_backticks_and_command_substitution_are_inert(self):
        for hostile in ("`touch /tmp/x`", "$(touch /tmp/x)", "a && touch /tmp/x", "a | tee b"):
            with self.subTest(hostile=hostile):
                open(self.log, "w", encoding="utf-8").close()
                plan = make_plan([self.tool_step("sign", hostile)])
                result = run_module.run_plan(plan, environment=self.environment())
                self.assertTrue(result.ok)
                self.assertEqual(self.recorded()[0], [hostile])

    def test_the_runner_never_invokes_a_shell(self):
        with open(run_module.__file__, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("shell=False", text)
        self.assertNotIn("os.system", text)
        self.assertNotIn("os.popen", text)


class PlanTypeTests(unittest.TestCase):
    def test_a_step_needs_a_command_or_a_destination(self):
        with self.assertRaises(plan_module.ReleaseError):
            PlanStep(name="empty")
        with self.assertRaises(plan_module.ReleaseError):
            PlanStep(name="materialize", kind=plan_module.STEP_MATERIALIZE, source_env="X")
        with self.assertRaises(plan_module.ReleaseError):
            PlanStep(
                name="materialize",
                kind=plan_module.STEP_MATERIALIZE,
                source_env="X",
                path="/tmp/x",
                argv=["true"],
            )

    def test_only_an_assert_step_may_pin_output(self):
        with self.assertRaises(plan_module.ReleaseError):
            PlanStep(name="run", argv=["true"], expect="something")
        self.assertTrue(PlanStep(name="a", kind=plan_module.STEP_ASSERT, argv=["true"], expect="x"))

    def test_repeated_step_names_are_refused(self):
        with self.assertRaises(plan_module.ReleaseError):
            make_plan(
                [PlanStep(name="same", argv=["true"]), PlanStep(name="same", argv=["true"])],
                teardown=False,
            )

    def test_a_plan_needs_a_target(self):
        with self.assertRaises(plan_module.ReleaseError):
            ReleasePlan(
                target="",
                adapter="apple",
                platform="macos",
                build_strategy="swiftpm",
                distribution="direct",
                step_list=(PlanStep(name="a", argv=["true"]),),
                signing_mode="adhoc",
                degraded=True,
            )

    def test_a_degraded_plan_needs_no_teardown_because_it_holds_no_key(self):
        plan = make_plan([PlanStep(name="sign", argv=["true", "-"])], degraded=True, teardown=False)
        self.assertEqual(plan.teardown, ())

    def test_the_plan_document_names_its_schema(self):
        plan = make_plan([PlanStep(name="sign", argv=["true"])])
        self.assertEqual(plan.describe()["schema"], plan_module.PLAN_SCHEMA)


class CorePurityTests(unittest.TestCase):
    """The release core must not know what platform it is releasing for.

    `plan.py` and `run.py` describe and execute argument vectors. If they
    mention `codesign` or `keychain` in *code*, then the next platform to be
    added has to come and edit the runner, and the runner's guarantees — no
    shell, teardown always, secrets never printed — are now spread across files
    that each know about a different toolchain. A second platform is the
    cheapest way to find out whether the boundary is real.

    Prose is exempt, deliberately. A module docstring that says "a plan can be
    reviewed without a macOS runner anywhere in sight" is explaining the
    boundary, not crossing it, and a test that failed on that sentence would
    push the explanation out of the code where it belongs.
    """

    CORE_MODULES = (plan_module, run_module)

    FORBIDDEN = (
        "codesign",
        "keychain",
        "security",
        "plutil",
        "plistbuddy",
        "xcodebuild",
        "xcrun",
        "sw_vers",
        "entitlement",
        "notariz",
        "appstore",
        "app-store",
        "apple",
        "macos",
        "ios",
        "swift",
        "dmg",
        "homebrew",
        "macports",
    )

    def source(self, module) -> str:
        with open(module.__file__, "r", encoding="utf-8") as handle:
            return handle.read()

    def tree(self, module):
        return ast.parse(self.source(module))

    def executable_code(self, module) -> str:
        """The module's code, with comments and docstrings removed.

        Parsed rather than regexed, so a name hidden behind an alias or a
        concatenation is still seen, and a name that only appears in prose is
        correctly ignored.
        """

        tree = self.tree(module)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                body = getattr(node, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and id(node) in docstrings:
                node.value = ""
        kept = [
            node
            for node in ast.walk(tree)
            if not (isinstance(node, ast.Constant) and id(node) in docstrings)
        ]
        return "\n".join(ast.unparse(node) for node in kept)

    def test_the_core_code_never_names_a_platform_tool(self):
        for module in self.CORE_MODULES:
            code = self.executable_code(module)
            for term in self.FORBIDDEN:
                with self.subTest(module=module.__name__, term=term):
                    self.assertIsNone(
                        re.search(rf"\b{re.escape(term)}\w*", code, re.IGNORECASE),
                        f"{module.__name__} refers to {term!r} in code",
                    )

    def test_the_core_does_not_import_an_adapter(self):
        for module in self.CORE_MODULES:
            with self.subTest(module=module.__name__):
                imported = set()
                for node in ast.walk(self.tree(module)):
                    if isinstance(node, ast.Import):
                        imported.update(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.add(node.module)
                for name in imported:
                    self.assertNotIn("apple", name)

    def test_only_the_sanitizing_function_reads_the_process_environment(self):
        """A step's environment is decided by the caller.

        The runner may look at `os.environ` in exactly one place — building the
        sanitized base that a caller then hands to a step. If it looked
        anywhere else it would be reaching for job credentials at the moment it
        launches a command, which is the moment nobody is reviewing.
        """

        readers = []
        for module in self.CORE_MODULES:
            for node in ast.walk(self.tree(module)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                text = ast.unparse(node)
                if re.search(r"\bos\.environ\b", text):
                    readers.append(f"{module.__name__}.{node.name}")
        self.assertEqual(readers, ["continuum.release.run.base_environment"])

    def test_a_step_runs_with_the_environment_it_was_given(self):
        # `env=self.environment`, not a fresh read: the guarantee is that the
        # environment is exactly what the caller chose.
        source = self.source(run_module)
        self.assertIn("env=self.environment", source)

    def test_the_base_environment_strips_job_credentials(self):
        stripped = run_module.base_environment()
        for name in ("GITHUB_TOKEN", "GITHUB_OUTPUT", "RUNNER_TEMP", "CI_JOB_ID"):
            self.assertNotIn(name, stripped)
        self.assertIn("PATH", stripped)

    def test_the_core_never_reaches_for_a_shell(self):
        for module in self.CORE_MODULES:
            code = self.executable_code(module)
            with self.subTest(module=module.__name__):
                for forbidden in ("os.system", "os.popen", "getoutput", "shell=True"):
                    self.assertNotIn(forbidden, code)

    def test_the_core_documents_why_it_is_platform_neutral(self):
        # The docstring exemption above is only safe while the explanation is
        # present.
        for module in self.CORE_MODULES:
            with self.subTest(module=module.__name__):
                self.assertTrue((module.__doc__ or "").strip())


if __name__ == "__main__":
    unittest.main()
