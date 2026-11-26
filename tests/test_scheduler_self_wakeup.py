"""Regression tests for #394 and #470.

Both scheduler entrypoints are evaluated from their real checked-in YAML: the
consumer caller stub's ``jobs.call.if`` and the self-dogfood ``automation.yml``
``jobs.scheduler.if``. The small expression evaluator below understands only
the GitHub Actions expression subset those conditions actually use, so the
tests never mirror a hand-written approximation of the decision.

The ``AutomationAutoMergeOnlyRoutingTests`` class applies the same evaluator to
every ``automation.yml`` job so the narrow external auto-merge wake is proven
from the real conditions rather than a detached model.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from continuum import opencode_commands
from continuum.periodic_wake import periodic_dedupe_decision

ROOT = Path(__file__).resolve().parents[1]
CONSUMER_SCHEDULER = ROOT / ".github" / "caller-stubs" / "continuum-issue-scheduler.yml"
SELF_SCHEDULER = ROOT / ".github" / "workflows" / "automation.yml"
CONSUMER_OPENCODE = ROOT / ".github" / "caller-stubs" / "continuum-opencode.yml"
SELF_OPENCODE = ROOT / ".github" / "workflows" / "opencode.yml"
INSTALL = ROOT / "install.sh"
TMP_PARENT = ROOT / ".opencode-tmp"

OWNER = "octo-owner"
OTHER = "someone-else"
DISPATCH_MARKER = "<!-- issue-scheduler-dispatch -->"
WATCHDOG_MARKER = "<!-- continuum-origin role=continuum component=opencode-watchdog -->"

DISPATCH_COMMENT = "/oc\n\n<!-- issue-scheduler-dispatch -->\nAutomatically dispatched."
WATCHDOG_COMMENT = (
    "/oc\n\n<!-- continuum-origin role=continuum component=opencode-watchdog -->\n"
    "Automatic recovery retry 1/1."
)

# The exact pre-#394 scheduler conditions. These are only the "before" side of
# the deterministic replay; every admission decision below is evaluated from
# the real YAML in the working tree.
OLD_CONSUMER_IF = """\
vars.CONTINUUM_ROLE != 'child' && (github.event_name != 'issue_comment' ||
 (github.actor == github.repository_owner &&
  (startsWith(github.event.comment.body, '/oc') ||
   startsWith(github.event.comment.body, '/opencode'))) ||
 ((github.actor == github.repository_owner ||
   github.actor == 'github-actions[bot]' ||
   github.event.comment.author_association == 'OWNER' ||
   github.event.comment.author_association == 'MEMBER' ||
   github.event.comment.author_association == 'COLLABORATOR') &&
  (contains(github.event.comment.body, 'continuum-qualification-result') ||
   contains(github.event.comment.body, 'continuum-docker-qualification-result') ||
   contains(github.event.comment.body, 'continuum-render-qualification-result'))))
"""

OLD_AUTOMATION_IF = """\
(github.event_name == 'schedule' && github.event.schedule == '*/15 * * * *') || github.event_name == 'workflow_dispatch' || github.event_name == 'issues' || (github.event_name == 'issue_comment' &&
 github.actor == github.repository_owner &&
 (startsWith(github.event.comment.body, '/oc') ||
  startsWith(github.event.comment.body, '/opencode'))) ||
