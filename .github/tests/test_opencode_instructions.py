#!/usr/bin/env python3
"""Tests for Continuum's global OpenCode instructions layer.

Three claims are asserted here, and each of them can be false in a way nothing
else in the repository would notice:

1. **The resolution is explicit.** OpenCode reads global instructions from a
   path derived from ``OPENCODE_CONFIG_DIR`` / ``XDG_CONFIG_HOME`` / the home
   directory. A runner that exports any of those moves the file, and installing
   to the documented default would then be an agent with no policy and a green
   run. ``ResolutionTests`` pins the derivation instead of trusting it.
2. **Every OpenCode execution path gets the file.** ``WorkflowWiringTests``
   discovers the launch sites by parsing the workflows rather than by a
   hand-maintained list, so a new path that forgets the install fails here.
3. **The repository keeps its own instructions.** ``RepositoryInstructionTests``
   runs the installer inside a real git worktree and asserts the worktree is
   byte-for-byte and status-for-status unchanged, and
   ``WorktreeRefusalTests`` asserts the installer refuses a target inside the
   worktree at all.

Run with::

    python3 -m unittest discover -s .github/tests -p 'test_*.py'
"""

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github" / "scripts"
WORKFLOWS = ROOT / ".github" / "workflows"
CANONICAL = ROOT / ".github" / "agents" / "AGENTS.md"
SCRIPT = SCRIPTS / "opencode_instructions.py"

sys.path.insert(0, str(SCRIPTS))
import opencode_instructions as instructions  # noqa: E402

#: The workflows that must start an OpenCode agent. Discovery below asserts
#: this is exactly the set the workflows actually contain, so the declaration
#: cannot drift into being a smaller, comfortable list.
EXECUTION_PATHS = (
    "consumer-child-review.yml",
    "consumer-child-worker.yml",
    "consumer-opencode.yml",
    "opencode.yml",
)

INSTALL_TOKEN = "opencode_instructions.py install"
VERIFY_TOKEN = "opencode_instructions.py verify"

#: The launch markers, one per way a workflow can start the agent. A plain
#: ``opencode run`` is Continuum's own invocation; the composite action is the
#: upstream GitHub integration, which Continuum cannot gate from inside but can
#: and must still precede with the install.
LAUNCH_TOKENS = ("opencode run", "anomalyco/opencode")


def git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


class Sandbox:
    """A worktree, a sibling fake home, and a runner for the installer.

    The home is a *sibling* of the worktree rather than a child of it, because
    that is the real relationship: on a hosted runner ``$HOME`` is
    ``/home/runner`` and the worktree is ``/home/runner/work/<repo>/<repo>``, so
    the resolved global configuration directory is never inside the worktree.
    Nesting the home under the worktree would make the worktree-refusal guard
    fire on every happy-path test and prove nothing.
    """

    def __init__(self, case):
        self.temp = tempfile.TemporaryDirectory()
        case.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.home = self.base / "home"
        self.worktree = self.base / "repo"
        self.home.mkdir()
        self.worktree.mkdir()
        git(["init", "--quiet", "--initial-branch=main"], self.worktree)
        git(["config", "user.email", "test@example.invalid"], self.worktree)
        git(["config", "user.name", "Continuum Test"], self.worktree)
        (self.worktree / "README.md").write_text("# fixture\n", encoding="utf-8")
        (self.worktree / ".continuum.yml").write_text("version: 1\n", encoding="utf-8")
        git(["add", "-A"], self.worktree)
        git(["commit", "--quiet", "-m", "fixture"], self.worktree)

    def env(self, **overrides):
        environ = {
            "PATH": os.environ.get("PATH", ""),
            # Every path variable Continuum's own runner would not set, blanked
            # so a test starts from the default resolution rather than from
            # whatever this machine happens to export.
            "HOME": str(self.home),
            "OPENCODE_TEST_HOME": "",
            "XDG_CONFIG_HOME": "",
            "OPENCODE_CONFIG_DIR": "",
            "LANG": "C.UTF-8",
        }
        environ.update({k: v for k, v in overrides.items() if v is not None})
        return environ

    def run(self, *args, cwd=None, **overrides):
        # The default working directory is the worktree, because that is where
        # the installer runs in a real job: the agent is about to work there.
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=str(cwd or self.worktree),
            env=self.env(**overrides),
            capture_output=True,
            text=True,
        )

    def project_file(self, *parts):
        return self.worktree.joinpath(*parts)

    def git_status(self):
        return git(["status", "--porcelain"], self.worktree).stdout

    def global_instructions(self):
        return self.home / ".config" / "opencode" / "AGENTS.md"


