"""The provenance ledger: a strict reader, and the two accounts that must agree.

``docs/parity-ledger.md`` is the human account of the cross-repository sweep and
``reference/parity-ledger.json`` is the machine one. Two accounts is a liability
unless something asserts they agree, so :class:`DocumentAgreementTests` reads the
prose and the JSON and compares them incident by incident.

The loader tests are mostly refusals on purpose. This file is the gate the drift
checker branches on, so the failure that matters is the one where a typo in a
disposition quietly turns a path nobody has to triage into one everybody does --
and the test for that is that the typo does not load.
"""

from __future__ import annotations

import json
import pathlib
import re
import tempfile
import unittest

from continuum.parity import ledger as ledger_module
from continuum.parity.ledger import (
    ABSORBED,
    CONSUMER_LOCAL,
    LEDGER_SCHEMA,
    MUST_PORT,
    NOT_APPLICABLE,
    SUPERSEDED,
    Ledger,
    LedgerError,
    load,
    path_matches,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "reference" / "parity-ledger.json"
OVERLAY_EXAMPLE = ROOT / "reference" / "parity-ledger-overlay.example.json"
DOCUMENT = ROOT / "docs" / "parity-ledger.md"

MINIMAL = {
    "schema": LEDGER_SCHEMA,
    "version": 1,
    "generated_at": "2026-09-01T00:00:00Z",
    "sources": [
        {
            "id": "example",
            "repository": "example/widgets",
            "visibility": "public",
            "audit": {
                "baseline_sha": "a" * 40,
                "current_sha": "b" * 40,
                "audited_at": "2026-09-01T00:00:00Z",
                "ledger_version": 1,
            },
            "paths": [
                {"pattern": ".github/workflows/opencode.yml", "disposition": ABSORBED},
                {"pattern": "docs/", "disposition": CONSUMER_LOCAL},
            ],
            "incidents": [
                {
                    "id": "example/widgets#7",
                    "disposition": MUST_PORT,
                    "paths": [".github/workflows/opencode.yml"],
                    "implementation": ["src/x.py"],
                }
            ],
        }
    ],
}


def document(**overrides):
    value = json.loads(json.dumps(MINIMAL))
    value.update(overrides)
    return value


def write(tmpdir, value, name="ledger.json"):
    path = pathlib.Path(tmpdir) / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


class PathTests(unittest.TestCase):
    def test_an_exact_pattern_matches_only_itself(self):
        self.assertTrue(path_matches("opencode.yml", "opencode.yml"))
        # A prefix match here would quietly classify a file nobody audited.
        self.assertFalse(path_matches("opencode.yml", "consumer-opencode.yml"))

    def test_a_trailing_slash_is_a_directory_prefix(self):
        self.assertTrue(path_matches(".github/workflows/", ".github/workflows/ci.yml"))
        self.assertTrue(path_matches(".github/workflows/", ".github/workflows/a/b/c.yml"))
        self.assertFalse(path_matches(".github/workflows/", ".github/scripts/x.py"))

    def test_a_glob_matches_across_depth(self):
        self.assertTrue(path_matches("automation/*.py", "automation/deep/x.py"))
        self.assertTrue(path_matches("automation/test_*.py", "automation/test_x.py"))

    def test_braces_expand(self):
        self.assertTrue(path_matches("*.{yml,yaml}", "ci.yaml"))
        self.assertTrue(path_matches("*.{yml,yaml}", "ci.yml"))
        self.assertTrue(path_matches("a/{b,{c,d}}/e", "a/c/e"))
        self.assertFalse(path_matches("*.{yml,yaml}", "ci.json"))

    def test_an_unbalanced_brace_is_refused(self):
        with self.assertRaises(LedgerError) as caught:
            path_matches("automation/{a,b/*.py", "automation/a/x.py")
        self.assertEqual(caught.exception.code, "unbalanced_brace")

    def test_paths_are_normalised(self):
        self.assertEqual(ledger_module.normalize_path("./a//b".replace("//", "/")), "a/b")
        self.assertEqual(ledger_module.normalize_path("a\\b"), "a/b")

    def test_escaping_paths_are_refused(self):
        for value, code in (
            ("/etc/passwd", "absolute_path"),
            ("../outside", "traversing_path"),
            ("a/../../b", "traversing_path"),
            ("~/secrets", "absolute_path"),
            ("", "empty_path"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(LedgerError) as caught:
                    ledger_module.normalize_path(value)
                self.assertEqual(caught.exception.code, code)

    def test_a_pattern_that_escapes_is_refused_at_load(self):
        value = document()
        value["sources"][0]["paths"].append(
            {"pattern": "../outside/*.py", "disposition": ABSORBED}
        )
        with self.assertRaises(LedgerError) as caught:
            ledger_module.ledger_from_document(value)
        self.assertEqual(caught.exception.code, "traversing_path")


class LoaderTests(unittest.TestCase):
    def test_the_minimal_ledger_loads(self):
        ledger = ledger_module.ledger_from_document(document())
        self.assertEqual(ledger.version, 1)
        self.assertEqual(ledger.sources[0].repository, "example/widgets")
        self.assertEqual(ledger.problems(), ())

    def test_an_unknown_schema_is_refused(self):
        with self.assertRaises(LedgerError) as caught:
            ledger_module.ledger_from_document(document(schema="something-else/v1"))
        self.assertEqual(caught.exception.code, "unknown_schema")

    def test_version_zero_is_refused(self):
        with self.assertRaises(LedgerError) as caught:
            ledger_module.ledger_from_document(document(version=0))
        self.assertEqual(caught.exception.code, "malformed_version")

    def test_an_unknown_disposition_is_refused(self):
        # The load-time refusal is the whole reason "unclassified" and "classified
        # as harmless" can never collapse into each other later.
        value = document()
        value["sources"][0]["paths"][0]["disposition"] = "probably-fine"
        with self.assertRaises(LedgerError) as caught:
            ledger_module.ledger_from_document(value)
        self.assertEqual(caught.exception.code, "unknown_disposition")

    def test_a_non_timestamp_is_refused(self):
        value = document()
        value["sources"][0]["audit"]["audited_at"] = "yesterday"
        with self.assertRaises(LedgerError) as caught:
            ledger_module.ledger_from_document(value)
        self.assertEqual(caught.exception.code, "malformed_audit")

    def test_a_non_sha_audit_range_is_refused(self):
        value = document()
        value["sources"][0]["audit"]["current_sha"] = "main"
        with self.assertRaises(LedgerError) as caught:
            ledger_module.ledger_from_document(value)
        self.assertEqual(caught.exception.code, "malformed_audit")

    def test_duplicate_sources_are_refused(self):
        value = document()
        value["sources"].append(json.loads(json.dumps(value["sources"][0])))
        with self.assertRaises(LedgerError) as caught:
            ledger_module.ledger_from_document(value)
        self.assertEqual(caught.exception.code, "duplicate_source")

    def test_duplicate_incidents_are_refused(self):
        value = document()
        value["sources"][0]["incidents"].append(
            json.loads(json.dumps(value["sources"][0]["incidents"][0]))
        )
        with self.assertRaises(LedgerError) as caught:
            ledger_module.ledger_from_document(value)
        self.assertEqual(caught.exception.code, "duplicate_incident")

    def test_an_incident_id_may_name_its_repository(self):
        # docs/parity-ledger.md cites `kodmial/runtime-lab#23`, and the machine
        # account has to be able to cite the same thing.
        value = document()
        value["sources"][0]["incidents"][0]["id"] = "kodmial/runtime-lab#23"
        ledger = ledger_module.ledger_from_document(value)
        self.assertEqual(ledger.sources[0].incidents[0].id, "kodmial/runtime-lab#23")

    def test_a_repository_must_be_owner_and_name(self):
        value = document()
        value["sources"][0]["repository"] = "widgets"
        with self.assertRaises(LedgerError) as caught:
            ledger_module.ledger_from_document(value)
        self.assertEqual(caught.exception.code, "malformed_repository")

    def test_a_missing_file_is_reported_as_such(self):
        with self.assertRaises(LedgerError) as caught:
            load("does/not/exist.json")
        self.assertEqual(caught.exception.code, "ledger_not_found")

    def test_malformed_json_is_reported_as_such(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "ledger.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(LedgerError) as caught:
                load(str(path))
            self.assertEqual(caught.exception.code, "malformed_ledger")


class ProblemTests(unittest.TestCase):
    def test_a_private_source_in_a_committed_ledger_is_an_error(self):
        value = document()
        value["sources"][0]["visibility"] = "private"
        value["sources"][0]["repository"] = "example/private"
        ledger = ledger_module.ledger_from_document(value)
        codes = [item.code for item in ledger.errors()]
        self.assertIn("private-source-in-committed-ledger", codes)

    def test_a_private_source_from_an_overlay_is_not(self):
        value = document()
        value["sources"][0]["visibility"] = "private"
        value["sources"][0]["repository"] = "example/private"
        base = ledger_module.ledger_from_document(value)
        merged = Ledger(
            version=base.version,
            generated_at=base.generated_at,
            sources=base.sources,
            overlay_ids=("example",),
        )
        self.assertEqual(merged.errors(), ())

    def test_a_source_that_tracks_no_path_is_an_error(self):
        value = document()
        value["sources"][0]["paths"] = []
        ledger = ledger_module.ledger_from_document(value)
        codes = [item.code for item in ledger.errors()]
        self.assertIn("source-tracks-no-path", codes)

    def test_a_classification_with_no_reference_is_a_warning_not_an_error(self):
        value = document()
        value["sources"][0]["incidents"][0].pop("implementation")
        ledger = ledger_module.ledger_from_document(value)
        self.assertEqual(ledger.errors(), ())
        self.assertEqual(
            [item.code for item in ledger.warnings()], ["unanswered-incident"]
        )

    def test_not_applicable_needs_no_reference(self):
        # Nothing in this repository answers it; that is the classification.
        value = document()
        incident = value["sources"][0]["incidents"][0]
        incident["disposition"] = NOT_APPLICABLE
        incident.pop("implementation")
        ledger = ledger_module.ledger_from_document(value)
        self.assertEqual(ledger.warnings(), ())

    def test_absorbed_without_a_reference_is_reported(self):
        value = document()
        incident = value["sources"][0]["incidents"][0]
        incident["disposition"] = ABSORBED
        incident.pop("implementation")
        ledger = ledger_module.ledger_from_document(value)
        self.assertEqual(len(ledger.warnings()), 1)


class ClassificationTests(unittest.TestCase):
    def test_first_matching_rule_wins(self):
        value = document()
        value["sources"][0]["paths"] = [
            {"pattern": "automation/exact.py", "disposition": CONSUMER_LOCAL},
            {"pattern": "automation/", "disposition": ABSORBED},
        ]
        source = ledger_module.ledger_from_document(value).sources[0]
        self.assertEqual(source.classify("automation/exact.py").disposition, CONSUMER_LOCAL)
        self.assertEqual(source.classify("automation/other.py").disposition, ABSORBED)

    def test_an_unmatched_path_has_no_classification(self):
        source = ledger_module.ledger_from_document(document()).sources[0]
        self.assertIsNone(source.classify("automation/other.py"))

    def test_only_two_dispositions_need_no_triage(self):
        self.assertEqual(
            ledger_module.NO_TRIAGE_DISPOSITIONS, (CONSUMER_LOCAL, NOT_APPLICABLE)
        )
        for disposition in (ABSORBED, MUST_PORT, SUPERSEDED):
            self.assertNotIn(disposition, ledger_module.NO_TRIAGE_DISPOSITIONS)

    def test_every_incident_path_is_classified(self):
        source = ledger_module.ledger_from_document(document()).sources[0]
        self.assertEqual(list(ledger_module.unclassified_paths(source)), [])

    def test_an_advance_record_closes_the_range(self):
        source = ledger_module.ledger_from_document(document()).sources[0]
        moved = source.advance_recorded(
            "c" * 40, audited_at="2026-09-02T00:00:00Z", audited_by="test"
        )
        self.assertEqual(moved.audit.baseline_sha, "b" * 40)
        self.assertEqual(moved.audit.current_sha, "c" * 40)
        self.assertEqual(moved.audit.audited_at, "2026-09-02T00:00:00Z")

    def test_disposition_counts_cover_every_incident(self):
        ledger = ledger_module.ledger_from_document(document())
        self.assertEqual(sum(ledger_module.disposition_counts(ledger).values()), 1)


class OverlayTests(unittest.TestCase):
    def test_an_overlay_adds_a_source_and_is_recorded_as_the_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            private = document()
            private["sources"][0]["id"] = "private-consumer"
            private["sources"][0]["repository"] = "example/private-consumer"
            private["sources"][0]["visibility"] = "private"
            merged = load(
                write(tmpdir, document(), "committed.json"),
                overlay=write(tmpdir, private, "overlay.json"),
            )
            self.assertEqual([item.id for item in merged.sources], ["example", "private-consumer"])
            self.assertEqual(merged.overlay_ids, ("private-consumer",))
            self.assertEqual(merged.errors(), ())

    def test_an_overlay_may_replace_a_source_of_the_same_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            replacement = document()
            replacement["sources"][0]["audit"]["current_sha"] = "c" * 40
            merged = load(
                write(tmpdir, document(), "committed.json"),
                overlay=write(tmpdir, replacement, "overlay.json"),
            )
            self.assertEqual(len(merged.sources), 1)
            self.assertEqual(merged.sources[0].audit.current_sha, "c" * 40)

    def test_no_overlay_means_no_overlay_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            merged = load(write(tmpdir, document()))
            self.assertEqual(merged.overlay_ids, ())


class CommittedLedgerTests(unittest.TestCase):
    """The committed file is the audit record, so it is held to its own rules."""

    def setUp(self):
        self.raw = json.loads(COMMITTED.read_text(encoding="utf-8"))
        self.ledger = ledger_module.ledger_from_document(
            self.raw, source_path=str(COMMITTED)
        )

    def test_it_declares_its_own_schema_and_version(self):
        self.assertEqual(self.raw["schema"], LEDGER_SCHEMA)
        self.assertGreaterEqual(self.raw["version"], 1)

    def test_it_is_a_valid_ledger_with_no_errors(self):
        self.assertEqual(list(self.ledger.errors()), [])

    def test_every_source_is_public(self):
        # Continuum is public; a private repository named here would be disclosed
        # by the committed ledger itself.
        for source in self.ledger.sources:
            self.assertEqual(source.visibility, "public", source.id)

    def test_every_source_tracks_at_least_one_path(self):
        for source in self.ledger.sources:
            self.assertTrue(source.paths, source.id)

    def test_every_incident_is_classified_and_answered(self):
        for source in self.ledger.sources:
            for incident in source.incidents:
                self.assertIn(incident.disposition, ledger_module.DISPOSITIONS)
                self.assertTrue(
                    incident.answered,
                    "{} has no Continuum reference".format(incident.id),
                )

    def test_every_incident_path_is_covered_by_a_rule(self):
        for source in self.ledger.sources:
            self.assertEqual(
                list(ledger_module.unclassified_paths(source)), [], source.id
            )

    def test_every_implementation_reference_exists_in_this_repository(self):
        # A classification that points at a file this repository does not have is
        # a claim about work nobody did.
        for source in self.ledger.sources:
            for incident in source.incidents:
                for reference in incident.implementation + incident.tests:
                    self.assertTrue(
                        (ROOT / reference).exists(),
                        "{} references {}".format(incident.id, reference),
                    )

    def test_the_audit_range_is_a_real_commit_pair(self):
        for source in self.ledger.sources:
            self.assertRegex(source.audit.baseline_sha, r"^[0-9a-f]{40}$")
            self.assertRegex(source.audit.current_sha, r"^[0-9a-f]{40}$")

    def test_the_overlay_example_loads_and_names_only_a_private_source(self):
        overlay = ledger_module.ledger_from_document(
            json.loads(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
        )
        self.assertTrue(overlay.sources)
        for source in overlay.sources:
            self.assertEqual(source.visibility, "private", source.id)
            self.assertTrue(source.repository.startswith("example/"), source.id)

    def test_the_overlay_example_names_no_real_consumer(self):
        text = OVERLAY_EXAMPLE.read_text(encoding="utf-8").lower()
        for name in ("kodmai", "runtime-lab", "nanodictate"):
            self.assertNotIn(name, text, name)

    def test_the_committed_ledger_carries_no_credential(self):
        text = COMMITTED.read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r"gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}", text)
        )
        self.assertNotIn("secrets.", text)


class DocumentAgreementTests(unittest.TestCase):
    """`docs/parity-ledger.md` and the machine ledger are one account, not two.

    The prose is what a reviewer reads and the JSON is what the checker branches
    on, so a disagreement is a defect in whichever one is wrong -- and there is no
    way to tell which from the file alone. These assertions compare them.
    """

    #: `### kodmial/runtime-lab#23 — `must-port`` in either dash style.
    HEADING = re.compile(
        r"^### (?P<id>[A-Za-z0-9][A-Za-z0-9._/#-]*)\s*(?:--|—)\s*`?(?P<disposition>[a-z-]+)`?",
        re.M,
    )

    def setUp(self):
        self.text = DOCUMENT.read_text(encoding="utf-8")
        self.ledger = ledger_module.ledger_from_document(
            json.loads(COMMITTED.read_text(encoding="utf-8"))
        )

    def headings(self):
        return {match.group("id"): match.group("disposition") for match in self.HEADING.finditer(self.text)}

    def test_the_document_has_headings_this_test_can_read(self):
        # If the prose is restructured, this fails rather than the comparison
        # below silently comparing nothing.
        self.assertGreaterEqual(len(self.headings()), 5)

    def test_every_machine_incident_appears_in_the_document_with_the_same_disposition(self):
        document_dispositions = self.headings()
        for source in self.ledger.sources:
            for incident in source.incidents:
                with self.subTest(incident=incident.id):
                    self.assertIn(incident.id, document_dispositions)
                    self.assertEqual(
                        document_dispositions[incident.id], incident.disposition
                    )

    def test_the_document_carries_no_committed_classification_the_machine_ledger_lacks(self):
        committed_ids = {
            incident.id
            for source in self.ledger.sources
            for incident in source.incidents
        }
        # One incident is deliberately absent: kodmai is a private source, and a
        # private classification cannot be committed to a public repository. The
        # document says so where it lists it.
        overlay_only = {"kodmai#35"}
        for identifier in self.headings():
            if identifier in committed_ids:
                continue
            self.assertIn(
                identifier,
                overlay_only,
                "{} is in the document but not in the machine ledger".format(identifier),
            )

    def test_the_document_names_the_five_dispositions(self):
        for disposition in ledger_module.DISPOSITIONS:
            self.assertIn("`{}`".format(disposition), self.text, disposition)

    def test_the_document_says_where_the_private_classification_lives(self):
        self.assertIn(
            "overlay", self.text.lower(), "the document must say where kodmai is tracked"
        )

    def test_the_document_still_runs_the_projects_verification(self):
        self.assertIn("python3 -m unittest discover", self.text)


if __name__ == "__main__":
    unittest.main()