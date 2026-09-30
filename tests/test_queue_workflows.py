"""The workflow wiring is part of the liveness contract.

`continuum review queue` only helps if the events that change eligibility
actually start a reconciliation, and only if two controllers cannot spend the
same shared provider slot. Those are properties of the YAML, not of Python, so
they are asserted here: a workflow can fail to parse, can silently drop a
trigger, or can gain a second concurrent controller, and no unit test of the
reconciler would notice.
"""

from __future__ import annotations

import pathlib
import re
import unittest

from continuum.review import queue

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHARED = ROOT / ".github" / "workflows" / "review-queue.yml"
CONSUMER = ROOT / "fixtures" / "consumer-repo" / ".github" / "workflows" / "continuum.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"

# The reconciler is triggered by `pull_request_target` (trusted metadata, no
# pull request code is ever checked out) but reasons about a pull request event.
PLATFORM_TO_QUEUE_EVENT = {"pull_request_target": queue.EVENT_PULL_REQUEST}

# Events whose payload the reconciler reads through the API. Every one of them
# can change *who the next eligible candidate is*, which is the whole point.
REQUIRED_TRIGGERS = {
    "pull_request_target": queue.PULL_REQUEST_ACTIONS,
    "pull_request_review": queue.PULL_REQUEST_REVIEW_ACTIONS,
    "issue_comment": queue.ISSUE_COMMENT_ACTIONS,
    "status": (),
    "check_run": ("completed", "rerequested", "created"),
    "check_suite": ("completed",),
    "workflow_run": ("completed",),
    "schedule": (),
    "workflow_dispatch": (),
}


def declared_types(text: str, trigger: str) -> tuple:
    """The `types:` filter declared for `trigger`.

    An empty tuple means the trigger is declared without a filter, so every
    action of that event is a wake-up. Only the two spellings GitHub accepts in
    the repository's own workflows (block list and flow list) are understood;
    anything else raises, so this helper cannot silently agree with a file it did
    not really read.
    """

    lines = text.splitlines()
    start = None
    indent = 0
    for index, line in enumerate(lines):
        body = line.split("#", 1)[0].rstrip()
        key, separator, tail = body.partition(":")
        if not separator or key.strip() != trigger:
            continue
        tail = tail.strip()
        if tail == "{}":
            # `status: {}` subscribes to every action of the event.
            return ()
        if tail:
            raise AssertionError(f"unsupported inline value for {trigger!r}: {tail!r}")
        start = index + 1
        indent = len(line) - len(line.lstrip())
        break
    if start is None:
        raise AssertionError(f"trigger {trigger!r} is not declared")

    types = []
    in_types = False
    for line in lines[start:]:
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if body.startswith("types:"):
            value = body[len("types:") :].strip()
            if value.startswith("["):
                if not value.endswith("]"):
                    raise AssertionError(f"unterminated flow list for {trigger!r}")
                return tuple(item.strip() for item in value[1:-1].split(",") if item.strip())
            in_types = True
            continue
        if in_types and body.startswith("- "):
            types.append(body[2:].strip())
            continue
        in_types = False
    return tuple(types)