class ResolutionTests(unittest.TestCase):
    """The path is derived from the runtime configuration, never assumed."""

    def test_default_is_the_documented_home_config_location(self):
        self.assertEqual(
            instructions.global_instruction_paths({}, home="/runner"),
            ["/runner/.config/opencode/AGENTS.md"],
        )

    def test_xdg_config_home_is_honoured_and_the_default_is_kept(self):
        # Both, not just the XDG one: whether this OpenCode build reads
        # $XDG_CONFIG_HOME or the home default is a property of the installed
        # version, and installing to only one of them is how the policy
        # silently goes missing on a runner that exports the other.
        self.assertEqual(
            instructions.global_instruction_paths(
                {"XDG_CONFIG_HOME": "/xdg"}, home="/runner"
            ),
            ["/xdg/opencode/AGENTS.md", "/runner/.config/opencode/AGENTS.md"],
        )

    def test_explicit_config_dir_override_is_honoured_and_the_default_is_kept(self):
        self.assertEqual(
            instructions.global_instruction_paths(
                {"OPENCODE_CONFIG_DIR": "/opt/opencode", "XDG_CONFIG_HOME": "/xdg"},
                home="/runner",
            ),
            [
                "/opt/opencode/AGENTS.md",
                "/xdg/opencode/AGENTS.md",
                "/runner/.config/opencode/AGENTS.md",
            ],
        )

    def test_test_home_outranks_home(self):
        # OpenCode checks OPENCODE_TEST_HOME before the home directory, so a
        # runner that sets it is redirecting every derived path.
        self.assertEqual(
            instructions.home_dir({"OPENCODE_TEST_HOME": "/th", "HOME": "/h"}), "/th"
        )
        self.assertEqual(instructions.home_dir({"HOME": "/h"}), "/h")

    def test_empty_variables_are_treated_as_unset(self):
        # An empty value concatenated into a path yields a *relative* path
        # rooted at the working directory, which would put the global
        # instructions inside the worktree.
        self.assertEqual(
            instructions.global_instruction_paths(
                {"OPENCODE_CONFIG_DIR": "", "XDG_CONFIG_HOME": ""}, home="/runner"
            ),
            ["/runner/.config/opencode/AGENTS.md"],
        )

    def test_duplicate_resolutions_are_collapsed(self):
        self.assertEqual(
            instructions.global_instruction_paths(
                {"XDG_CONFIG_HOME": "/runner/.config"}, home="/runner"
            ),
            ["/runner/.config/opencode/AGENTS.md"],
        )

    def test_unresolvable_environment_fails_instead_of_guessing(self):
        with self.assertRaises(instructions.InstructionError) as caught:
            instructions.global_config_dirs({}, home="")
        self.assertIn("No OpenCode global configuration directory", str(caught.exception))

    def test_paths_subcommand_reports_the_resolution(self):
        sandbox = Sandbox(self)
        result = sandbox.run("paths", HOME=str(sandbox.home))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split(),
            [str(sandbox.global_instructions())],
            result.stdout,
        )


class PathContainmentTests(unittest.TestCase):
    def test_containment_is_component_wise_not_string_prefix(self):
        # "/repo2" is not inside "/repo". A string prefix check would refuse a
        # legitimate target whose name merely starts with the worktree's name.
        self.assertTrue(instructions.is_within("/repo/AGENTS.md", "/repo"))
        self.assertTrue(instructions.is_within("/repo", "/repo"))
        self.assertFalse(instructions.is_within("/repo2/AGENTS.md", "/repo"))
        self.assertFalse(instructions.is_within("/other/AGENTS.md", "/repo"))


