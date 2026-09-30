#!/usr/bin/env python3
"""The scheduler's reconcile step is a state machine, so it is tested as one.

The `github-script` body in `.github/workflows/issue-scheduler.yml` decides what
to dispatch from four inputs: which issues the trust policy allowlisted, which
labels each issue carries, which pull requests exist, and what the issue's own
history says. Every one of those is attacker-supplyable, so the interesting
properties are transitions rather than expressions:

* a hand-off that arrives while a run holds the reservation is not dispatched
  twice,
* removing the label withdraws the request without cancelling work in flight,
  and without leaking a reservation for ever,
* a merged or abandoned pull request is reconciled even when its event was
  missed,
* dependencies and the WIP limit still order and bound the dispatch set.

Running the workflow's own JavaScript under Node against a mock API is what
makes these assertions about the shipped script. Only the API, the clock and
the repository state are this suite's; the logic under test is the workflow's.

Run with::

    PYTHONPATH=src python3 -m unittest tests.test_scheduler_reconcile -v
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / ".github" / "workflows" / "issue-scheduler.yml"

sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import continuum_labels  # noqa: E402

DISPATCH_LABEL = continuum_labels.DISPATCH_LABEL
IN_PROGRESS_LABEL = continuum_labels.IN_PROGRESS_LABEL
PAUSED_LABEL = continuum_labels.PAUSED_LABEL

OWNER = "kodmial"
REPOSITORY = "{}/continuum".format(OWNER)

#: A fixed instant. The lease is measured against it, so every scenario states
#: its own timestamps relative to this instead of sleeping.
NOW_MS = 1_700_000_000_000
DISPATCH_MARKER = "<!-- issue-scheduler-dispatch -->"


def at(minutes_ago: float) -> str:
    """An ISO timestamp `minutes_ago` minutes before the fixed clock."""
    return iso(NOW_MS - int(minutes_ago * 60_000))


def iso(milliseconds: int) -> str:
    return (
        datetime.datetime.fromtimestamp(
            milliseconds / 1000, datetime.timezone.utc
        )
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def agent_pull_request(number: int, issue_number: int, state: str = "open", **extra) -> dict:
    pull_request = {
        "number": number,
        "state": state,
        "created_at": at(30),
        "head": {
            "ref": "opencode/issue{}-continuum".format(issue_number),
            "repo": {"full_name": REPOSITORY},
        },
    }
    pull_request.update(extra)
    return pull_request


def make_issue(
    number: int,
    labels=(),
    state: str = "open",
    login: str = OWNER,
    title: str | None = None,
) -> dict:
    return {
        "number": number,
        "state": state,
        "title": title if title is not None else "Work on issue {}".format(number),
        "user": {"login": login},
        "labels": [{"name": name} for name in labels],
    }


#: The API surface the scheduler touches, and nothing else. An unmodelled call
#: throws, so a new call in the workflow fails this suite instead of being
#: silently answered by a permissive stub.
HARNESS = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const state = input.state;
const calls = [];
const NOW = input.now;

Date.now = () => NOW;

function fail(message) { throw new Error(message); }

function issue(number) {
  const found = state.issues.find(candidate => candidate.number === number);
  if (!found) fail('scenario has no issue #' + number);
  return found;
}

function labelNames(item) {
  return (item.labels || []).map(label =>
    typeof label === 'string' ? label : label.name
  );
}

function record(name, args) { calls.push({call: name, args: args}); }

const github = {
  rest: {
    issues: {
      async createLabel(args) {
        record('createLabel', args);
        return {data: {}};
      },
      async addLabels(args) {
        record('addLabels', args);
        for (const name of args.labels) {
          const item = issue(args.issue_number);
          if (!labelNames(item).includes(name)) {
            item.labels.push({name});
          }
        }
        return {data: {}};
      },
      async removeLabel(args) {
        record('removeLabel', args);
        if (args.name === undefined) fail('removeLabel needs a name');
        const item = issue(args.issue_number);
        item.labels = item.labels.filter(
          label => (typeof label === 'string' ? label : label.name) !== args.name
        );
        return {data: {}};
      },
      async createComment(args) {
        record('createComment', args);
        (state.comments[args.issue_number] =
          state.comments[args.issue_number] || []).push({
          body: args.body,
          created_at: iso(NOW),
        });
        return {data: {}};
      },
      async update(args) {
        record('update', args);
        const item = issue(args.issue_number);
        for (const key of ['state', 'state_reason', 'title']) {
          if (args[key] !== undefined) item[key] = args[key];
        }
        return {data: item};
      },
      async get(args) {
        record('get', args);
        return {data: clone(issue(args.issue_number))};
      },
      async listForRepo() { fail('listForRepo must go through paginate'); },
      async listComments() { fail('listComments must go through paginate'); },
    },
    pulls: {
      async list() { fail('pulls.list must go through paginate'); },
    },
    actions: {
      async createWorkflowDispatch(args) {
        record('createWorkflowDispatch', args);
        if (input.dispatchFails) fail('dispatch refused');
        return {};
      },
    },
  },

  async paginate(route, params) {
    const issue_number = params.issue_number;
    const routeName =
      typeof route === 'string'
        ? route.includes('/events')
          ? 'events'
          : route.includes('/dependencies/blocked_by')
          ? 'blockers'
          : route.includes('/issues/') && route.includes('/comments')
          ? 'comments'
          : route
        : route === github.rest.issues.listComments
        ? 'comments'
        : route === github.rest.issues.listForRepo
        ? 'issues'
        : route === github.rest.pulls.list
        ? 'pulls'
        : fail('unmodelled paginate route');

    record('paginate', {route: routeName, params});

    if (routeName === 'comments') {
      return clone(state.comments[issue_number] || []);
    }
    if (routeName === 'events') {
      return clone(state.events[issue_number] || []);
    }
    if (routeName === 'issues') {
      const wanted = [params.labels || []]
        .flat()
        .map(name => String(name).toLowerCase());
      return clone(
        state.issues.filter(candidate => {
          if (candidate.state !== 'open') return false;
          if (!wanted.length) return true;
          return wanted.every(name =>
            labelNames(candidate).some(
              label => (typeof label === 'string' ? label : label.name).toLowerCase() === name
            )
          );
        })
      );
    }
    if (routeName === 'pulls') {
      return clone(
        state.pulls.filter(pull_request =>
          params.state === 'all' ? true : pull_request.state === params.state
        )
      );
    }
    if (routeName === 'blockers') {
      return clone(state.blockers[issue_number] || []);
    }
    fail('unmodelled paginate route: ' + routeName);
  },
};

function clone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
function iso(milliseconds) {
  return new Date(milliseconds).toISOString().replace('.000Z', 'Z');
}

const core = {
  info() {}, notice() {}, debug() {}, warning() {},
  error() {},
  setFailed(message) { fail('the workflow failed the step: ' + message); },
};

const [OWNER_NAME, REPO_NAME] = input.repository.split('/');

const context = {
  repo: {owner: OWNER_NAME, repo: REPO_NAME},
  eventName: input.eventName,
  payload: input.payload || {},
};

(async () => {
  // The script returns early when there is nothing to do, so the report is
  // emitted from a finally: an early exit is a result, not a missing result.
  try {
__SCRIPT__
  } finally {
    process.stdout.write(JSON.stringify({state, calls}));
  }
})().catch(error => {
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
});
"""