(github.event_name == 'pull_request_target' && github.event.action == 'closed') || (github.event_name == 'workflow_run' && github.event.workflow_run.path == '.github/workflows/opencode.yml')
"""


class ExpressionError(ValueError):
    pass


class GitHubExpression:
    """Evaluate the subset of GitHub Actions expressions used by job ``if``."""

    _TOKEN = re.compile(
        r"\s*(?:"
        r"(?P<string>'(?:[^']|'')*')|"
        r"(?P<number>-?\d+(?:\.\d+)?)|"
        r"(?P<op>&&|\|\||==|!=|\(|\)|,|!)|"
        r"(?P<ident>[A-Za-z_][A-Za-z0-9_]*)|"
        r"(?P<dot>\.)|"
        r"(?P<bad>\S)"
        r")"
    )

    def __init__(self, text, context):
        self.context = context
        self.tokens = self._tokenize(text)
        self.index = 0

    @classmethod
    def _tokenize(cls, text):
        tokens = []
        position = 0
        while position < len(text):
            match = cls._TOKEN.match(text, position)
            if match is None:
                if text[position:].strip() == "":
                    break
                raise ExpressionError(
                    "cannot tokenize expression at {!r}".format(text[position:])
                )
            position = match.end()
            kind = match.lastgroup
            if kind == "bad":
                raise ExpressionError(
                    "unsupported token {!r}".format(match.group("bad"))
                )
            if kind == "string":
                value = match.group("string")[1:-1].replace("''", "'")
                tokens.append(("string", value))
            elif kind == "number":
                raw = match.group("number")
                tokens.append(("number", float(raw) if "." in raw else int(raw)))
            else:
                tokens.append((kind, match.group(kind)))
        return tokens

    def evaluate(self):
        value = self._parse_or()
        if self._peek() is not None:
            raise ExpressionError("unexpected token {!r}".format(self._peek()))
        return value

    def _peek(self):
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def _accept(self, kind, value=None):
        token = self._peek()
        if token is None:
            return None
        if token[0] != kind:
            return None
        if value is not None and token[1] != value:
            return None
        self.index += 1
        return token

    def _expect(self, kind, value=None):
        token = self._accept(kind, value)
        if token is None:
            raise ExpressionError(
                "expected {} {!r}, got {!r}".format(kind, value, self._peek())
            )
        return token

    def _parse_or(self):
        value = self._parse_and()
        while self._accept("op", "||"):
            right = self._parse_and()
            value = value if self._truthy(value) else right
        return value

    def _parse_and(self):
        value = self._parse_equality()
        while self._accept("op", "&&"):
            right = self._parse_equality()
            value = right if self._truthy(value) else value
        return value

    def _parse_equality(self):
        value = self._parse_unary()
        while True:
            if self._accept("op", "=="):
                right = self._parse_unary()
                value = self._equals(value, right)
            elif self._accept("op", "!="):
                right = self._parse_unary()
                value = not self._equals(value, right)
            else:
                return value

    def _parse_unary(self):
        if self._accept("op", "!"):
            return not self._truthy(self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self):
        token = self._peek()
        if token is None:
            raise ExpressionError("unexpected end of expression")
        if token[0] == "op" and token[1] == "(":
            self.index += 1
            value = self._parse_or()
            self._expect("op", ")")
            return value
        if token[0] == "string":
            self.index += 1
            return token[1]
        if token[0] == "number":
            self.index += 1
            return token[1]
        if token[0] == "ident":
            self.index += 1
            name = token[1]
            if name == "true":
                return True
            if name == "false":
                return False
            if name in ("null", "nil"):
                return None
            if self._accept("op", "("):
                args = []
                if not self._accept("op", ")"):
                    while True:
                        args.append(self._parse_or())
                        if self._accept("op", ")"):
                            break
                        self._expect("op", ",")
                return self._call(name, args)
            value = self._resolve(name)
            while self._accept("dot"):
                key = self._expect("ident")[1]
                value = value.get(key) if isinstance(value, dict) else None
            return value
        raise ExpressionError("unexpected token {!r}".format(token))

    def _resolve(self, name):
        if name in self.context:
            return self.context[name]
        return None

    @staticmethod
    def _stringify(value):
        if value is None:
            return ""
        if value is True:
            return "true"
        if value is False:
            return "false"
        return str(value)

    @staticmethod
    def _call(name, args):
        if name in ("startsWith", "contains") and len(args) == 2:
            haystack = "" if args[0] is None else str(args[0]).casefold()
            needle = "" if args[1] is None else str(args[1]).casefold()
            if name == "startsWith":
                return haystack.startswith(needle)
            return needle in haystack
        if name == "format" and args:
            template = "" if args[0] is None else str(args[0])

            def replace(match):
                index = int(match.group(1)) + 1
                value = args[index] if index < len(args) else None
                return GitHubExpression._stringify(value)

            return re.sub(r"\{(\d+)\}", replace, template)
        if name == "fromJSON" and len(args) == 1:
            try:
                return json.loads(args[0])
            except (TypeError, ValueError) as error:
                raise ExpressionError(
                    "fromJSON argument is not valid JSON: {!r}".format(args[0])
                ) from error
        raise ExpressionError("unsupported function {!r}".format(name))

    @staticmethod
    def _truthy(value):
        if value is None or value is False:
            return False
        if isinstance(value, (int, float)) and value == 0:
            return False
        if isinstance(value, str) and value == "":
            return False
        return True

    @staticmethod
    def _equals(left, right):
        if isinstance(left, str) and isinstance(right, str):
            return left.casefold() == right.casefold()
        return left == right


def load_job_if(path, job):
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parsed["jobs"][job]["if"]


def event_context(
    event_name,
    *,
    actor=OWNER,
    role="parent",
    comment=None,
    action=None,
    schedule=None,
    workflow_run=None,
    inputs=None,
    label=None,
    issue_state=None,
    issue_pull_request=None,
    pause_label=None,
):
    return {
        "github": {
            "event_name": event_name,
            "actor": actor,
            "repository_owner": OWNER,
            "event": {
                # `run-name:` reads the raw dispatch payload; job conditions
                # read the typed top-level `inputs` context.
                "inputs": inputs or {},
                "comment": comment or {},
                "action": action,
                "schedule": schedule,
                "workflow_run": workflow_run or {},
                "issue": {
                    "number": 439,
                    "state": issue_state,
                    "pull_request": issue_pull_request,
                },
                "label": {"name": label},
            },
        },
        "vars": {"CONTINUUM_ROLE": role, "AUTOMATION_PAUSE_LABEL": pause_label},
        "inputs": inputs or {},
    }


def render_run_name(template, event_name, **context):
    """Interpolate the checked-in ``run-name:`` template for an event."""

    def evaluate(match):
        value = GitHubExpression(
            match.group(1), event_context(event_name, **context)
        ).evaluate()
        return GitHubExpression._stringify(value)

    return re.sub(r"\$\{\{(.*?)\}\}", evaluate, template, flags=re.S)


def comment(body, author=OWNER, association="OWNER"):
    return {
        "body": body,
        "author_association": association,
        "user": {"login": author},
    }


def admitted(condition, context):
    return bool(GitHubExpression(condition, context).evaluate())


class ActualSchedulerConditionTests(unittest.TestCase):
    def setUp(self):
        self.consumer = load_job_if(CONSUMER_SCHEDULER, "call")
        self.automation = load_job_if(SELF_SCHEDULER, "scheduler")

    def test_consumer_caller_skips_both_machine_origin_commands(self):
        for body in (DISPATCH_COMMENT, WATCHDOG_COMMENT):
            with self.subTest(body=body):
                context = event_context("issue_comment", comment=comment(body))
                self.assertFalse(
                    admitted(self.consumer, context),
                    "the consumer scheduler job must skip a machine-origin command",
                )

    def test_self_dogfood_scheduler_skips_both_machine_origin_commands(self):
        for body in (DISPATCH_COMMENT, WATCHDOG_COMMENT):
            with self.subTest(body=body):
                context = event_context("issue_comment", comment=comment(body))
                self.assertFalse(
                    admitted(self.automation, context),
                    "automation.yml must skip a machine-origin command on its own",
                )

    def test_consumer_event_regression_matrix(self):
        cases = [
            ("manual /oc", event_context("issue_comment", comment=comment("/oc")), True),
            (
                "manual /opencode",
                event_context("issue_comment", comment=comment("/opencode")),
                True,
            ),
            (
                "manual /oc with a generic continuum-origin marker",
                event_context(
                    "issue_comment",
                    comment=comment(
                        "/oc\n\n<!-- continuum-origin role=continuum -->"
                    ),
                ),
                True,
            ),
            (
                "unrelated owner comment",
                event_context(
                    "issue_comment", comment=comment("Thanks, that fixed it.")
                ),
                False,
            ),
            (
                "scheduler dispatch",
                event_context("issue_comment", comment=comment(DISPATCH_COMMENT)),
                False,
            ),
            (
                "watchdog recovery",
                event_context("issue_comment", comment=comment(WATCHDOG_COMMENT)),
                False,
            ),
            (
                "dispatch marker on /opencode",
                event_context(
                    "issue_comment",
                    comment=comment("/opencode\n\n" + DISPATCH_MARKER),
                ),
                False,
            ),
            (
                "manual /oc after a leading blank line",
                event_context("issue_comment", comment=comment("\n/oc")),
                True,
            ),
            (
                "manual /oc with two leading spaces",
                event_context("issue_comment", comment=comment("  /oc")),
                True,
            ),
            (
                "manual /oc followed by a tab",
                event_context("issue_comment", comment=comment("/oc\t")),
                True,
            ),
            (
                "manual /oc followed by CRLF",
                event_context("issue_comment", comment=comment("/oc\r\n")),
                True,
            ),
            (
                "lookalike command /ocfoo",
                event_context(
                    "issue_comment", comment=comment("/ocfoo please")
                ),
                False,
            ),
            (
                "lookalike command /opencodefoo",
                event_context(
                    "issue_comment", comment=comment("/opencodefoo")
                ),
                False,
            ),
            (
                "non-owner forged /oc with dispatch marker",
                event_context(
                    "issue_comment",
                    comment=comment(DISPATCH_COMMENT, author=OTHER, association="NONE"),
                    actor=OTHER,
                ),
                False,
            ),
            (
                "non-owner /oc without marker",
                event_context(
                    "issue_comment",
                    comment=comment("/oc", author=OTHER, association="NONE"),
                    actor=OTHER,
                ),
                False,
            ),
            (
                "marker without a command",
                event_context(
                    "issue_comment",
                    comment=comment("No command here.\n\n" + DISPATCH_MARKER),
                ),
                False,
            ),
            (
                "bot qualification result",
                event_context(
                    "issue_comment",
                    comment=comment(
                        "continuum-qualification-result: passed",
                        author="github-actions[bot]",
                        association="NONE",
                    ),
                    actor="github-actions[bot]",
                ),
                True,
            ),
            (
                "member qualification result",
                event_context(
                    "issue_comment",
                    comment=comment(
                        "continuum-docker-qualification-result: passed",
                        author=OTHER,
                        association="MEMBER",
                    ),
                    actor=OTHER,
                ),
                True,
            ),
            (
                "untrusted qualification result",
                event_context(
                    "issue_comment",
                    comment=comment(
                        "continuum-qualification-result: passed",
                        author=OTHER,
                        association="NONE",
                    ),
                    actor=OTHER,
                ),
                False,
            ),
            (
                "issues opened",
                event_context("issues", action="opened"),
                True,
            ),
            (
                "issues labeled by the PAT actor",
                event_context(
                    "issues", action="labeled", actor="github-actions[bot]"
                ),
                True,
            ),
            ("pull_request_target", event_context("pull_request_target"), True),
            ("push", event_context("push"), True),
            ("workflow_run", event_context("workflow_run"), True),
            ("schedule", event_context("schedule"), True),
            ("workflow_dispatch", event_context("workflow_dispatch"), True),
            (
                "child manual /oc",
                event_context(
                    "issue_comment", role="child", comment=comment("/oc")
                ),
                False,
            ),
            (
                "child issues labeled",
                event_context("issues", role="child", action="labeled"),
                False,
            ),
            ("child push", event_context("push", role="child"), False),
        ]
        for label, context, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(expected, admitted(self.consumer, context), label)

    def test_self_dogfood_event_regression_matrix(self):
        cases = [
            (
                "primary schedule",
                event_context("schedule", schedule="*/15 * * * *"),
                True,
            ),
            (
                "secondary schedule",
                event_context("schedule", schedule="17,47 * * * *"),
                False,
            ),
            ("workflow_dispatch", event_context("workflow_dispatch"), True),
            ("issues opened", event_context("issues", action="opened"), True),
            (
                "issues labeled by the PAT actor",
                event_context(
                    "issues", action="labeled", actor="github-actions[bot]"
                ),
                True,
            ),
            ("manual /oc", event_context("issue_comment", comment=comment("/oc")), True),
            (
                "manual /opencode",
                event_context("issue_comment", comment=comment("/opencode")),
                True,
            ),
            (
                "manual /oc with a generic continuum-origin marker",
                event_context(
                    "issue_comment",
                    comment=comment(
                        "/oc\n\n<!-- continuum-origin role=continuum -->"
                    ),
                ),
                True,
            ),
            (
                "unrelated owner comment",
                event_context(
                    "issue_comment", comment=comment("Thanks, that fixed it.")
                ),
                False,
            ),
            (
                "scheduler dispatch",
                event_context("issue_comment", comment=comment(DISPATCH_COMMENT)),
                False,
            ),
            (
                "watchdog recovery",
                event_context("issue_comment", comment=comment(WATCHDOG_COMMENT)),
                False,
            ),
            (
                "manual /oc after a leading blank line",
                event_context("issue_comment", comment=comment("\n/oc")),
                True,
            ),
            (
                "manual /oc with two leading spaces",
                event_context("issue_comment", comment=comment("  /oc")),
                True,
            ),
            (
                "manual /oc followed by a tab",
                event_context("issue_comment", comment=comment("/oc\t")),
                True,
            ),
            (
                "manual /oc followed by CRLF",
                event_context("issue_comment", comment=comment("/oc\r\n")),
                True,
            ),
            (
                "lookalike command /ocfoo",
                event_context(
                    "issue_comment", comment=comment("/ocfoo please")
                ),
                False,
            ),
            (
                "lookalike command /opencodefoo",
                event_context(
                    "issue_comment", comment=comment("/opencodefoo")
                ),
                False,
            ),
            (
                "non-owner forged /oc with dispatch marker",
                event_context(
                    "issue_comment",
                    comment=comment(DISPATCH_COMMENT, author=OTHER, association="NONE"),
                    actor=OTHER,
                ),
                False,
            ),
            (
                "pull_request_target closed",
                event_context(
                    "pull_request_target", action="closed"
                ),
                True,
            ),
            (
                "pull_request_target opened",
                event_context(
                    "pull_request_target", action="opened"
                ),
                False,
            ),
            (
                "OpenCode workflow_run completed",
                event_context(
                    "workflow_run",
                    workflow_run={"path": ".github/workflows/opencode.yml"},
                ),
                True,
            ),
            (
                "other workflow_run completed",
                event_context(
                    "workflow_run",
                    workflow_run={"path": ".github/workflows/ci.yml"},
                ),
                False,
            ),
            ("push is not a scheduler trigger", event_context("push"), False),
        ]
        for label, context, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(expected, admitted(self.automation, context), label)

    def test_before_after_replay_counts_scheduler_jobs(self):
        events = [
            (
                "scheduler dispatch comment on #439",
                event_context("issue_comment", comment=comment(DISPATCH_COMMENT)),
            ),
            (
                "OpenCode run completed",
                event_context(
                    "workflow_run",
                    workflow_run={"path": ".github/workflows/opencode.yml"},
                ),
            ),
            (
                "issue label changed",
                event_context("issues", action="labeled", actor="github-actions[bot]"),
            ),
        ]

        for name, condition, old_condition in (
            ("consumer", self.consumer, OLD_CONSUMER_IF),
            ("self-dogfood", self.automation, OLD_AUTOMATION_IF),
        ):
            with self.subTest(entrypoint=name):
                before = [admitted(old_condition, context) for _, context in events]
                after = [admitted(condition, context) for _, context in events]
                self.assertEqual([True, True, True], before)
                self.assertEqual([False, True, True], after)
                self.assertEqual(3, sum(before))
                self.assertEqual(2, sum(after))
                self.assertFalse(after[0], "the dispatch comment must skip the scheduler job")
                self.assertTrue(after[1], "the required workflow_run wake stays eligible")
                self.assertTrue(after[2], "the required label wake stays eligible")

    def test_old_conditions_match_the_purpose_not_the_new_guards(self):
        self.assertFalse(
            "issue-scheduler-dispatch" in OLD_CONSUMER_IF
            or "opencode-watchdog" in OLD_CONSUMER_IF
        )
        self.assertFalse(
            "issue-scheduler-dispatch" in OLD_AUTOMATION_IF
            or "opencode-watchdog" in OLD_AUTOMATION_IF
        )


class WatchdogPeriodicGateTests(unittest.TestCase):
    """A skipped or stale caller run is never evidence of watchdog work."""

    def setUp(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/continuum-opencode-watchdog.yml").read_text(encoding="utf-8"))
        self.steps = workflow["jobs"]["recover"]["steps"]
        gate = next(step for step in self.steps if step.get("id") == "periodic_gate")
        self.script = gate["with"]["script"]
        self.condition = gate["if"]
        self.concurrent = workflow["concurrency"]

    def run_gate(self, candidates, jobs, *, error=False, context=None, workflow_id=369085546):
        harness = r"""
