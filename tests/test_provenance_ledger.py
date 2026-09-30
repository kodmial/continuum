"""The provenance ledger: the record the drift checker compares against.

The tests here are mostly about what the reader *refuses*. A ledger is a claim
about what has been audited, and the dangerous failure is not a malformed document
-- it is a well-formed one that quietly stops meaning anything: a disposition
nobody recognises, a source with no baseline commit, a classification that points
at Continuum code which has since been deleted. Each of those would let a source
sit in the ledger looking audited.
"""

from __future__ import annotations

import json
import unittest

from continuum.provenance import ledger
from continuum.provenance.ledger import ProvenanceError


def _document(**overrides):
    source = {
        "id": "nanodictate",
        "repository": "kodmial/nanodictate",
        "visibility": "public",
        "disposition": "absorbed",
        "severity": "p0",
        "baseline_sha": "08a5d920c5dbf1f4203160e92e3e530c5bbb3b8f",
        "audited_at": "2026-09-30T00:00:00Z",
        "tracked_prefixes": [".github/workflows/"],
        "path_dispositions": {
            ".github/workflows/opencode.yml": "absorbed",
            ".github/workflows/ci.yml": "consumer-local",
        },
        "continuum": {
            "implementation": ["src/continuum/shadow/planner.py"],
            "tests": ["tests/test_provenance_ledger.py"],
        },
    }
    source.update(overrides.pop("source", {}))
    document = {
        "schema": ledger.PROVENANCE_SCHEMA,
        "version": ledger.LEDGER_VERSION,
        "sources": [source],
    }
    document.update(overrides)
    return document


def _read(**overrides):
    return ledger.read_provenance(_document(**overrides))


class SchemaTests(unittest.TestCase):
    def test_a_ledger_from_the_shipped_document_reads(self) -> None:
        parsed = ledger.load_provenance("docs/provenance-ledger.json")
        self.assertEqual(parsed.version, ledger.LEDGER_VERSION)
        self.assertTrue(parsed.sources)
        self.assertEqual(parsed.sources[0].id, "nanodictate")

    def test_the_shipped_ledger_and_the_shipped_markdown_agree(self) -> None:
        # The Markdown is generated from the object the gate reads, so the two are
        # checked against each other rather than trusted to stay in step.
        with open("docs/provenance-ledger.md", encoding="utf-8") as handle:
            rendered = handle.read()
        self.assertEqual(
            rendered.strip(),
            ledger.render_markdown(ledger.load_provenance("docs/provenance-ledger.json")).strip(),
        )

    def test_another_schema_is_refused(self) -> None:
        with self.assertRaises(ProvenanceError) as caught:
            _read(schema="continuum.provenance-ledger/v99")
        self.assertEqual(caught.exception.code, "unknown_ledger_schema")

    def test_another_version_is_refused_rather_than_guessed_at(self) -> None:
        with self.assertRaises(ProvenanceError) as caught:
            _read(version=2)
        self.assertEqual(caught.exception.code, "unknown_ledger_version")

    def test_a_ledger_with_no_sources_is_refused(self) -> None:
        # An empty ledger would audit nothing and report a clean drift for every
        # repository Continuum tracks, which is the worst possible failure.
        with self.assertRaises(ProvenanceError) as caught:
            _read(sources=[])
        self.assertEqual(caught.exception.code, "no_sources")

    def test_a_source_declared_twice_is_refused(self) -> None:
        document = _document()
        document["sources"].append(dict(document["sources"][0]))
        with self.assertRaises(ProvenanceError) as caught:
            ledger.read_provenance(document)
        self.assertEqual(caught.exception.code, "duplicate_source_id")