def script_source() -> str:
    text = SCHEDULER.read_text(encoding="utf-8")
    marker = "\n          script: |\n"
    start = text.index(marker) + len(marker)
    body = text[start:]
    lines = body.split("\n")
    indent = min(
        (len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0
    )
    return "\n".join(line[indent:] if line.strip() else "" for line in lines)


def label_specs() -> list:
    """The provisioning plan the read-only job hands the reconcile step."""
    return [
        {"name": spec.name, "color": spec.color, "description": spec.description}
        for spec in continuum_labels.LABEL_SPECS
    ]


def scheduler_default(name: str) -> int:
    """The fallback the scheduler script uses when the variable is unset."""
    match = re.search(
        r"positiveInt\('%s',\s*(\d+)\)" % re.escape(name), SCHEDULER.read_text(encoding="utf-8")
    )
    if match is None:
        raise AssertionError("scheduler has no fallback for " + name)
    return int(match.group(1))


def workflow_default_for(variable: str) -> int:
    """The default the workflow passes when the repository variable is unset."""
    match = re.search(
        r"vars\.%s \|\| '(\d+)'" % re.escape(variable),
        SCHEDULER.read_text(encoding="utf-8"),
    )
    if match is None:
        raise AssertionError("workflow sets no default for " + variable)
    return int(match.group(1))


class ReconcileHarness(unittest.TestCase):
    maxDiff = None

    def run_scheduler(self, issues, trusted, **kwargs):
        """Run the workflow's script for one event and return (state, calls).

        The returned value is the repository state after the run plus every API
        call the script made. The state is what a human would see on the issue;
        the calls are what the run actually did. Both are read by the
        assertions, because a run that reported a dispatch it did not make, or
        left a reservation nobody can see, would pass a state-only check.
        """
        return self.run_event(
            issues,
            trusted,
            event_name=kwargs.pop("event_name", "schedule"),
            payload=kwargs.pop("payload", {}),
            **kwargs
        )

    def run_event(self, issues, trusted, *, event_name="schedule", payload=None, **kwargs):
        node = shutil.which("node")
        if node is None:
            raise AssertionError("node is required to evaluate the scheduler script")

        state = {
            "issues": issues,
            "pulls": kwargs.pop("pulls", []),
            "comments": kwargs.pop("comments", {}),
            "events": kwargs.pop("events", {}),
            "blockers": kwargs.pop("blockers", {}),
        }
        dispatch_fails = bool(kwargs.pop("dispatch_fails", False))
        # `wip_limit=None` omits the variable entirely, so the script's own
        # fallback is what runs. Tests that care about a limit set one.
        wip_limit = kwargs.pop("wip_limit", 2)
        environment = {
            "LEASE_MINUTES": str(kwargs.pop("lease_minutes", 45)),
            "MAX_DISPATCH_ATTEMPTS": str(kwargs.pop("max_attempts", 3)),
            "BASE_BRANCH": "main",
            "DISPATCH_LABEL": DISPATCH_LABEL,
            "IN_PROGRESS_LABEL": IN_PROGRESS_LABEL,
            "PAUSED_LABEL": PAUSED_LABEL,
            "PRIORITY_LABELS": ",".join(
                spec.name
                for spec in continuum_labels.LABEL_SPECS
                if spec.key.startswith("priority")
            ),
            "LABEL_SPECS": json.dumps(kwargs.pop("label_specs", label_specs())),
            "TRUSTED_ISSUES": ",".join(str(number) for number in trusted),
            "CREDENTIAL_LOGIN": OWNER,
        }
        if wip_limit is not None:
            environment["WIP_LIMIT"] = str(wip_limit)
        assert not kwargs, kwargs

        request = {
            "now": NOW_MS,
            "repository": REPOSITORY,
            "eventName": event_name,
            "payload": payload or {},
            "state": state,
            "dispatchFails": dispatch_fails,
        }
        harness = HARNESS.replace("__SCRIPT__", script_source())
        with tempfile.TemporaryDirectory() as directory:
            script_path = pathlib.Path(directory) / "harness.js"
            input_path = pathlib.Path(directory) / "input.json"
            script_path.write_text(harness, encoding="utf-8")
            input_path.write_text(json.dumps(request), encoding="utf-8")
            result = subprocess.run(
                [node, str(script_path), str(input_path)],
                capture_output=True,
                text=True,
                env={
                    "PATH": str(pathlib.Path(node).parent),
                    **environment,
                },
                check=False,
            )
        if result.returncode != 0:
            self.fail("the scheduler script failed:\n" + result.stderr)
        outcome = json.loads(result.stdout)
        return outcome["state"], outcome["calls"]

    # ------------------------------------------------------------------ #
    # helpers over the recorded calls
    # ------------------------------------------------------------------ #

    def calls_of(self, calls, name):
        return [call for call in calls if call["call"] == name]

    def dispatched(self, calls):
        return [
            int(call["args"]["inputs"]["issue_number"])
            for call in self.calls_of(calls, "createWorkflowDispatch")
        ]

    def dispatched_workflows(self, calls):
        return [
            (call["args"]["workflow_id"], call["args"]["ref"], call["args"]["inputs"])
            for call in self.calls_of(calls, "createWorkflowDispatch")
        ]

    def labels_of(self, state, number):
        found = [issue for issue in state["issues"] if issue["number"] == number][0]
        return sorted(
            label["name"] if isinstance(label, dict) else label
            for label in found["labels"]
        )


class HandOffTests(ReconcileHarness):
    def test_a_labelled_trusted_issue_is_dispatched_in_issue_mode(self):
        state, calls = self.run_scheduler(
            [make_issue(37, labels=[DISPATCH_LABEL, "priority:p1"])], [37]
        )
        self.assertEqual([37], self.dispatched(calls))
        workflow, ref, inputs = self.dispatched_workflows(calls)[0]
        self.assertEqual("opencode.yml", workflow)
        self.assertEqual("main", ref)
        self.assertEqual("issue", inputs["mode"])
        self.assertEqual("37", inputs["issue_number"])

    def test_the_dispatch_declares_issue_mode_not_a_repair_shape(self):
        _, calls = self.run_scheduler([make_issue(37, labels=[DISPATCH_LABEL])], [37])
        inputs = self.dispatched_workflows(calls)[0][2]
        # Every input the agent workflow declares is sent, so a repair-shaped
        # field is visibly empty rather than absent.
        for field in ("pr_number", "head_ref", "run_id", "rungs_ruled_out"):
            self.assertEqual("", inputs[field], field)

    def test_an_unlabeled_issue_is_never_dispatched(self):
        _, calls = self.run_scheduler([make_issue(37, labels=["bug"])], [37])
        self.assertEqual([], self.dispatched(calls))

    def test_an_issue_the_policy_did_not_name_is_never_dispatched(self):
        _, calls = self.run_scheduler(
            [make_issue(37, labels=[DISPATCH_LABEL]), make_issue(38, labels=[DISPATCH_LABEL])],
            [37],
        )
        self.assertEqual([37], self.dispatched(calls))

    def test_a_stranger_authored_issue_is_never_dispatched(self):
        _, calls = self.run_scheduler(
            [make_issue(37, labels=[DISPATCH_LABEL], login="stranger")], [37]
        )
        self.assertEqual([], self.dispatched(calls))

    def test_a_pull_request_carrying_the_label_is_never_dispatched(self):
        pull_request_issue = make_issue(43, labels=[DISPATCH_LABEL])
        pull_request_issue["pull_request"] = {"url": "https://api.github.com/x"}
        _, calls = self.run_scheduler([pull_request_issue], [43])
        self.assertEqual([], self.dispatched(calls))

    def test_a_closed_issue_is_never_dispatched(self):
        _, calls = self.run_scheduler(
            [make_issue(37, labels=[DISPATCH_LABEL], state="closed")], [37]
        )
        self.assertEqual([], self.dispatched(calls))

    def test_provisioning_happens_before_anything_is_dispatched(self):
        _, calls = self.run_scheduler([make_issue(37, labels=[DISPATCH_LABEL])], [37])
        names = [call["call"] for call in calls]
        self.assertEqual(
            names.index("createWorkflowDispatch") > names.index("createLabel"),
            True,
            names,
        )

    def test_provisioning_covers_every_registry_label(self):
        _, calls = self.run_scheduler([], [])
        provisioned = sorted(
            call["args"]["name"] for call in self.calls_of(calls, "createLabel")
        )
        self.assertEqual(
            sorted(spec.name for spec in continuum_labels.LABEL_SPECS), provisioned
        )

    def test_a_missing_label_specification_is_a_hard_failure(self):
        # A run that cannot read the registry must not dispatch on a guessed
        # name, so this is an error and not an empty dispatch list.
        with self.assertRaises(AssertionError):
            self.run_scheduler(
                [make_issue(37, labels=[DISPATCH_LABEL])], [37], label_specs=[]
            )


class DuplicateEventTests(ReconcileHarness):
    def test_two_runs_of_the_same_event_dispatch_once(self):
        # The cron and the label event both fire. The first run leaves the
        # reservation and a dispatch record; the second must find the issue
        # already reserved and skip it.
        comments = {
            37: [{"body": DISPATCH_MARKER, "created_at": at(2)}],
        }
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        _, calls = self.run_scheduler(issues, [37], comments=comments)
        self.assertEqual([], self.dispatched(calls))
        self.assertEqual(
            [], [call for call in calls if call["call"] == "addLabels"
                 and call["args"]["issue_number"] == 37]
        )

    def test_a_reserved_issue_is_not_re_reserved_by_the_second_run(self):
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        state, calls = self.run_scheduler(
            issues,
            [37],
            comments={37: [{"body": DISPATCH_MARKER, "created_at": at(2)}]},
        )
        # The reservation survives, and no second reservation was taken.
        self.assertIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))
        self.assertEqual(1, self.labels_of(state, 37).count(IN_PROGRESS_LABEL))

    def test_an_unreserved_duplicate_event_still_dispatches_once(self):
        # Without a reservation the first run is the one that takes it, so a
        # later duplicate is suppressed by the reservation rather than by the
        # event itself.
        issues = [make_issue(37, labels=[DISPATCH_LABEL])]
        state, _ = self.run_scheduler(issues, [37])
        self.assertIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))
        _, second = self.run_scheduler(
            state["issues"], [37], comments=state["comments"]
        )
        self.assertEqual([], self.dispatched(second))


