"""Deterministic admission fixture for the Quick Start.

Run from the Continuum checkout:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
      python3 -m unittest discover -s examples/minimal-consumer/verification -v
"""

from datetime import datetime, timezone
import unittest

from continuum.scheduler_reconcile import IssueState, SchedulerConfig, reconcile

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def reconcile_one(config, labels):
    issues = {
        1: IssueState(
            number=1,
            title="Quick Start issue",
            body="A harmless test issue.",
            labels=set(labels),
            is_open=True,
        )
    }
    result = reconcile(
        NOW,
        issues,
        set(),
        set(),
        {},
        {},
        {},
        set(),
        config,
    )
    return result, issues[1]


class ReadyAdmissionTest(unittest.TestCase):
    def test_unadmitted_issue_is_not_dispatched(self):
        result, issue = reconcile_one(
            SchedulerConfig(ready_label="automation:ready"),
            labels=set(),
        )

        self.assertEqual([], result.dispatched)
        self.assertFalse(result.failed)
        self.assertIn("missing required ready label", result.skip_reasons[1])
        self.assertNotIn("automation:in-progress", issue.labels)

    def test_admitted_issue_is_dispatched(self):
        result, issue = reconcile_one(
            SchedulerConfig(ready_label="automation:ready"),
            labels={"automation:ready"},
        )

        self.assertEqual([1], result.dispatched)
        self.assertIn("automation:in-progress", issue.labels)

    def test_empty_ready_label_makes_unlabelled_issues_eligible(self):
        result, issue = reconcile_one(
            SchedulerConfig(ready_label=""),
            labels=set(),
        )

        self.assertEqual([1], result.dispatched)
        self.assertIn("automation:in-progress", issue.labels)


if __name__ == "__main__":
    unittest.main()