class SourceTests(unittest.TestCase):
    def test_a_source_must_name_an_owner_and_a_repository(self) -> None:
        for bad in ("", "nanodictate", "/nanodictate", "kodmial/"):
            with self.subTest(repository=bad):
                with self.assertRaises(ProvenanceError) as caught:
                    _read(source={"repository": bad})
                self.assertEqual(caught.exception.code, "invalid_repository")

    def test_a_baseline_commit_is_required(self) -> None:
        # Drift is a comparison. A source with no audited commit has nothing to be
        # compared against, and would otherwise read as one that has not moved.
        for bad in ("", "main", "08a5d92", "z" * 40):
            with self.subTest(baseline=bad):
                with self.assertRaises(ProvenanceError) as caught:
                    _read(source={"baseline_sha": bad})
                self.assertEqual(caught.exception.code, "invalid_sha")

    def test_the_disposition_vocabulary_is_the_cutover_ledgers(self) -> None:
        # Two lists of dispositions that are allowed to drift apart is one too many,
        # so this asserts the import rather than a copy of it.
        from continuum.shadow import baseline

        self.assertEqual(ledger.DISPOSITIONS, baseline.CLASSIFICATIONS)
        self.assertEqual(
            set(ledger.PERSISTENT_DISPOSITIONS),
            {"consumer-local", "not-applicable"},
        )
        self.assertEqual(
            sorted(set(ledger.DISPOSITIONS) - set(ledger.PERSISTENT_DISPOSITIONS)),
            ["absorbed", "must-port", "superseded"],
        )

    def test_an_unknown_disposition_is_refused_by_name(self) -> None:
        with self.assertRaises(ProvenanceError) as caught:
            _read(source={"path_dispositions": {".github/workflows/ci.yml": "looks-fine"}})
        self.assertEqual(caught.exception.code, "unknown_disposition")

    def test_a_source_with_no_scope_is_refused(self) -> None:
        with self.assertRaises(ProvenanceError) as caught:
            _read(source={"tracked_prefixes": [], "incidents": []})
        self.assertEqual(caught.exception.code, "untracked_source")

    def test_a_source_tracked_only_by_incident_is_allowed(self) -> None:
        # Some claims come from an incident rather than from a path, and refusing
        # those would be refusing to record them at all.
        parsed = _read(source={"tracked_prefixes": [], "incidents": ["#62"]})
        self.assertEqual(parsed.sources[0].incidents, ("#62",))

    def test_an_audit_needs_an_instant(self) -> None:
        with self.assertRaises(ProvenanceError) as caught:
            _read(source={"audited_at": "  "})
        self.assertEqual(caught.exception.code, "missing_audit_timestamp")

    def test_a_severity_outside_the_vocabulary_is_refused(self) -> None:
        with self.assertRaises(ProvenanceError) as caught:
            _read(source={"severity": "urgent"})
        self.assertEqual(caught.exception.code, "unknown_severity")

    def test_the_sources_are_read_in_a_stable_order(self) -> None:
        document = _document()
        document["sources"].append(
            dict(document["sources"][0], id="aaa-first-alphabetically", repository="acme/first")
        )
        parsed = ledger.read_provenance(document)
        self.assertEqual(
            [source.id for source in parsed.sources],
            ["aaa-first-alphabetically", "nanodictate"],
        )


class ContinuumReferenceTests(unittest.TestCase):
    """A classification with nothing behind it is a claim with nothing behind it."""

    def test_a_source_with_no_continuum_reference_is_refused(self) -> None:
        with self.assertRaises(ProvenanceError) as caught:
            _read(source={"continuum": None})
        self.assertEqual(caught.exception.code, "missing_continuum_reference")

    def test_a_reference_with_no_tests_is_refused(self) -> None:
        # Nothing would fail if the property were lost, which is the failure this
        # ledger is read to catch -- applied to the ledger itself.
        with self.assertRaises(ProvenanceError) as caught:
            _read(source={"continuum": {"implementation": ["src/x.py"], "tests": []}})
        self.assertEqual(caught.exception.code, "missing_continuum_reference")

    def test_a_reference_with_no_implementation_is_refused(self) -> None:
        with self.assertRaises(ProvenanceError) as caught:
            _read(source={"continuum": {"implementation": [], "tests": ["tests/x.py"]}})
        self.assertEqual(caught.exception.code, "missing_continuum_reference")

    def test_references_that_no_longer_exist_are_reported(self) -> None:
        parsed = _read(
            source={
                "continuum": {
                    "implementation": ["src/continuum/shadow/planner.py"],
                    "tests": ["tests/test_provenance_ledger.py"],
                }
            }
        )
        self.assertEqual(ledger.missing_references(parsed, "."), ())

    def test_a_renamed_implementation_is_reported_not_ignored(self) -> None:
        parsed = _read(
            source={
                "continuum": {
                    "implementation": ["src/continuum/shadow/planner.py"],
                    "tests": ["tests/test_that_was_renamed.py"],
                }
            }
        )
        self.assertEqual(
            ledger.missing_references(parsed, "."),
            ("nanodictate: tests/test_that_was_renamed.py",),
        )


