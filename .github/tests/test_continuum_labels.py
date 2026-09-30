#!/usr/bin/env python3
"""Regression tests for the canonical Continuum label registry.

The registry replaces the ``/oc`` comment as the activation signal, so these
tests hold three lines: the dispatch label has exactly one definition, every
lookup fails closed, and no workflow has re-inlined the label as a literal.

Run with::

    python3 -m unittest discover -s .github/tests -t .
"""

import json
import pathlib
import re
import subprocess
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / ".github" / "scripts"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
sys.path.insert(0, str(SCRIPTS_DIR))

import continuum_labels  # noqa: E402


class DispatchLabelIdentityTests(unittest.TestCase):
    """The activation signal is a single canonical name."""

    def test_the_dispatch_label_has_one_definition(self):
        self.assertEqual("continuum:dispatch", continuum_labels.DISPATCH_LABEL)

    def test_the_registry_declares_the_dispatch_label_first(self):
        self.assertEqual("dispatch", continuum_labels.LABEL_SPECS[0].key)
        self.assertEqual(
            continuum_labels.DISPATCH_LABEL, continuum_labels.LABEL_SPECS[0].name
        )

    def test_priority_labels_stay_ordered_high_to_low(self):
        self.assertEqual(
            ("priority:p0", "priority:p1", "priority:p2"),
            continuum_labels.PRIORITY_LABELS,
        )

    def test_issue_plane_labels_cover_every_owned_spec(self):
        self.assertEqual(
            [spec.name for spec in continuum_labels.LABEL_SPECS],
            list(continuum_labels.ISSUE_PLANE_LABELS),
        )

    def test_every_spec_has_a_renderable_description_and_colour(self):
        for spec in continuum_labels.LABEL_SPECS:
            self.assertTrue(spec.description.strip(), spec.name)
            self.assertRegex(spec.color, r"^[0-9a-fA-F]{6}$", spec.name)


class ResolveLabelTests(unittest.TestCase):
    """Every lookup that is not an exact hit must return None."""

    def test_the_dispatch_key_resolves(self):
        spec = continuum_labels.resolve_label("dispatch")
        self.assertIsNotNone(spec)
        self.assertEqual(continuum_labels.DISPATCH_LABEL, spec.name)

    def test_the_dispatch_name_resolves(self):
        spec = continuum_labels.resolve_label(continuum_labels.DISPATCH_LABEL)
        self.assertIsNotNone(spec)
        self.assertEqual("dispatch", spec.key)

    def test_label_lookup_ignores_case_and_surrounding_space(self):
        self.assertTrue(continuum_labels.is_dispatch_label("  Continuum:Dispatch "))
        self.assertTrue(continuum_labels.is_dispatch_label("CONTINUUM:DISPATCH"))

    def test_a_registry_key_is_never_the_dispatch_label(self):
        # Keys are an ergonomic lookup for the CLI and for workflow expressions.
        # The activation predicate compares names, so a repository's own
        # `dispatch` label cannot hand an issue to a write-capable agent.
        self.assertFalse(continuum_labels.is_dispatch_label("dispatch"))
        self.assertFalse(continuum_labels.is_dispatch_label("in-progress"))
        self.assertFalse(continuum_labels.is_dispatch_label(continuum_labels.PAUSED_LABEL))

    def test_every_registry_key_is_rejected_by_the_activation_predicate(self):
        keys = [spec.key for spec in continuum_labels.LABEL_SPECS]
        self.assertIn("dispatch", keys)
        for key in keys:
            self.assertFalse(continuum_labels.is_dispatch_label(key), key)

    def test_the_only_registry_name_that_activates_is_the_dispatch_label(self):
        for spec in continuum_labels.LABEL_SPECS:
            expected = spec.name == continuum_labels.DISPATCH_LABEL
            self.assertEqual(expected, continuum_labels.is_dispatch_label(spec.name), spec.name)

    def test_unrelated_repository_labels_do_not_resolve(self):
        for value in ("bug", "help wanted", "continuum", "continuum:dispatcher"):
            self.assertIsNone(continuum_labels.resolve_label(value), value)

    def test_non_string_and_empty_values_do_not_resolve(self):
        for value in (None, "", "   ", 0, 1, True, [], {}, ["continuum:dispatch"]):
            self.assertIsNone(continuum_labels.resolve_label(value), repr(value))
            self.assertFalse(continuum_labels.is_dispatch_label(value), repr(value))

    def test_a_near_miss_of_the_dispatch_label_does_not_resolve(self):
        for value in (
            "continuum",
            "continuum:",
            "continuum:dispatch:",
            "xcontinuum:dispatch",
            "continuum:dispatchx",
        ):
            self.assertFalse(continuum_labels.is_dispatch_label(value), value)


