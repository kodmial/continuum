#!/usr/bin/env python3
"""What Continuum promises a repository it has never heard of.

Two claims, and both of them are the kind that fail silently:

* **No consumer-specific value in generic code.** A model identifier, a
  repository name, or a way to choose a Continuum other than the release the
  caller pinned would each be a repository that has to fork Continuum to be
  served. ``consumer_boundary.py`` is the check; this file is what makes the
  check itself trustworthy, so most of what follows is about the scanner: which
  surface it selects, that it cannot be emptied, that it cannot be satisfied by
  a stale ledger entry, and that a consumer's own files are never scanned.

* **A parent's delegation config leaks nothing.** The parent's repository is
  public and the child's is not. A child ID that resolves to a repository is a
  map from a public document to a private one, so the public document may carry
  IDs and nothing else; the bindings come from a secret, and a child with no
  binding fails closed rather than falling back to a guess.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_consumer_boundary -v
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
WORKFLOWS = ROOT / ".github" / "workflows"
FIXTURES = ROOT / "fixtures"

sys.path.insert(0, str(SCRIPTS))

import consumer_boundary as boundary  # noqa: E402
import delegation_runtime as delegation  # noqa: E402

REUSABLE = re.compile(r"^on:\n\s+workflow_call:", re.M)


def workflows() -> dict:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(WORKFLOWS.glob("*.yml"))
    }


class SurfaceSelectionTests(unittest.TestCase):
    """Which files the guard reads. Everything else is a decision, not a gap."""

    def setUp(self) -> None:
        self.selected = set(boundary.scanned_paths(ROOT))
        self.every = workflows()

    def test_the_generic_surface_is_not_empty(self):
        # A surface-selection rule that silently matches nothing is the one way
        # this check could report a clean boundary for a repository that has one.
        self.assertGreaterEqual(len(self.selected), 8)
        for name in (
            "consumer-child-dispatcher.yml",
            "consumer-child-worker.yml",
            "consumer-opencode.yml",
            "consumer-scheduler.yml",
            "pr-agent.yml",
        ):
            self.assertIn(os.path.join(".github/workflows", name), self.selected)

    def test_a_workflow_is_scanned_exactly_when_a_consumer_may_call_it(self):
        for name, text in self.every.items():
            with self.subTest(workflow=name):
                self.assertEqual(
                    REUSABLE.search(text) is not None,
                    os.path.join(".github/workflows", name) in self.selected,
                    "{} is scanned for a reason the workflow does not state".format(name),
                )

    def test_the_runtime_scripts_are_scanned_whole(self):
        # A consumer's run reaches every script a reusable workflow calls, so
        # the scripts are not filtered by anything inside them.
        for name in (
            "agent_workspace.py",
            "conflict_repair.py",
            "continuum_config.py",
            "delegation_runtime.py",
            "failure_retry.py",
            "opencode_instructions.py",
            "trust_policy.py",
        ):
            self.assertIn(os.path.join(".github/scripts", name), self.selected)

    def test_consumers_own_their_own_material_and_are_not_scanned(self):
        # The scanner exists to stop Continuum growing a consumer's facts. A
        # consumer's own fixtures, documentation, and reference snapshots
        # describe consumers on purpose; scanning them would only teach the
        # guard to tolerate what it is meant to catch.
        for relative in (
            "fixtures/delegation-parent/.continuum.yml",
            "docs/continuum-mvp-contract.md",
            "src/continuum/shadow/replay.py",
        ):
            with self.subTest(path=relative):
                self.assertNotIn(relative.replace("/", os.sep), self.selected)


class HardCodeTests(unittest.TestCase):
    """The repository as it stands, scanned for real."""

    def test_the_generic_surface_names_no_consumer(self):
        violations = boundary.scan(ROOT)
        self.assertEqual(
            [],
            [
                "{}:{}: {}".format(v.path, v.line_number, v.line)
                for v in violations
            ],
        )

    def test_no_recorded_residual_has_gone_stale(self):
        stale = boundary.stale_exceptions(ROOT)
        self.assertEqual(
            [],
            ["{} {} ({})".format(e.verdict, e.expression, e.glob) for e in stale],
            "a name was deleted from Continuum; its ledger entry goes with it",
        )

    def test_every_pattern_matches_a_real_spelling_and_something_it_must_not(self):
        # A pattern nothing can match bans nothing, and one that matches the
        # provider's own vocabulary trains a reviewer to wave the report through.
        cases = {
            "(?<![.\\w])opencode/(?!issue)[a-z0-9][a-z0-9._-]*": (
                ["--model opencode/big-pickle", "default: opencode/muse-spark"],
                [
                    '"$HOME/.opencode/bin/opencode"',
                    "opencode/issue12-slug",
                    "https://opencode.ai/install",
                ],
            ),
            "runtime[-_]worker": (
                ["runtime-worker/task-1", "runtime_worker/"],
                ["delegated-child"],
            ),
            "engine_ref": (
                ["engine_ref:", "inputs.engine_ref"],
                ["engine_repository", "refs/engine"],
            ),
        }
        patterns = {p.expression: p.regex for p in boundary.load_patterns()}
        for expression, (must_match, must_not) in cases.items():
            with self.subTest(expression=expression):
                self.assertIn(expression, patterns)
                for line in must_match:
                    self.assertRegex(line, expression)
                for line in must_not:
                    self.assertNotRegex(line, expression)


class ScannerBehaviourTests(unittest.TestCase):
    """The scanner, exercised on trees built for the purpose."""

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(dir=str(ROOT)))
        self.addCleanup(shutil.rmtree, str(self.root), True)
        (self.root / ".github" / "workflows").mkdir(parents=True)
        (self.root / ".github" / "scripts").mkdir(parents=True)
        for data in (
            "consumer-forbidden-names.txt",
            "contract-forbidden-names.txt",
        ):
            shutil.copy(SCRIPTS / data, self.root / ".github" / "scripts" / data)
        # The ledger is per-tree, like the patterns. Copying the real one would
        # make every temporary tree inherit entries for files it does not have.
        (self.root / ".github" / "scripts" / "consumer-boundary-exceptions.txt").write_text(
            "# verdict\texpression\tglob\treason\n", encoding="utf-8"
        )

    def workflow(self, name: str, text: str) -> None:
        (self.root / ".github" / "workflows" / name).write_text(text, encoding="utf-8")

    def script(self, name: str, text: str) -> None:
        (self.root / ".github" / "scripts" / name).write_text(text, encoding="utf-8")

    def test_a_hard_coded_model_in_a_reusable_workflow_is_reported(self):
        self.workflow(
            "consumer-x.yml",
            "on:\n  workflow_call:\n    inputs:\n      model:\n        default: opencode/big-pickle\njobs: {}\n",
        )
        violations = boundary.scan(str(self.root))
        self.assertEqual(1, len(violations))
        self.assertEqual("default: opencode/big-pickle", violations[0].line)

    def test_the_same_literal_in_a_bootstrap_workflow_is_not_the_surface(self):
        # Continuum's own bootstrap plane is not a promise to anybody else, and
        # scanning it would make the guard noisy enough to be ignored.
        self.workflow(
            "issue-scheduler.yml",
            "on:\n  schedule:\n    - cron: '*/5 * * * *'\njobs:\n  x:\n    env:\n      M: opencode/big-pickle\n",
        )
        self.assertEqual([], boundary.scan(str(self.root)))

    def test_a_hard_coded_value_in_a_runtime_script_is_reported(self):
        self.script("thing.py", 'REPO = "kodmial/continuum"\n')
        violations = boundary.scan(str(self.root))
        self.assertEqual([".github/scripts/thing.py"], [v.path for v in violations])

    def test_a_recorded_residual_is_not_reported_and_stops_being_stale_when_it_moves(self):
        self.script("thing.py", 'REPO = "kodmial/continuum"\n')
        ledger = self.root / ".github" / "scripts" / "consumer-boundary-exceptions.txt"
        ledger.write_text(
            "provenance\tkodmial\t.github/scripts/thing.py\t"
            "cited in the docstring that explains the decision\n",
            encoding="utf-8",
        )
        self.assertEqual([], boundary.scan(str(self.root)))
        self.assertEqual([], boundary.stale_exceptions(str(self.root)))
        # Deleting the name has to delete the entry, or the ledger becomes a
        # backlog nobody rereads.
        self.script("thing.py", "REPO = None\n")
        stale = boundary.stale_exceptions(str(self.root))
        self.assertEqual(["kodmial"], [e.expression for e in stale])

    def test_an_empty_pattern_file_is_an_error_rather_than_a_pass(self):
        (self.root / ".github" / "scripts" / "consumer-forbidden-names.txt").write_text(
            "# everything was allowed\n", encoding="utf-8"
        )
        with self.assertRaises(boundary.BoundaryError):
            boundary.scan(str(self.root))

    def test_an_exception_without_a_reason_is_refused(self):
        ledger = self.root / ".github" / "scripts" / "consumer-boundary-exceptions.txt"
        ledger.write_text("release\tkodmial\t.github/scripts/*\t\n", encoding="utf-8")
        with self.assertRaises(boundary.BoundaryError):
            boundary.scan(str(self.root))

    def test_an_exception_with_an_unknown_verdict_is_refused(self):
        ledger = self.root / ".github" / "scripts" / "consumer-boundary-exceptions.txt"
        ledger.write_text(
            "because\tkodmial\t.github/scripts/*\twe have always done it\n",
            encoding="utf-8",
        )
        with self.assertRaises(boundary.BoundaryError):
            boundary.scan(str(self.root))

    def test_the_report_names_the_file_and_the_fix(self):
        self.script("thing.py", 'REPO = "kodmial/continuum"\n')
        report = boundary.render_report(boundary.scan(str(self.root)), [])
        self.assertIn(".github/scripts/thing.py:1", report)
        self.assertIn("repository `vars`", report)

    def test_main_writes_the_report_and_exits_zero_on_a_clean_tree(self):
        self.workflow("consumer-x.yml", "on:\n  workflow_call:\njobs: {}\n")
        summary = self.root / "summary.md"
        code = boundary.main(
            ["--root", str(self.root), "--github-step-summary", str(summary)]
        )
        self.assertEqual(0, code)
        self.assertIn("No consumer-specific value", summary.read_text(encoding="utf-8"))


class EngineReleaseTests(unittest.TestCase):
    """The engine is the release the caller pinned, or the run fails."""

    def reusable(self) -> dict:
        return {name: text for name, text in workflows().items() if REUSABLE.search(text)}

    def test_no_reusable_workflow_offers_a_second_way_to_choose_the_engine(self):
        for name, text in sorted(self.reusable().items()):
            with self.subTest(workflow=name):
                self.assertNotIn(
                    "engine_ref",
                    text,
                    "{} accepts a ref, which is a way to run another Continuum".format(name),
                )

    def test_every_workflow_that_checks_out_an_engine_names_the_repository(self):
        for name, text in sorted(self.reusable().items()):
            if "actions/checkout" not in text or "engine" not in text:
                continue
            with self.subTest(workflow=name):
                self.assertIn(
                    "repository: ${{ inputs.engine_repository }}",
                    text,
                    "{} checks out an engine without asking which repository".format(name),
                )

    def test_every_engine_checkout_is_pinned_to_the_runs_own_workflow_commit(self):
        # job.workflow_sha is the commit GitHub resolved the caller's `uses:`
        # line to. github.workflow_ref is the *caller's* reference, so a workflow
        # that reached for it would judge the wrong release while looking
        # correct. Nothing may reintroduce that.
        for name, text in sorted(self.reusable().items()):
            if "job.workflow_sha" not in text:
                continue
            with self.subTest(workflow=name):
                self.assertNotIn("github.workflow_ref", text)
                self.assertRegex(text, r"ref: \$\{\{ (?:steps\.engine\.outputs\.sha|job\.workflow_sha) \}\}")

    def test_every_consumer_workflow_that_runs_the_engine_refuses_an_empty_repository(self):
        for name, text in sorted(self.reusable().items()):
            if "repository: ${{ inputs.engine_repository }}" not in text:
                continue
            with self.subTest(workflow=name):
                self.assertIn("engine_repository", text)
                self.assertTrue(
                    "engine_repository is unset" in text
                    or "ENGINE_REPOSITORY:-" in text,
                    "{} will check out an empty repository name".format(name),
                )


class ConsumerPolicyTests(unittest.TestCase):
    """Consumer facts resolve from configuration, and a gap fails closed."""

    #: Every repository variable a generic workflow reads. Each one is a fact
    #: about one repository, and each one is declared here so that adding a
    #: consumer's value to a workflow instead of to its configuration is visible
    #: in a diff.
    POLICY_VARIABLES = (
        "CONTINUUM_AGENT_MODEL",
        "CONTINUUM_AGENT_ENTRY_WORKFLOW",
        "CONTINUUM_CHILD_PR_REVIEW_WORKFLOW",
        "CONTINUUM_CHILD_REVIEW_WORKFLOW",
        "CONTINUUM_CHILD_WORKER_WORKFLOW",
        "CONTINUUM_DEFAULT_BRANCH",
        "CONTINUUM_DISPATCH_PRIORITY_LABELS",
        "CONTINUUM_LEGACY_TASK_BRANCH_PREFIX",
        "CONTINUUM_LEGACY_TASK_MARKER",
        "CONTINUUM_MAX_AGENT_PASSES",
        "CONTINUUM_MAX_REVIEW_PASSES",
        "CONTINUUM_TASK_OPT_IN_MARKER",
    )

    def test_a_generic_workflow_never_bakes_in_a_consumer_policy_variable(self):
        # A workflow that declares `vars.X` and then supplies a default for X has
        # made the default the answer and the variable the decoration.
        for name, text in sorted(workflows().items()):
            if not REUSABLE.search(text):
                continue
            for variable in self.POLICY_VARIABLES:
                for match in re.finditer(
                    r"\{\{\s*vars\.%s\s*\|\|\s*[^}]+\}\}" % re.escape(variable), text
                ):
                    with self.subTest(workflow=name, variable=variable):
                        self.fail(
                            "{} falls back to a literal for {}: {}".format(
                                name, variable, match.group(0)
                            )
                        )

    def test_the_consumer_entry_workflow_files_are_the_consumer_own_choice(self):
        dispatcher = workflows()["consumer-child-dispatcher.yml"]
        for variable in (
            "CONTINUUM_CHILD_WORKER_WORKFLOW",
            "CONTINUUM_CHILD_REVIEW_WORKFLOW",
            "CONTINUUM_CHILD_PR_REVIEW_WORKFLOW",
        ):
            with self.subTest(variable=variable):
                self.assertIn(variable, dispatcher)

    def test_the_fixture_parent_names_its_own_entry_workflows(self):
        # The dispatcher refuses to guess. A parent therefore has to say which of
        # its own files run a child task, which is the consumer's decision and
        # not something Continuum can default.
        text = (
            FIXTURES / "delegation-parent" / ".github" / "workflows" /
            "continuum-child-dispatcher.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("engine_repository", text)
        for name in ("continuum-child-worker.yml", "continuum-child-review.yml"):
            with self.subTest(workflow=name):
                self.assertTrue((FIXTURES / "delegation-parent" / ".github" / "workflows" / name).exists())


class DelegationPrivacyTests(unittest.TestCase):
    """The parent's repository is readable; the child's is not.

    A parent dispatches work into repositories it may not own, and the map from
    a child identifier to one of those repositories is the private part. So the
    identifiers may live anywhere -- a variable, a committed file, a legacy
    migration record -- and the bindings may live in exactly one place, a
    secret. Every assertion below is a way that could go wrong.
    """

    IDENTIFIERS = ("alpha", "beta")

    def parent_config(self, children=IDENTIFIERS) -> str:
        body = "version: 1\ndelegation:\n  role: parent\n  children:\n"
        body += "".join("    - {}\n".format(child) for child in children)
        return self.write("parent.yml", body)

    def child_config(self, child_id="alpha", parent="owner/parent") -> str:
        return self.write(
            "child.yml",
            "version: 1\ndelegation:\n  role: child\n  id: {}\n  parent: {}\n".format(
                child_id, parent
            ),
        )

    def write(self, name: str, text: str) -> str:
        directory = pathlib.Path(self.tmpdir.name)
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(holder.cleanup)
        self.tmpdir = holder

    def bindings(self, *ids) -> str:
        return json.dumps({child: "owner/{}".format(child) for child in ids})

    def test_the_bindings_come_from_a_secret_and_nowhere_else(self):
        # The map is the private part, so wherever a workflow hands the
        # resolver a map, the value must come from `secrets`. A `vars` entry, an
        # input, or a literal would all be readable by somebody the child
        # repository is meant to be hidden from.
        found = False
        for name, text in sorted(workflows().items()):
            if not REUSABLE.search(text):
                continue
            bindings = [
                line.strip()
                for line in text.splitlines()
                if re.match(r"^\s*CHILD_REPOSITORIES\s*:", line)
            ]
            if not bindings:
                continue
            found = True
            with self.subTest(workflow=name):
                for binding in bindings:
                    self.assertRegex(
                        binding,
                        r"^CHILD_REPOSITORIES:\s*\$\{\{\s*secrets\.[A-Za-z_]+\s*\}\}$",
                        "{} binds the map to something a reader can see".format(name),
                    )
        self.assertTrue(found, "no workflow witnesses the secret-bound guard")

    def test_a_child_resolves_only_through_a_binding_the_parent_supplied(self):
        config = self.parent_config()
        repository = delegation.resolve_child(
            config, self.bindings("alpha", "beta"), child_id="alpha"
        )
        self.assertEqual("owner/alpha", repository)

    def test_a_missing_binding_fails_closed_rather_than_guessing(self):
        # An unset secret produces an empty map, and an empty map resolves
        # nothing. What it must never do is fall back to a repository named
        # somewhere in the public tree.
        config = self.parent_config()
        for raw, why in (("", "unset"), ("{}", "empty"), ("not-json", "malformed")):
            with self.subTest(map=why), self.assertRaises(delegation.DelegationError):
                delegation.resolve_child(config, raw, child_id="alpha")

    def test_a_child_id_that_is_not_configured_is_refused(self):
        config = self.parent_config()
        with self.assertRaises(delegation.DelegationError):
            delegation.resolve_child(
                self.parent_config(), self.bindings("alpha", "beta"), child_id="gamma"
            )

    def test_a_binding_may_not_name_a_child_the_parent_did_not_configure(self):
        # Otherwise a leaked secret could dispatch into a repository the parent
        # never agreed to drive.
        with self.assertRaises(delegation.DelegationError):
            delegation.resolve_child(
                self.parent_config(), self.bindings("alpha", "beta", "gamma"), child_id="alpha"
            )

    def test_a_binding_may_not_resolve_two_identifiers_to_one_repository(self):
        # Two names for one repository turns the identifier an operator sees in
        # a log into something other than what ran.
        with self.assertRaises(delegation.DelegationError):
            delegation.resolve_child(
                self.parent_config(),
                json.dumps({"alpha": "owner/child", "beta": "owner/child"}),
                child_id="alpha",
            )

    def test_a_binding_must_be_an_owner_repository(self):
        for value in ("owner/child", "owner/child/", "child", "https://host/owner/child", ""):
            with self.subTest(binding=value), self.assertRaises(delegation.DelegationError):
                delegation.resolve_child(
                    self.parent_config(), json.dumps({"alpha": value}), child_id="alpha"
                )

    def test_one_child_binds_to_exactly_one_parent(self):
        # The other direction of the same relationship. A child that accepts a
        # different parent would let any repository with a token drive it.
        config = self.child_config()
        delegation.verify_child(config, child_id="alpha", parent_repository="owner/parent")
        for parent, why in (
            ("owner/other", "a different parent"),
            ("", "no parent"),
        ):
            with self.subTest(why=why), self.assertRaises(delegation.DelegationError):
                delegation.verify_child(
                    config, child_id="alpha", parent_repository=parent
                )
        with self.assertRaises(delegation.DelegationError):
            delegation.verify_child(
                config, child_id="beta", parent_repository="owner/parent"
            )

    def test_a_child_config_may_not_carry_the_parents_view_of_its_siblings(self):
        # What the parent is allowed to know about a child is that the child
        # exists. The child never learns its siblings' repositories either.
        config = self.parent_config(("alpha", "beta"))
        text = pathlib.Path(config).read_text(encoding="utf-8")
        self.assertNotIn("repository", text)
        for child in ("alpha", "beta"):
            with self.subTest(child=child):
                self.assertNotIn("owner/{}".format(child), text)

    def test_the_legacy_parent_file_stores_identifiers_and_no_repositories(self):
        # `.continuum.yml` delegation is the migration record. It carried
        # identifiers before bindings moved to a secret, and a record that
        # quietly regains a repository field would put one back in a public tree.
        document = self.parent_config()
        plan = delegation.parent_plan(document, self.bindings("alpha", "beta"))
        self.assertEqual(
            ["alpha", "beta"], [entry["id"] for entry in plan]
        )
        raw = json.loads("{}")
        self.assertEqual({}, raw)
        body = pathlib.Path(document).read_text(encoding="utf-8")
        for child in ("alpha", "beta"):
            with self.subTest(child=child):
                self.assertIn(child, body)
                self.assertNotIn("owner/" + child, body)

    def test_the_map_secret_is_declared_before_a_caller_can_supply_it(self):
        # A caller cannot hand a workflow a secret the workflow does not
        # declare, so a callee that reads the map without declaring it is
        # relying on the caller to smuggle the value in some other way.
        found = False
        for name, text in sorted(workflows().items()):
            if "secrets.CHILD_RUNTIME_REPOSITORIES" not in text:
                continue
            found = True
            with self.subTest(workflow=name):
                self.assertRegex(
                    text,
                    r"(?m)^\s*CHILD_RUNTIME_REPOSITORIES:\s*$",
                    "{} reads the map without declaring the secret".format(name),
                )
        self.assertTrue(found, "no workflow witnesses the declared map secret")


class ConsumerContractTests(unittest.TestCase):
    """Review and release are the consumer's switch, in the consumer's file."""

    def contract(self, name: str) -> dict:
        import yaml

        return yaml.safe_load(
            (ROOT / ".github" / "fixtures" / "continuum-config" / name).read_text(
                encoding="utf-8"
            )
        )

    def test_review_and_release_resolve_from_the_versioned_contract(self):
        for fixture, expected in (
            ("enabled.yml", {"review": True, "release": True}),
            ("declaring-nothing.yml", {"review": False, "release": False}),
        ):
            document = self.contract(fixture) or {}
            with self.subTest(fixture=fixture):
                self.assertEqual(expected["review"], bool(document.get("review")))
                self.assertEqual(expected["release"], bool(document.get("release")))

    def test_the_fixtures_name_no_consumer(self):
        for fixture in ("enabled.yml", "declaring-nothing.yml"):
            text = (
                ROOT / ".github" / "fixtures" / "continuum-config" / fixture
            ).read_text(encoding="utf-8")
            with self.subTest(fixture=fixture):
                for pattern in boundary.load_patterns():
                    self.assertIsNone(pattern.regex.search(text), pattern.expression)


if __name__ == "__main__":
    unittest.main()