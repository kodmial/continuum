"""Evidence-only CodeRabbit verdicts must not trigger no-op repairs (#463).

Production trace: PR #459 at HEAD ``a03dd5dc...`` had one unresolved
CodeRabbit thread whose reviewer said *no specific remaining code defect*
but withheld a verdict pending live run/artifact evidence bound to exact
commits. The unresolved batcher nevertheless selected the finding again
after every completed no-change repair (same HEAD, same finding digest
``e938bc71...``, changed CodeRabbit answer id), producing OpenCode runs
43894683811, 43904953729 and 43910386392 with ``REPAIR_CHANGED=false``.

The regression below executes the real collector JavaScript with the
production identities. Before this fix the collector returned
``should_run=true`` and selected finding #4577019422 again; after the fix
it returns no dispatchable item and publishes one durable
``external-evidence-required`` diagnostic per HEAD/finding/digest, while a
new actionable code defect still dispatches exactly once.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

from continuum import coderabbit_adapter as rabbit

from test_coderabbit_unresolved_dispatch import js_for_step

ROOT = Path(__file__).resolve().parents[1]
BATCHER = ROOT / ".github/workflows/continuum-coderabbit-unresolved.yml"
FIXTURES = ROOT / "tests" / "fixtures" / "coderabbit"
FIXTURE = json.loads(
    (FIXTURES / "pr459-evidence-verdicts.json").read_text(encoding="utf-8")
)

BLOCK_START = "// continuum-coderabbit-external-evidence:start"
BLOCK_END = "// continuum-coderabbit-external-evidence:end"


def finding_digest(head: str, review_id: int, finding_id: int) -> str:
    identity = json.dumps([[review_id, finding_id]], separators=(",", ":"))
    return hashlib.sha256((head + "\n" + identity).encode("utf-8")).hexdigest()


def external_evidence_block() -> str:
    text = BATCHER.read_text(encoding="utf-8")
    assert text.count(BLOCK_START) == 1
    assert text.count(BLOCK_END) == 1
    marker = text.index(BLOCK_START)
    start = text.rfind("\n", 0, marker) + 1
    end_marker = text.index(BLOCK_END, marker)
    end = text.find("\n", end_marker)
    raw = text[start:end]
    return textwrap.dedent(raw)


def handoff(
    *,
    head: str,
    finding_id: int = FIXTURE["finding_id"],
    answer_id: int = 4588199753,
    changed: str | None = "false",
    run: str | None = "43894683811",
    attempt: str | None = "4",
    author: str = "kodmial",
    comment_id: int = 8000,
    created_at: str = "2026-11-16T16:15:00Z",
) -> dict:
    marker = (
        f"<!-- opencode-coderabbit-verification-pending head={head} "
        f"findings={finding_id} answers={answer_id}"
    )
    if changed is not None:
        marker += f" changed={changed}"
    if run is not None:
        marker += f" run={run}"
    if attempt is not None:
        marker += f" attempt={attempt}"
    marker += " -->"
    return {
        "id": comment_id,
        "user": {"login": author},
        "created_at": created_at,
        "body": marker + "\nCanonical repair finished.",
    }


def diagnostic_comment(
    *,
    head: str,
    finding_id: int = FIXTURE["finding_id"],
    digest: str | None = None,
    author: str = "kodmial",
    comment_id: int = 8100,
) -> dict:
    digest = digest or finding_digest(
        head, FIXTURE["review_id"], finding_id
    )
    return {
        "id": comment_id,
        "user": {"login": author},
        "created_at": "2026-11-16T16:45:00Z",
        "body": (
            f"<!-- continuum-coderabbit-external-evidence-required "
            f"head={head} finding={finding_id} digest={digest} -->\n"
            "external-evidence-required"
        ),
    }


class CodeRabbitVerdictClassificationTests(unittest.TestCase):
    def test_production_evidence_only_verdicts_are_classified(self):
        for name in ("external_evidence_final", "external_evidence_earlier"):
            with self.subTest(fixture=name):
                result = rabbit.classify_unresolved_verdict(
                    FIXTURE["answers"][name]["body"]
                )
                self.assertEqual(result["kind"], rabbit.VERDICT_EXTERNAL_EVIDENCE)
                self.assertTrue(result["verdict"])

    def test_actionable_code_defect_outranks_a_no_defect_summary(self):
        for name in ("actionable_defect", "actionable_after_no_defect"):
            with self.subTest(fixture=name):
                result = rabbit.classify_unresolved_verdict(
                    FIXTURE["answers"][name]["body"]
                )
                self.assertEqual(result["kind"], rabbit.VERDICT_CODE_DEFECT)

    def test_exact_head_ci_evidence_stays_a_verification_wait(self):
        result = rabbit.classify_unresolved_verdict(
            FIXTURE["answers"]["ci_only"]["body"]
        )
        self.assertEqual(result["kind"], rabbit.VERDICT_VERIFICATION_EVIDENCE)

    def test_analysis_chain_cannot_supply_the_visible_verdict(self):
        body = FIXTURE["answers"]["external_evidence_earlier"]["body"]
        # The quoted command output contains actionable defect language.
        self.assertIn("defect remains and must be fixed", body)
        self.assertEqual(
            rabbit.classify_unresolved_verdict(body)["kind"],
            rabbit.VERDICT_EXTERNAL_EVIDENCE,
        )

    def test_empty_review_answer_is_unverifiable(self):
        self.assertEqual(
            rabbit.classify_unresolved_verdict("")["kind"],
            rabbit.VERDICT_UNVERIFIABLE,
        )

    def test_marker_and_digest_reproduce_the_production_identity(self):
        digest = finding_digest(
            FIXTURE["head"], FIXTURE["review_id"], FIXTURE["finding_id"]
        )
        self.assertEqual(digest, FIXTURE["digest"])
        self.assertEqual(
            rabbit.external_evidence_marker(
                FIXTURE["head"], FIXTURE["finding_id"], digest
            ),
            "<!-- continuum-coderabbit-external-evidence-required "
            f"head={FIXTURE['head']} finding={FIXTURE['finding_id']} "
            f"digest={digest} -->",
        )

    def test_repair_handoff_outcome_is_provenance_bound(self):
        body = handoff(head=FIXTURE["head"])["body"]
        self.assertFalse(rabbit.repair_handoff_changed(body))
        changed = handoff(head=FIXTURE["head"], changed="true")["body"]
        self.assertTrue(rabbit.repair_handoff_changed(changed))
        legacy = (
            f"<!-- opencode-coderabbit-verification-pending "
            f"head={FIXTURE['head']} findings={FIXTURE['finding_id']} -->"
        )
        self.assertIsNone(rabbit.repair_handoff_changed(legacy))


@unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
class CodeRabbitExternalEvidenceJavaScriptParityTests(unittest.TestCase):
    def test_workflow_classifier_matches_the_python_adapter(self):
        script = (
            external_evidence_block()
            + "\nconst data = "
            + json.dumps(FIXTURE)
            + ";\n"
            + "const out = {};\n"
            + "for (const [name, value] of Object.entries(data.answers)) {\n"
            + "  out[name] = classifyUnresolvedVerdict(value.body).kind;\n"
            + "}\n"
            + "process.stdout.write(JSON.stringify(out));\n"
        )
        result = subprocess.run(
            ["node", "-"], input=script, text=True,
            capture_output=True, check=False, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        for name in FIXTURE["answers"]:
            with self.subTest(fixture=name):
                self.assertEqual(
                    actual[name],
                    rabbit.classify_unresolved_verdict(
                        FIXTURE["answers"][name]["body"]
                    )["kind"],
                )

    def test_collector_publishes_one_diagnostic_and_never_dispatches_noop(self):
        results = self.run_collector()
        evidence = results["external_evidence_final"]
        self.assertEqual(evidence["should_run"], "false")
        self.assertEqual(evidence["items"], [])
        self.assertEqual(len(evidence["evidence_required"]), 1)
        diagnostic = evidence["evidence_required"][0]
        self.assertEqual(diagnostic["finding_id"], FIXTURE["finding_id"])
        self.assertEqual(diagnostic["digest"], FIXTURE["digest"])
        self.assertFalse(diagnostic["previous_repair_changed"])
        self.assertEqual(diagnostic["previous_repair_run"], "43894683811")
        self.assertIn(
            "downloaded by artifact ID",
            diagnostic["required_evidence"],
        )
        self.assertTrue(
            any("repair dispatch is suppressed" in notice
                for notice in evidence["notices"]),
            evidence["notices"],
        )

    def test_repeated_identical_evidence_only_answers_do_not_redispatch(self):
        results = self.run_collector()
        for name in ("external_evidence_final", "external_evidence_earlier"):
            with self.subTest(fixture=name):
                self.assertEqual(results[name]["should_run"], "false")
                self.assertEqual(results[name]["items"], [])
                self.assertEqual(len(results[name]["evidence_required"]), 1)
                self.assertEqual(
                    results[name]["evidence_required"][0]["digest"],
                    FIXTURE["digest"],
                )

    def test_existing_trusted_diagnostic_is_idempotent(self):
        result = self.run_collector(
            issue_comments=[diagnostic_comment(head=FIXTURE["head"])],
            fixture_names=("external_evidence_final",),
        )["external_evidence_final"]
        self.assertEqual(result["should_run"], "false")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["evidence_required"], [])

    def test_forged_diagnostic_does_not_suppress_a_real_report(self):
        result = self.run_collector(
            issue_comments=[
                diagnostic_comment(head=FIXTURE["head"], author="stranger")
            ],
            fixture_names=("external_evidence_final",),
        )["external_evidence_final"]
        self.assertEqual(len(result["evidence_required"]), 1)

    def test_changed_head_wakes_the_diagnostic_for_the_new_digest(self):
        new_head = "b" * 40
        result = self.run_collector(
            head=new_head,
            fixture_names=("external_evidence_final",),
            issue_comments=[diagnostic_comment(head=FIXTURE["head"])],
        )["external_evidence_final"]
        self.assertEqual(result["should_run"], "false")
        self.assertEqual(len(result["evidence_required"]), 1)
        self.assertEqual(
            result["evidence_required"][0]["digest"],
            finding_digest(new_head, FIXTURE["review_id"], FIXTURE["finding_id"]),
        )

    def test_real_actionable_defect_still_dispatches_exactly_once(self):
        results = self.run_collector(
            fixture_names=("actionable_defect",),
        )["actionable_defect"]
        self.assertEqual(results["should_run"], "true")
        self.assertEqual(
            [item["finding_id"] for item in results["items"]],
            [FIXTURE["finding_id"]],
        )
        self.assertEqual(results["evidence_required"], [])

    def test_mixed_batch_repairs_only_the_real_code_defect(self):
        results = self.run_collector(
            fixture_names=("actionable_defect", "external_evidence_final"),
            distinct_findings=True,
        )
        actionable = results["actionable_defect"]
        evidence_finding_id = FIXTURE["finding_id"] + 1
        self.assertEqual(actionable["should_run"], "true")
        self.assertEqual(
            [item["finding_id"] for item in actionable["items"]],
            [FIXTURE["finding_id"]],
        )
        self.assertEqual(len(actionable["evidence_required"]), 1)
        self.assertEqual(
            actionable["evidence_required"][0]["finding_id"],
            evidence_finding_id,
        )

    def test_unreadable_handoff_metadata_fails_closed(self):
        result = self.run_collector(
            fixture_names=("external_evidence_final",),
            fail_issue_read=True,
        )["external_evidence_final"]
        self.assertEqual(result["should_run"], "false")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["evidence_required"], [])
        self.assertTrue(
            any("could not read CodeRabbit repair handoffs" in warning
                for warning in result["warnings"]),
            result["warnings"],
        )

    def test_unreadable_verdict_fails_closed_instead_of_dispatching(self):
        result = self.run_collector(
            fixture_names=("empty",),
        )["empty"]
        self.assertEqual(result["should_run"], "false")
        self.assertEqual(result["items"], [])
        self.assertTrue(
            any("has no readable verdict" in warning
                for warning in result["warnings"]),
            result["warnings"],
        )

    def test_unreadable_repair_outcome_is_reported_unknown_not_fabricated(self):
        legacy_handoff = handoff(
            head=FIXTURE["head"], changed=None, run=None, attempt=None
        )
        result = self.run_collector(
            fixture_names=("external_evidence_final",),
            issue_comments=[legacy_handoff],
            fail_compare=True,
        )["external_evidence_final"]
        self.assertEqual(len(result["evidence_required"]), 1)
        self.assertIsNone(
            result["evidence_required"][0]["previous_repair_changed"]
        )
        self.assertTrue(
            any("could not establish the repair commit range" in warning
                for warning in result["warnings"]),
            result["warnings"],
        )

    def test_forged_no_change_handoff_cannot_fake_the_outcome(self):
        result = self.run_collector(
            fixture_names=("external_evidence_final",),
            issue_comments=[
                handoff(head=FIXTURE["head"], author="stranger")
            ],
            compare_ahead_by=1,
        )["external_evidence_final"]
        self.assertEqual(len(result["evidence_required"]), 1)
        self.assertTrue(
            result["evidence_required"][0]["previous_repair_changed"]
        )

    def run_collector(
        self,
        *,
        head: str | None = None,
        fixture_names=(
            "external_evidence_final",
            "external_evidence_earlier",
        ),
        issue_comments=None,
        fail_issue_read: bool = False,
        fail_compare: bool = False,
        compare_ahead_by: int = 0,
        distinct_findings: bool = False,
    ):
        head = head or FIXTURE["head"]
        issue_comments = (
            [handoff(head=head)] if issue_comments is None else issue_comments
        )
        fixture_names = tuple(fixture_names)
        finding_ids = {
            name: FIXTURE["finding_id"] + (index if distinct_findings else 0)
            for index, name in enumerate(fixture_names)
        }
        fixtures = {
            name: FIXTURE["answers"][name]["body"]
            for name in fixture_names
        }
        fixture_ids = {
            name: FIXTURE["answers"][name]["id"]
            for name in fixture_names
        }
        comments = []
        for index, name in enumerate(fixture_names):
            finding_id = finding_ids[name]
            comments.append({
                "id": finding_id,
                "user": {"login": "coderabbitai[bot]"},
                "pull_request_review_id": FIXTURE["review_id"],
                "created_at": "2026-11-15T04:30:19Z",
                "body": "original CodeRabbit finding",
            })
            comments.append({
                "id": 9001 + index,
                "in_reply_to_id": finding_id,
                "user": {"login": "kodmial"},
                "created_at": "2026-11-16T16:36:45Z",
                "body": (
                    "<!-- opencode-coderabbit-verification "
                    f"review={FIXTURE['review_id']} "
                    f"finding={finding_id} head={head} -->"
                ),
            })
        for index, (name, body) in enumerate(fixtures.items(), start=1):
            comments.append({
                "id": fixture_ids[name],
                "in_reply_to_id": finding_ids[name],
                "user": {"login": "coderabbitai[bot]"},
                "created_at": f"2026-11-16T16:38:{index:02d}Z",
                "body": body,
            })
        thread_nodes = ", ".join(
            "{isResolved: false, comments: {nodes: "
            f"[{{databaseId: {finding_ids[name]}, "
            "author: {login: 'coderabbitai[bot]'}}]}}"
            for name in fixture_names
        )
        harness = f"""