const scenario = JSON.parse(process.argv[1]);
const source = process.argv[2];
Date.now = () => Date.parse('2026-11-26T18:08:00Z');
const context = Object.assign({
  repo: { owner: 'kodmial', repo: 'continuum' },
  payload: { repository: { default_branch: 'main' } },
  ref: 'refs/heads/main', sha: 'abc', runId: 999,
}, scenario.context || {});
const outputs = {}, calls = [], warnings = [];
const github = {rest: {actions: {
  getWorkflowRun: async params => {
    calls.push(['caller', params]);
    return {data: {workflow_id: scenario.workflow_id}};
  },
  listWorkflowRuns: async params => {
    calls.push(['runs', params]);
    if (scenario.error) throw new Error('API unavailable');
    return {data: {workflow_runs: scenario.candidates}};
  },
  listJobsForWorkflowRun: async params => {
    calls.push(['jobs', params]);
    return {data: {jobs: scenario.jobs[params.run_id] || []}};
  },
}}};
const core = {
  setOutput: (key, value) => outputs[key] = value,
  notice: () => {},
  warning: text => warnings.push(text),
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
(async () => {
  await new AsyncFunction('github', 'context', 'core', source)(github, context, core);
  console.log(JSON.stringify({outputs, calls, warnings}));
})().catch(err => { console.error(err); process.exitCode = 1; });
"""
        result = subprocess.run(
            ["node", "-e", harness, json.dumps(dict(candidates=candidates, jobs=jobs, error=error, context=context, workflow_id=workflow_id)), self.script],
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    @staticmethod
    def candidate(**extra):
        return dict(id=101, event="schedule", head_sha="abc", head_branch="main", status="completed", created_at="2026-11-26T18:01:00Z", **extra)

    @staticmethod
    def job(attest="success", conclusion="success"):
        return {"name": "watchdog / recover", "status": "completed", "conclusion": conclusion,
                "steps": [{"name": "Attest completed watchdog catch-up", "conclusion": attest}]}

    def test_gate_only_applies_to_self_dogfood_periodic_sources(self):
        for event, schedule, inputs, repo, workflow, expected in (
            ("schedule", "*/15 * * * *", {}, "kodmial/continuum", "Continuum automation", True),
            ("schedule", "17,47 * * * *", {}, "kodmial/continuum", "Continuum automation", False),
            ("workflow_dispatch", None, {"watchdog_only": True, "periodic_wake": "true"}, "kodmial/continuum", "Continuum automation", True),
            ("workflow_dispatch", None, {"watchdog_only": "true", "periodic_wake": "true"}, "kodmial/continuum", "Continuum automation", True),
            ("workflow_dispatch", None, {"watchdog_only": True}, "kodmial/continuum", "Continuum automation", False),
            ("workflow_dispatch", None, {"periodic_wake": "true"}, "kodmial/continuum", "Continuum automation", False),
            ("workflow_run", None, {}, "kodmial/continuum", "Continuum automation", False),
            ("issues", None, {}, "kodmial/continuum", "Continuum automation", False),
            ("schedule", "*/15 * * * *", {}, "consumer/repo", "OpenCode watchdog", False),
            ("schedule", "*/15 * * * *", {}, "kodmial/continuum", "Other caller", False),
        ):
            with self.subTest(event=event, inputs=inputs, repo=repo, workflow=workflow):
                context = event_context(event, schedule=schedule, inputs=inputs)
                context["github"].update(repository=repo, workflow=workflow)
                self.assertEqual(expected, admitted(self.condition, context))

    def test_actual_successful_watchdog_evidence_skips_duplicate(self):
        for event in ("schedule", "workflow_dispatch"):
            run = self.candidate()
            run["event"] = event
            result = self.run_gate([run], {101: [self.job()]})
            self.assertEqual({"skip": "true"}, result["outputs"])
            self.assertEqual("caller", result["calls"][0][0])
            self.assertEqual(999, result["calls"][0][1]["run_id"])
            params = result["calls"][1][1]
            self.assertEqual(369085546, params["workflow_id"])
            self.assertEqual("abc", params["head_sha"])
            self.assertEqual("main", params["branch"])
            self.assertEqual(">=2026-11-26T18:00:00Z", params["created"])
            self.assertEqual(100, params["per_page"])

    def test_noop_missing_failed_stale_and_auto_merge_only_runs_do_not_skip(self):
        bad_jobs = [
            [], [{"name": "auto-merge / controller", "status": "completed", "conclusion": "success"}],
            [self.job(attest="skipped")], [self.job(conclusion="failure")],
            [self.job(conclusion="cancelled")],
        ]
        for jobs in bad_jobs:
            self.assertEqual({}, self.run_gate([self.candidate()], {101: jobs})["outputs"])
        for change in (
            {"head_sha": "old"}, {"head_branch": "non-main"}, {"id": 999},
            {"status": "in_progress"}, {"created_at": "2026-11-26T17:59:00Z"},
            {"event": "workflow_run"},
            {"event": "workflow_dispatch", "display_title": "Continuum automation [continuum-auto-merge-only]"},
        ):
            run = self.candidate()
            run.update(change)
            self.assertEqual({}, self.run_gate([run], {101: [self.job()]})["outputs"])

    def test_unavailable_evidence_runs_guarded_pass_and_bounds_inventory(self):
        unknown = self.run_gate([self.candidate()], {101: [self.job()]}, workflow_id=None)
        self.assertEqual({}, unknown["outputs"])
        self.assertEqual(["caller"], [call[0] for call in unknown["calls"]])
        self.assertTrue(unknown["warnings"])
        result = self.run_gate([], {}, error=True)
        self.assertEqual({}, result["outputs"])
        self.assertTrue(result["warnings"])
        candidates = []
        for n in range(40):
            run = self.candidate()
            run["id"] = 101 + n
            candidates.append(run)
        result = self.run_gate(candidates, {})
        self.assertEqual(8, len([c for c in result["calls"] if c[0] == "jobs"]))
        self.assertEqual({}, result["outputs"])

    def test_duplicate_skips_expensive_steps_and_eventless_lanes_are_shared(self):
        self.assertFalse(self.concurrent["cancel-in-progress"])
        self.assertIn("github.event_name == 'workflow_run' && github.event.workflow_run.id || 'catch-up'", self.concurrent["group"])
        for step in self.steps:
            if step["name"] in ("Checkout Continuum watchdog helpers", "Recover failed OpenCode issue run"):
                self.assertEqual("steps.periodic_gate.outputs.skip != 'true'", step["if"])
        attest = next(step for step in self.steps if step["name"] == "Attest completed watchdog catch-up")
        self.assertEqual("steps.recover.outputs.catch_up_complete == 'true'", attest["if"])

class AutomationAutoMergeOnlyRoutingTests(unittest.TestCase):
    """#470: the external wake must admit only the existing auto-merge job.

    Every decision below is evaluated from the checked-in ``automation.yml``
    job conditions, not from a hand-written model of the workflow.
    """

    AUTO_MERGE_JOBS = (
        "scheduler",
        "watchdog",
        "repair",
        "auto-merge",
        "global-pr-watchdog",
    )

    def setUp(self):
        self.workflow = yaml.safe_load(SELF_SCHEDULER.read_text(encoding="utf-8"))
        self.conditions = {
            name: self.workflow["jobs"][name]["if"] for name in self.AUTO_MERGE_JOBS
        }

    def events(self):
        return self.workflow.get("on") or self.workflow.get(True) or {}

    def routing(self, event_name, **context):
        return {
            name: admitted(condition, event_context(event_name, **context))
            for name, condition in self.conditions.items()
        }

    def test_auto_merge_only_is_an_optional_typed_dispatch_selector(self):
        dispatch = self.events()["workflow_dispatch"]["inputs"]["auto_merge_only"]
        self.assertEqual("boolean", dispatch["type"])
        self.assertIs(False, dispatch["default"])
        self.assertIs(False, dispatch["required"])

        run_name = self.workflow["run-name"]
        self.assertIn("[continuum-periodic]", run_name)
        self.assertIn("[continuum-wake:", run_name)
        self.assertIn(
            "[continuum-auto-merge-only]",
            run_name,
            "the narrow selector must emit a marker the periodic gate cannot read"
            " as scheduler completion evidence",
        )
        self.assertIn("'auto-merge-only'", run_name)

    def test_narrow_run_does_not_close_the_scheduler_periodic_window(self):
        # A narrow run at 12:19 and a periodic dispatch at 12:21 share the
        # native 12:17 window. The narrow run must not be completion evidence.
        narrow = render_run_name(
            self.workflow["run-name"],
            "workflow_dispatch",
            inputs={"periodic_wake": "true", "auto_merge_only": "true"},
        )
        self.assertIn("[continuum-auto-merge-only]", narrow)
        self.assertIn("[continuum-wake:auto-merge-only]", narrow)
        self.assertNotIn("[continuum-wake:true]", narrow)
        self.assertNotIn("[continuum-periodic]", narrow)

        broad = render_run_name(
            self.workflow["run-name"],
            "workflow_dispatch",
            inputs={"periodic_wake": "true"},
        )
        self.assertEqual(
            "Continuum automation [continuum-periodic] [continuum-wake:true]", broad
        )

        dispatch_epoch = int(
            datetime(2026, 10, 23, 12, 21, tzinfo=timezone.utc).timestamp()
        )
        created = datetime.fromtimestamp(
            dispatch_epoch - 120, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        def decision(title):
            return periodic_dedupe_decision(
                now_epoch=dispatch_epoch,
                runs=[
                    {
                        "id": 1,
                        "event": "workflow_dispatch",
                        "status": "completed",
                        "conclusion": "success",
                        "display_title": title,
                        "created_at": created,
                        "head_branch": "main",
                    }
                ],
                current_run_id=2,
                caller_event_name="workflow_dispatch",
                periodic_wake="true",
                default_branch="main",
            )

        narrow_decision = decision(narrow)
        self.assertTrue(
            narrow_decision.run,
            "a successful narrow run must not close the periodic window",
        )
        self.assertEqual("window-due", narrow_decision.reason)

        broad_decision = decision(broad)
        self.assertFalse(broad_decision.run)
        self.assertEqual("duplicate-window", broad_decision.reason)

    def test_external_two_input_wake_admits_only_auto_merge(self):
        expected = {
            "scheduler": False,
            "watchdog": False,
            "repair": False,
            "auto-merge": True,
            "global-pr-watchdog": False,
        }
        # REST dispatch sends input values as strings; the boolean declaration
        # may surface the same value as a JSON boolean in the inputs context.
        for selector in (True, "true"):
            with self.subTest(selector=selector):
                result = self.routing(
                    "workflow_dispatch",
                    inputs={
                        "periodic_wake": "true",
                        "auto_merge_only": selector,
                    },
                )
                self.assertEqual(expected, result)

    def test_watchdog_only_dispatch_is_narrow_and_has_distinct_marker(self):
        expected = dict.fromkeys(self.AUTO_MERGE_JOBS, False)
        expected["watchdog"] = True
        selector = self.events()["workflow_dispatch"]["inputs"]["watchdog_only"]
        self.assertEqual("boolean", selector["type"])
        self.assertIs(False, selector["default"])
        self.assertIs(False, selector["required"])
        for value in (True, "true"):
            for periodic in (None, "false", "true"):
                inputs = {"watchdog_only": value}
                if periodic is not None:
                    inputs["periodic_wake"] = periodic
                with self.subTest(inputs=inputs):
                    self.assertEqual(expected, self.routing("workflow_dispatch", inputs=inputs))
                    title = render_run_name(self.workflow["run-name"], "workflow_dispatch", inputs=inputs)
                    self.assertEqual("Continuum automation [continuum-watchdog-only] [continuum-wake:watchdog-only]", title)
                    self.assertNotIn("[continuum-wake:true]", title)
                    self.assertNotIn("[continuum-periodic]", title)

    def test_watchdog_only_false_preserves_broad_dispatch(self):
        for value in (False, "false"):
            for periodic in ("true", "false"):
                with self.subTest(value=value, periodic=periodic):
                    self.assertEqual(
                        self.routing("workflow_dispatch", inputs={"periodic_wake": periodic}),
                        self.routing("workflow_dispatch", inputs={"periodic_wake": periodic, "watchdog_only": value}),
                    )

    def test_overlapping_selectors_keep_original_auto_merge_only_precedence(self):
        for auto_merge in (True, "true"):
            for watchdog in (True, "true"):
                inputs = {"watchdog_only": watchdog, "auto_merge_only": auto_merge, "periodic_wake": "true"}
                with self.subTest(inputs=inputs):
                    self.assertEqual(self.routing("workflow_dispatch", inputs={"auto_merge_only": auto_merge}), self.routing("workflow_dispatch", inputs=inputs))
                    title = render_run_name(self.workflow["run-name"], "workflow_dispatch", inputs=inputs)
                    self.assertIn("[continuum-wake:auto-merge-only]", title)
                    self.assertNotIn("[continuum-watchdog-only]", title)

    def test_owner_unpause_is_an_explicit_watchdog_wake_not_a_generic_issue_event(self):
        for actor, action, label, issue_state, pull_request, custom_pause, expected in (
            (OWNER, "unlabeled", "automation:paused", "open", None, None, True),
            (OWNER, "unlabeled", "custom-pause", "open", None, "custom-pause", True),
            (OTHER, "unlabeled", "automation:paused", "open", None, None, False),
            (OWNER, "labeled", "automation:paused", "open", None, None, False),
            (OWNER, "unlabeled", "automation:ready", "open", None, None, False),
            (OWNER, "unlabeled", "automation:paused", "closed", None, None, False),
            (OWNER, "unlabeled", "automation:paused", "open", {"url": "pr"}, None, False),
        ):
            with self.subTest(actor=actor, action=action, label=label, state=issue_state):
                route = self.routing(
                    "issues",
                    actor=actor,
                    action=action,
                    label=label,
                    issue_state=issue_state,
                    issue_pull_request=pull_request,
                    pause_label=custom_pause,
                )
                self.assertEqual(expected, route["watchdog"])
                self.assertTrue(route["scheduler"])
                self.assertFalse(route["auto-merge"])
                self.assertFalse(route["repair"])
                self.assertFalse(route["global-pr-watchdog"])

    def test_auto_merge_only_alone_remains_narrow_for_manual_testing(self):
        expected = {
            "scheduler": False,
            "watchdog": False,
            "repair": False,
            "auto-merge": True,
            "global-pr-watchdog": False,
        }
        for selector in (True, "true"):
            with self.subTest(selector=selector):
                result = self.routing(
                    "workflow_dispatch", inputs={"auto_merge_only": selector}
                )
                self.assertEqual(expected, result)

    def test_false_or_absent_selector_preserves_ordinary_manual_dispatch(self):
        ordinary = {
            "scheduler": True,
            "watchdog": True,
            "repair": False,
            "auto-merge": True,
            "global-pr-watchdog": False,
        }
        for label, inputs in (
            ("absent", {}),
            ("explicit false", {"auto_merge_only": False}),
            ("REST string false", {"auto_merge_only": "false"}),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    ordinary, self.routing("workflow_dispatch", inputs=inputs)
                )

    def test_prior_periodic_wake_behavior_is_unchanged(self):
        periodic = {
            "scheduler": True,
            "watchdog": True,
            "repair": False,
            "auto-merge": True,
            "global-pr-watchdog": True,
        }
        for label, inputs in (
            ("legacy periodic", {"periodic_wake": "true"}),
            (
                "periodic with selector false",
                {"periodic_wake": "true", "auto_merge_only": False},
            ),
            (
                "periodic with REST selector false",
                {"periodic_wake": "true", "auto_merge_only": "false"},
            ),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    periodic, self.routing("workflow_dispatch", inputs=inputs)
                )

    def test_native_cron_routing_is_unchanged(self):
        primary = {
            "scheduler": True,
            "watchdog": True,
            "repair": False,
            "auto-merge": False,
            "global-pr-watchdog": False,
        }
        safety_net = {
            "scheduler": False,
            "watchdog": False,
            "repair": False,
            "auto-merge": True,
            "global-pr-watchdog": True,
        }
        self.assertEqual(
            primary, self.routing("schedule", schedule="*/15 * * * *")
        )
        self.assertEqual(
            safety_net, self.routing("schedule", schedule="17,47 * * * *")
        )

    def test_non_dispatch_event_routing_is_unchanged(self):
        cases = {
            "push": {
                "scheduler": False,
                "watchdog": False,
                "repair": False,
                "auto-merge": True,
                "global-pr-watchdog": False,
            },
            "status": {
                "scheduler": False,
                "watchdog": False,
                "repair": False,
                "auto-merge": True,
                "global-pr-watchdog": False,
            },
            "pull_request_review": {
                "scheduler": False,
                "watchdog": False,
                "repair": False,
                "auto-merge": True,
                "global-pr-watchdog": False,
            },
            "pull_request_review_comment": {
                "scheduler": False,
                "watchdog": False,
                "repair": False,
                "auto-merge": True,
                "global-pr-watchdog": False,
            },
            "issues": {
                "scheduler": True,
                "watchdog": False,
                "repair": False,
                "auto-merge": False,
                "global-pr-watchdog": False,
            },
        }
        for event_name, expected in cases.items():
            with self.subTest(event=event_name):
                self.assertEqual(expected, self.routing(event_name))

        self.assertEqual(
            {
                "scheduler": False,
                "watchdog": False,
                "repair": True,
                "auto-merge": False,
                "global-pr-watchdog": False,
            },
            self.routing("pull_request_target", action="opened"),
        )
        self.assertEqual(
            {
                "scheduler": True,
                "watchdog": False,
                "repair": False,
                "auto-merge": False,
                "global-pr-watchdog": False,
            },
            self.routing("pull_request_target", action="closed"),
        )
        self.assertEqual(
            {
                "scheduler": True,
                "watchdog": True,
                "repair": False,
                "auto-merge": False,
                "global-pr-watchdog": False,
            },
            self.routing(
                "workflow_run",
                workflow_run={"path": ".github/workflows/opencode.yml"},
            ),
        )
        self.assertEqual(
            {
                "scheduler": False,
                "watchdog": False,
                "repair": True,
                "auto-merge": True,
                "global-pr-watchdog": False,
            },
            self.routing(
                "workflow_run",
                workflow_run={"path": ".github/workflows/ci.yml"},
            ),
        )

    def test_auto_merge_reuses_one_caller_engine_and_serialization(self):
        jobs = self.workflow["jobs"]
        auto_merge = jobs["auto-merge"]
        self.assertEqual(
            "./.github/workflows/continuum-auto-merge.yml", auto_merge["uses"]
        )
        self.assertEqual(
            {
                "continuum_ref": "main",
                "opencode_workflow": "opencode.yml",
                "coderabbit_retry_workflow": "coderabbit-retry.yml",
                "coderabbit_unresolved_workflow": "coderabbit-unresolved.yml",
            },
            auto_merge["with"],
        )
        callers = [
            name
            for name, job in jobs.items()
            if str(job.get("uses", "")).endswith("/continuum-auto-merge.yml")
        ]
        self.assertEqual(["auto-merge"], callers)

        engine = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "continuum-auto-merge.yml").read_text(
                encoding="utf-8"
            )
        )
        concurrency = engine["concurrency"]
        self.assertEqual("${{ github.workflow }}-auto-merge", concurrency["group"])
        self.assertIs(False, concurrency["cancel-in-progress"])

        condition = auto_merge["if"]
        for event in (
            "pull_request_review",
            "pull_request_review_comment",
            "status",
            "push",
            "workflow_dispatch",
        ):
            self.assertIn(f"github.event_name == '{event}'", condition)
        self.assertIn(
            "(github.event_name == 'schedule' && "
            "github.event.schedule == '17,47 * * * *')",
            condition,
        )
        self.assertIn(
            "(github.event_name == 'workflow_run' && "
            "github.event.workflow_run.path == '.github/workflows/ci.yml')",
            condition,
        )


class OpenCodeIngressTests(unittest.TestCase):
    def test_both_opencode_entrypoints_still_receive_issue_comment_created(self):
        for path in (CONSUMER_OPENCODE, SELF_OPENCODE):
            with self.subTest(path=path):
                parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
                events = parsed.get("on") or parsed.get(True) or {}
                issue_comment = events.get("issue_comment") or {}
                self.assertEqual(["created"], issue_comment.get("types"), path)
                call = parsed["jobs"]["call"]
                if path == SELF_OPENCODE:
                    # The self-caller delegates only the explicit owner PR
                    # repair command to its own guarded dispatch job.
                    # Ordinary /oc issue and watchdog commands still reach
                    # the canonical OpenCode executor unchanged.
                    condition = " ".join(str(call.get("if") or "").split())
                    self.assertEqual(
                        "github.event_name != 'issue_comment' || "
                        "!startsWith(github.event.comment.body, '/oc-repair ')",
                        condition,
                    )
                    repair = parsed["jobs"]["owner-coderabbit-repair"]
                    repair_gate = " ".join(str(repair.get("if") or "").split())
                    for predicate in (
                        "github.actor == github.repository_owner",
                        "github.event.issue.pull_request",
                        "startsWith(github.event.comment.body, '/oc-repair ')",
                    ):
                        self.assertIn(predicate, repair_gate)
                else:
                    self.assertIsNone(
                        call.get("if"),
                        "the consumer OpenCode caller must admit machine-origin /oc",
                    )

    def test_exact_machine_commands_still_admit_opencode_runs(self):
        for body in (DISPATCH_COMMENT, WATCHDOG_COMMENT):
            with self.subTest(body=body):
                self.assertEqual(
                    opencode_commands.RUN,
                    opencode_commands.classify_issue_comment(body),
                    "the canonical OpenCode parser must keep accepting the automation command",
                )


def snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class InstallUpgradeTests(unittest.TestCase):
    def test_install_upgrades_only_the_scheduler_condition_and_is_idempotent(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="scheduler-self-wakeup-", dir=TMP_PARENT
        ) as directory:
            target = Path(directory) / "consumer"
            env = os.environ.copy()
            env["TMPDIR"] = str(TMP_PARENT)

            def run_install(*args):
                completed = subprocess.run(
                    ["bash", str(INSTALL), str(target), *args],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                return completed.returncode, completed.stdout

            status, output = run_install()
            self.assertEqual(0, status, output)

            canonical = CONSUMER_SCHEDULER.read_text(encoding="utf-8")
            # Drop only the machine-origin suppression clauses to simulate the
            # pre-upgrade caller; the command prefilter itself is shared so the
            # replay cannot drift from the real stub.
            marker_clauses = (
                " &&\n"
                "        !contains(github.event.comment.body, '<!-- issue-scheduler-dispatch -->') &&\n"
                "        !contains(github.event.comment.body, '<!-- continuum-origin role=continuum component=opencode-watchdog -->')"
            )
            old_body = canonical.replace(marker_clauses, "")
            self.assertNotEqual(
                canonical, old_body, "the simulated caller must differ in the condition"
            )
            old_document = yaml.safe_load(old_body)
            new_document = yaml.safe_load(canonical)
            old_condition = old_document["jobs"]["call"].pop("if")
            new_condition = new_document["jobs"]["call"].pop("if")
            self.assertEqual(
                old_document,
                new_document,
                "the simulated pre-upgrade caller must differ only in the scheduler condition",
            )
            self.assertNotIn("issue-scheduler-dispatch", old_condition)
            self.assertIn("issue-scheduler-dispatch", new_condition)

            installed = target / ".github" / "workflows" / "continuum-issue-scheduler.yml"
            installed.write_text(old_body, encoding="utf-8")
            before = snapshot(target)

            status, output = run_install("--dry-run")
            self.assertEqual(0, status, output)
            self.assertIn("update: continuum-issue-scheduler.yml", output)
            self.assertIn("keep: continuum-opencode.yml", output)
            self.assertEqual(before, snapshot(target), "a dry run must change nothing")

            status, output = run_install()
            self.assertEqual(0, status, output)
            self.assertIn("update: continuum-issue-scheduler.yml", output)
            after = snapshot(target)
            self.assertEqual(
                {".github/workflows/continuum-issue-scheduler.yml"},
                {key for key in before if before[key] != after.get(key)},
                "an upgrade may change only the installer-owned scheduler caller",
            )
            self.assertEqual(set(before), set(after), "no new consumer files are needed")
            self.assertEqual(canonical.encode(), after[".github/workflows/continuum-issue-scheduler.yml"])
            for relative, content in after.items():
                self.assertNotIn(b"CONTINUUM_CHILD", content, relative)
                self.assertNotIn(b"CONTINUUM_PARENT", content, relative)
            self.assertNotIn("CONTINUUM_CHILD", output)

            status, output = run_install()
            self.assertEqual(0, status, output)
            self.assertIn("keep: continuum-issue-scheduler.yml", output)
            self.assertEqual(after, snapshot(target), "a same-version install must be idempotent")


if __name__ == "__main__":
    unittest.main()