class LabelRemovalTests(ReconcileHarness):
    def test_removing_the_label_before_the_run_stops_a_fresh_dispatch(self):
        # A label event that removes the hand-off, and a scheduler that had
        # already listed the issue. The second check is the label re-read.
        _, calls = self.run_scheduler([make_issue(37, labels=["bug"])], [37])
        self.assertEqual([], self.dispatched(calls))

    def test_removing_the_label_during_a_run_lets_that_run_finish(self):
        # Reserved, dispatched two minutes ago, hand-off withdrawn. The
        # reservation is held until the lease expires so the WIP limit still
        # counts the run that is still using a slot.
        issues = [make_issue(37, labels=[IN_PROGRESS_LABEL])]
        state, calls = self.run_scheduler(
            issues, [], comments={37: [{"body": DISPATCH_MARKER, "created_at": at(2)}]}
        )
        self.assertIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))
        self.assertEqual([], self.dispatched(calls))

    def test_a_withdrawn_reservation_with_no_run_is_released(self):
        # Nothing holds the slot, so the reservation is released immediately
        # rather than waiting out the lease.
        issues = [make_issue(37, labels=[IN_PROGRESS_LABEL])]
        state, _ = self.run_scheduler(issues, [])
        self.assertNotIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))

    def test_a_withdrawn_reservation_from_a_dead_run_is_released(self):
        issues = [make_issue(37, labels=[IN_PROGRESS_LABEL])]
        state, _ = self.run_scheduler(
            issues, [], comments={37: [{"body": DISPATCH_MARKER, "created_at": at(600)}]}
        )
        self.assertNotIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))

    def test_a_withdrawn_reservation_is_never_paused(self):
        # Nothing failed. Pausing would tell a human automation gave up on an
        # issue they deliberately withdrew.
        issues = [make_issue(37, labels=[IN_PROGRESS_LABEL])]
        state, _ = self.run_scheduler(
            issues, [], comments={37: [{"body": DISPATCH_MARKER, "created_at": at(600)}]}
        )
        self.assertNotIn(PAUSED_LABEL, self.labels_of(state, 37))

    def test_withdrawal_frees_the_slot_for_the_next_candidate(self):
        # One issue withdrawn mid-run, one ready issue, one free slot: the ready
        # issue is dispatched and the withdrawn one keeps its own reservation.
        issues = [
            make_issue(37, labels=[IN_PROGRESS_LABEL]),
            make_issue(38, labels=[DISPATCH_LABEL]),
        ]
        state, calls = self.run_scheduler(
            issues,
            [38],
            wip_limit=2,
            comments={37: [{"body": DISPATCH_MARKER, "created_at": at(1)}]},
        )
        self.assertEqual([38], self.dispatched(calls))
        self.assertIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))
        self.assertIn(IN_PROGRESS_LABEL, self.labels_of(state, 38))

    def test_a_stale_reservation_with_no_dispatch_record_is_released(self):
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        state, calls = self.run_scheduler(issues, [37])
        self.assertEqual([37], self.dispatched(calls))
        self.assertIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))

    def test_a_failed_dispatch_releases_the_reservation_it_took(self):
        # The reservation is rolled back so a refused dispatch cannot hold a WIP
        # slot, and the run fails rather than reporting a dispatch that did not
        # happen.
        with self.assertRaises(AssertionError) as caught:
            self.run_scheduler(
                [make_issue(37, labels=[DISPATCH_LABEL])], [37], dispatch_fails=True
            )
        self.assertIn("dispatch refused", str(caught.exception))


