"""The migration controller's entry point: `.github/workflows/continuum-migrate.yml`.

The controller is the most privileged workflow in this repository. It rewrites
every writer in a repository and merges the result, from a manual trigger, with a
write-capable credential. That makes it a set of properties about *what is
permitted* rather than about what is computed -- properties a unit test of the
engine cannot see, because the engine runs happily with whatever the workflow
hands it.

This module reads the workflow and the document beside it and asserts the
properties #28 names. Each assertion is a way the cutover could quietly become
dangerous without anything failing:

* the workflow can only be started by somebody the trust policy already accepts,
  and the credential is verified separately from the person who pressed the
  button;
* the read half cannot reach the write credential, and the write half runs only
  for the two modes that are allowed to write;
* the candidate is a commit this repository can vouch for, not a string a
  dispatcher supplied;
* a cutover is refused unless the gate named this head, this phase, and these
  file changes;
* and the document claims exactly what the workflow does, because a document that
  describes a control the code does not have is worse than no document.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "continuum-migrate.yml"
DOCS = REPO_ROOT / "docs" / "migration-controller.md"
README = REPO_ROOT / "README.md"

RUBY_YAML_TO_JSON = r"""
require "yaml"
require "json"
data = YAML.safe_load(File.read(ARGV[0]), aliases: true) || {}
data["on"] = data.delete(true) if data.key?(true)
puts JSON.generate(data)
"""


def load_workflow():
    result = subprocess.run(
        ["ruby", "-e", RUBY_YAML_TO_JSON, str(WORKFLOW)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def steps_of(job):
    return job.get("steps") or []


def job_text(job):
    parts = [json.dumps(job, sort_keys=True)]
    for step in steps_of(job):
        parts.append(step.get("run") or "")
        parts.append(json.dumps(step.get("with") or {}, sort_keys=True))
        parts.append(json.dumps(step.get("env") or {}, sort_keys=True))
    return "\n".join(parts)


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = load_workflow()
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = cls.document.get("jobs") or {}
        cls.docs = DOCS.read_text(encoding="utf-8")

    def test_it_is_a_manual_trigger_only(self):
        """Nothing automatic may start a cutover.

        A cutover needs a verdict from a window of evidence that only a person
        can know is complete. A trigger that fires on its own would mean the
        controller deciding for itself that the window was ready.
        """

        self.assertEqual(list((self.document.get("on") or {}).keys()), ["workflow_dispatch"])

    def test_a_dispatcher_has_to_pass_the_trust_policy_first(self):
        authorize = self.jobs["authorize"]
        plan = self.jobs["plan"]

        self.assertIn("check-dispatcher", job_text(authorize))
        # And the check is the first thing that happens, before a checkout that
        # could run anything.
        names = [step.get("name", "") for step in steps_of(authorize)]
        self.assertIn("Refuse a dispatch from an untrusted sender", names[1])
        self.assertEqual(plan["needs"], "authorize")

    def test_the_authorization_job_holds_nothing_at_all(self):
        """The job that decides who is allowed to ask must not itself be able to
        answer the question by having already acted."""

        self.assertEqual(self.jobs["authorize"]["permissions"], {})
        self.assertNotIn("TAP_PAT", job_text(self.jobs["authorize"]))
        self.assertNotIn("github.token", job_text(self.jobs["authorize"]))

    def test_the_credential_is_verified_in_the_job_that_holds_it(self):
        """Separately from the dispatcher.

        A trusted maintainer pressing the button does not make a PAT belonging to
        somebody else safe to spend, so the two identities are two checks.
        """

        act = self.jobs["act"]
        self.assertIn("check-token", job_text(act))
        self.assertIn("TAP_PAT", job_text(act))
        self.assertNotIn("TAP_PAT", job_text(self.jobs["plan"]))

    def test_the_reading_job_cannot_write(self):
        plan = self.jobs["plan"]

        self.assertEqual(
            sorted(plan["permissions"]),
            ["actions", "checks", "contents", "issues", "statuses"],
        )
        for scope, level in plan["permissions"].items():
            self.assertEqual(level, "read", scope)

    def test_the_write_job_holds_only_what_a_cutover_needs(self):
        self.assertEqual(
            self.jobs["act"]["permissions"],
            {"contents": "write", "pull-requests": "write", "issues": "write"},
        )

    def test_the_write_half_runs_only_for_the_modes_that_write(self):
        condition = str(self.jobs["act"].get("if") or "")

        self.assertIn("github.event.inputs.mode == 'apply'", condition)
        self.assertIn("github.event.inputs.mode == 'rollback'", condition)
        self.assertEqual(self.jobs["act"]["needs"], ["authorize", "plan"])

    def test_the_candidate_is_proven_rather_than_trusted(self):
        """A pin is a promise about what a consumer will run tomorrow.

        Three checks, because there are three ways to be wrong: a branch name
        (tomorrow's code is nobody's), a commit this repository has never seen
        (a fork, a dangling ref), and a commit this repository has seen but
        abandoned (an ancestor check catches that).
        """

        for needle in ("cat-file -e", "merge-base --is-ancestor", "^[0-9a-f]{40}$"):
            self.assertIn(needle, self.text, needle)
        # And no input may default to a branch.
        for match in re.finditer(r"default:\s*(\S+)", self.text):
            self.assertNotIn(match.group(1).strip("\"'"), ("main", "master"), match.group(0))

    def test_every_checkout_is_at_a_ref_github_chose(self):
        for job in self.jobs.values():
            for step in steps_of(job):
                if "actions/checkout" not in (step.get("uses") or ""):
                    continue
                self.assertIn(
                    "github.event.repository.default_branch",
                    str((step.get("with") or {}).get("ref", "")),
                    step.get("name"),
                )

    def test_no_expression_is_spliced_into_a_shell_script(self):
        """The runner substitutes `${{ }}` before the shell parses anything, so an
        expression inside a `run:` body is text the shell treats as code."""

        for job_name, job in self.jobs.items():
            for step in steps_of(job):
                for number, line in enumerate((step.get("run") or "").splitlines(), 1):
                    if "${{" not in line or line.lstrip().startswith("#"):
                        continue
                    self.fail(
                        "{}:{}:{}:{}".format(job_name, step.get("name"), number, line.strip())
                    )

    def test_apply_cannot_run_without_the_gate_verdict(self):
        apply_steps = [step for step in steps_of(self.jobs["act"]) if "migrate.cli apply" in (step.get("run") or "")]

        self.assertEqual(len(apply_steps), 1)
        self.assertIn("--gate migration-in/gate.json", apply_steps[0]["run"])
        self.assertIn("apply needs the cutover gate's verdict", self.text)

    def test_rollback_needs_the_record_the_cutover_wrote(self):
        rollback_steps = [
            step for step in steps_of(self.jobs["act"]) if "migrate.cli rollback" in (step.get("run") or "")
        ]

        self.assertEqual(len(rollback_steps), 1)
        self.assertIn("--record migration-in/record.json", rollback_steps[0]["run"])
        self.assertIn("rollback needs the cutover record", self.text)

    def test_the_ledger_is_required_by_everything_but_an_inventory(self):
        self.assertIn("the ledger input is required for", self.text)
        self.assertIn("writer roles come from it, never from a workflow's name", self.text)
        self.assertIn("--ledger migration-in/ledger.json", self.text)

    def test_evidence_is_uploaded_even_when_the_run_fails(self):
        uploads = [
            step
            for job in self.jobs.values()
            for step in steps_of(job)
            if "actions/upload-artifact" in (step.get("uses") or "")
        ]

        self.assertTrue(uploads)
        for step in uploads:
            self.assertEqual(step.get("if"), "always()", step.get("name"))

    def test_a_cancelled_run_cannot_be_the_thing_that_lost_the_state(self):
        """`apply` is invoked with no `--state` on purpose.

        Resuming from the branch's own tree is what makes a run cancelled between
        the push and the artifact upload recoverable. A `--state` the workflow
        could not supply would turn that into a stranded branch.
        """

        apply_steps = [step for step in steps_of(self.jobs["act"]) if "migrate.cli apply" in (step.get("run") or "")]

        # The trailing comment in the run block names the flag, so the assertion
        # is on the command itself rather than on the whole block.
        command = apply_steps[0]["run"].split("#", 1)[0]

        self.assertNotIn("--state", command)
        self.assertIn("No `--state` on purpose", self.text)

    def test_two_runs_cannot_cut_over_the_same_default_branch_at_once(self):
        self.assertFalse(self.document["concurrency"]["cancel-in-progress"])
        self.assertIn("github.repository", str(self.document["concurrency"]["group"]))

    def test_a_malformed_input_is_refused_rather_than_becoming_an_empty_one(self):
        """An unparseable gate document that became an empty one would turn a
        broken input into a silent "no" -- or worse, a silent "yes"."""

        self.assertIn("json.load(open(sys.argv[1]))", self.text)
        self.assertIn("Malformed input is a refusal", self.text)


class DocumentTest(unittest.TestCase):
    """The document has to describe this controller, not a nicer one."""

    @classmethod
    def setUpClass(cls):
        cls.docs = DOCS.read_text(encoding="utf-8")
        cls.readme = README.read_text(encoding="utf-8")
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = load_workflow().get("jobs") or {}

    def test_it_is_linked_from_the_readme(self):
        self.assertIn("docs/migration-controller.md", self.readme)

    def test_it_does_not_claim_the_cutover_has_happened(self):
        """A document written as if the migration were complete is the easiest
        possible way to convince a reader that a repository has been migrated."""

        self.assertIn("not yet run against a live repository", self.docs)
        self.assertNotIn("NanoDictate has been migrated", self.docs)

    def test_it_names_the_refusals_the_code_actually_implements(self):
        """Every code this list names is raised by the controller.

        A documented refusal that the code does not raise is a promise the
        repository does not keep; an implemented refusal the document omits is a
        refusal an operator finds out about the hard way.
        """

        for code in (
            "candidate_not_immutable",
            "unclassified_workflow",
            "phase_violation",
            "phase_not_implemented",
            "migration_branch_owned_elsewhere",
            "ref_moved_externally",
            "stranded_generated_caller",
            "gate_head_mismatch",
            "gate_phase_mismatch",
            "gate_change_set_missing",
            "gate_change_set_mismatch",
        ):
            with self.subTest(code=code):
                self.assertIn(code, self.docs, code)
                # Naming a code in a document is not the same as raising it. The
                # earlier audit only checked the document, so a prose sentence
                # could name a code nothing emits and the suite stayed green.
                self.assertIn(code, self.raised_codes(), code)

    def test_every_named_refusal_is_raised_somewhere_in_the_controller(self):
        """A documented code that nothing raises is a promise the repository does
        not keep; and a code the controller raises but the table omits is a
        refusal an operator meets for the first time in a failing run."""

        documented = set(self.documented_codes())

        self.assertTrue(documented, "the document names no refusal codes at all")
        for code in sorted(documented):
            with self.subTest(code=code):
                self.assertIn(code, self.raised_codes(), code)

    def test_prose_refusal_codes_are_raised_too(self):
        """The table is not the only place a code is written down.

        Prose names them as well -- "a plan that would leave a maintained workflow
        beside a generated one is refused (`phase_violation`)" -- and a reader
        searching a failing run for that name has to find it. Scanning the whole
        document rather than the table rows is what makes a code in prose as
        binding as one in the table.
        """

        source = DOCS.read_text(encoding="utf-8")
        raised = self.raised_codes()
        for code in sorted(set(re.findall(r"`([a-z]+(?:_[a-z0-9]+)+)`", source))):
            if code in self.not_a_refusal_code():
                continue
            with self.subTest(code=code):
                self.assertIn(code, raised, code)

    @staticmethod
    def not_a_refusal_code():
        """Backticked tokens in the document that name something other than a code.

        Listed rather than pattern-matched away, so a new one has to be
        classified instead of silently accepted.
        """

        return {
            # A function the document names as the thing that does the checking.
            "build_plan",
            "compute_restore",
            "phase_writers",
            # Client methods the document names as the boundary of what is read.
            "get_ref",
            "read_file_at_ref",
            # The GitHub trigger, which is a platform fact, not a refusal.
            "workflow_dispatch",
            # The workflow input that names which repository to cut over. A knob,
            # not a verdict: refusing it is a `set -euo pipefail` failure in the
            # workflow, and it happens before the engine is ever reached.
            "target_repository",
        }

    @staticmethod
    def documented_codes():
        """Every refusal code the document writes down, in the table or in prose."""

        source = DOCS.read_text(encoding="utf-8")
        candidates = set(
            re.findall(r"^\| `([a-z]+_[a-z_]+)` \|", source, re.MULTILINE)
        ) | set(re.findall(r"`([a-z]+(?:_[a-z0-9]+)+)`", source))
        return candidates - DocumentTest.not_a_refusal_code()

    @staticmethod
    def raised_codes():
        """Every refusal code the controller can actually emit.

        Read from the source rather than hand-listed, so a code added to the
        engine is covered the moment it exists and a code removed from the engine
        cannot linger in a test's expectation list.
        """

        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((REPO_ROOT / "src" / "continuum" / "migrate").glob("*.py"))
        )
        return set(
            re.findall(
                r"(?:PlanError|ApplyError|RollbackError|MigrationError|Reason|BaselineError)"
                r'\(\s*"([a-z][a-z0-9_]*)"',
                source,
            )
        )

    def test_the_jobs_it_describes_are_the_jobs_the_workflow_has(self):
        for job in ("authorize", "plan", "act"):
            self.assertIn(job, self.jobs, job)
            self.assertIn("`{}`".format(job), self.docs)

    def test_it_names_the_credential_and_the_two_checks(self):
        self.assertIn("TAP_PAT", self.docs)
        self.assertIn("check-dispatcher", self.docs)
        self.assertIn("check-token", self.docs)

    def test_it_says_phase_b_is_refused(self):
        """A phase that is not implemented does not get a partial implementation,
        and a document is where somebody would otherwise assume otherwise."""

        self.assertIn("phase-b` is refused", self.docs)
        self.assertIn("#21", self.docs)
        self.assertIn("#22", self.docs)


if __name__ == "__main__":
    unittest.main()