const head = {json.dumps(head)};
const comments = {json.dumps(comments)};
let issueComments = {json.dumps(issue_comments)};
const failIssueRead = {json.dumps(fail_issue_read)};
const failCompare = {json.dumps(fail_compare)};
const compareAheadBy = {json.dumps(compare_ahead_by)};
process.env.COMMENT_PAT = 'test-pat';
const out = {{}};
const notices = [];
const warnings = [];
const core = {{
  setOutput: (key, value) => {{ out[key] = String(value); }},
  notice: message => notices.push(message),
  warning: message => warnings.push(message),
}};
const context = {{repo: {{owner: 'kodmial', repo: 'continuum'}},
  payload: {{pull_request: {{number: 459}}}}}};
const github = {{
  request: {{endpoint: {{DEFAULTS: {{baseUrl: 'https://api.github.com'}}}}}},
  constructor: function () {{
    this.rest = {{users: {{getAuthenticated: async () =>
      ({{data: {{login: 'kodmial'}}}})}}}};
  }},
  paginate: async endpoint => (await endpoint({{}})).data,
  graphql: async () => ({{repository: {{pullRequest: {{reviewThreads: {{
    nodes: [{thread_nodes}],
    pageInfo: {{hasNextPage: false, endCursor: null}},
  }}}}}}}}),
  rest: {{
    pulls: {{
      get: async () => ({{data: {{
        review_comments: comments.length,
        comments: issueComments.length,
        head: {{sha: head, ref: 'opencode/issue463',
          repo: {{full_name: 'kodmial/continuum'}}}},
      }}}}),
      listReviewComments: async () => ({{data: comments.slice()}}),
      getReviewComment: async ({{comment_id}}) => ({{
        data: comments.find(comment => comment.id === comment_id),
      }}),
    }},
    git: {{getRef: async () => ({{data: {{object: {{sha: head}}}}}})}},
    repos: {{
      compareCommits: async () => {{
        if (failCompare) throw new Error('compare unavailable');
        return {{data: {{ahead_by: compareAheadBy}}}};
      }},
    }},
    issues: {{
      listComments: async () => {{
        if (failIssueRead) throw new Error('handoffs unavailable');
        return {{data: issueComments.slice()}};
      }},
    }},
  }},
}};
async function collect() {{
"""
        production = js_for_step(BATCHER, "Collect all unresolved findings")
        harness += textwrap.indent(production, "  ")
        harness += """
}
(async () => {
  await collect();
  process.stdout.write(JSON.stringify({
    out,
    notices,
    warnings,
  }));
})().catch(error => { console.error(error); process.exit(1); });
"""
        result = subprocess.run(
            ["node", "-"], input=harness, text=True,
            capture_output=True, check=False, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = json.loads(result.stdout)
        return {
            name: {
                "should_run": raw["out"].get("should_run", ""),
                "items": json.loads(raw["out"].get("items_json") or "[]"),
                "evidence_required": json.loads(
                    raw["out"].get("evidence_required_json") or "[]"
                ),
                "notices": raw["notices"],
                "warnings": raw["warnings"],
            }
            for name in fixture_names
        }


class ExternalEvidenceWorkflowContractTests(unittest.TestCase):
    def test_diagnostic_is_durable_attributed_and_never_resolves_threads(self):
        source = BATCHER.read_text(encoding="utf-8")
        self.assertIn("continuum-coderabbit-external-evidence-required", source)
        self.assertIn("external-evidence-required", source)
        self.assertIn("Publish external evidence requirement", source)
        self.assertIn("steps.context.outputs.evidence_required_json", source)
        self.assertIn(
            "<!-- continuum-origin role=continuum component=coderabbit-unresolved -->",
            source,
        )
        self.assertNotIn("resolveReviewThread", source)
        self.assertNotIn("/comments/{comment_id}/replies", source)

    def test_repair_handoff_parser_accepts_optional_provenance(self):
        # #463 does not change the protected OpenCode workflow or the
        # serialized auto-merge handoff contract. The collector must keep
        # accepting handoffs that carry the optional changed/run/attempt
        # provenance tokens, and must fall back to the exact commit range
        # when a handoff predates them.
        source = BATCHER.read_text(encoding="utf-8")
        self.assertIn("(?: changed=(true|false))?", source)
        self.assertIn("(?: run=([0-9]+))?", source)
        self.assertIn("(?: attempt=([0-9]+))?", source)
        self.assertIn("compareCommits", source)

    def test_diagnostic_step_is_parsed_as_javascript(self):
        code = (
            "async function testWorkflow() {\n"
            + js_for_step(BATCHER, "Publish external evidence requirement")
            + "\n}\n"
        )
        result = subprocess.run(
            ["node", "--check", "-"], input=code, text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