class InstallTests(unittest.TestCase):
    def test_install_writes_the_canonical_bytes(self):
        sandbox = Sandbox(self)
        result = sandbox.run("install", "--source", str(CANONICAL))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            sandbox.global_instructions().read_bytes(), CANONICAL.read_bytes()
        )
        self.assertIn("continuum-instructions installed", result.stderr)

    def test_install_covers_every_resolved_location(self):
        sandbox = Sandbox(self)
        xdg = sandbox.base / "xdg"
        override = sandbox.base / "override"
        result = sandbox.run(
            "install",
            "--source",
            str(CANONICAL),
            XDG_CONFIG_HOME=str(xdg),
            OPENCODE_CONFIG_DIR=str(override),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = CANONICAL.read_bytes()
        for path in (
            override / "AGENTS.md",
            xdg / "opencode" / "AGENTS.md",
            sandbox.global_instructions(),
        ):
            self.assertEqual(path.read_bytes(), expected, str(path))

    def test_install_reports_the_resolved_paths_on_both_sinks(self):
        sandbox = Sandbox(self)
        output = sandbox.base / "github_output"
        output.write_text("", encoding="utf-8")
        result = sandbox.run(
            "install", "--source", str(CANONICAL), GITHUB_OUTPUT=str(output)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        reported = str(sandbox.global_instructions())
        self.assertIn("{}={}".format(instructions.OUTPUT_KEY, reported), result.stdout)
        self.assertIn(
            "{}={}".format(instructions.OUTPUT_KEY, reported),
            output.read_text(encoding="utf-8"),
        )
        self.assertIn(
            instructions.DIGEST_OUTPUT_KEY,
            output.read_text(encoding="utf-8"),
        )

    def test_install_is_idempotent(self):
        sandbox = Sandbox(self)
        first = sandbox.run("install", "--source", str(CANONICAL))
        self.assertEqual(first.returncode, 0, first.stderr)
        second = sandbox.run("install", "--source", str(CANONICAL))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already current", second.stderr)
        self.assertEqual(
            sandbox.global_instructions().read_bytes(), CANONICAL.read_bytes()
        )

    def test_install_refreshes_its_own_previous_copy(self):
        sandbox = Sandbox(self)
        target = sandbox.global_instructions()
        target.parent.mkdir(parents=True)
        target.write_text(
            instructions.MARKER + "\nstale policy\n", encoding="utf-8"
        )
        result = sandbox.run("install", "--source", str(CANONICAL))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), CANONICAL.read_bytes())

    def test_install_refuses_to_clobber_a_file_it_does_not_own(self):
        # Someone else's global configuration is not Continuum's to delete, and
        # leaving it in place would run the agent under rules Continuum does
        # not control -- which is the failure this layer exists to prevent.
        sandbox = Sandbox(self)
        target = sandbox.global_instructions()
        target.parent.mkdir(parents=True)
        target.write_text("# somebody's personal rules\n", encoding="utf-8")
        result = sandbox.run("install", "--source", str(CANONICAL))
        self.assertEqual(result.returncode, 1)
        self.assertIn("was not written by Continuum", result.stderr)
        self.assertEqual(
            target.read_text(encoding="utf-8"), "# somebody's personal rules\n"
        )

    def test_install_refuses_a_dangling_symlink(self):
        sandbox = Sandbox(self)
        target = sandbox.global_instructions()
        target.parent.mkdir(parents=True)
        os.symlink(str(sandbox.base / "never-created"), str(target))
        result = sandbox.run("install", "--source", str(CANONICAL))
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a regular file", result.stderr)

    def test_install_refuses_a_symlink_to_a_real_file(self):
        # The case that matters: ``is_file`` follows the link and answers True,
        # so a check on file type alone would pass and the install would write
        # through the link into a path Continuum never resolved -- somewhere
        # entirely outside every global configuration directory it was given.
        sandbox = Sandbox(self)
        elsewhere = sandbox.base / "somebody-elses-file"
        elsewhere.write_text("not a config directory\n", encoding="utf-8")
        target = sandbox.global_instructions()
        target.parent.mkdir(parents=True)
        os.symlink(str(elsewhere), str(target))
        result = sandbox.run("install", "--source", str(CANONICAL))
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a regular file", result.stderr)
        self.assertEqual(
            elsewhere.read_text(encoding="utf-8"), "not a config directory\n"
        )

    def test_source_without_the_ownership_marker_is_rejected(self):
        # Without the marker the installer cannot tell its own document from a
        # stranger's, so the whole ownership rule would be unenforceable.
        sandbox = Sandbox(self)
        impostor = sandbox.base / "AGENTS.md"
        impostor.write_text("# no marker here\n", encoding="utf-8")
        result = sandbox.run("install", "--source", str(impostor))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ownership marker", result.stderr)
        self.assertFalse(sandbox.global_instructions().exists())

    def test_missing_source_fails(self):
        sandbox = Sandbox(self)
        result = sandbox.run("install", "--source", str(sandbox.base / "absent.md"))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(sandbox.global_instructions().exists())


