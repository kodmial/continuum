"""Deterministic tests for the shared comment-attribution contract (#258).

Every human-visible GitHub comment created or updated by Continuum must make
machine authorship unambiguous even when the GitHub actor renders as the
repository owner. The canonical contract lives in
``src/continuum/comment_attribution.py`` (mirrored byte-for-byte by the JS
helpers in ``.github/scripts/pr_agent_policy.js``); this suite proves:

1. every Continuum-owned comment-writing path uses the common contract;
2. every visible body contains the Continuum/component identity;
3. every body carries the stable hidden origin marker;
4. upsert/update paths preserve the marker and the visible attribution;
5. no private repository identifier or secret is introduced;
6. no extra comment is created solely for attribution.

Scoping notes (deliberate, not gaps):

* ``gh pr create --body`` / ``issues.create`` / ``issues.update`` bodies are
  PR/issue descriptions, not comments; they are out of scope for this
  contract. The shipped implementation bodies already identify themselves
  (``Automated implementation ...``, ``Delegated implementation ...
  executed by the configured Continuum parent``), which is asserted below
  so a regression is caught.
* Marker-only election/dedup comments (``auto-main-sync``,
  ``FAILURE_MARKER``-only anchors) render no prose, so they carry the
  hidden origin marker only and stay invisible; every body with rendered
  prose carries the visible header.
* ``/oc`` command comments keep the command token on the first significant
  line (the command gate requires it there); attribution follows the
  command block. ``/review``, ``/verify`` and ``@coderabbitai`` commands
  likewise keep the command line first for third-party parser safety.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from continuum import comment_attribution as attribution
from continuum import opencode_commands as commands
from continuum import pr_agent as pr_agent

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_DIR = os.path.join(ROOT, ".github", "workflows")
POLICY_JS = os.path.join(ROOT, ".github", "scripts", "pr_agent_policy.js")
RECOVERY_WORKFLOW = os.path.join(WORKFLOWS_DIR, "continuum-pr-agent-recovery.yml")

MUTATION_RES = (
    re.compile(r"(?:github|commentGithub)\.rest\.issues\.(?:createComment|updateComment)"),
    re.compile(r"gh\s+(?:issue|pr)\s+comment\b"),
    re.compile(r"gh\s+issue\s+close\b"),
    re.compile(
        r"POST\s+/repos/\{owner\}/\{repo\}/pulls/\{pull_number\}/comments/"
        r"\{comment_id\}/replies"
    ),
    re.compile(
        r"gh\s+api\s+--method\s+(?:POST|PATCH)\s+\"repos/[^\"]*?/issues[^\"]*?/comments[^\"]*\""
    ),
)

#: Pinned comment-mutation counts per workflow. Any new comment-writing
#: path must be classified here (role + component) before it can land:
#: the count pin fails first with instructions.
MUTATION_COUNTS = {
    "continuum-auto-merge.yml": 5,
    "continuum-coderabbit-retry.yml": 1,
    "continuum-coderabbit-unresolved.yml": 1,
    "continuum-consumer-child-dispatcher.yml": 1,
    "continuum-consumer-child-pr-review.yml": 7,
    "continuum-consumer-child-review.yml": 10,
    "continuum-consumer-child-worker.yml": 4,
    "continuum-docker-qualification.yml": 1,
    "continuum-issue-scheduler.yml": 11,
    "continuum-opencode-watchdog.yml": 4,
    "continuum-opencode.yml": 6,
    "continuum-pr-agent-auto-merge.yml": 1,
    "continuum-pr-agent-canary.yml": 4,
    "continuum-pr-agent-recovery.yml": 4,
    "continuum-pr-agent-repair.yml": 6,
    "continuum-pr-agent.yml": 3,
    "continuum-render-executor.yml": 17,
}

#: (role, component) pairs each workflow's comment bodies must use.
EXPECTED_IDENTITIES = {
    "continuum-auto-merge.yml": {("continuum", "auto-merge")},
    "continuum-coderabbit-retry.yml": {("continuum", "coderabbit-retry")},
    "continuum-coderabbit-unresolved.yml": {("continuum", "coderabbit-unresolved")},
    "continuum-consumer-child-dispatcher.yml": {("continuum", "delegation-dispatcher")},
    "continuum-consumer-child-pr-review.yml": {("continuum", "delegation-pr-review")},
    "continuum-consumer-child-review.yml": {("continuum", "delegation-review")},
    "continuum-consumer-child-worker.yml": {
        ("continuum", "delegation-worker"),
        ("agent-coder", "delegation-worker"),
    },
    "continuum-docker-qualification.yml": {("project", "qualification")},
    "continuum-issue-scheduler.yml": {("continuum", "issue-scheduler")},
    "continuum-opencode-watchdog.yml": {("continuum", "opencode-watchdog")},
    "continuum-opencode.yml": {("continuum", "opencode")},
    "continuum-pr-agent-auto-merge.yml": {("continuum", "pr-agent-auto-merge")},
    "continuum-pr-agent-canary.yml": {("continuum", "pr-agent-canary")},
    "continuum-pr-agent-recovery.yml": {("continuum", "pr-agent-recovery")},
    "continuum-pr-agent-repair.yml": {
        ("continuum", "pr-agent-repair"),
        ("continuum", "pr-agent-repair-handoff"),
    },
    "continuum-pr-agent.yml": {
        ("continuum", "pr-agent-review"),
        ("continuum", "pr-agent-repair-handoff"),
    },
    "continuum-render-executor.yml": {
        ("continuum", "render-executor"),
        ("project", "qualification"),
    },
}

#: Pre-existing functional content anchors per workflow. Every anchor must
#: still sit next to a comment-mutation call: attribution is inserted into
#: the existing body, never posted as a replacement or a second comment.
FUNCTIONAL_ANCHORS = {
    "continuum-auto-merge.yml": (
        "'@coderabbitai full review",
        "Re-check this unresolved original finding",
        "conflictRepairMarker(headSha, attempt)",
        "continuum-conflict-repair-deferred head=",
        "auto-main-sync head=${fresh.head.sha}",
    ),
    "continuum-coderabbit-retry.yml": ("@coderabbitai full review",),
    "continuum-coderabbit-unresolved.yml": (
        "Re-check this exact original finding after the batched OpenCode retry.",
    ),
    "continuum-consumer-child-dispatcher.yml": (
        "Closed automatically: delegated task #",
    ),
    "continuum-consumer-child-pr-review.yml": (
        "continuum-child-pr-review-started",
        "continuum-child-pr-validation-missing",
        "continuum-child-pr-review-infra",
        "continuum-child-pr-review-needs-retry",
        "continuum-child-pr-review-security",
        "continuum-child-pr-review-merge-retry",
        "continuum-child-pr-review-merged",
    ),
    "continuum-consumer-child-review.yml": (
        "continuum-child-review-reopened",
        "continuum-child-review-retry",
        "review_started",
        "continuum-child-validation-missing",
        "continuum-child-review-infra",
        "continuum-child-review-needs-retry",
        "continuum-child-review-security",
        "continuum-child-review-merge-retry",
        "Closed automatically: task #",
        "continuum-child-review-merged",
    ),
    "continuum-consumer-child-worker.yml": (
        "continuum-child-infra",
        "continuum-child-needs-retry",
        "continuum-child-security",
        "continuum-child-pr",
    ),
    "continuum-docker-qualification.yml": ('"$RESULT_MARKER" "$BODY"',),
    "continuum-issue-scheduler.yml": (
        "Automatically dispatched by the issue scheduler",
        "qualificationDispatchMarker(",
        "Capability complete: mandatory",
        "repair tracked in",
        "Implementation merged",
        "Automation paused: ",
        "Reopened by the qualification gate",
        "Closed automatically: delegated task #",
    ),
    "continuum-opencode-watchdog.yml": (
        "OpenCode automation paused after ",
        "Automatic recovery retry ",
        "fresh-VM resumption for operation",
        "bounded infrastructure backoff",
    ),
    "continuum-opencode.yml": (
        "Continuum classified this CodeRabbit",
        "@coderabbitai full review",
        "Re-check this exact original finding after the OpenCode fixes.",
        "Automation produced no code changes",
        "Qualification attempted product changes",
        "produced no trusted pass/fail evidence",
    ),
    "continuum-pr-agent-auto-merge.yml": (
        "Automatic OpenCode conflict repair, attempt 1/1",
    ),
    "continuum-pr-agent-canary.yml": (
        "'/review',",
        "`/verify ${findingId}`",
        "Canary evidence bundle:",
    ),
    "continuum-pr-agent-recovery.yml": (
        "controllerStateBody(",
        "Controller dispatch run: ",
    ),
    "continuum-pr-agent-repair.yml": (
        "policy.controllerStateBody",
        "printf -v BODY",
    ),
    "continuum-pr-agent.yml": (
        "policy.controllerStateBody",
        "continuum-pr-agent-repair-batch:v1",
    ),
    "continuum-render-executor.yml": (
        '"$QUALIFICATION_MARKER" "$BODY"',
        "Mandatory Render cleanup failed",
        "failure fingerprint repeated",
        '"$FAILURE_MARKER"',
        '"$HOLD_MARKER',
        "continuum-render-transient-attempt",
        "Render transient infrastructure failures repeated",
        "repository failure fingerprint repeated",
        "continuum-render-repair-attempt",
        "already open, so no duplicate repair",
        "Automatic recovery limit",
        "unrecognized engine classification",
        "without a matching recovery route",
        "Real Render smoke execution",
        "but no compatible ${E2E_BRANCH_PREFIX}",
        "is now in the CI/merge chain",
    ),
}

HEADER_BY_ROLE = {
    "continuum": "⚡ **Continuum · ",
    "agent-coder": "🦾 **Agent Coder · ",
    "project": "🛰️ **Project · ",
}

KNOWN_COMPONENTS = {
    "auto-merge",
    "coderabbit-retry",
    "coderabbit-unresolved",
    "delegation-dispatcher",
    "delegation-pr-review",
    "delegation-review",
    "delegation-worker",
    "issue-scheduler",
    "issue-worker",
    "opencode",
    "opencode-watchdog",
    "pr-agent",
    "pr-agent-auto-merge",
    "pr-agent-canary",
    "pr-agent-recovery",
    "pr-agent-repair",
    "pr-agent-repair-handoff",
    "pr-agent-review",
    "qualification",
    "render-executor",
}

SECRET_RES = (
    re.compile(r"ghp_[A-Za-z0-9]+"),
    re.compile(r"gho_[A-Za-z0-9_-]+"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"\brnd_[A-Za-z0-9]+\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"xox[abp]-"),
)


def read_workflow(name):
    with open(os.path.join(WORKFLOWS_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def mutation_positions(text):
    positions = []
    for pattern in MUTATION_RES:
        for match in pattern.finditer(text):
            # `gh issue close` only mutates conversation text when it posts
            # a closing comment.
            if match.group(0).startswith("gh issue close"):
                window = text[match.start(): match.start() + 400]
                if "--comment" not in window:
                    continue
            positions.append(match.start())
    return sorted(positions)


#: Comment bodies assembled by a shared builder instead of inline: the
#: mutation call references the builder, and the builder conformance suites
#: pin the builder output. Maps mutation-call reference -> (builder
#: definition anchor, file holding the definition).
BUILDER_REFS = {
    "conflictRepairMarker(headSha, attempt)": (
        "function conflictRepairMarker(headSha, attempt)",
        "continuum-auto-merge.yml",
    ),
    "policy.controllerStateBody": (
        "function controllerStateBody(stateMarker, summary)",
        None,  # definition lives in .github/scripts/pr_agent_policy.js
    ),
    "controllerStateBody(": (
        "function controllerStateBody(stateMarker, summary)",
        "continuum-pr-agent-recovery.yml",
    ),
    "printf -v BODY": (
        "printf -v BODY",
        "continuum-pr-agent-repair.yml",
    ),
    "markerComment": (
        "const markerComment = [",
        "continuum-issue-scheduler.yml",
    ),
    "body: markerComment": (
        "const markerComment = [",
        "continuum-issue-scheduler.yml",
    ),
}


#: Marker-only election/dedup bodies: they render no prose, so they carry
#: the hidden origin marker only and stay invisible. Window anchors that
#: identify such sites (the marker assertion still applies to them).
MARKER_ONLY_REFS = (
    "body: marker +",
    '"$FAILURE_MARKER" "<!-- continuum-origin',
)

#: Files whose bodies are assembled at runtime by the shared policy bundle
#: (headers are composed from the canonical prefix plus the marker-derived
#: component). Literal output for those sites is pinned by the node suites;
#: here the plumbing reference is asserted instead of a literal.
POLICY_BUNDLE_PLUMBING = (
    "attributionHeader('continuum', component)",
    "originMarker('continuum', component)",
    "controllerAttributionComponent(stateMarker)",
)


def builder_text(ref, own_file_text, own_name):
    anchor, definition_file = BUILDER_REFS[ref]
    if definition_file is None:
        with open(POLICY_JS, encoding="utf-8") as handle:
            source = handle.read()
    elif definition_file == own_name:
        source = own_file_text
    else:
        source = read_workflow(definition_file)
    index = source.find(anchor)
    assert index != -1, f"builder definition gone: {anchor}"
    return source[index: index + 2500]


def expected_header(role, component):
    if role == "project":
        # Project headers derive the repository display name from context;
        # the literal component never appears in the header.
        return HEADER_BY_ROLE[role]
    return f"{HEADER_BY_ROLE[role]}{component}**"


def expected_marker(role, component):
    return f"<!-- continuum-origin role={role} component={component} -->"


class ContractHeaderTests(unittest.TestCase):
    def test_visible_headers_identify_continuum_automation(self):
        self.assertEqual(
            attribution.attribution_header("continuum", "pr-agent-repair"),
            "⚡ **Continuum · pr-agent-repair**",
        )
        self.assertEqual(
            attribution.attribution_header("agent-coder", "issue-worker"),
            "🦾 **Agent Coder · issue-worker**",
        )
        self.assertEqual(
            attribution.attribution_header("project", repo="owner/Acme"),
            "🛰️ **Project · Acme**",
        )

    def test_headers_name_continuum_and_component(self):
        for role, component in (
            ("continuum", "auto-merge"),
            ("agent-coder", "delegation-worker"),
        ):
            header = attribution.attribution_header(role, component)
            self.assertIn("Continuum" if role == "continuum" else "Agent Coder", header)
            self.assertIn(component, header)

    def test_project_display_name_is_derived_never_hard_coded(self):
        self.assertEqual(attribution.require_repo_display("owner/Acme"), "Acme")
        self.assertEqual(attribution.require_repo_display("Acme"), "Acme")
        for bad in ("", "   ", "owner/", "/", "has space", "na#me", "a<b", "@x"):
            with self.subTest(repo=bad):
                with self.assertRaises(ValueError):
                    attribution.require_repo_display(bad)
        with self.assertRaises(ValueError):
            attribution.attribution_header("project")

    def test_unknown_role_rejected(self):
        with self.assertRaises(ValueError):
            attribution.attribution_header("bot", "pr-agent-repair")
        with self.assertRaises(ValueError):
            attribution.origin_marker("bot", "pr-agent-repair")

    def test_components_cannot_smuggle_repository_identity(self):
        for bad in ("owner/repo", "UPPER", "has space", "a_b", "", "trailing-", "-lead"):
            with self.subTest(component=bad):
                with self.assertRaises(ValueError):
                    attribution.require_component(bad)
        self.assertEqual(attribution.require_component("pr-agent-repair"), "pr-agent-repair")


class ContractMarkerTests(unittest.TestCase):
    def test_origin_marker_shape(self):
        self.assertEqual(
            attribution.origin_marker("continuum", "pr-agent-repair"),
            "<!-- continuum-origin role=continuum component=pr-agent-repair -->",
        )
        self.assertEqual(
            attribution.origin_marker("agent-coder", "issue-worker"),
            "<!-- continuum-origin role=agent-coder component=issue-worker -->",
        )
        self.assertEqual(
            attribution.origin_marker("project", "qualification"),
            "<!-- continuum-origin role=project component=qualification -->",
        )

    def test_origin_round_trip(self):
        for role, component in (
            ("continuum", "auto-merge"),
            ("agent-coder", "delegation-worker"),
            ("project", "qualification"),
        ):
            with self.subTest(role=role, component=component):
                marker = attribution.origin_marker(role, component)
                self.assertEqual(attribution.parse_origin(marker), (role, component))

    def test_marker_never_carries_secrets(self):
        for role, component in (
            ("continuum", "pr-agent-repair"),
            ("agent-coder", "delegation-worker"),
            ("project", "qualification"),
        ):
            marker = attribution.origin_marker(role, component)
            self.assertIsNone(attribution.contains_secret(marker))
            self.assertNotIn("/", marker)


class ContractInsertionTests(unittest.TestCase):
    def test_prose_body_gets_header_first(self):
        body = attribution.with_attribution(
            "Automation produced no code changes.", "continuum", "opencode"
        )
        lines = body.split("\n")
        self.assertEqual(lines[0], "⚡ **Continuum · opencode**")
        self.assertEqual(
            lines[1], "<!-- continuum-origin role=continuum component=opencode -->"
        )
        self.assertIn("Automation produced no code changes.", body)

    def test_command_token_stays_first(self):
        body = attribution.with_attribution(
            "/oc\n\n<!-- issue-scheduler-dispatch -->\nAutomatically dispatched.",
            "continuum",
            "issue-scheduler",
        )
        self.assertEqual(body.split("\n")[0], "/oc")
        self.assertIn("⚡ **Continuum · issue-scheduler**", body)
        self.assertIn(
            "<!-- continuum-origin role=continuum component=issue-scheduler -->",
            body,
        )
        self.assertEqual(commands.classify_issue_comment(body), "run")

    def test_review_command_still_recognized(self):
        body = attribution.with_attribution("/review", "continuum", "pr-agent-canary")
        self.assertTrue(pr_agent.is_review_command(body))

    def test_verify_command_stays_on_its_own_line(self):
        body = attribution.with_attribution(
            "/verify abc123", "continuum", "pr-agent-canary"
        )
        self.assertEqual(pr_agent.parse_verify_command(body), "abc123")

    def test_insertion_is_idempotent(self):
        once = attribution.with_attribution(
            "Some prose.", "continuum", "issue-scheduler"
        )
        twice = attribution.with_attribution(once, "continuum", "issue-scheduler")
        self.assertEqual(once, twice)
        self.assertEqual(once.count("continuum-origin"), 1)
        self.assertEqual(once.count("⚡ **Continuum · issue-scheduler**"), 1)

    def test_ensure_attribution_preserves_existing_body(self):
        legacy = (
            "<!-- continuum-pr-agent-controller-state:v1 -->\n"
            "<!-- continuum-pr-agent-retry head=abc kind=review attempt=1 -->\n"
            "<details><summary>state</summary></details>"
        )
        repaired = attribution.ensure_attribution(
            legacy, "continuum", "pr-agent-review"
        )
        self.assertIn("<!-- continuum-pr-agent-controller-state:v1 -->", repaired)
        self.assertIn("continuum-pr-agent-retry head=abc", repaired)
        self.assertIn("⚡ **Continuum · pr-agent-review**", repaired)
        self.assertIn(
            "<!-- continuum-origin role=continuum component=pr-agent-review -->",
            repaired,
        )
        self.assertEqual(attribution.ensure_attribution(repaired, "continuum", "pr-agent-review"), repaired)


class PolicyBundleAttributionTests(unittest.TestCase):
    @staticmethod
    def node():
        node = shutil.which("node")
        assert node is not None, (
            "node is required to execute the shipped JS; "
            "failing closed instead of silently skipping"
        )
        return node

    def run_policy(self, script):
        with open(POLICY_JS, encoding="utf-8") as handle:
            bundle = handle.read()
        tmpdir = os.path.join(ROOT, ".opencode-tmp")
        os.makedirs(tmpdir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, dir=tmpdir
        ) as handle:
            handle.write(bundle + "\n" + script)
            path = handle.name
        try:
            completed = subprocess.run(
                [self.node(), path], capture_output=True, text=True, timeout=30
            )
        finally:
            os.unlink(path)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return completed.stdout

    def test_bundle_exports_the_attribution_contract(self):
        out = self.run_policy(
            "const assert = require('assert');\n"
            "const p = require(" + json.dumps(POLICY_JS) + ");\n"
            "for (const key of ['attributionHeader', 'originMarker', "
            "'controllerAttributionComponent', 'COMMENT_ATTRIBUTION_CONTINUUM_PREFIX', "
            "'COMMENT_ATTRIBUTION_AGENT_CODER_PREFIX', "
            "'COMMENT_ATTRIBUTION_PROJECT_PREFIX']) {\n"
            "  assert.ok(p[key] !== undefined, 'missing export: ' + key);\n"
            "}\n"
            "console.log('EXPORTS_OK');\n"
        )
        self.assertIn("EXPORTS_OK", out)

    def test_bundle_matches_python_contract_byte_for_byte(self):
        script = (
            "const assert = require('assert');\n"
            "const p = require(" + json.dumps(POLICY_JS) + ");\n"
            f"assert.strictEqual(p.attributionHeader('continuum', 'pr-agent-repair'), {json.dumps(attribution.attribution_header('continuum', 'pr-agent-repair'))});\n"
            f"assert.strictEqual(p.attributionHeader('agent-coder', 'issue-worker'), {json.dumps(attribution.attribution_header('agent-coder', 'issue-worker'))});\n"
            f"assert.strictEqual(p.originMarker('continuum', 'pr-agent-repair'), {json.dumps(attribution.origin_marker('continuum', 'pr-agent-repair'))});\n"
            f"assert.strictEqual(p.originMarker('agent-coder', 'delegation-worker'), {json.dumps(attribution.origin_marker('agent-coder', 'delegation-worker'))});\n"
            f"assert.strictEqual(p.originMarker('project', 'qualification'), {json.dumps(attribution.origin_marker('project', 'qualification'))});\n"
            "console.log('PARITY_OK');\n"
        )
        self.assertIn("PARITY_OK", self.run_policy(script))

    def test_controller_body_attributes_review_and_repair(self):
        head = "a" * 40
        fingerprint = "b" * 64
        script = (
            "const assert = require('assert');\n"
            "const p = require(" + json.dumps(POLICY_JS) + ");\n"
            f"const review = p.controllerStateBody('<!-- continuum-pr-agent-retry head={head} kind=review attempt=1 -->', 'summary');\n"
            f"const repair = p.controllerStateBody('<!-- continuum-pr-agent-retry head={head} kind=repair attempt=1 -->', 'summary');\n"
            f"const held = p.controllerStateBody('<!-- continuum-pr-agent-no-progress head={head} fingerprint={fingerprint} -->', 'summary');\n"
            "assert.ok(review.includes('⚡ **Continuum · pr-agent-review**'));\n"
            "assert.ok(review.includes('<!-- continuum-origin role=continuum component=pr-agent-review -->'));\n"
            "assert.ok(repair.includes('⚡ **Continuum · pr-agent-repair**'));\n"
            "assert.ok(repair.includes('<!-- continuum-origin role=continuum component=pr-agent-repair -->'));\n"
            "assert.ok(held.includes('⚡ **Continuum · pr-agent-repair**'));\n"
            "assert.ok(held.includes('<!-- continuum-origin role=continuum component=pr-agent-repair -->'));\n"
            "// Legacy marker lines stay first: positional upsert matching is unchanged.\n"
            "for (const body of [review, repair, held]) {\n"
            "  const lines = body.split('\\n');\n"
            "  assert.strictEqual(lines[0], p.CONTROLLER_STATE_MARKER);\n"
            "  assert.ok(lines[2].startsWith('⚡ **Continuum · pr-agent-'));\n"
            "  assert.ok(lines[3].startsWith('<!-- continuum-origin '));\n"
            "}\n"
            "console.log('CONTROLLER_OK');\n"
        )
        self.assertIn("CONTROLLER_OK", self.run_policy(script))

    def test_controller_upsert_preserves_attribution(self):
        head = "c" * 40
        script = (
            "const assert = require('assert');\n"
            "const p = require(" + json.dumps(POLICY_JS) + ");\n"
            f"const marker = '<!-- continuum-pr-agent-retry head={head} kind=review attempt=1 -->';\n"
            "const created = p.controllerStateBody(marker, 'first');\n"
            "const updated = p.controllerStateBody(marker, 'second');\n"
            "for (const body of [created, updated]) {\n"
            "  assert.ok(body.includes(marker), 'retry identity must survive');\n"
            "  assert.ok(body.includes('⚡ **Continuum · pr-agent-review**'));\n"
            "  assert.ok(body.includes('<!-- continuum-origin role=continuum component=pr-agent-review -->'));\n"
            "}\n"
            "console.log('UPSERT_OK');\n"
        )
        self.assertIn("UPSERT_OK", self.run_policy(script))


class RecoveryInlineAttributionTests(unittest.TestCase):
    def test_recovery_controller_body_carries_attribution(self):
        body = read_workflow("continuum-pr-agent-recovery.yml")
        start = body.index("function controllerStateBody(stateMarker, summary)")
        brace = body.index("{", start)
        depth = 0
        end = None
        for pos in range(brace, len(body)):
            if body[pos] == "{":
                depth += 1
            elif body[pos] == "}":
                depth -= 1
                if depth == 0:
                    end = pos + 1
                    break
        self.assertIsNotNone(end, "could not extract controllerStateBody")
        fn_source = body[start:end]
        node = shutil.which("node")
        self.assertIsNotNone(
            node,
            "node is required to execute the shipped JS; failing closed instead of silently skipping",
        )
        head = "d" * 40
        harness = (
            "const assert = require('assert');\n"
            "const CONTROLLER_STATE_MARKER = '<!-- continuum-pr-agent-controller-state:v1 -->';\n"
            + fn_source
            + "\n"
            f"const marker = '<!-- continuum-pr-agent-retry head={head} kind=recovery attempt=1 -->';\n"
            "const created = controllerStateBody(marker, 'first');\n"
            "const updated = controllerStateBody(marker, 'second');\n"
            "for (const text of [created, updated]) {\n"
            "  assert.ok(text.includes(marker), 'retry identity must survive');\n"
            "  assert.ok(text.includes('⚡ **Continuum · pr-agent-recovery**'));\n"
            "  assert.ok(text.includes('<!-- continuum-origin role=continuum component=pr-agent-recovery -->'));\n"
            "  const lines = text.split('\\n');\n"
            "  assert.strictEqual(lines[0], CONTROLLER_STATE_MARKER);\n"
            "  assert.strictEqual(lines[1], marker);\n"
            "}\n"
            "console.log('RECOVERY_OK');\n"
        )
        tmpdir = os.path.join(ROOT, ".opencode-tmp")
        os.makedirs(tmpdir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, dir=tmpdir
        ) as handle:
            handle.write(harness)
            path = handle.name
        try:
            completed = subprocess.run(
                [node, path], capture_output=True, text=True, timeout=30
            )
        finally:
            os.unlink(path)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("RECOVERY_OK", completed.stdout)


class WorkflowAttributionTests(unittest.TestCase):
    def test_every_mutation_file_is_classified(self):
        files = sorted(
            name
            for name in os.listdir(WORKFLOWS_DIR)
            if name.startswith("continuum-") and name.endswith(".yml")
        )
        for name in files:
            with self.subTest(workflow=name):
                text = read_workflow(name)
                positions = mutation_positions(text)
                if positions:
                    self.assertIn(
                        name,
                        MUTATION_COUNTS,
                        f"{name} writes comments but is not classified; "
                        "add its (role, component) to EXPECTED_IDENTITIES, "
                        "its anchors to FUNCTIONAL_ANCHORS, and its count "
                        "to MUTATION_COUNTS",
                    )
        for name in MUTATION_COUNTS:
            with self.subTest(workflow=name):
                self.assertIn(name, EXPECTED_IDENTITIES)
                self.assertIn(name, FUNCTIONAL_ANCHORS)

    def test_no_extra_comment_is_created_for_attribution(self):
        for name, pinned in sorted(MUTATION_COUNTS.items()):
            with self.subTest(workflow=name):
                text = read_workflow(name)
                actual = len(mutation_positions(text))
                self.assertEqual(
                    actual,
                    pinned,
                    f"{name}: {actual} comment-mutation calls, expected {pinned}; "
                    "a new call means a new comment path that must be classified, "
                    "and attribution must never add a call",
                )

    def test_every_mutation_site_uses_the_contract(self):
        for name, identities in sorted(EXPECTED_IDENTITIES.items()):
            text = read_workflow(name)
            positions = mutation_positions(text)
            self.assertTrue(positions, f"{name} has no mutation calls")
            headers = [expected_header(role, comp) for role, comp in identities]
            markers = [expected_marker(role, comp) for role, comp in identities]
            for pos in positions:
                with self.subTest(workflow=name, offset=pos):
                    window = text[max(0, pos - 4500): pos + 1500]
                    if "policy.controllerStateBody" in window:
                        # Assembled at runtime by the shared bundle from the
                        # canonical prefix plus the marker-derived component;
                        # the node suites pin the literal output. Here the
                        # plumbing reference must survive (no silent bypass).
                        policy = open(POLICY_JS, encoding="utf-8").read()
                        for plumbing in POLICY_BUNDLE_PLUMBING:
                            self.assertIn(plumbing, policy)
                        continue
                    resolved = window
                    for ref in sorted(BUILDER_REFS, key=len, reverse=True):
                        if ref in window:
                            resolved += "\n" + builder_text(ref, text, name)
                    marker_only = any(ref in window for ref in MARKER_ONLY_REFS)
                    self.assertTrue(
                        any(marker in resolved for marker in markers),
                        f"{name}@{pos}: no origin marker near the mutation call",
                    )
                    if not marker_only:
                        self.assertTrue(
                            any(header in resolved for header in headers),
                            f"{name}@{pos}: no attributed header near the mutation call",
                        )

    def test_controller_builders_are_attributed(self):
        policy = open(POLICY_JS, encoding="utf-8").read()
        # The bundle assembles headers from the canonical prefix plus the
        # marker-derived component (runtime output is pinned byte-for-byte
        # by the node suites above); the source must carry the contract.
        self.assertIn("COMMENT_ATTRIBUTION_CONTINUUM_PREFIX", policy)
        self.assertIn("⚡ **Continuum · ", policy)
        self.assertIn("controllerAttributionComponent", policy)
        self.assertIn("pr-agent-' + kindMatch[1]", policy)
        self.assertIn("originMarker('continuum', component)", policy)
        recovery = read_workflow("continuum-pr-agent-recovery.yml")
        self.assertIn(
            expected_header("continuum", "pr-agent-recovery"), recovery
        )
        self.assertIn(
            expected_marker("continuum", "pr-agent-recovery"), recovery
        )
        repair = read_workflow("continuum-pr-agent-repair.yml")
        self.assertIn(expected_header("continuum", "pr-agent-repair"), repair)
        self.assertIn(expected_marker("continuum", "pr-agent-repair"), repair)
        agent = read_workflow("continuum-pr-agent.yml")
        self.assertIn("policy.controllerStateBody", agent)

    def test_functional_bodies_survive_attribution(self):
        for name, anchors in sorted(FUNCTIONAL_ANCHORS.items()):
            text = read_workflow(name)
            positions = mutation_positions(text)
            for anchor in anchors:
                with self.subTest(workflow=name, anchor=anchor[:50]):
                    occurrences = [
                        match.start() for match in re.finditer(re.escape(anchor), text)
                    ]
                    self.assertTrue(
                        occurrences, f"{name}: functional body {anchor!r} is gone"
                    )
                    self.assertTrue(
                        any(
                            abs(index - pos) < 4500
                            for index in occurrences
                            for pos in positions
                        ),
                        f"{name}: {anchor!r} is no longer attached to a mutation call",
                    )

    def test_every_marker_is_valid_and_secret_free(self):
        origin_re = re.compile(
            r"<!--\s*continuum-origin\s+role=([^\s]+)\s+component=([^\s]+?)\s*-->"
        )
        with open(POLICY_JS, encoding="utf-8") as handle:
            policy = handle.read()
        for name in sorted(EXPECTED_IDENTITIES):
            text = read_workflow(name)
            found = origin_re.findall(text)
            if not found:
                # Bodies assembled at runtime by the shared policy bundle
                # compose the marker from the canonical helper; the node
                # suites pin the literal output.
                self.assertIn(
                    "policy.controllerStateBody",
                    text,
                    f"{name}: no literal origin marker and no bundle reference",
                )
                self.assertIn("originMarker('continuum', component)", policy)
                self.assertIn("controllerAttributionComponent(stateMarker)", policy)
            for role, component in found:
                with self.subTest(workflow=name, role=role, component=component):
                    self.assertIn(role, ("continuum", "agent-coder", "project"))
                    self.assertIn(component, KNOWN_COMPONENTS)
                    self.assertNotIn("/", component)
                    self.assertEqual(component, component.lower())
                    self.assertIn(
                        (role, component),
                        EXPECTED_IDENTITIES[name],
                        f"{name}: unexpected identity {(role, component)}",
                    )
            for pattern in SECRET_RES:
                self.assertIsNone(
                    pattern.search(text),
                    f"{name}: secret-shaped token in a workflow",
                )

    def test_every_visible_header_uses_the_taxonomy(self):
        header_re = re.compile(r"(⚡ \*\*Continuum · |🦾 \*\*Agent Coder · |🛰️ \*\*Project · )(.*?)(\*\*)")
        for name in sorted(EXPECTED_IDENTITIES):
            text = read_workflow(name)
            for match in header_re.finditer(text):
                prefix, subject, _ = match.groups()
                with self.subTest(workflow=name, header=match.group(0)[:60]):
                    if prefix.startswith("🛰"):
                        # The repository display name is derived from context,
                        # never a hard-coded consumer name.
                        self.assertTrue(
                            "$" in subject or "{{" in subject or "repo" in subject.lower(),
                            f"{name}: project header must derive the repo name: {subject!r}",
                        )
                        if "${" not in subject and "{{" not in subject:
                            self.assertNotIn("/", subject)
                    else:
                        self.assertIn(subject, KNOWN_COMPONENTS)

    def test_command_comments_keep_the_command_first(self):
        scheduler = read_workflow("continuum-issue-scheduler.yml")
        self.assertIn("body: ['/oc', '', marker, dispatchMarker,", scheduler)
        watchdog = read_workflow("continuum-opencode-watchdog.yml")
        retry_body = watchdog[watchdog.index("'/oc',"):]
        first_lines = [
            line.strip().strip("',")
            for line in retry_body.split("\n")[:8]
            if line.strip().strip("',")
        ]
        self.assertEqual(first_lines[0], "/oc")
        for body_text, kind in (
            (
                "/oc\n\n<!-- issue-scheduler-dispatch -->\n"
                "⚡ **Continuum · issue-scheduler**\n"
                "<!-- continuum-origin role=continuum component=issue-scheduler -->\n"
                "Automatically dispatched.",
                "run",
            ),
        ):
            self.assertEqual(commands.classify_issue_comment(body_text), kind)

    def test_run_provenance_references_survive(self):
        recovery = read_workflow("continuum-pr-agent-recovery.yml")
        self.assertIn("Controller dispatch run: ", recovery)
        self.assertIn("String(context.runId)", recovery)
        watchdog = read_workflow("continuum-opencode-watchdog.yml")
        self.assertIn("run.html_url", watchdog)
        render = read_workflow("continuum-render-executor.yml")
        self.assertIn("GITHUB_RUN_ID", render)

    def test_out_of_scope_bodies_stay_self_identifying(self):
        # PR/issue descriptions are not comments, so they keep their existing
        # automation wording instead of this contract; pin that wording so a
        # silent regression to owner-like prose is caught.
        worker = read_workflow("continuum-consumer-child-worker.yml")
        self.assertIn("Delegated implementation for #", worker)
        self.assertIn("executed by the configured Continuum parent", worker)
        opencode = read_workflow("continuum-opencode.yml")
        self.assertIn("Automated implementation for #", opencode)


class RenderedAttributionTests(unittest.TestCase):
    """Two producers, rendered as GitHub would, must read as automation."""

    def test_controller_comment_reads_as_automation(self):
        body = (
            "<!-- continuum-pr-agent-controller-state:v1 -->\n"
            "<!-- continuum-pr-agent-retry head="
            + "e" * 40
            + " kind=review attempt=1 -->\n"
            "⚡ **Continuum · pr-agent-review**\n"
            "<!-- continuum-origin role=continuum component=pr-agent-review -->\n"
            "<details>\n"
            "<summary>Continuum PR-Agent controller state</summary>\n\n"
            "Transient review failure; bounded retry 1/3 is scheduled.\n\n"
            "</details>"
        )
        rendered = "\n".join(
            line for line in body.split("\n") if not line.strip().startswith("<!--")
        )
        first = rendered.strip().split("\n")[0]
        self.assertIn("Continuum", first)
        self.assertIn("pr-agent-review", first)

    def test_agent_coder_comment_reads_as_automation(self):
        body = attribution.with_attribution(
            "Child task #12 produced an accepted implementation PR: https://example.invalid/pr/1",
            "agent-coder",
            "delegation-worker",
        )
        self.assertTrue(body.startswith("🦾 **Agent Coder · delegation-worker**"))
        self.assertIn(
            "<!-- continuum-origin role=agent-coder component=delegation-worker -->",
            body,
        )


if __name__ == "__main__":
    unittest.main()
