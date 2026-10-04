#!/usr/bin/env python3
"""Contract tests for the unified local/delegated priority normalization."""

from __future__ import annotations

import unittest

from continuum.delegation_priority import (
    UNPRIORITIZED_RANK,
    effective_priority,
    labels_to_add,
    title_priority,
)


class DelegationPriorityTests(unittest.TestCase):
    def test_local_and_delegated_p1_title_without_label_match(self):
        local = effective_priority([], "P1: migrate backlog")
        delegated = effective_priority([], "P1: child task")
        self.assertEqual(local, ("priority:p1", 1))
        self.assertEqual(delegated, local)

    def test_explicit_label_wins_over_title(self):
        self.assertEqual(
            effective_priority(["priority:p2"], "P0: urgent"),
            ("priority:p2", 2),
        )

    def test_no_prefix_no_label_stays_unprioritized(self):
        priority, rank = effective_priority([], "Plain task title")
        self.assertIsNone(priority)
        self.assertEqual(rank, UNPRIORITIZED_RANK)
        self.assertEqual(rank, 3)

    def test_p0_p1_p2_ordering_is_identical(self):
        ranks = [
            effective_priority([], "P0: critical")[1],
            effective_priority([], "P1: high")[1],
            effective_priority([], "P2: normal")[1],
            effective_priority([], "Untitled work")[1],
        ]
        self.assertEqual(ranks, [0, 1, 2, 3])
        mixed = [
            (effective_priority(["priority:p2"], "anything"), "local-labelled"),
            (effective_priority([], "P0: child"), "delegated-title"),
            (effective_priority([], "P1: local"), "local-title"),
            (effective_priority(["priority:p0"], "P2: stale"), "label-wins"),
        ]
        ordered = sorted(mixed, key=lambda item: (item[0][1], item[1]))
        self.assertEqual(
            [name for _, name in ordered],
            ["delegated-title", "label-wins", "local-title", "local-labelled"],
        )

    def test_multiple_labels_resolve_to_highest(self):
        self.assertEqual(
            effective_priority(["priority:p2", "priority:p0"], "P1: title"),
            ("priority:p0", 0),
        )

    def test_title_never_overrides_explicit_label(self):
        for label in ("priority:p0", "priority:p1", "priority:p2"):
            with self.subTest(label=label):
                priority, _ = effective_priority([label], "P0: title")
                self.assertEqual(priority, label)

    def test_normalization_is_idempotent_without_duplicates(self):
        first = labels_to_add([], "P1: child task")
        self.assertEqual(first, ["priority:p1"])
        second = labels_to_add(["priority:p1"], "P1: child task")
        self.assertEqual(second, [])
        # Applying the persisted label yields the same effective priority.
        self.assertEqual(
            effective_priority(["priority:p1"], "P1: child task"),
            effective_priority([], "P1: child task"),
        )

    def test_title_prefix_requires_trailing_whitespace(self):
        self.assertIsNone(title_priority("P1:hello"))
        self.assertEqual(title_priority("P1: hello"), "priority:p1")
        self.assertEqual(title_priority("p2:  spaced"), "priority:p2")

    def test_unsupported_prefixes_stay_unprioritized(self):
        for title in ("P3: future", "P: vague", "[P1] brackets", "  P1: indented"):
            with self.subTest(title=title):
                self.assertIsNone(title_priority(title))
                priority, rank = effective_priority([], title)
                self.assertIsNone(priority)
                self.assertEqual(rank, 3)


if __name__ == "__main__":
    unittest.main()