class PrivateSourceTests(unittest.TestCase):
    def test_a_private_source_names_its_own_credential(self) -> None:
        parsed = _read(
            source={
                "visibility": "private",
                "repository": "acme/private",
                "continuum": {
                    "implementation": ["src/continuum/shadow/planner.py"],
                    "tests": ["tests/test_provenance_ledger.py"],
                },
            }
        )
        source = parsed.sources[0]
        self.assertTrue(source.is_private)
        self.assertEqual(source.credential_env, ledger.DEFAULT_CREDENTIAL_ENV)

    def test_a_private_source_may_name_its_own_variable(self) -> None:
        parsed = _read(
            source={
                "visibility": "private",
                "credential_env": "ACME_SOURCE_READ",
            }
        )
        self.assertEqual(parsed.sources[0].credential_env, "ACME_SOURCE_READ")

    def test_a_public_source_may_not_name_a_credential(self) -> None:
        # Reaching for a credential broader than a public read needs is the mistake
        # this ledger is not allowed to make quietly.
        with self.assertRaises(ProvenanceError) as caught:
            _read(source={"credential_env": "SOMETHING_BROAD"})
        self.assertEqual(caught.exception.code, "public_source_with_credential")

    def test_a_credential_variable_name_must_be_one(self) -> None:
        with self.assertRaises(ProvenanceError) as caught:
            _read(source={"visibility": "private", "credential_env": "not a name"})
        self.assertEqual(caught.exception.code, "invalid_credential_env")

    def test_the_rendered_document_never_names_a_private_repository(self) -> None:
        # The ledger is a public document. It names the source and its visibility,
        # and the rendered table must not leak the repository it points at.
        parsed = _read(source={"visibility": "private", "repository": "acme/private"})
        rendered = ledger.render_markdown(parsed)
        self.assertNotIn("acme/private", rendered)
        self.assertIn("private", rendered)


class ScopeTests(unittest.TestCase):
    def test_tracked_prefixes_decide_what_is_in_scope(self) -> None:
        source = _read().sources[0]
        self.assertTrue(source.tracks(".github/workflows/ci.yml"))
        self.assertFalse(source.tracks("README.md"))
        self.assertFalse(source.tracks(".github/ISSUE_TEMPLATE/bug.yml"))

    def test_the_longest_matching_rule_wins(self) -> None:
        source = _read(
            source={
                "path_dispositions": {
                    ".github/workflows/": "consumer-local",
                    ".github/workflows/ci.yml": "must-port",
                }
            }
        ).sources[0]
        self.assertEqual(source.disposition_for(".github/workflows/ci.yml"), "must-port")
        self.assertEqual(source.disposition_for(".github/workflows/other.yml"), "consumer-local")

    def test_an_unclassified_path_has_no_disposition(self) -> None:
        source = _read().sources[0]
        self.assertIsNone(source.disposition_for(".github/workflows/brand-new.yml"))


class DigestTests(unittest.TestCase):
    def test_the_digest_changes_when_a_classification_changes(self) -> None:
        before = _read().digest
        after = _read(
            source={
                "path_dispositions": {
                    ".github/workflows/opencode.yml": "absorbed",
                    ".github/workflows/ci.yml": "absorbed",
                }
            }
        ).digest
        self.assertNotEqual(before, after)

    def test_the_digest_does_not_change_when_only_the_audit_instant_moves(self) -> None:
        # Otherwise every weekly run would produce a different ledger digest, and
        # the promotion gate's evidence digest could never be compared.
        self.assertEqual(_read().digest, _read(source={"audited_at": "2026-10-07T00:00:00Z"}).digest)

    def test_the_digest_is_stable_across_reads(self) -> None:
        self.assertEqual(ledger.load_provenance("docs/provenance-ledger.json").digest,
                         ledger.load_provenance("docs/provenance-ledger.json").digest)


if __name__ == "__main__":
    unittest.main()