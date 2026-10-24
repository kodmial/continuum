"""Replay and fault-injection coverage for CI-repair wake-up admission.

GitHub cannot interpolate a repository variable into a ``workflow_run`` event
filter, so the installed caller still creates a controller run for every
completion. The production optimization is a job-level admission that reads
``vars.CONTINUUM_CI_REPAIR_WORKFLOWS`` and skips the runner for an unapproved
name. These tests evaluate the *actual* YAML expressions with a small GitHub
expression evaluator, so a future edit that removes the admission, hardcodes a
workflow, or lets an unapproved completion occupy the approved concurrency
group fails here.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml

from continuum.opencode_repair_state import classify_latest_state

ROOT = Path(__file__).resolve().parents[1]
CALLER = ROOT / ".github" / "caller-stubs" / "continuum-opencode-repair.yml"
CONTROLLER = ROOT / ".github" / "workflows" / "continuum-opencode-repair.yml"


# --- minimal GitHub Actions expression evaluator ---------------------------
#
# Only the operators and functions used by the repair workflow are supported:
# ||, &&, !, ==, !=, parentheses, string/number literals, dotted contexts, and
# the contains()/format() functions. Evaluation follows GitHub's short-circuit
# and truthiness rules so the tests exercise the same admission control flow.

_TOKEN = re.compile(
    r"""
    \s*(?:
      (?P<string>'(?:[^']|'')*')
    | (?P<number>-?\d+)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
    | (?P<op>&&|\|\||==|!=|!|\(|\)|,)
    )
    """,
    re.VERBOSE,
)


def _truthy(value) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and value == 0:
        return False
    if isinstance(value, str) and value == "":
        return False
    return True


def _string(value) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


class _Evaluator:
    def __init__(self, expression: str, context: dict):
        self.tokens = []
        for match in _TOKEN.finditer(expression):
            kind = match.lastgroup
            text = match.group(kind)
            if kind == "string":
                text = text[1:-1].replace("''", "'")
            elif kind == "number":
                text = int(text)
            self.tokens.append((kind, text))
        self.pos = 0
        self.context = context

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def _take(self, expected=None):
        kind, value = self._peek()
        if expected is not None and value != expected:
            raise AssertionError(f"expected {expected!r}, got {value!r}")
        self.pos += 1
        return value

    def evaluate(self):
        value = self._or()
        if self.pos != len(self.tokens):
            raise AssertionError(f"unconsumed tokens: {self.tokens[self.pos:]}")
        return value

    def _or(self):
        value = self._and()
        while self._peek()[1] == "||":
            self._take("||")
            right = self._and()
            value = value if _truthy(value) else right
        return value

    def _and(self):
        value = self._not()
        while self._peek()[1] == "&&":
            self._take("&&")
            right = self._not()
            value = right if _truthy(value) else value
        return value

    def _not(self):
        if self._peek()[1] == "!":
            self._take("!")
            return not _truthy(self._not())
        return self._comparison()

    def _comparison(self):
        value = self._primary()
        if self._peek()[1] in ("==", "!="):
            op = self._take()
            right = self._primary()
            equal = _string(value) == _string(right)
            return equal if op == "==" else not equal
        return value

    def _primary(self):
        kind, value = self._peek()
        if value == "(":
            self._take("(")
            result = self._or()
            self._take(")")
            return result
        if kind in ("string", "number"):
            self._take()
            return value
        if kind == "ident":
            self._take()
            if self._peek()[1] == "(":
                return self._function(value)
            return self._resolve(value)
        raise AssertionError(f"unexpected token {value!r}")

    def _function(self, name):
        self._take("(")
        args = []
        if self._peek()[1] != ")":
            args.append(self._or())
            while self._peek()[1] == ",":
                self._take(",")
                args.append(self._or())
        self._take(")")
        if name == "contains":
            return _string(args[1]) in _string(args[0])
        if name == "startsWith":
            return _string(args[0]).startswith(_string(args[1]))
        if name == "format":
            template = _string(args[0])
            for index, arg in enumerate(args[1:]):
                template = template.replace("{%d}" % index, _string(arg))
            return template.replace("{{", "{").replace("}}", "}")
        raise AssertionError(f"unsupported function {name}")

    def _resolve(self, path):
        value = self.context
        for part in path.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value


def evaluate(expression: str, context: dict):
    return _Evaluator(expression, context).evaluate()


def render(template: str, context: dict):
    return re.sub(
        r"\$\{\{(.*?)\}\}",
        lambda match: _string(evaluate(match.group(1), context)),
        template,
        flags=re.S,
    )


# --- event fixtures --------------------------------------------------------


def workflow_run(name="CI", conclusion="failure", head_sha="a" * 40, event="pull_request", run_id=100):
    return {
        "event_name": "workflow_run",
        "run_id": run_id,
        "sha": "b" * 40,
        "event": {
            "workflow_run": {
                "name": name,
                "conclusion": conclusion,
                "event": event,
                "head_sha": head_sha,
                "id": run_id,
            }
        },
    }


def dispatch(head_sha="c" * 40, pr_number=245):
    return {
        "event_name": "workflow_dispatch",
        "run_id": 200,
        "sha": "d" * 40,
        "event": {"pull_request": None},
        "inputs": {
            "pr_number": str(pr_number),
            "head_sha": head_sha,
            "conclusion": "failure",
            "run_id": "300",
        },
    }


def pull_request_target(pr_number=245):
    return {
        "event_name": "pull_request_target",
        "run_id": 201,
        "sha": "e" * 40,
        "repository": "kodmial/nanodictate",
        "event": {
            "pull_request": {
                "number": pr_number,
                "state": "open",
                "head": {
                    "ref": "opencode/issue405-test",
                    "repo": {"full_name": "kodmial/nanodictate"},
                },
            }
        },
    }


def context(event, *, vars=None, inputs=None, role=None):
    values = {
        "github": dict(event),
        "vars": dict(vars or {}),
        "inputs": dict(inputs or event.get("inputs") or {}),
    }
    if role is not None:
        values["vars"]["CONTINUUM_ROLE"] = role
    return values


def run_record(run_id, name, status="completed", conclusion="success"):
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion}


def listing(*runs):
    return json.dumps({"total_count": len(runs), "workflow_runs": list(runs)})


class WakeupAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.caller = yaml.safe_load(CALLER.read_text(encoding="utf-8"))
        cls.controller = yaml.safe_load(CONTROLLER.read_text(encoding="utf-8"))
        cls.caller_if = cls.caller["jobs"]["call"]["if"]
        cls.ci_repair_if = cls.controller["jobs"]["ci-repair"]["if"]
        cls.group_template = cls.controller["concurrency"]["group"]

    def caller_admits(self, event, *, workflows=None, inputs=None):
        values = {"CONTINUUM_CI_REPAIR_WORKFLOWS": workflows} if workflows is not None else {}
        return bool(evaluate(self.caller_if, context(event, vars=values, inputs=inputs)))

    def repair_admits(self, event, *, workflows=None, inputs=None, role=None):
        values = {"CONTINUUM_CI_REPAIR_WORKFLOWS": workflows} if workflows is not None else {}
        return bool(evaluate(self.ci_repair_if, context(event, vars=values, inputs=inputs, role=role)))

    def group(self, event, *, workflows=None, inputs=None):
        values = {"CONTINUUM_CI_REPAIR_WORKFLOWS": workflows} if workflows is not None else {}
        return render(self.group_template, context(event, vars=values, inputs=inputs))

    def simulate_burst(self, events, *, workflows):
        """Model GitHub's one-running-plus-one-pending queue per group.

        Every event occupies the concurrency group its expression renders,
        even when its job is later skipped. A group's first event starts
        immediately; later events replace its single pending slot. Draining
        the pending slot after the burst is the conservative worst case for
        rapid arrivals.
        """
        queues = {}
        started = []
        for event in events:
            queue = queues.setdefault(self.group(event, workflows=workflows), {"running": None, "pending": None})
            if queue["running"] is None:
                queue["running"] = event
                started.append(event)
            else:
                queue["pending"] = event
        started.extend(queue["pending"] for queue in queues.values() if queue["pending"] is not None)
        return started

    # Approved completions and every supported delimiter style stay admitted.

    def test_default_ci_completion_is_admitted(self):
        event = workflow_run("CI", "failure")
        self.assertTrue(self.caller_admits(event))
        self.assertTrue(self.repair_admits(event))

    def test_custom_workflows_are_admitted_without_hardcoding(self):
        for configured in (
            "CI,Windows host",
            "CI, Windows host",
            "CI\nWindows host",
            "CI\r\nWindows host",
            " CI , Windows host ",
        ):
            with self.subTest(configured=configured):
                self.assertTrue(self.caller_admits(workflow_run("Windows host"), workflows=configured))
                self.assertTrue(self.repair_admits(workflow_run("Windows host"), workflows=configured))

    def test_configured_list_replaces_the_ci_default(self):
        event = workflow_run("CI", "failure")
        self.assertFalse(self.caller_admits(event, workflows="Windows host"))
        self.assertFalse(self.repair_admits(event, workflows="Windows host"))
        self.assertTrue(self.caller_admits(workflow_run("Windows host"), workflows="Windows host"))

    def test_caller_input_can_override_the_repository_variable(self):
        event = workflow_run("Windows host")
        self.assertFalse(self.caller_admits(event, workflows="CI"))
        self.assertTrue(
            self.caller_admits(event, workflows="CI", inputs={"ci_repair_workflows": "CI,Windows host"})
        )
        self.assertFalse(self.repair_admits(event, workflows="CI"))
        self.assertTrue(
            self.repair_admits(event, workflows="CI", inputs={"ci_repair_workflows": "CI,Windows host"})
        )

    def test_an_unapproved_completion_is_skipped_before_the_runner(self):
        event = workflow_run("Release notes", "success")
        self.assertFalse(self.caller_admits(event, workflows="CI,Windows host"))
        self.assertFalse(self.repair_admits(event, workflows="CI,Windows host"))

    def test_controller_completion_can_never_admit_itself(self):
        event = workflow_run("OpenCode repair controller", "success")
        self.assertFalse(self.caller_admits(event, workflows="OpenCode repair controller"))
        self.assertFalse(self.repair_admits(event, workflows="OpenCode repair controller"))

    def test_role_and_event_filters_still_apply(self):
        event = workflow_run("CI", "failure")
        self.assertFalse(self.repair_admits(event, role="child"))
        self.assertFalse(self.repair_admits(workflow_run("CI", "failure", event="push")))
        self.assertFalse(self.repair_admits(workflow_run("CI", "cancelled")))
        self.assertFalse(self.repair_admits(workflow_run("CI", "skipped")))

    # Both independent non-workflow_run paths remain reachable.

    def test_direct_dispatch_is_admitted_independently_of_the_allow_list(self):
        event = dispatch()
        self.assertTrue(self.caller_admits(event, workflows="Windows host"))
        job = self.controller["jobs"]["ci-repair-dispatch"]
        self.assertTrue(bool(evaluate(job["if"], context(event, vars={"CONTINUUM_ROLE": "owner"}))))

    def test_pull_request_target_branch_sync_is_admitted_independently(self):
        event = pull_request_target()
        self.assertTrue(self.caller_admits(event, workflows="Windows host"))
        job = self.controller["jobs"]["sync-current-pr"]
        self.assertTrue(bool(evaluate(job["if"], context(event, vars={"CONTINUUM_ROLE": "owner"}))))

    # Concurrency isolation preserves approved liveness without letting a
    # suppressed completion displace a pending approved run.

    def test_approved_completions_share_the_exact_head_group(self):
        first = self.group(workflow_run("CI", "failure", run_id=1), workflows="CI,Windows host")
        second = self.group(workflow_run("Windows host", "success", run_id=2), workflows="CI,Windows host")
        self.assertEqual("opencode-repair-" + "a" * 40, first)
        self.assertEqual(first, second)

    def test_unapproved_completion_never_joins_the_approved_group(self):
        approved = self.group(workflow_run("CI", "failure", run_id=1), workflows="CI")
        unapproved = self.group(workflow_run("Release notes", "success", run_id=2), workflows="CI")
        self.assertNotEqual(approved, unapproved)
        self.assertNotIn("a" * 40, unapproved)
        self.assertEqual("opencode-repair-unapproved-2", unapproved)

    def test_rapid_and_reordered_approved_completions_stay_serialized(self):
        events = [
            workflow_run("CI", "failure", run_id=10),
            workflow_run("Windows host", "success", run_id=11),
            workflow_run("CI", "success", run_id=12),
        ]
        groups = {self.group(event, workflows="CI,Windows host") for event in events}
        self.assertEqual({"opencode-repair-" + "a" * 40}, groups)

    def test_unapproved_burst_cannot_strand_an_approved_failure(self):
        # Replay the concern from review: while an approved workflow is running,
        # its approved failure is pending and a burst of unapproved completions
        # arrives. GitHub keeps one running and one pending per group; because
        # the unapproved events render unique groups they cannot replace the
        # approved pending slot, so the approved survivor still reconciles the
        # latest state and notices the failure.
        workflows = "CI,Windows host"
        events = [
            workflow_run("Windows host", "success", run_id=10),
            workflow_run("CI", "failure", run_id=11),
            workflow_run("Release notes", "success", run_id=12),
            workflow_run("Packaging smoke", "success", run_id=13),
            workflow_run("Release notes", "failure", run_id=14),
        ]
        started = self.simulate_burst(events, workflows=workflows)
        approved_started = [event for event in started if self.repair_admits(event, workflows=workflows)]

        self.assertIn(events[1], approved_started,
                      'the approved failure must survive an unapproved completion burst')
        payload = listing(
            *(run_record(event["event"]["workflow_run"]["id"], event["event"]["workflow_run"]["name"],
                         conclusion=event["event"]["workflow_run"]["conclusion"]) for event in events)
        )
        expected = ("failure", "11", "CI")
        for event in approved_started:
            self.assertEqual(
                expected,
                classify_latest_state(payload, trusted=True, allowed_workflows=workflows),
                f'{event["event"]["workflow_run"]["name"]} survivor did not reconcile the approved failure',
            )

    def test_caller_declares_and_forwards_the_admission_input(self):
        caller_on = self.caller.get("on") or self.caller.get(True)
        declaration = caller_on["workflow_dispatch"]["inputs"]["ci_repair_workflows"]
        self.assertEqual("string", declaration["type"])
        self.assertFalse(declaration["required"])
        self.assertEqual(
            "${{ inputs.ci_repair_workflows }}",
            self.caller["jobs"]["call"]["with"]["ci_repair_workflows"],
            'the declared caller input must be forwarded rather than left as dead expression code',
        )

    def test_dispatch_and_branch_sync_keep_their_existing_group_keys(self):
        self.assertEqual(
            "opencode-repair-245", self.group(pull_request_target(pr_number=245))
        )
        self.assertEqual(
            "opencode-repair-" + "c" * 40, self.group(dispatch(head_sha="c" * 40))
        )

    # A configuration change immediately changes admission without a trigger
    # edit or a reinstall.

    def test_newly_allowed_custom_workflow_is_admitted_after_config_change(self):
        event = workflow_run("Packaging smoke", "failure")
        self.assertFalse(self.caller_admits(event, workflows="CI"))
        self.assertTrue(self.caller_admits(event, workflows="CI,Packaging smoke"))
        self.assertTrue(self.repair_admits(event, workflows="CI,Packaging smoke"))

    # Replay the latest-state classifier that both paths share.

    def test_replay_keeps_repair_liveness_through_rapid_and_reordered_events(self):
        allowed = "CI,Windows host"
        rapid = listing(
            run_record(10, "CI", conclusion="failure"),
            run_record(11, "Windows host", conclusion="success"),
        )
        self.assertEqual(
            ("failure", "10", "CI"),
            classify_latest_state(rapid, trusted=True, allowed_workflows=allowed),
        )

        reordered = listing(
            run_record(11, "Windows host", conclusion="success"),
            run_record(10, "CI", conclusion="failure"),
        )
        self.assertEqual(
            ("failure", "10", "CI"),
            classify_latest_state(reordered, trusted=True, allowed_workflows=allowed),
        )

    def test_replay_unapproved_completion_cannot_mask_an_approved_failure(self):
        payload = listing(
            run_record(10, "CI", conclusion="failure"),
            run_record(11, "Release notes", conclusion="success"),
        )
        self.assertEqual(
            ("failure", "10", "CI"),
            classify_latest_state(payload, trusted=True, allowed_workflows="CI,Windows host"),
        )

    def test_replay_inflight_exact_head_lock_is_preserved(self):
        payload = listing(
            run_record(10, "CI", conclusion="success"),
            run_record(11, "Windows host", status="in_progress", conclusion=None),
        )
        self.assertEqual(
            ("inflight", "", "Windows host"),
            classify_latest_state(payload, trusted=True, allowed_workflows="CI,Windows host"),
        )

    def test_replay_newly_allowed_workflow_participates_after_config_change(self):
        payload = listing(
            run_record(10, "CI", conclusion="success"),
            run_record(11, "Packaging smoke", conclusion="failure"),
        )
        self.assertEqual(
            ("green", "", ""),
            classify_latest_state(payload, trusted=True, allowed_workflows="CI"),
        )
        self.assertEqual(
            ("failure", "11", "Packaging smoke"),
            classify_latest_state(payload, trusted=True, allowed_workflows="CI,Packaging smoke"),
        )

    def test_missed_workflow_run_event_is_recoverable_by_direct_dispatch(self):
        # This optimization deliberately adds no timer. If a workflow_run
        # delivery is missed, the consumer can still report the same head
        # through the independent workflow_dispatch path, whose admission does
        # not depend on the completed workflow name.
        event = dispatch(head_sha="a" * 40)
        self.assertTrue(self.caller_admits(event, workflows="Windows host"))
        dispatch_job = self.controller["jobs"]["ci-repair-dispatch"]
        self.assertTrue(
            bool(evaluate(dispatch_job["if"], context(event, vars={"CONTINUUM_ROLE": "owner"})))
        )
        payload = listing(run_record(10, "CI", conclusion="failure"))
        self.assertEqual(
            ("failure", "10", "CI"),
            classify_latest_state(
                payload,
                trusted=True,
                allowed_workflows="CI,Windows host",
                own_name="__reported__",
                own_id="900",
                own_conclusion="failure",
                own_force=True,
            ),
        )

    # The broad trigger stays intact so removing the job-level admission is a
    # safe rollback and no retry timer is silently introduced in this scope.

    def test_trigger_and_rollback_shape_are_unchanged(self):
        caller_on = self.caller.get("on") or self.caller.get(True)
        controller_on = self.controller.get("on") or self.controller.get(True)
        workflows = caller_on["workflow_run"]["workflows"]
        self.assertEqual(["*", "!OpenCode repair controller"], workflows)
        for event_name in ("pull_request_target", "workflow_dispatch"):
            self.assertIn(event_name, caller_on)
        self.assertNotIn("schedule", caller_on)
        self.assertNotIn("schedule", controller_on)
        self.assertIn("github.event.workflow_run.name", self.caller_if)
        self.assertIn("vars.CONTINUUM_CI_REPAIR_WORKFLOWS", self.caller_if)


if __name__ == "__main__":
    unittest.main()