class WakeUpTriggerTests(unittest.TestCase):
    def setUp(self):
        self.shared = SHARED.read_text(encoding="utf-8")
        self.consumer = CONSUMER.read_text(encoding="utf-8")

    def test_every_pull_request_transition_is_a_wake_up(self):
        declared = declared_types(self.shared, "pull_request_target")
        self.assertEqual(set(declared), set(queue.PULL_REQUEST_ACTIONS))
        # The two transitions whose absence is the reported stall.
        for action in ("closed", "converted_to_draft", "synchronize", "labeled"):
            self.assertIn(action, declared)
            self.assertTrue(queue.is_queue_wake_up("pull_request_target", action))

    def test_provider_and_ci_transitions_are_wake_ups(self):
        for trigger, actions in REQUIRED_TRIGGERS.items():
            declared = declared_types(self.shared, trigger)
            if not actions:
                # Declared without a filter: every action wakes the queue.
                self.assertEqual(declared, ())
            else:
                self.assertEqual(set(declared), set(actions), trigger)

    def test_the_wake_up_vocabulary_cannot_outlive_its_workflow(self):
        # Every event the vocabulary calls a wake-up must be something GitHub
        # actually sends to this workflow, and vice versa.
        for event in queue.WAKE_UP_ACTIONS:
            trigger = next(
                (name for name, value in PLATFORM_TO_QUEUE_EVENT.items() if value == event),
                event,
            )
            if event == queue.EVENT_PUSH:
                # A push cannot change a candidate's review need; it is accepted
                # so a wake-up is never lost, but it is not subscribed to.
                continue
            declared_types(self.shared, trigger)

    def test_a_forks_event_never_spends_the_shared_slot(self):
        # A fork's HEAD is unreachable by a repository-scoped provider, so the
        # job must be skipped rather than reconcile a candidate it cannot review.
        self.assertIn("head.repo.full_name == github.repository", self.shared)
        self.assertIn("pull_request_target", self.consumer)

    def test_the_reconciler_is_single_flight(self):
        # The common concurrency group prevents two controllers from spending
        # the same provider slot.
        for name, text in (("shared", self.shared), ("consumer", self.consumer)):
            self.assertIn("group: continuum-review-queue", text, name)

    def test_a_waiting_reconciler_cannot_be_starved(self):
        # A run waiting out provider cooldown must not be superseded by every
        # subsequent wake-up. The reconciler recomputes GitHub state when it runs,
        # so serialization is safe and cancellation is unnecessary.
        for name, text in (("shared", self.shared), ("consumer", self.consumer)):
            self.assertIn("cancel-in-progress: false", text, name)
            self.assertNotIn("cancel-in-progress: true", text, name)
        timeout = re.search(r"^\s*timeout-minutes: (\d+)\s*$", self.shared, re.MULTILINE)
        self.assertIsNotNone(timeout)
        self.assertGreaterEqual(int(timeout.group(1)), 5)

    def test_the_job_runs_the_reconciler_and_its_config_gate(self):
        self.assertIn("continuum.cli config-check", self.shared)
        self.assertIn("continuum.cli queue reconcile", self.shared)
        self.assertIn("--wait-minutes", self.shared)
        # The run's own timeout must bound the in-run wait loop, or a stale
        # cooldown turns every event into a job that never ends.
        timeout = re.search(r"^\s*timeout-minutes: (\d+)\s*$", self.shared, re.MULTILINE)
        self.assertIsNotNone(timeout)
        self.assertGreaterEqual(int(timeout.group(1)), 5)

    def test_no_pull_request_code_is_checked_out(self):
        # Only the module and the configuration are needed. Checking out a
        # candidate's HEAD would execute its code in a privileged job.
        self.assertEqual(self.shared.count("uses: actions/checkout@"), 1)
        for line in self.shared.splitlines():
            if "actions/checkout@" in line:
                continue
            self.assertNotIn("github.event.pull_request.head.ref", line)

    def test_the_consumer_entry_delegates_and_stays_thin(self):
        # One call to the release entrypoint, not a pin to the queue's own
        # workflow: a consumer has one Continuum dependency, and the pin names
        # the release rather than one surface inside it.
        self.assertRegex(
            self.consumer,
            r"uses: kodmial/continuum/\.github/workflows/consumer\.yml@v\d+\.\d+\.\d+\b",
        )
        self.assertNotIn("review-queue.yml", self.consumer)
        self.assertNotIn("python3 -m continuum", self.consumer)
        # Dispatching a provider gate run needs the token to write actions.
        self.assertIn("actions: write", self.consumer)
        self.assertIn("continuum_token: ${{ secrets.GITHUB_TOKEN }}", self.consumer)
        # The consumer owns the event set, so it must match the shared one.
        for trigger in REQUIRED_TRIGGERS:
            self.assertEqual(
                declared_types(self.consumer, trigger),
                declared_types(self.shared, trigger),
                trigger,
            )


class BoundaryTests(unittest.TestCase):
    def test_the_queue_workflow_is_inside_the_active_boundary(self):
        allowlist = re.search(r"allowed='([^']+)'", CI.read_text(encoding="utf-8"))
        self.assertIsNotNone(allowlist)
        self.assertIn("review-queue", allowlist.group(1))

    def test_the_reconciler_is_reusable(self):
        text = SHARED.read_text(encoding="utf-8")
        self.assertEqual(declared_types(text, "workflow_call"), ())
        self.assertIn("continuum_token:", text)


if __name__ == "__main__":
    unittest.main()
