"""Drift detection, source reads, the deduplicated issue, and the promotion gate.

Each class here owns one decision the checker makes, and the tests for each are
mostly refusals: what happens when a source cannot be read, when a change list is
truncated, when a path was never classified, when the same scan runs twice. Those
are the cases where a checker that reports "clean" is worse than one that fails,
so they are the ones pinned down here.

No test in this file touches the network. The reader is driven through a fake
opener that returns the *shapes* the real endpoints return -- a JSON array for the
commit list, an object with `files` and `commits` for the compare -- because the
bug this guards against is a reader that rejects a shape GitHub actually uses.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import tempfile
import unittest
from typing import Any, Dict, List, Tuple

from continuum.parity import drift as drift_module
from continuum.parity import issue as issue_module
from continuum.parity import ledger as ledger_module
from continuum.parity import promote as promote_module
from continuum.parity import sources as sources_module
from continuum.parity import cli as parity_cli
from continuum.parity.ledger import LEDGER_SCHEMA
from continuum.parity.sources import Change, Commit, GitHubSourceReader, SourceObservation

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "reference" / "parity-ledger.json"
HEAD = "d" * 40
AUDITED = "c" * 40

#: The endpoint answers with an array; the compare answers with an object. Both
#: shapes appear here because the reader has to accept exactly what GitHub sends.
def commits_payload(sha: str):
    """The commit list endpoint answers with an array, and this is its shape."""

    return [
        {
            "sha": sha,
            "html_url": "https://example.test/widgets/commit/" + sha,
            "commit": {"message": "fix: something (#412)\n\nBody that must not be published."},
        }
    ]


COMMITS_PAYLOAD = commits_payload(HEAD)

COMPARE_PAYLOAD = {
    "status": "ahead",
    "ahead_by": 1,
    "files": [
        {
            "filename": ".github/workflows/opencode.yml",
            "status": "modified",
            "patch": "@@ -1,3 +1,4 @@\n context\n+added\n",
        }
    ],
    "commits": [
        {
            "sha": HEAD,
            "html_url": "https://example.test/widgets/commit/" + HEAD,
            "commit": {"message": "fix: something (#412)"},
        }
    ],
}


def ledger_document():
    return {
        "schema": LEDGER_SCHEMA,
        "version": 3,
        "generated_at": "2026-09-01T00:00:00Z",
        "sources": [
            {
                "id": "widgets",
                "repository": "example/widgets",
                "visibility": "public",
                "audit": {
                    "baseline_sha": AUDITED,
                    "current_sha": AUDITED,
                    "audited_at": "2026-09-01T00:00:00Z",
                    "ledger_version": 3,
                },
                "paths": [
                    {"pattern": ".github/workflows/opencode.yml", "disposition": "absorbed"},
                    {"pattern": "legacy/", "disposition": "superseded"},
                    {"pattern": "docs/", "disposition": "consumer-local"},
                    {"pattern": "never/", "disposition": "not-applicable"},
                ],
                "incidents": [],
            },
            {
                "id": "private-consumer",
                "repository": "example/private-consumer",
                "visibility": "private",
                "audit": {
                    "baseline_sha": AUDITED,
                    "current_sha": AUDITED,
                    "audited_at": "2026-09-01T00:00:00Z",
                    "ledger_version": 3,
                },
                "paths": [{"pattern": "automation/", "disposition": "absorbed"}],
                "incidents": [],
            },
        ],
    }


def overlay_document():
    value = ledger_document()
    value["sources"] = [value["sources"][1]]
    return value


def observation(
    *,
    identifier: str = "widgets",
    visibility: str = "public",
    paths: Tuple[str, ...] = (),
    head: str = HEAD,
    previous: str = AUDITED,
    truncated: bool = False,
    state: str = "readable",
    code: str = "",
    reason: str = "",
    private: bool = False,
    commits=None,
) -> SourceObservation:
    advanced = state == "readable" and head != previous
    if commits is None:
        commits = (
            (Commit(sha=head, url="", message="fix: something (#412)", pull_requests=(412,)),)
            if advanced
            else ()
        )
    return SourceObservation(
        id=identifier,
        repository="example/private-consumer" if private else "example/widgets",
        visibility=visibility,
        state=state,
        access="credential" if private else "public",
        head_sha=head,
        previous_sha=previous,
        changes=tuple(Change(path=path, status="modified") for path in paths),
        commits=tuple(commits),
        code=code,
        reason=reason,
        truncated=truncated,
    )


def all_read(*observations):
    """Every tracked source, with the given observations over them.

    The private source is added in sync at the audited commit. Anything else
    would make every claim test fail for the wrong reason: a private source that
    advanced is unclassified ``p0`` on its own, which is the behaviour
    ``test_a_private_advance_cannot_be_classified_from_publishable_data`` covers.
    """

    return tuple(observations) + (
        observation(
            identifier="private-consumer",
            visibility="private",
            private=True,
            head=AUDITED,
            previous=AUDITED,
        ),
    )


def report_for(observations, *, sources=None):
    document = ledger_document()
    if sources is not None:
        document["sources"] = sources
    ledger = ledger_module.ledger_from_document(document)
    return drift_module.compute(
        ledger,
        observations,
        generated_at="2026-09-02T00:00:00Z",
        continuum_sha="e" * 40,
    )


# --------------------------------------------------------------------------- #
# Reading a source
# --------------------------------------------------------------------------- #


class FakeOpener:
    """Answers by URL suffix and records every request it was given."""

    def __init__(self, routes: Dict[str, Any]):
        self.routes = routes
        self.requests: List[str] = []
        self.tokens: List[str] = []

    def __call__(self, request, timeout):
        self.requests.append(request.full_url)
        self.tokens.append(request.headers.get("Authorization") or "")
        for suffix, payload in self.routes.items():
            if request.full_url.endswith(suffix) or suffix in request.full_url:
                if isinstance(payload, Exception):
                    raise payload
                return json.dumps(payload) if not isinstance(payload, str) else payload
        raise AssertionError("unexpected request: {}".format(request.full_url))


def reader_for(routes, **kwargs):
    opener = FakeOpener(routes)
    return GitHubSourceReader(opener=opener, **kwargs), opener


class SourceReadTests(unittest.TestCase):
    def source(self):
        return ledger_module.ledger_from_document(ledger_document()).sources[0]

    def private_source(self):
        ledger = ledger_module.ledger_from_document(
            ledger_document(), overlay_ids=("private-consumer",)
        )
        return ledger.sources[1]

    def test_an_unchanged_head_is_in_sync(self):
        reader, _ = reader_for(
            {"?per_page=1": commits_payload(AUDITED)}, public_token="read-only"
        )
        result = reader.read(self.source())
        self.assertEqual(result.state, sources_module.READABLE)
        self.assertEqual(result.head_sha, AUDITED)
        self.assertFalse(result.advanced)
        self.assertEqual(result.changes, ())

    def test_an_advanced_head_is_compared(self):
        reader, _ = reader_for(
            {"?per_page=1": COMMITS_PAYLOAD, "compare/": COMPARE_PAYLOAD},
            public_token="read-only",
        )
        result = reader.read(self.source())
        self.assertTrue(result.advanced)
        self.assertEqual([item.path for item in result.changes], [".github/workflows/opencode.yml"])
        self.assertEqual(result.pull_requests, (412,))

    def test_a_commit_patch_is_dropped_at_the_parse_boundary(self):
        reader, _ = reader_for(
            {"?per_page=1": COMMITS_PAYLOAD, "compare/": COMPARE_PAYLOAD},
            public_token="read-only",
        )
        result = reader.read(self.source())
        self.assertNotIn("patch", json.dumps(result.describe()))
        sources_module.assert_public(result.describe())

    def test_a_public_source_with_no_credential_is_unavailable_without_a_request(self):
        reader, opener = reader_for({}, public_token="")
        result = reader.read(self.source())
        self.assertEqual(result.state, sources_module.UNAVAILABLE)
        self.assertEqual(result.code, "read-credential-absent")
        self.assertEqual(opener.requests, [], "no request may be made without a credential")

    def test_a_private_source_with_no_credential_is_unavailable_without_a_request(self):
        reader, opener = reader_for({}, public_token="read-only", private_token="")
        result = reader.read(self.private_source())
        self.assertEqual(result.state, sources_module.UNAVAILABLE)
        self.assertEqual(result.code, "private-credential-absent")
        self.assertEqual(opener.requests, [])

    def test_a_private_source_is_read_with_the_private_credential_only(self):
        reader, opener = reader_for(
            {"?per_page=1": COMMITS_PAYLOAD, "compare/": COMPARE_PAYLOAD},
            public_token="read-only",
            private_token="least-privilege",
        )
        result = reader.read(self.private_source())
        self.assertEqual(result.access, "credential")
        self.assertTrue(all("least-privilege" in item for item in opener.tokens))
        self.assertNotIn("read-only", " ".join(opener.tokens))

    def test_a_refused_read_becomes_an_unavailable_result_not_an_exception(self):
        reader, _ = reader_for(
            {"?per_page=1": sources_module.urllib.error.HTTPError(
                "https://example.test", 404, "Not Found", {}, None
            )},
            public_token="read-only",
        )
        result = reader.read(self.source())
        self.assertEqual(result.state, sources_module.UNAVAILABLE)
        self.assertEqual(result.code, "not-found")

    def test_an_unreachable_api_becomes_an_unavailable_result(self):
        reader, _ = reader_for(
            {"?per_page=1": sources_module.urllib.error.URLError("no route")},
            public_token="read-only",
        )
        result = reader.read(self.source())
        self.assertEqual(result.code, "unreachable")

    def test_a_non_json_response_is_reported_as_malformed(self):
        reader, _ = reader_for({"?per_page=1": "<html>nope</html>"}, public_token="read-only")
        result = reader.read(self.source())
        self.assertEqual(result.code, "malformed_response")

    def test_a_missing_head_is_reported_rather_than_assumed_in_sync(self):
        reader, _ = reader_for({"?per_page=1": []}, public_token="read-only")
        result = reader.read(self.source())
        # An empty commit list means the head is unknown. Reporting it as the
        # audited commit would be the one answer that is certainly wrong.
        self.assertEqual(result.state, sources_module.UNAVAILABLE)

    def test_a_truncated_change_list_is_recorded_as_truncated(self):
        payload = json.loads(json.dumps(COMPARE_PAYLOAD))
        payload["files"] = payload["files"] * 4
        reader, _ = reader_for(
            {"?per_page=1": COMMITS_PAYLOAD, "compare/": payload},
            public_token="read-only",
            max_files=2,
        )
        result = reader.read(self.source())
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.changes), 2)

    def test_one_unreadable_source_does_not_stop_the_others(self):
        routes = {"?per_page=1": COMMITS_PAYLOAD, "compare/": COMPARE_PAYLOAD}
        reader = GitHubSourceReader(
            opener=FakeOpener(routes), public_token="read-only", private_token=""
        )
        ledger = ledger_module.ledger_from_document(
            ledger_document(), overlay_ids=("private-consumer",)
        )
        results = reader.read_all(ledger)
        self.assertEqual(
            [(item.id, item.state) for item in results],
            [("widgets", "readable"), ("private-consumer", "unavailable")],
        )

    def test_a_squash_merge_subject_yields_its_pull_request(self):
        self.assertEqual(sources_module.pull_requests_in("fix: thing (#412)"), (412,))
        self.assertEqual(sources_module.pull_requests_in("fix: thing"), ())
        # A number in the middle of a sentence is not a pull request reference.
        self.assertEqual(sources_module.pull_requests_in("fix (#412) then more text"), ())

    def test_a_token_is_never_placed_in_a_url(self):
        reader, opener = reader_for(
            {"?per_page=1": COMMITS_PAYLOAD, "compare/": COMPARE_PAYLOAD},
            public_token="read-only",
        )
        reader.read(self.source())
        self.assertTrue(all("read-only" not in item for item in opener.requests))


class PublicArtifactTests(unittest.TestCase):
    def test_a_private_source_is_redacted_but_still_accounted_for(self):
        private = observation(
            identifier="private-consumer",
            visibility="private",
            private=True,
            paths=("automation/secret_thing.py",),
        )
        view = sources_module.public_view(private)
        self.assertEqual(view["repository"], "<redacted:private>")
        self.assertEqual(view["changes"], [])
        self.assertEqual(view["commits"], [])
        self.assertIn("repository", view["redacted"])
        # The commit range survives: a reader has to be able to see that
        # something moved, even when it may not see what.
        self.assertEqual(view["head_sha"], HEAD)
        self.assertEqual(view["state"], "readable")
        self.assertEqual(view["count"], 1)

    def test_a_private_reason_never_repeats_its_text(self):
        private = observation(
            identifier="private-consumer",
            visibility="private",
            private=True,
            state="unavailable",
            code="not-found",
            reason="example/private-consumer is private",
        )
        view = sources_module.public_view(private)
        self.assertNotIn("private-consumer is private", view["reason"])
        self.assertEqual(view["code"], "not-found")

    def test_a_public_source_is_unchanged_by_the_public_view(self):
        public = observation()
        self.assertEqual(
            sources_module.public_view(public), public.describe()
        )

    def test_a_field_outside_the_allowlist_is_refused(self):
        with self.assertRaises(sources_module.RedactionError) as caught:
            sources_module.assert_public({"schema": "x", "surprise": 1})
        self.assertIn("outside the public allowlist", str(caught.exception))

    def test_a_forbidden_field_is_refused_even_if_allowed(self):
        forbidden = {
            "schema": "x",
            "token": "value",
        }
        with self.assertRaises(sources_module.RedactionError) as caught:
            sources_module.assert_public(forbidden)
        self.assertIn("may never appear", str(caught.exception))

    def test_a_credential_shaped_value_is_refused_through_an_allowed_field(self):
        # The allowlist cannot catch a leak through a permitted key; the value scan
        # is what catches a commit message that quotes a token.
        with self.assertRaises(sources_module.RedactionError) as caught:
            sources_module.assert_public({"schema": "x", "summary": "token ghp_" + "a" * 30})
        self.assertIn("looks like a credential", str(caught.exception))

    def test_a_diff_hunk_is_refused_through_an_allowed_field(self):
        with self.assertRaises(sources_module.RedactionError) as caught:
            sources_module.assert_public({"schema": "x", "body": "@@ -1,3 +1,4 @@\n context"})
        self.assertIn("diff hunk", str(caught.exception))

    def test_a_map_keyed_by_data_names_is_allowed_while_its_shape_is_not(self):
        # by_source/by_disposition are keyed by names the allowlist cannot know;
        # every other mapping in an artifact stays closed.
        sources_module.assert_public({"schema": "x", "by_source": {"widgets": "in-sync"}})
        with self.assertRaises(sources_module.RedactionError):
            sources_module.assert_public({"schema": "x", "totals": {"surprise": 1}})


class ObservationsRoundTripTests(unittest.TestCase):
    def test_observations_survive_a_document_round_trip(self):
        original = (observation(paths=("a.py", "b.py")), observation(identifier="other", state="unavailable"))
        document = sources_module.observations_document(original, generated_at="2026-09-02T00:00:00Z")
        restored = sources_module.observations_from_document(document)
        self.assertEqual([item.id for item in restored], ["widgets", "other"])
        self.assertEqual([item.paths for item in restored], [("a.py", "b.py"), ()])

    def test_an_unknown_schema_is_refused(self):
        with self.assertRaises(ledger_module.LedgerError) as caught:
            sources_module.observations_from_document({"schema": "nope"})
        self.assertEqual(caught.exception.code, "unknown_schema")

    def test_an_unknown_state_is_refused(self):
        with self.assertRaises(ledger_module.LedgerError) as caught:
            sources_module.observations_from_document(
                {"schema": sources_module.OBSERVATIONS_SCHEMA, "sources": [{"state": "maybe"}]}
            )
        self.assertEqual(caught.exception.code, "unknown_observation_state")


# --------------------------------------------------------------------------- #
# Classifying an advance
# --------------------------------------------------------------------------- #


class ClassificationTests(unittest.TestCase):
    def verdict_for(self, paths, **kwargs):
        report = report_for([observation(paths=paths, **kwargs)])
        return report.sources[0]

    def test_an_unclassified_path_is_p0(self):
        verdict = self.verdict_for(("brand/new/file.py",))
        self.assertEqual(verdict.priority, "p0")
        self.assertEqual(verdict.paths[0].disposition, "")

    def test_an_absorbed_path_is_p0(self):
        verdict = self.verdict_for((".github/workflows/opencode.yml",))
        self.assertEqual(verdict.priority, "p0")
        self.assertEqual(verdict.paths[0].disposition, "absorbed")

    def test_a_superseded_path_is_p1(self):
        verdict = self.verdict_for(("legacy/thing.py",))
        self.assertEqual(verdict.priority, "p1")
        self.assertEqual(verdict.paths[0].disposition, "superseded")

    def test_a_consumer_local_path_is_a_green_no_op(self):
        verdict = self.verdict_for(("docs/readme.md",))
        self.assertEqual(verdict.priority, "")
        self.assertEqual(verdict.verdict, drift_module.ADVANCED)
        self.assertFalse(verdict.needs_triage)
        report = report_for([observation(paths=("docs/readme.md",))])
        self.assertTrue(report.clean)

    def test_a_not_applicable_path_is_a_green_no_op(self):
        verdict = self.verdict_for(("never/thing.py",))
        self.assertEqual(verdict.priority, "")

    def test_a_mixed_advance_takes_its_worst_priority(self):
        verdict = self.verdict_for(("docs/readme.md", "new/file.py"))
        self.assertEqual(verdict.priority, "p0")

    def test_a_changed_path_absorbed_by_a_rule_is_p0(self):
        verdict = self.verdict_for((".github/workflows/opencode.yml",))
        self.assertEqual(verdict.priority, "p0")
        self.assertEqual(verdict.paths[0].status, "changed")

    def test_a_removed_path_is_p0_even_when_the_rule_absorbed_it(self):
        observation_value = observation()
        observation_value = dataclasses.replace(
            observation_value,
            changes=(
                sources_module.Change(
                    path=".github/workflows/opencode.yml", status="removed"
                ),
            ),
        )
        report = report_for([observation_value])
        verdict = report.sources[0]
        self.assertEqual(verdict.paths[0].status, "removed")
        self.assertEqual(verdict.priority, "p0")
        self.assertIn("removed", verdict.paths[0].summary)

    def test_a_private_advance_cannot_be_classified_from_publishable_data(self):
        report = report_for(
            [
                observation(head=AUDITED, previous=AUDITED),
                observation(
                    identifier="private-consumer",
                    visibility="private",
                    private=True,
                    paths=("automation/x.py",),
                ),
            ]
        )
        verdict = [item for item in report.sources if item.id == "private-consumer"][0]
        self.assertEqual(verdict.priority, "p0")
        self.assertEqual(verdict.paths, ())
        self.assertIn("private", verdict.summary)
        self.assertFalse(report.clean)
        self.assertFalse(report.can_claim_clean)

    def test_a_private_source_in_sync_does_not_need_the_operator_every_run(self):
        report = report_for(
            [
                observation(head=AUDITED, previous=AUDITED),
                observation(
                    identifier="private-consumer",
                    visibility="private",
                    private=True,
                    head=AUDITED,
                    previous=AUDITED,
                ),
            ]
        )
        self.assertTrue(report.can_claim_clean)
        verdict = [item for item in report.sources if item.id == "private-consumer"][0]
        self.assertEqual(verdict.priority, "")

    def test_an_unchanged_source_is_in_sync_and_carries_no_paths(self):
        verdict = self.verdict_for((), head=AUDITED, previous=AUDITED)
        self.assertEqual(verdict.verdict, drift_module.IN_SYNC)
        self.assertEqual(verdict.priority, "")
        self.assertEqual(verdict.paths, ())

    def test_a_truncated_advance_is_p0_even_when_what_was_visible_is_p1(self):
        # The cap must not decide the severity of a list nobody read in full.
        verdict = self.verdict_for(("legacy/thing.py",), truncated=True)
        self.assertEqual(verdict.priority, "p0")

    def test_an_unreadable_source_has_no_path_verdicts(self):
        verdict = self.verdict_for((), state="unavailable", code="not-found", reason="gone")
        self.assertEqual(verdict.verdict, drift_module.UNAVAILABLE)
        self.assertEqual(verdict.priority, "")
        self.assertEqual(verdict.paths, ())

    def test_an_unreadable_source_is_not_counted_as_clean(self):
        report = report_for([observation(state="unavailable", code="not-found")])
        self.assertFalse(report.can_claim_clean)

    def test_a_source_with_no_observation_is_unavailable_rather_than_absent(self):
        report = report_for([observation()])
        self.assertEqual(len(report.sources), 2)
        missing = [item for item in report.sources if item.id == "private-consumer"][0]
        self.assertEqual(missing.verdict, drift_module.UNAVAILABLE)
        self.assertEqual(missing.code, "no-observation")

    def test_an_empty_ledger_is_not_a_clean_ledger(self):
        report = report_for([], sources=[])
        self.assertEqual(report.sources, ())
        self.assertFalse(report.clean)
        self.assertFalse(report.can_claim_clean)


class ReportTests(unittest.TestCase):
    def test_clean_and_can_claim_clean_are_different_claims(self):
        clean = report_for(all_read(observation(head=AUDITED, previous=AUDITED)))
        self.assertTrue(clean.clean)
        self.assertTrue(clean.can_claim_clean)

        p1 = report_for(all_read(observation(paths=("legacy/thing.py",))))
        self.assertTrue(p1.clean)
        self.assertTrue(
            p1.can_claim_clean,
            "an advisory P1 finding must not block a claim about P0",
        )

        unread = report_for(all_read(observation(state="unavailable", code="not-found")))
        self.assertTrue(unread.clean)
        self.assertFalse(unread.can_claim_clean)

        p0 = report_for(all_read(observation(paths=("new/file.py",))))
        self.assertFalse(p0.clean)
        self.assertFalse(p0.can_claim_clean)

    def test_the_report_document_passes_the_public_allowlist(self):
        document = report_for(
            [
                observation(paths=("new/file.py", "docs/x.md")),
                observation(
                    identifier="private-consumer",
                    visibility="private",
                    private=True,
                    paths=("automation/x.py",),
                ),
            ]
        ).describe()
        sources_module.assert_public(document)
        # Two, not one: the private source advanced, and an advance it may not
        # describe is an advance nobody can classify, so it counts too.
        self.assertEqual(document["unclassified_p0"], 2)

    def test_a_private_source_publishes_no_paths_or_repository_name(self):
        document = report_for(
            [
                observation(head=AUDITED, previous=AUDITED),
                observation(
                    identifier="private-consumer",
                    visibility="private",
                    private=True,
                    paths=("automation/x.py",),
                    commits=("f" * 40,),
                ),
            ]
        ).describe()
        sources_module.assert_public(document)
        published = [item for item in document["sources"] if item["id"] == "private-consumer"][0]
        self.assertEqual(published["paths"], [])
        self.assertEqual(published["commits"], [])
        self.assertEqual(published["pull_requests"], [])
        self.assertEqual(published["repository"], "<redacted:private>")
        self.assertIn("paths", published["redacted"])
        self.assertEqual(published["verdict"], "advanced")
        self.assertEqual(published["priority"], "p0")
        # The ledger id is deliberately kept: the operator holding the credential
        # is the only one who can act on it, and it is in their overlay, not here.
        self.assertNotIn("example/private-consumer", json.dumps(document))

    def test_the_markdown_report_names_every_finding_and_no_upstream_content(self):
        report = report_for([observation(paths=("new/file.py",), truncated=True)])
        text = drift_module.render_markdown(report)
        self.assertIn("new/file.py", text)
        self.assertIn("#412", text)
        self.assertNotIn("@@", text)

    def test_the_summary_line_counts_what_the_gate_reads(self):
        line = drift_module.summary_line(report_for([observation(paths=("new/file.py",))]))
        self.assertIn("1 unclassified P0", line)
        self.assertIn("not clean", line)

    def test_an_unanswered_incident_is_reported_without_being_drift(self):
        document = ledger_document()
        document["sources"][0]["incidents"] = [
            {"id": "example/widgets#1", "disposition": "must-port"}
        ]
        document["sources"] = [document["sources"][0]]
        ledger = ledger_module.ledger_from_document(document)
        self.assertEqual(
            drift_module.unanswered_incidents(ledger), (("widgets", "example/widgets#1"),)
        )


# --------------------------------------------------------------------------- #
# The deduplicated issue
# --------------------------------------------------------------------------- #


class IssuePlanTests(unittest.TestCase):
    def drifting_report(self):
        return report_for(
            [observation(paths=("new/file.py",)), observation(identifier="private-consumer", visibility="private", private=True, paths=("automation/x.py",))]
        )

    def test_the_first_finding_creates_one_issue(self):
        plan = issue_module.plan(self.drifting_report())
        self.assertEqual([item.action for item in plan], ["create", "create"])

    def test_a_source_that_needs_no_triage_gets_no_plan(self):
        report = report_for([observation(paths=("docs/readme.md",))])
        self.assertEqual(issue_module.plan(report), ())

    def test_a_repeat_scan_of_the_same_head_writes_nothing(self):
        report = self.drifting_report()
        first = issue_module.plan(report)[0]
        existing = [issue_module.ExistingIssue(number=7, body=first.body, title=first.title)]
        again = issue_module.plan(report, existing)
        self.assertEqual(again[0].action, "none")
        self.assertEqual(again[0].number, 7)
        self.assertFalse(again[0].writes)

    def test_a_new_head_updates_the_same_issue(self):
        report = self.drifting_report()
        first = issue_module.plan(report)[0]
        existing = [issue_module.ExistingIssue(number=7, body=first.body, title=first.title)]
        moved = report_for([observation(paths=("new/file.py",), head="f" * 40)])
        updated = issue_module.plan(moved, existing)
        self.assertEqual(updated[0].action, "update")
        self.assertEqual(updated[0].number, 7)
        self.assertNotEqual(updated[0].body, first.body)

    def test_one_issue_per_source_is_chosen_deterministically(self):
        report = self.drifting_report()
        body = issue_module.marker_for("widgets", HEAD)
        existing = [
            issue_module.ExistingIssue(number=11, body="text\n{}\n".format(body), title="a"),
            issue_module.ExistingIssue(number=4, body="text\n{}\n".format(body), title="b"),
        ]
        planned = issue_module.plan(report, existing)[0]
        self.assertEqual(planned.number, 4)
        self.assertEqual(planned.duplicate_numbers, (11,))

    def test_the_title_is_stable_across_scans(self):
        first = issue_module.plan(self.drifting_report())[0].title
        moved = report_for([observation(paths=("other/file.py",), head="f" * 40)])
        self.assertEqual(issue_module.plan(moved)[0].title, first)

    def test_the_marker_round_trips(self):
        marker = issue_module.marker_for("widgets", HEAD)
        self.assertEqual(issue_module.parse_marker(marker), ("widgets", HEAD[:12]))
        self.assertIsNone(issue_module.parse_marker("no marker here"))

    def test_the_body_carries_evidence_and_no_upstream_content(self):
        body = issue_module.plan(self.drifting_report())[0].body
        self.assertIn(HEAD[:12], body)
        self.assertIn("new/file.py", body)
        self.assertIn("#412", body)
        self.assertNotIn("@@", body)
        self.assertIn("do not promote a consumer pin", body.lower().replace("\n", " "))

    def test_the_private_issue_says_what_was_withheld(self):
        body = issue_module.plan(self.drifting_report())[1].body
        self.assertIn("private", body)
        self.assertNotIn("automation/x.py", body)
        self.assertNotIn("example/private-consumer", body)

    def test_the_plan_document_passes_the_public_allowlist(self):
        document = issue_module.plan_document(self.drifting_report())
        sources_module.assert_public(document)
        self.assertEqual(
            document["totals"],
            {"create": 2, "update": 0, "reopen": 0, "close": 0, "skip": 0},
        )

    def test_a_closed_issue_over_live_drift_is_reopened(self):
        report = self.drifting_report()
        first = issue_module.plan(report)[0]
        existing = [
            issue_module.ExistingIssue(
                number=7, body=first.body, title=first.title, state="closed"
            )
        ]
        planned = issue_module.plan(report, existing)
        self.assertEqual(planned[0].action, "reopen")
        self.assertEqual(planned[0].number, 7)
        # Reopened with the current evidence, not the body it had when closed.
        self.assertEqual(planned[0].body, first.body)

    def test_a_closed_issue_is_reopened_even_at_the_same_head(self):
        # The head matching is what makes a repeat scan free. It must not also be
        # what lets a closed issue over live drift stay closed.
        report = self.drifting_report()
        first = issue_module.plan(report)[0]
        existing = [
            issue_module.ExistingIssue(
                number=7, body=first.body, title=first.title, state="closed"
            )
        ]
        self.assertEqual(issue_module.plan(report, existing)[0].action, "reopen")

    def test_a_source_that_stopped_drifting_closes_its_issue(self):
        report = self.drifting_report()
        first = issue_module.plan(report)[0]
        existing = [
            issue_module.ExistingIssue(number=7, body=first.body, title=first.title)
        ]
        settled = report_for(all_read(observation(head=AUDITED, previous=AUDITED)))
        planned = issue_module.plan(settled, existing)
        self.assertEqual([item.action for item in planned], ["close"])
        self.assertEqual(planned[0].number, 7)
        self.assertEqual(planned[0].source_id, "widgets")
        self.assertIn("no longer reporting unclassified drift", issue_module.RESOLUTION_COMMENT)

    def test_a_closed_issue_is_not_closed_again(self):
        report = self.drifting_report()
        first = issue_module.plan(report)[0]
        existing = [
            issue_module.ExistingIssue(
                number=7, body=first.body, title=first.title, state="closed"
            )
        ]
        settled = report_for(all_read(observation(head=AUDITED, previous=AUDITED)))
        self.assertEqual(issue_module.plan(settled, existing), ())

    def test_an_issue_about_a_source_the_ledger_dropped_is_left_alone(self):
        # The marker is Continuum's namespace, but the plan does not get to decide
        # that a human's note about an untracked repository is stale.
        report = report_for(all_read(observation(head=AUDITED, previous=AUDITED)))
        existing = [
            issue_module.ExistingIssue(
                number=7,
                body="text\n{}\n".format(issue_module.marker_for("retired-source", HEAD)),
                title="a note",
            )
        ]
        self.assertEqual(issue_module.plan(report, existing), ())

    def test_an_issue_with_no_marker_is_never_planned_for(self):
        report = report_for(all_read(observation(head=AUDITED, previous=AUDITED)))
        existing = [issue_module.ExistingIssue(number=7, body="a human's issue", title="x")]
        self.assertEqual(issue_module.plan(report, existing), ())

    def test_an_existing_issue_without_a_number_is_refused(self):
        with self.assertRaises(issue_module.IssuePlanError):
            issue_module.existing_from_documents([{"body": "text"}])

    def test_existing_issues_may_be_wrapped_in_an_object(self):
        issues = issue_module.existing_from_documents({"issues": [{"number": 3, "body": ""}]})
        self.assertEqual([item.number for item in issues], [3])


class ApplyPlanTests(unittest.TestCase):
    def plan_document(self, *actions):
        return {"plans": [dict(action, number=number, title="T", body="B")
                          for number, action in enumerate(actions, start=1)]}

    def test_a_dry_run_runs_nothing_and_returns_the_commands(self):
        ran = []
        document = issue_module.apply_plan(
            self.plan_document({"action": "create"}),
            "kodmial/continuum",
            run=ran.append,
            dry_run=True,
        )
        self.assertEqual(ran, [])
        self.assertEqual(len(document["commands"]), 1)
        argv = document["commands"][0]["argv"]
        self.assertEqual(argv[:5], ["gh", "issue", "create", "--repo", "kodmial/continuum"])
        sources_module.assert_public(document)

    def test_a_body_is_one_argument_and_never_a_shell_string(self):
        ran = []
        issue_module.apply_plan(
            {"plans": [{"action": "create", "number": 0, "title": "t",
                        "body": "text\n--body /etc/passwd\n"}]},
            "kodmial/continuum",
            run=ran.append,
        )
        self.assertEqual(len(ran), 1)
        argv = ran[0]
        self.assertEqual(argv.count("--body"), 1)
        self.assertEqual(argv[-1], "text\n--body /etc/passwd\n")

    def test_reopen_writes_the_state_before_the_body(self):
        ran = []
        issue_module.apply_plan(
            self.plan_document({"action": "reopen"}), "kodmial/continuum", run=ran.append
        )
        self.assertEqual([argv[2] for argv in ran], ["reopen", "edit"])

    def test_close_explains_itself_before_it_closes(self):
        ran = []
        issue_module.apply_plan(
            self.plan_document({"action": "close"}), "kodmial/continuum", run=ran.append
        )
        self.assertEqual([argv[2] for argv in ran], ["comment", "close"])
        self.assertEqual(ran[0][-1], issue_module.RESOLUTION_COMMENT)

    def test_an_unchanged_source_writes_nothing(self):
        ran = []
        document = issue_module.apply_plan(
            self.plan_document({"action": "none"}), "kodmial/continuum", run=ran.append
        )
        self.assertEqual(ran, [])
        self.assertEqual(document["totals"]["skip"], 1)

    def test_a_plan_with_no_repository_is_refused(self):
        with self.assertRaises(issue_module.IssuePlanError):
            issue_module.apply_plan(self.plan_document(), "  ", run=lambda argv: None)

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(issue_module.IssuePlanError):
            issue_module.apply_plan(
                self.plan_document({"action": "delete"}),
                "kodmial/continuum",
                run=lambda argv: None,
            )

    def test_a_document_that_is_not_a_plan_is_refused(self):
        for bad in ({}, {"plans": {}}, [], {"plans": [1]}):
            with self.assertRaises(issue_module.IssuePlanError):
                issue_module.apply_plan(bad, "kodmial/continuum", run=lambda argv: None)


# --------------------------------------------------------------------------- #
# The promotion gate
# --------------------------------------------------------------------------- #


class PromotionTests(unittest.TestCase):
    def clean_report(self):
        return report_for(all_read(observation(head=AUDITED, previous=AUDITED)))

    def test_a_clean_fully_read_report_may_assert_the_claim(self):
        verdict = promote_module.promote(self.clean_report(), consumer="acme-widgets")
        self.assertTrue(verdict.approved)
        self.assertEqual(verdict.claim, "no unclassified P0 drift")
        self.assertTrue(verdict.asserts_claim)
        self.assertEqual(verdict.blockers, ())

    def test_p0_drift_refuses_and_names_the_source(self):
        report = report_for(all_read(observation(paths=("new/file.py",))))
        verdict = promote_module.promote(report, consumer="acme-widgets")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.claim, "")
        self.assertIn("widgets", verdict.blockers[0])

    def test_an_unreadable_source_refuses_because_the_claim_is_holey(self):
        report = report_for(all_read(observation(state="unavailable", code="not-found")))
        verdict = promote_module.promote(report, consumer="acme-widgets")
        self.assertFalse(verdict.approved)
        self.assertIn("not read", " ".join(verdict.blockers))

    def test_p1_is_recorded_and_does_not_block(self):
        report = report_for(all_read(observation(paths=("legacy/thing.py",))))
        verdict = promote_module.promote(report, consumer="acme-widgets")
        self.assertTrue(verdict.approved)
        self.assertEqual(len(verdict.advisories), 1)

    def test_a_report_with_no_sources_cannot_support_a_claim(self):
        with self.assertRaises(promote_module.PromotionError) as caught:
            promote_module.promote(report_for([], sources=[]), consumer="acme-widgets")
        self.assertEqual(caught.exception.code, "empty_ledger")

    def test_a_consumer_is_required(self):
        with self.assertRaises(promote_module.PromotionError):
            promote_module.promote(self.clean_report(), consumer="  ")

    def test_a_pin_must_be_a_commit_sha(self):
        for field in ("from_pin", "to_pin"):
            with self.assertRaises(promote_module.PromotionError) as caught:
                promote_module.promote(
                    self.clean_report(), consumer="acme-widgets", **{field: "main"}
                )
            self.assertEqual(caught.exception.code, "malformed_pin")

    def test_the_verdict_document_passes_the_public_allowlist(self):
        verdict = promote_module.promote(
            self.clean_report(), consumer="acme-widgets", to_pin="a" * 40
        )
        document = verdict.describe()
        sources_module.assert_public(document)
        self.assertEqual(document["to_pin"], "a" * 40)

    def test_a_report_is_readable_back_and_judged_the_same_way(self):
        document = report_for(all_read(observation(paths=("new/file.py",)))).describe()
        rebuilt = promote_module.report_from_document(document)
        verdict = promote_module.promote(rebuilt, consumer="acme-widgets")
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.unclassified_p0, 1)

    def test_a_report_that_understates_its_own_findings_is_refused(self):
        # A stale artifact that claims no P0 while listing one must not authorise a
        # promotion, so the recorded count is checked against the findings.
        document = report_for(all_read(observation(paths=("new/file.py",)))).describe()
        document["unclassified_p0"] = 0
        with self.assertRaises(promote_module.PromotionError) as caught:
            promote_module.report_from_document(document)
        self.assertEqual(caught.exception.code, "report_self_inconsistent")

    def test_the_claim_string_is_exported_so_it_cannot_be_retyped(self):
        self.assertEqual(
            promote_module.NO_UNCLASSIFIED_P0_CLAIM, "no unclassified P0 drift"
        )


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #


class CliTests(unittest.TestCase):
    def run_cli(self, argv, **environment):
        previous = dict(os.environ)
        os.environ.update({key: value for key, value in environment.items()})
        try:
            import contextlib
            import io

            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = parity_cli.main(argv)
            return code, out.getvalue()
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def write(self, tmpdir, name, value):
        path = pathlib.Path(tmpdir) / name
        path.write_text(
            value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
        )
        return str(path)

    def test_ledger_check_accepts_the_committed_ledger(self):
        code, output = self.run_cli(
            ["ledger-check", "--ledger", str(COMMITTED)]
        )
        self.assertEqual(code, 0)
        self.assertIn("parity-ledger-check", output)

    def test_ledger_check_fails_on_an_error_level_finding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            document = ledger_document()
            document["sources"] = [document["sources"][1]]
            path = self.write(tmpdir, "ledger.json", document)
            code, output = self.run_cli(["ledger-check", "--ledger", path])
            self.assertEqual(code, 1)
            self.assertIn("private-source-in-committed-ledger", output)

    def test_scan_reads_a_recorded_observations_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            observations = self.write(
                tmpdir,
                "observations.json",
                sources_module.observations_document(
                    [observation(paths=("docs/readme.md",))],
                    generated_at="2026-09-02T00:00:00Z",
                ),
            )
            ledger = self.write(tmpdir, "ledger.json", ledger_document())
            out = self.write(tmpdir, "report.json", "{}")
            code, output = self.run_cli(
                [
                    "scan",
                    "--ledger",
                    ledger,
                    "--observations",
                    observations,
                    "--out",
                    out,
                ]
            )
            self.assertEqual(code, 0, output)
            document = json.loads(pathlib.Path(out).read_text(encoding="utf-8"))
            # docs/readme.md is classified consumer-local, so the public source is
            # a green no-op and counts nothing. private-consumer had no
            # observation: unaccounted for, which is a different thing from
            # drifting, and is reported separately rather than as P0.
            self.assertEqual(document["unclassified_p0"], 0)
            self.assertEqual(document["unavailable_sources"], 1)
            self.assertEqual(
                [item["verdict"] for item in document["sources"]],
                ["advanced", "unavailable"],
            )
            self.assertEqual(document["unclassified_p0"], 0)
            self.assertEqual(
                [path["disposition"] for path in document["sources"][0]["paths"]],
                ["consumer-local"],
            )
            self.assertEqual(document["sources"][0]["paths"][0]["priority"], "")

    def test_scan_can_fail_on_drift_on_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            observations = self.write(
                tmpdir,
                "observations.json",
                sources_module.observations_document(
                    [observation(paths=("new/file.py",))], generated_at="2026-09-02T00:00:00Z"
                ),
            )
            ledger = self.write(tmpdir, "ledger.json", ledger_document())
            code, output = self.run_cli(
                ["scan", "--ledger", ledger, "--observations", observations, "--fail-on-drift"]
            )
            self.assertEqual(code, 1)
            self.assertIn("unclassified P0", output)

    def test_fail_on_drift_also_refuses_an_unreadable_source(self):
        # The flag asks the question the gate asks. A run that read nothing has no
        # P0 to report, so keying the refusal on P0 alone would let a scheduled
        # audit pass having verified nothing at all.
        with tempfile.TemporaryDirectory() as tmpdir:
            observations = self.write(
                tmpdir,
                "observations.json",
                sources_module.observations_document(
                    [observation(state="unavailable", code="read-credential-absent",
                                 reason="no read-only token was configured")],
                    generated_at="2026-09-02T00:00:00Z",
                ),
            )
            ledger = self.write(tmpdir, "ledger.json", ledger_document())
            code, output = self.run_cli(
                ["scan", "--ledger", ledger, "--observations", observations, "--fail-on-drift"]
            )
            self.assertEqual(code, 1)
            self.assertIn("unaccounted for", output)
            self.assertIn("widgets", output)

    def test_an_unreadable_source_is_reported_as_a_result_when_not_asked_to_refuse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            observations = self.write(
                tmpdir,
                "observations.json",
                sources_module.observations_document(
                    [observation(state="unavailable", code="not-found")],
                    generated_at="2026-09-02T00:00:00Z",
                ),
            )
            ledger = self.write(tmpdir, "ledger.json", ledger_document())
            code, output = self.run_cli(["scan", "--ledger", ledger, "--observations", observations])
            self.assertEqual(code, 0, output)
            self.assertIn("unaccounted for", output)

    def test_the_summary_line_says_which_claim_holds(self):
        clean = report_for(all_read(observation(head=AUDITED, previous=AUDITED)))
        self.assertIn("clean over the tracked set", drift_module.summary_line(clean))
        unreadable = report_for(all_read(observation(state="unavailable", code="not-found")))
        line = drift_module.summary_line(unreadable)
        self.assertIn("unreadable", line)
        self.assertNotIn("clean over the tracked set", line)
        dirty = report_for(all_read(observation(paths=("new/file.py",))))
        self.assertTrue(drift_module.summary_line(dirty).endswith("not clean"))

    def test_no_two_handlers_write_the_same_job_output_key(self):
        # Every handler writes GITHUB_OUTPUT, and the audit job runs two of them in
        # one step group. One key with two meanings is a silent last-writer-wins
        # bug, so the union of written keys has to be unique per handler.
        source = pathlib.Path(parity_cli.__file__).read_text(encoding="utf-8")
        written = {}
        for name in re.findall(r"def (_[a-z_]+)\(args", source):
            start = source.index("def {}".format(name))
            following = [
                source.index("def {}".format(other))
                for other in re.findall(r"def (_[a-z_]+)\(args", source)
                if source.index("def {}".format(other)) > start
            ]
            body = source[start : min(following) if following else len(source)]
            block = body[body.index("_write_output(") :] if "_write_output(" in body else ""
            keys = set(re.findall(r'"([a-z_0-9]+)":', block))
            for key in keys:
                self.assertNotIn(
                    key, written, "{} and {} both write {}".format(written.get(key), name, key)
                )
                written[key] = name
        self.assertIn("drift_p0", written)

    def test_scan_without_a_recorded_file_does_not_reach_the_network(self):
        # No credential is configured and no observations file was given, so every
        # source is unavailable rather than the command hanging on an API call.
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = self.write(tmpdir, "ledger.json", ledger_document())
            code, output = self.run_cli(
                ["scan", "--ledger", ledger, "--fetch"],
                GITHUB_TOKEN="",
                CONTINUUM_PARITY_PRIVATE_TOKEN="",
            )
            self.assertEqual(code, 0)
            self.assertIn("was not read", output)

    def test_the_issue_command_plans_and_passes_the_allowlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.write(tmpdir, "report.json", report_for([observation(paths=("new/file.py",))]).describe())
            out = self.write(tmpdir, "plan.json", "{}")
            code, output = self.run_cli(["issue", "--report", report, "--out", out])
            self.assertEqual(code, 0)
            document = json.loads(pathlib.Path(out).read_text(encoding="utf-8"))
            sources_module.assert_public(document)
            self.assertGreaterEqual(document["totals"]["create"], 1)

    def test_the_apply_command_dry_runs_without_touching_gh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.write(tmpdir, "report.json", report_for(all_read(observation(paths=("new/file.py",)))).describe())
            plan_path = self.write(tmpdir, "plan.json", "{}")
            self.run_cli(["issue", "--report", report, "--out", plan_path])
            out = self.write(tmpdir, "applied.json", "{}")
            # A PATH with no gh in it at all: if the dry run reached for the
            # binary, this fails rather than quietly writing an issue.
            code, output = self.run_cli(
                [
                    "apply",
                    "--plan",
                    plan_path,
                    "--repository",
                    "kodmial/continuum",
                    "--dry-run",
                    "--out",
                    out,
                ],
                PATH="",
            )
            self.assertEqual(code, 0, output)
            document = json.loads(pathlib.Path(out).read_text(encoding="utf-8"))
            sources_module.assert_public(document)
            self.assertTrue(document["dry_run"])
            self.assertEqual(document["totals"]["create"], 1)
            self.assertEqual(len(document["commands"]), 1)
            self.assertIn("would run", output)

    def test_the_apply_command_refuses_without_a_repository(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.write(tmpdir, "report.json", report_for([observation(paths=("new/file.py",))]).describe())
            plan_path = self.write(tmpdir, "plan.json", "{}")
            self.run_cli(["issue", "--report", report, "--out", plan_path])
            code, output = self.run_cli(
                ["apply", "--plan", plan_path, "--repository", "", "--dry-run"]
            )
            self.assertEqual(code, 3, output)
            self.assertIn("no repository", output)

    def test_a_second_issue_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_document = report_for([observation(paths=("new/file.py",))]).describe()
            report = self.write(tmpdir, "report.json", report_document)
            first = self.write(tmpdir, "first.json", "{}")
            self.run_cli(["issue", "--report", report, "--out", first])
            planned = json.loads(pathlib.Path(first).read_text(encoding="utf-8"))["plans"]
            existing = self.write(
                tmpdir,
                "existing.json",
                [
                    {"number": item["number"] or 42, "body": item["body"], "title": item["title"]}
                    for item in planned
                ],
            )
            second = self.write(tmpdir, "second.json", "{}")
            code, output = self.run_cli(
                ["issue", "--report", report, "--existing", existing, "--out", second]
            )
            self.assertEqual(code, 0)
            document = json.loads(pathlib.Path(second).read_text(encoding="utf-8"))
            self.assertEqual(
            document["totals"],
            {
                "create": 0,
                "update": 0,
                "reopen": 0,
                "close": 0,
                "skip": len(planned),
            },
        )

    def test_the_promote_command_refuses_and_can_fail_the_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.write(tmpdir, "report.json", report_for([observation(paths=("new/file.py",))]).describe())
            out = self.write(tmpdir, "verdict.json", "{}")
            code, output = self.run_cli(
                [
                    "promote",
                    "--report",
                    report,
                    "--consumer",
                    "acme-widgets",
                    "--fail-on-refused",
                    "--out",
                    out,
                ]
            )
            self.assertEqual(code, 1)
            self.assertIn("refused", output)
            document = json.loads(pathlib.Path(out).read_text(encoding="utf-8"))
            self.assertFalse(document["approved"])
            self.assertEqual(document["claim"], "")

    def test_the_promote_command_approves_a_clean_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.write(
                tmpdir,
                "report.json",
                report_for(
                    all_read(observation(head=AUDITED, previous=AUDITED))
                ).describe(),
            )
            out = self.write(tmpdir, "verdict.json", "{}")
            code, output = self.run_cli(
                ["promote", "--report", report, "--consumer", "acme-widgets", "--out", out]
            )
            self.assertEqual(code, 0, output)
            document = json.loads(pathlib.Path(out).read_text(encoding="utf-8"))
            self.assertEqual(document["claim"], "no unclassified P0 drift")

    def test_a_token_is_never_echoed_by_a_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code, output = self.run_cli(
                ["scan", "--ledger", str(tmpdir) + "/missing.json", "--fetch"],
                GITHUB_TOKEN="ghp_" + "a" * 36,
            )
            self.assertEqual(code, 3)
            self.assertNotIn("ghp_", output)

    def test_the_job_summary_is_written_when_a_runner_provides_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = pathlib.Path(tmpdir) / "summary.md"
            code, _ = self.run_cli(
                ["ledger-check", "--ledger", str(COMMITTED)],
                GITHUB_STEP_SUMMARY=str(summary),
            )
            self.assertEqual(code, 0)
            self.assertIn("Parity ledger check", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()