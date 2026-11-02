"""Regression tests for #394: machine-origin comments must not wake the scheduler.

Both scheduler entrypoints are evaluated from their real checked-in YAML: the
consumer caller stub's ``jobs.call.if`` and the self-dogfood ``automation.yml``
``jobs.scheduler.if``. The small expression evaluator below understands only
the GitHub Actions expression subset those conditions actually use, so the
tests never mirror a hand-written approximation of the decision.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from continuum import opencode_commands

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
    def _call(name, args):
        if name in ("startsWith", "contains") and len(args) == 2:
            haystack = "" if args[0] is None else str(args[0]).casefold()
            needle = "" if args[1] is None else str(args[1]).casefold()
            if name == "startsWith":
                return haystack.startswith(needle)
            return needle in haystack
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
):
    return {
        "github": {
            "event_name": event_name,
            "actor": actor,
            "repository_owner": OWNER,
            "event": {
                "comment": comment or {},
                "action": action,
                "schedule": schedule,
                "workflow_run": workflow_run or {},
                "issue": {"number": 439},
            },
        },
        "vars": {"CONTINUUM_ROLE": role},
    }


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


class OpenCodeIngressTests(unittest.TestCase):
    def test_both_opencode_entrypoints_still_receive_issue_comment_created(self):
        for path in (CONSUMER_OPENCODE, SELF_OPENCODE):
            with self.subTest(path=path):
                parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
                events = parsed.get("on") or parsed.get(True) or {}
                issue_comment = events.get("issue_comment") or {}
                self.assertEqual(["created"], issue_comment.get("types"), path)
                call = parsed["jobs"]["call"]
                self.assertIsNone(
                    call.get("if"),
                    "the OpenCode caller must not copy the scheduler's machine-origin filter",
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
            old_body = canonical.replace(
                """       (github.actor == github.repository_owner &&
        (startsWith(github.event.comment.body, '/oc') ||
         startsWith(github.event.comment.body, '/opencode')) &&
        !contains(github.event.comment.body, '<!-- issue-scheduler-dispatch -->') &&
        !contains(github.event.comment.body, '<!-- continuum-origin role=continuum component=opencode-watchdog -->')) ||
""",
                """       (github.actor == github.repository_owner &&
        (startsWith(github.event.comment.body, '/oc') ||
         startsWith(github.event.comment.body, '/opencode'))) ||
""",
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