class VerifyTests(unittest.TestCase):
    def test_verify_passes_after_install(self):
        sandbox = Sandbox(self)
        self.assertEqual(
            sandbox.run("install", "--source", str(CANONICAL)).returncode, 0
        )
        result = sandbox.run("verify", "--source", str(CANONICAL))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("continuum-instructions verified", result.stderr)

    def test_verify_fails_when_the_file_was_never_installed(self):
        # The failure this guards is invisible: an agent with no global
        # instructions does not error, it just behaves differently.
        sandbox = Sandbox(self)
        result = sandbox.run("verify", "--source", str(CANONICAL))
        self.assertEqual(result.returncode, 1)
        self.assertIn("are missing from", result.stderr)

    def test_verify_fails_when_the_file_was_tampered_with(self):
        sandbox = Sandbox(self)
        self.assertEqual(
            sandbox.run("install", "--source", str(CANONICAL)).returncode, 0
        )
        target = sandbox.global_instructions()
        target.write_text(
            instructions.MARKER + "\n# edited by something else\n", encoding="utf-8"
        )
        result = sandbox.run("verify", "--source", str(CANONICAL))
        self.assertEqual(result.returncode, 1)
        self.assertIn("was changed, replaced", result.stderr)

    def test_verify_fails_when_one_of_several_locations_is_missing(self):
        sandbox = Sandbox(self)
        xdg = sandbox.base / "xdg"
        self.assertEqual(
            sandbox.run(
                "install",
                "--source",
                str(CANONICAL),
                XDG_CONFIG_HOME=str(xdg),
            ).returncode,
            0,
        )
        (xdg / "opencode" / "AGENTS.md").unlink()
        result = sandbox.run("verify", "--source", str(CANONICAL), XDG_CONFIG_HOME=str(xdg))
        self.assertEqual(result.returncode, 1)
        self.assertIn("are missing from", result.stderr)

    def test_verify_does_not_repair_what_it_finds(self):
        sandbox = Sandbox(self)
        self.assertEqual(
            sandbox.run("install", "--source", str(CANONICAL)).returncode, 0
        )
        target = sandbox.global_instructions()
        target.unlink()
        self.assertEqual(
            sandbox.run("verify", "--source", str(CANONICAL)).returncode, 1
        )
        self.assertFalse(target.exists())