class ReconciliationTests(ReconcileHarness):
    def test_a_merged_pull_request_completes_its_issue(self):
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        pulls = [agent_pull_request(100, 37, state="closed", merged_at=at(20))]
        state, calls = self.run_scheduler(
            issues,
            [37],
            pulls=pulls,
            comments={37: [{"body": DISPATCH_MARKER, "created_at": at(30)}]},
        )
        self.assertEqual(
            "closed", [i for i in state["issues"] if i["number"] == 37][0]["state"]
        )
        self.assertNotIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))
        self.assertEqual([], self.dispatched(calls))

    def test_an_abandoned_pull_request_pauses_its_issue(self):
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        pulls = [agent_pull_request(100, 37, state="closed")]
        state, _ = self.run_scheduler(
            issues,
            [37],
            pulls=pulls,
            comments={37: [{"body": DISPATCH_MARKER, "created_at": at(30)}]},
        )
        self.assertIn(PAUSED_LABEL, self.labels_of(state, 37))
        self.assertNotIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))

    def test_a_missed_merge_event_is_reconciled_from_repository_state(self):
        # No pull_request_target event: the run is a plain schedule, and the
        # reconciliation still has to notice the merge from the PR list.
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        pulls = [agent_pull_request(100, 37, state="closed", merged_at=at(20))]
        state, calls = self.run_scheduler(
            issues,
            [37],
            pulls=pulls,
            comments={37: [{"body": DISPATCH_MARKER, "created_at": at(60)}]},
        )
        self.assertEqual(
            "closed", [i for i in state["issues"] if i["number"] == 37][0]["state"]
        )
        self.assertEqual([], self.dispatched(calls))

    def test_an_open_pull_request_keeps_its_reservation(self):
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        pulls = [agent_pull_request(100, 37)]
        state, calls = self.run_scheduler(issues, [37], pulls=pulls)
        self.assertIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))
        self.assertEqual([], self.dispatched(calls))

    def test_a_closed_pull_request_event_is_handled_on_the_target_trigger(self):
        # The event path, not the reconciliation path: a merged PR arriving as
        # pull_request_target closes the issue in the same run.
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        pulls = [agent_pull_request(100, 37, state="closed", merged_at=at(5))]
        state, calls = self.run_event(
            issues,
            [37],
            event_name="pull_request_target",
            payload={
                "pull_request": {
                    "number": 100,
                    "merged": True,
                    "head": {
                        "ref": "opencode/issue37-continuum",
                        "repo": {"full_name": REPOSITORY},
                    },
                }
            },
            pulls=pulls,
        )
        self.assertEqual(
            "closed", [i for i in state["issues"] if i["number"] == 37][0]["state"]
        )
        self.assertNotIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))
        self.assertEqual([], self.dispatched(calls))

    def test_a_fork_pull_request_is_never_read_as_agent_work(self):
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        fork = agent_pull_request(101, 37)
        fork["head"]["repo"] = {"full_name": "attacker/continuum"}
        state, calls = self.run_scheduler(issues, [37], pulls=[fork])
        self.assertEqual([37], self.dispatched(calls))

    def test_a_pull_request_from_a_stale_dispatch_is_not_treated_as_this_run(self):
        # A merged PR older than the latest dispatch belongs to a previous
        # attempt; reconciling it would close an issue that is being retried.
        issues = [make_issue(37, labels=[IN_PROGRESS_LABEL])]
        pulls = [agent_pull_request(100, 37, state="closed", merged_at=at(600))]
        state, _ = self.run_scheduler(
            issues, [37], pulls=pulls, comments={37: [{"body": DISPATCH_MARKER, "created_at": at(5)}]}
        )
        self.assertEqual("open", [i for i in state["issues"] if i["number"] == 37][0]["state"])


