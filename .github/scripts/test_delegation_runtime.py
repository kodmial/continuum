#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
import unittest

import delegation_runtime as runtime


def write(directory: str, name: str, text: str) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class DelegationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = self.tmpdir.name

    def parent(self, children=("alpha", "beta")) -> str:
        body = "version: 1\ndelegation:\n  role: parent\n  children:\n"
        body += "".join(f"    - {child}\n" for child in children)
        return write(self.tmp, "parent.yml", body)

    def child(
        self,
        child_id="alpha",
        parent="owner/parent",
        validation_script="",
    ) -> str:
        validation = (
            f"  validation_script: {validation_script}\n"
            if validation_script
            else ""
        )
        return write(
            self.tmp,
            "child.yml",
            "version: 1\ndelegation:\n"
            "  role: child\n"
            f"  id: {child_id}\n"
            f"  parent: {parent}\n"
            + validation,
        )

    def test_parent_plan_resolves_only_explicit_children(self):
        result = runtime.parent_plan(
            self.parent(),
            json.dumps({"alpha": "owner/private-a", "beta": "owner/public-b"}),
        )
        self.assertEqual(
            result,
            [
                {"id": "alpha", "repository": "owner/private-a"},
                {"id": "beta", "repository": "owner/public-b"},
            ],
        )

    def test_visibility_is_not_part_of_relationship_resolution(self):
        result = runtime.parent_plan(
            self.parent(("alpha",)),
            json.dumps({"alpha": "owner/repository"}),
        )
        self.assertEqual(result[0]["repository"], "owner/repository")

    def test_repository_map_must_exactly_match_parent_allowlist(self):
        for value in (
            {"alpha": "owner/a"},
            {"alpha": "owner/a", "beta": "owner/b", "gamma": "owner/c"},
        ):
            with self.subTest(value=value), self.assertRaises(runtime.DelegationError):
                runtime.parent_plan(self.parent(), json.dumps(value))

    def test_duplicate_repository_binding_is_rejected(self):
        with self.assertRaises(runtime.DelegationError):
            runtime.parent_plan(
                self.parent(),
                json.dumps({"alpha": "owner/same", "beta": "owner/same"}),
            )

    def test_bidirectional_child_verification_passes(self):
        runtime.verify_child(
            self.child(),
            child_id="alpha",
            parent_repository="owner/parent",
        )

    def test_bidirectional_child_verification_rejects_wrong_parent(self):
        with self.assertRaises(runtime.DelegationError):
            runtime.verify_child(
                self.child(parent="owner/other"),
                child_id="alpha",
                parent_repository="owner/parent",
            )

    def test_bidirectional_child_verification_rejects_wrong_id(self):
        with self.assertRaises(runtime.DelegationError):
            runtime.verify_child(
                self.child(child_id="beta"),
                child_id="alpha",
                parent_repository="owner/parent",
            )

    def test_validation_script_is_read_from_verified_child_config(self):
        path = self.child(validation_script="automation/validate.sh")
        self.assertEqual(
            runtime.child_validation_script(path),
            "automation/validate.sh",
        )

    def test_missing_validation_script_resolves_to_empty_string(self):
        self.assertEqual(runtime.child_validation_script(self.child()), "")

    def test_parent_cannot_be_used_as_validation_source(self):
        with self.assertRaises(runtime.DelegationError):
            runtime.child_validation_script(self.parent(("alpha",)))

    def test_resolve_child_cannot_escape_allowlist(self):
        with self.assertRaises(runtime.DelegationError):
            runtime.resolve_child(
                self.parent(("alpha",)),
                json.dumps({"alpha": "owner/a"}),
                child_id="beta",
            )


if __name__ == "__main__":
    unittest.main()