class WorktreeRefusalTests(unittest.TestCase):
    """The global layer may never become the project layer."""

    def test_install_refuses_a_target_inside_the_worktree(self):
        sandbox = Sandbox(self)
        # The runtime configuration resolves into the worktree. Writing there
        # would replace the repository's own AGENTS.md and every project rule
        # in it, so the installer stops instead.
        inside = sandbox.worktree / ".config"
        result = sandbox.run(
            "install",
            "--source",
            str(CANONICAL),
            XDG_CONFIG_HOME=str(inside),
            cwd=sandbox.worktree,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("inside the worktree", result.stderr)
        self.assertFalse((inside / "opencode" / "AGENTS.md").exists())

    def test_install_refuses_an_explicit_forbid_root(self):
        sandbox = Sandbox(self)
        result = sandbox.run(
            "install",
            "--source",
            str(CANONICAL),
            "--forbid-root",
            str(sandbox.home),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to treat", result.stderr)

    def test_the_working_directory_is_always_protected(self):
        sandbox = Sandbox(self)
        result = sandbox.run(
            "install", "--source", str(CANONICAL), cwd=sandbox.worktree
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((sandbox.worktree / "AGENTS.md").exists())


class RepositoryInstructionTests(unittest.TestCase):
    """The project's own instructions survive the injection, unchanged."""

    PROJECT_DOCS = {
        "AGENTS.md": "# Project instructions\n\nRun `make check` before finishing.\n",
        "CLAUDE.md": "# Claude fallback\n",
        ".opencode/agent/reviewer.md": "reviewer\n",
    }

    def populate(self, sandbox):
        for relative, body in self.PROJECT_DOCS.items():
            path = sandbox.project_file(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        git(["add", "-A"], sandbox.worktree)
        git(["commit", "--quiet", "-m", "project instructions"], sandbox.worktree)

    def test_install_leaves_the_worktree_byte_for_byte_identical(self):
        sandbox = Sandbox(self)
        self.populate(sandbox)
        before = {
            name: (sandbox.worktree / name).read_bytes()
            for name in self.PROJECT_DOCS
        }
        status_before = sandbox.git_status()

        result = sandbox.run(
            "install", "--source", str(CANONICAL), cwd=sandbox.worktree
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        for name, payload in before.items():
            self.assertEqual((sandbox.worktree / name).read_bytes(), payload, name)
        self.assertEqual(sandbox.git_status(), status_before)
        self.assertEqual(
            sorted(p.name for p in sandbox.worktree.iterdir()),
            sorted(
                [".continuum.yml", ".git", ".opencode", "AGENTS.md", "CLAUDE.md", "README.md"]
            ),
        )

    def test_verify_leaves_the_worktree_byte_for_byte_identical(self):
        sandbox = Sandbox(self)
        self.populate(sandbox)
        self.assertEqual(
            sandbox.run(
                "install", "--source", str(CANONICAL), cwd=sandbox.worktree
            ).returncode,
            0,
        )
        status_before = sandbox.git_status()
        result = sandbox.run(
            "verify", "--source", str(CANONICAL), cwd=sandbox.worktree
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sandbox.git_status(), status_before)
        self.assertEqual(
            (sandbox.worktree / "AGENTS.md").read_text(encoding="utf-8"),
            self.PROJECT_DOCS["AGENTS.md"],
        )

    def test_a_worktree_without_project_instructions_gets_none_invented(self):
        # Continuum's layer is global. A repository that ships no AGENTS.md
        # must not acquire one from the injection.
        sandbox = Sandbox(self)
        result = sandbox.run(
            "install", "--source", str(CANONICAL), cwd=sandbox.worktree
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((sandbox.worktree / "AGENTS.md").exists())
        self.assertEqual(sandbox.git_status(), "")


class CanonicalDocumentTests(unittest.TestCase):
    """The global document holds cross-project policy and nothing else."""

    def setUp(self):
        self.text = CANONICAL.read_text(encoding="utf-8")
        self.body = self.text.lower()

    def test_the_canonical_document_exists_and_is_owned(self):
        self.assertTrue(CANONICAL.is_file(), "{} is missing".format(CANONICAL))
        self.assertIn(instructions.MARKER, self.text)

    def test_the_canonical_document_is_not_a_repository_root_instruction_file(self):
        # Living at the repository root would make Continuum's global policy
        # load as *this* repository's project policy too, collapsing the very
        # separation the two layers exist to keep.
        self.assertNotEqual(CANONICAL, ROOT / "AGENTS.md")

    def test_it_carries_no_consumer_provider_or_platform_name(self):
        # The same standard the contract core is held to, read from the same
        # list, so there is one definition of a banned name in the repository
        # rather than two that drift. The list is deliberately not widened
        # here: this document has to name GitHub and OpenCode to mean anything,
        # so a broader net would forbid the words the policy is written in.
        banned = [
            line.strip()
            for line in (SCRIPTS / "contract-forbidden-names.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertTrue(banned, "the forbidden-name list is empty")
        for pattern in banned:
            self.assertIsNone(
                re.search(pattern, self.body),
                "global instructions must not name {}".format(pattern),
            )

    def test_it_carries_no_project_specific_build_or_release_mechanics(self):
        # These are the rules that made the global layer worth having: they are
        # the ones that must live in each repository, because they differ
        # between every two of them.
        for token in (
            "swift build",
            "swift test",
            "swift run",
            "version.swift",
            "release-please",
            "cocoapods",
            "gradle",
            "maven",
            "npm run",
            "make check",
            "xcodebuild",
            "tcc",
            "accessibility",
            "microphone",
            "notariz",
        ):
            self.assertNotIn(token, self.body, token)

    def test_it_names_no_project_source_path(self):
        for token in ("sources/", "tests/", "package.json", "pom.xml", "build.gradle", "makefile"):
            self.assertNotIn(token, self.body, token)

    def test_it_states_the_universal_policy(self):
        # One assertion per rule that is genuinely cross-repository, so removing
        # any of them is a test failure rather than a silent policy regression.
        for phrase in (
            "issue or pull request request is the task specification",
            "unrelated refactors",
            "continue through implementation",
            "never claim successful completion",
            "headless",
            "never request interactive approval",
            "the invoking workflow owns the git lifecycle",
            "focused automated tests",
            "weaken, skip, disable, or delete an existing test",
            "inside the repository worktree",
            "verify the relevant current upstream documentation",
            "in english",
        ):
            self.assertIn(phrase, self.body, phrase)

    def test_it_delegates_project_mechanics_to_the_repository(self):
        self.assertIn("project-specific rules belong in the repository", self.body)
        self.assertIn("source of truth for its build, test, and release", self.body)

    def test_it_does_not_duplicate_a_task_specification(self):
        # The issue or pull request is the specification. A global file that
        # restated acceptance criteria would be a second, staler one.
        for token in ("acceptance criteria:", "acceptance: pass", "review: pass", "closes #"):
            self.assertNotIn(token, self.body, token)

    def test_it_stays_a_policy_layer_rather_than_a_manual(self):
        # A capable coding model infers ordinary engineering advice. The file
        # earns its place only while it is the short list of decisions a model
        # would otherwise get wrong here, so a length ceiling is a control.
        self.assertLessEqual(
            len([line for line in self.text.splitlines() if line.strip()]),
            80,
            "the global instructions have grown into a second project document",
        )


def workflows():
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(WORKFLOWS.glob("*.yml"))}


def parsed(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def step_text(step):
    """Everything a step can say, as one string."""
    return "{}\n{}".format(step.get("run") or "", step.get("uses") or "")


def search_text(text):
    """Text with shell quoting removed, so a call site is found however it is
    spelled. ``python3 "$ROOT/.github/scripts/x.py" verify`` and an unquoted
    ``python3 $ROOT/.github/scripts/x.py verify`` are the same instruction, and
    the audit below must not depend on which one the workflow happened to use."""
    return text.replace('"', "").replace("'", "")


def jobs_of(name):
    return (parsed(name).get("jobs") or {})


def all_jobs(name):
    """Every job in a workflow, as a list.

    The audits below run over all of them rather than over one job by name: a
    workflow that moved its install into a second job would otherwise look
    covered while half its launch sites were not.
    """
    return list(jobs_of(name).values())


def positions(jobs, token):
    """Every ``(step_index, offset)`` at which ``token`` occurs, in order."""
    found = []
    for job in jobs:
        for index, step in enumerate(job.get("steps") or []):
            text = search_text(step_text(step))
            start = text.find(token)
            while start != -1:
                found.append((index, start))
                start = text.find(token, start + len(token))
    return sorted(found)


def launches(jobs):
    """Every ``(step_index, offset)`` that starts an OpenCode agent."""
    found = []
    for job in jobs:
        for index, step in enumerate(job.get("steps") or []):
            text = search_text(step_text(step))
            for token in LAUNCH_TOKENS:
                start = text.find(token)
                if start != -1:
                    found.append((index, start, token))
    return sorted(found)


class WorkflowWiringTests(unittest.TestCase):
    """Every OpenCode launch is preceded by the install, and by a verify."""

    def setUp(self):
        self.texts = workflows()

    def discovering_workflows(self):
        return {
            name
            for name, source in self.texts.items()
            if any(token in source for token in LAUNCH_TOKENS)
        }

    def test_the_execution_path_declaration_is_exact(self):
        # A hand-maintained list is only a control if it is complete. Discovery
        # finds the real launch sites; the two sets must be equal, so a new
        # execution path cannot join the workflows without joining this list.
        self.assertEqual(self.discovering_workflows(), set(EXECUTION_PATHS))

    def test_every_execution_path_installs_the_global_instructions(self):
        for name in sorted(self.discovering_workflows()):
            installs = positions(all_jobs(name), INSTALL_TOKEN)
            self.assertTrue(installs, "{} never installs the global instructions".format(name))

    def test_every_launch_is_preceded_by_an_install(self):
        for name in sorted(self.discovering_workflows()):
            installs = positions(all_jobs(name), INSTALL_TOKEN)
            for index, offset, token in launches(all_jobs(name)):
                self.assertTrue(
                    any(position < (index, offset) for position in installs),
                    "{}: {} at step {} is not preceded by an install".format(name, token, index),
                )

    def test_every_plain_invocation_is_preceded_by_a_verify(self):
        # `opencode run` is Continuum's own invocation, so it carries the
        # point-of-use gate the executable already carries: a global file that
        # was removed or replaced between install and launch must fail the step
        # rather than start an agent with no policy.
        for name in sorted(self.discovering_workflows()):
            verifies = positions(all_jobs(name), VERIFY_TOKEN)
            for index, offset, token in launches(all_jobs(name)):
                if token != "opencode run":
                    continue
                self.assertTrue(
                    any(position < (index, offset) for position in verifies),
                    "{}: opencode run at step {} is not preceded by a verify".format(name, index),
                )

    def test_a_failed_verify_stops_the_launch_in_the_child_workflows(self):
        # The child workflows wrap the agent in a retry loop, so they need
        # `set +e` around it -- and that is exactly the trap: a verify inside
        # the failure-tolerant block gates nothing, and the remaining passes
        # would burn under no policy while the job still looks like an ordinary
        # agent retry. The control is the ordering of the two, read literally.
        for name in ("consumer-child-worker.yml", "consumer-child-review.yml"):
            source = search_text(self.texts[name])
            verify = source.index(VERIFY_TOKEN)
            launch = source.index("opencode run", verify)
            guarded = source.rindex("set +e", 0, launch)
            self.assertGreater(
                guarded,
                verify,
                "{}: the failure-tolerant block starts before the verify, so the "
                "verify's exit status is discarded and the launch still "
                "happens".format(name),
            )
            self.assertLess(
                source.rindex(INSTALL_TOKEN, 0, verify),
                verify,
                "{}: the verify must come after the install it checks".format(name),
            )
            for match in re.finditer(
                r"set \+e\n(?P<body>(?:.*\n)*?)opencode run", source
            ):
                self.assertNotIn(
                    VERIFY_TOKEN,
                    match.group("body"),
                    "{}: the verify is inside the failure-tolerant block".format(name),
                )

    def test_the_install_names_the_canonical_document(self):
        # A relative default would resolve against whatever directory the step
        # happened to be in. The source is named explicitly on every call site.
        for name in sorted(self.discovering_workflows()):
            for job in all_jobs(name):
                for index, step in enumerate(job.get("steps") or []):
                    if INSTALL_TOKEN not in search_text(step_text(step)):
                        continue
                    self.assertIn(
                        ".github/agents/AGENTS.md",
                        step_text(step),
                        "{}: step {} installs from an unnamed source".format(name, index),
                    )

    def test_the_install_step_is_never_conditional(self):
        # An install guarded by the same condition as one launch mode covers
        # that mode only. Unconditional means every launch in the job is
        # covered, including modes added to the job later.
        for name in sorted(self.discovering_workflows()):
            for job in all_jobs(name):
                for index, step in enumerate(job.get("steps") or []):
                    if INSTALL_TOKEN not in search_text(step_text(step)):
                        continue
                    self.assertIsNone(
                        step.get("if"),
                        "{}: the install step is conditional, so it covers only "
                        "the launch modes its condition names".format(name),
                    )

    def test_the_engine_action_requires_the_new_files(self):
        # A partial engine checkout must fail at the action, not half way
        # through an agent run with the policy missing.
        action = (ROOT / ".github" / "actions" / "engine-path" / "action.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(".github/scripts/opencode_instructions.py", action)
        self.assertIn(".github/agents/AGENTS.md", action)

    def test_control_planes_do_not_start_an_agent(self):
        # The repair, scheduler, and merge controllers dispatch; they never run
        # a model. That is what keeps the execution-path list finite, and it is
        # asserted so a controller cannot quietly grow into a second launch site
        # that the install step above does not cover.
        for name, source in self.texts.items():
            if name in EXECUTION_PATHS:
                continue
            self.assertNotIn(
                "opencode run", source, "{} must not start an agent".format(name)
            )
            self.assertNotIn(
                "anomalyco/opencode", source, "{} must not start an agent".format(name)
            )

    def test_existing_runtime_guards_are_untouched(self):
        # The instructions layer is additive. The executable-identity gate must
        # still precede every launch, and it must be in the same step as the
        # launch it guards -- a guard verified in an earlier step says nothing
        # about the binary that later step executes.
        for name in ("opencode.yml", "consumer-opencode.yml"):
            for index, step in enumerate(jobs_of(name)["opencode"].get("steps") or []):
                text = step_text(step)
                launch = text.find("opencode run")
                if launch == -1:
                    continue
                self.assertIn(
                    "opencode_runtime.py verify",
                    search_text(text)[:launch],
                    "{}: step {} launches the agent with no runtime-identity "
                    "gate in the same step".format(name, index),
                )


if __name__ == "__main__":
    unittest.main()