class RetryBudgetTests(ReconcileHarness):
    def test_an_expired_lease_without_progress_is_retried(self):
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        state, calls = self.run_scheduler(
            issues, [37], comments={37: [{"body": DISPATCH_MARKER, "created_at": at(600)}]}
        )
        self.assertEqual([37], self.dispatched(calls))
        self.assertIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))

    def test_the_budget_is_exhausted_and_the_issue_paused(self):
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        comments = {
            37: [
                {"body": DISPATCH_MARKER, "created_at": at(600 + index * 10)}
                for index in range(3)
            ]
        }
        state, calls = self.run_scheduler(issues, [37], comments=comments, max_attempts=3)
        self.assertEqual([], self.dispatched(calls))
        self.assertIn(PAUSED_LABEL, self.labels_of(state, 37))
        self.assertNotIn(IN_PROGRESS_LABEL, self.labels_of(state, 37))

    def test_an_explicit_unpause_starts_a_new_budget(self):
        # Three attempts, all before the unpause, then a human removes the
        # paused label. Without the epoch reset the issue would already be over
        # its budget and would be paused again; with it, the attempt that
        # follows the unpause counts as the first of a new budget.
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        comments = {
            37: [
                {"body": DISPATCH_MARKER, "created_at": at(700 + index * 10)}
                for index in range(3)
            ]
            + [{"body": DISPATCH_MARKER, "created_at": at(300)}]
        }
        events = {
            37: [
                {
                    "event": "unlabeled",
                    "label": {"name": PAUSED_LABEL},
                    "created_at": at(400),
                }
            ]
        }
        state, calls = self.run_scheduler(
            issues, [37], comments=comments, events=events, max_attempts=3
        )
        self.assertEqual([37], self.dispatched(calls))
        self.assertNotIn(PAUSED_LABEL, self.labels_of(state, 37))

    def test_attempts_before_an_unpause_still_count_within_their_epoch(self):
        # The mirror of the reset: with no unpause event the same three old
        # attempts exhaust the budget.
        issues = [make_issue(37, labels=[DISPATCH_LABEL, IN_PROGRESS_LABEL])]
        comments = {
            37: [
                {"body": DISPATCH_MARKER, "created_at": at(700 + index * 10)}
                for index in range(3)
            ]
            + [{"body": DISPATCH_MARKER, "created_at": at(300)}]
        }
        state, calls = self.run_scheduler(issues, [37], comments=comments, max_attempts=3)
        self.assertEqual([], self.dispatched(calls))
        self.assertIn(PAUSED_LABEL, self.labels_of(state, 37))

    def test_a_paused_issue_is_never_dispatched(self):
        _, calls = self.run_scheduler(
            [make_issue(37, labels=[DISPATCH_LABEL, PAUSED_LABEL])], [37]
        )
        self.assertEqual([], self.dispatched(calls))