class RenderTests(unittest.TestCase):
    """Workflows cannot import Python at expression time, so the registry ships
    renderings that a step can turn back into the same names."""

    def test_env_rendering_uses_readable_keys(self):
        rendered = continuum_labels.as_env()
        self.assertIn(
            "CONTINUUM_LABEL_DISPATCH=continuum:dispatch", rendered.splitlines()
        )
        self.assertIn("CONTINUUM_LABEL_IN_PROGRESS=automation:in-progress", rendered)

    def test_json_rendering_carries_only_creatable_fields(self):
        entries = json.loads(continuum_labels.as_json())
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual({"name", "color", "description"}, set(entry))

    def test_provision_plan_matches_the_json_rendering(self):
        self.assertEqual(
            json.loads(continuum_labels.as_json()), continuum_labels.provision_plan()
        )

    def test_names_rendering_is_the_dispatch_label_alone_in_a_list(self):
        names = json.loads(continuum_labels.dispatch_labels_json())
        self.assertIn(continuum_labels.DISPATCH_LABEL, names)


class CliTests(unittest.TestCase):
    """The CLI is how a workflow step reads the registry."""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "continuum_labels.py"), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_list_env_exits_zero(self):
        result = self._run("list", "--format", "env")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("CONTINUUM_LABEL_DISPATCH=continuum:dispatch", result.stdout)

    def test_name_prints_the_canonical_name(self):
        result = self._run("name", "dispatch")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(continuum_labels.DISPATCH_LABEL, result.stdout.strip())

    def test_name_refuses_an_unknown_key(self):
        result = self._run("name", "nope")
        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout.strip())

    def test_check_accepts_the_dispatch_label_by_name(self):
        # Case folding is the API's own rule, so both spellings are the label.
        for value in ("continuum:dispatch", "Continuum:Dispatch"):
            result = self._run("check", value)
            self.assertEqual(0, result.returncode, value)

    def test_check_rejects_anything_else(self):
        # Including the bare registry key: `check` answers "is this issue
        # handed off?", and the answer must not depend on a second spelling of
        # the same word.
        for value in ("bug", "", "continuum:dispatcher", "dispatch"):
            result = self._run("check", value)
            self.assertEqual(1, result.returncode, value)


class NoReInlinedLabelTests(unittest.TestCase):
    """The registry is only canonical while nobody re-types the name.

    The scheduler and the agent workflow both have to recognise the dispatch
    label, and both read it from the registry. A literal in a workflow is the
    drift this test exists to catch.
    """

    def test_workflows_read_the_dispatch_label_from_the_registry(self):
        offenders = []
        pattern = re.compile(re.escape(continuum_labels.DISPATCH_LABEL))
        for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if pattern.search(line):
                    offenders.append("{0}:{1}".format(path.name, number))
        self.assertEqual(
            [],
            offenders,
            "the canonical dispatch label is inlined at {0}; read it from "
            ".github/scripts/continuum_labels.py".format(", ".join(offenders)),
        )

    def test_no_workflow_carries_the_removed_activation_comment_command(self):
        offenders = []
        pattern = re.compile(r"(?<![A-Za-z0-9])/oc(?![A-Za-z0-9])")
        for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if pattern.search(line):
                    offenders.append("{0}:{1}".format(path.name, number))
        self.assertEqual(
            [],
            offenders,
            "the /oc activation command is still present at {0}".format(
                ", ".join(offenders)
            ),
        )

    def test_no_script_defines_its_own_dispatch_label_constant(self):
        offenders = []
        pattern = re.compile(
            r"^\s*(DISPATCH_LABEL|CONTINUUM_DISPATCH_LABEL|CANONICAL_DISPATCH_LABEL)\s*=",
            re.M,
        )
        for path in sorted(SCRIPTS_DIR.glob("*.py")):
            if path.name == "continuum_labels.py":
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