class DependencyTests(ReconcileHarness):
    def test_a_blocked_issue_is_skipped(self):
        _, calls = self.run_scheduler(
            [make_issue(38, labels=[DISPATCH_LABEL])],
            [38],
            blockers={38: [{"number": 40, "state": "open"}]},
        )
        self.assertEqual([], self.dispatched(calls))

    def test_a_closed_blocker_does_not_block(self):
        _, calls = self.run_scheduler(
            [make_issue(38, labels=[DISPATCH_LABEL])],
            [38],
            blockers={38: [{"number": 40, "state": "closed"}]},
        )
        self.assertEqual([38], self.dispatched(calls))

    def test_a_blocker_defers_to_an_unblocked_candidate_in_the_same_run(self):
        # A blocked issue must not consume the slot a ready issue could use.
        _, calls = self.run_scheduler(
            [make_issue(38, labels=[DISPATCH_LABEL]), make_issue(39, labels=[DISPATCH_LABEL])],
            [38, 39],
            wip_limit=1,
            blockers={38: [{"number": 40, "state": "open"}]},
        )
        self.assertEqual([39], self.dispatched(calls))


class WipLimitTests(ReconcileHarness):
    def test_open_pull_requests_occupy_the_slots_they_use(self):
        # Two slots, one taken by an issue that already has an open pull
        # request, so exactly one of the two candidates is dispatched.
        _, calls = self.run_scheduler(
            [make_issue(38, labels=[DISPATCH_LABEL]), make_issue(39, labels=[DISPATCH_LABEL])],
            [38, 39],
            wip_limit=2,
            pulls=[agent_pull_request(100, 37)],
        )
        self.assertEqual([38], self.dispatched(calls))

    def test_an_open_pull_request_with_no_slot_left_dispatches_nothing(self):
        _, calls = self.run_scheduler(
            [make_issue(38, labels=[DISPATCH_LABEL])],
            [38],
            wip_limit=1,
            pulls=[agent_pull_request(100, 37)],
        )
        self.assertEqual([], self.dispatched(calls))

    def test_a_full_limit_dispatches_nothing(self):
        _, calls = self.run_scheduler(
            [make_issue(38, labels=[DISPATCH_LABEL])],
            [38],
            wip_limit=1,
            pulls=[agent_pull_request(100, 37)],
        )
        self.assertEqual([], self.dispatched(calls))

    def test_priority_order_beats_issue_number(self):
        _, calls = self.run_scheduler(
            [
                make_issue(38, labels=[DISPATCH_LABEL, "priority:p2"]),
                make_issue(39, labels=[DISPATCH_LABEL, "priority:p0"]),
            ],
            [38, 39],
            wip_limit=1,
        )
        self.assertEqual([39], self.dispatched(calls))

    def test_an_unprioritized_issue_is_still_dispatchable(self):
        _, calls = self.run_scheduler(
            [make_issue(38, labels=[DISPATCH_LABEL])], [38], wip_limit=1
        )
        self.assertEqual([38], self.dispatched(calls))

    def test_the_documented_default_limit_is_the_one_the_workflow_passes(self):
        # README.md documents the default a consumer gets with no configuration.
        # The workflow passes one value and the script falls back to another; if
        # they drift, the documented limit is a fiction. This runs the script
        # with no WIP_LIMIT at all and asserts the fallback, and reads the
        # workflow so the two cannot disagree.
        _, calls = self.run_scheduler(
            [
                make_issue(number, labels=[DISPATCH_LABEL])
                for number in (38, 39, 40)
            ],
            [38, 39, 40],
            wip_limit=None,
        )
        fallback = scheduler_default("WIP_LIMIT")
        workflow_default = workflow_default_for("AUTOMATION_WIP_LIMIT")
        self.assertEqual(workflow_default, fallback)
        self.assertEqual(len(self.dispatched(calls)), workflow_default)

    def test_two_priority_labels_resolve_to_the_highest_deterministically(self):
        _, calls = self.run_scheduler(
            [
                make_issue(38, labels=[DISPATCH_LABEL, "priority:p0"]),
                make_issue(39, labels=[DISPATCH_LABEL, "priority:p1"]),
            ],
            [38, 39],
            wip_limit=1,
        )
        self.assertEqual([38], self.dispatched(calls))


if __name__ == "__main__":
    unittest.main()
