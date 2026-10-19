"""Parent-local vs delegated scheduling isolation (kodmial/continuum#326).

Pins the executable decision that an unavailable delegated Child inventory
never blocks Parent-local scheduling, is never reported as an empty
successful inventory, and that a trusted inventory is only ever reused when
it is authenticated, scoped and within its TTL.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from continuum.delegation_discovery import (
    DELEGATION_DEGRADED,
    DELEGATION_NONE,
    DELEGATION_OK,
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    InventoryCache,
    allowlist_fingerprint,
    deletion_evidence,
    resolve_child_inventory,
)


class ChildDispatchDecisionTest(unittest.TestCase):
    def test_ok_empty_inventory_is_a_genuine_success(self):
        decision = resolve_child_inventory(DELEGATION_OK)
        self.assertTrue(decision.enabled)
        self.assertFalse(decision.degraded)

    def test_degraded_disables_children_without_fabricating(self):
        decision = resolve_child_inventory(
            DELEGATION_DEGRADED,
            [{"id": "alpha", "repository": "owner/child"}],
        )
        self.assertFalse(decision.enabled)
        self.assertTrue(decision.degraded)
        self.assertIn("unavailable", decision.reason)

    def test_none_is_not_degraded(self):
        decision = resolve_child_inventory(DELEGATION_NONE)
        self.assertFalse(decision.enabled)
        self.assertFalse(decision.degraded)

    def test_unknown_state_fails_closed_as_degraded(self):
        decision = resolve_child_inventory("unexpected")
        self.assertFalse(decision.enabled)
        self.assertTrue(decision.degraded)

    def test_evidence_never_names_a_child(self):
        for state in (DELEGATION_OK, DELEGATION_DEGRADED, DELEGATION_NONE, "x"):
            line = deletion_evidence(state, state == DELEGATION_OK)
            self.assertNotIn("owner/", line)
            self.assertIn("delegation=", line)


class InventoryCacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "inventory.json")
        self.plan = [{"id": "alpha", "repository": "owner/private-child"}]
        self.allowlist = '["alpha"]'

    def write(self, now=1000, plan=None):
        InventoryCache(self.path).write(
            parent_repository="owner/parent",
            allowlist_json=self.allowlist,
            plan=self.plan if plan is None else plan,
            now=now,
        )

    def test_round_trip_within_ttl(self):
        self.write(now=1000)
        cache = InventoryCache(self.path)
        self.assertEqual(
            cache.read(parent_repository="owner/parent",
                       allowlist_json=self.allowlist, now=1000),
            self.plan,
        )

    def test_expired_entry_is_refused(self):
        self.write(now=1000)
        cache = InventoryCache(self.path, ttl=DEFAULT_TTL_SECONDS)
        self.assertIsNone(cache.read(
            parent_repository="owner/parent", allowlist_json=self.allowlist,
            now=1000 + DEFAULT_TTL_SECONDS + 1))

    def test_different_parent_or_allowlist_is_refused(self):
        self.write(now=1000)
        cache = InventoryCache(self.path)
        self.assertIsNone(cache.read(
            parent_repository="owner/other", allowlist_json=self.allowlist, now=1000))
        self.assertIsNone(cache.read(
            parent_repository="owner/parent", allowlist_json='["beta"]', now=1000))

    def test_tampered_or_malformed_cache_is_refused(self):
        self.write(now=1000)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"schema": 99, "parent": "owner/parent",
                       "scope": allowlist_fingerprint("owner/parent", self.allowlist),
                       "created_at": 1000, "plan": self.plan}, handle)
        self.assertIsNone(InventoryCache(self.path).read(
            parent_repository="owner/parent", allowlist_json=self.allowlist, now=1000))

        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "parent": "owner/parent",
                       "scope": allowlist_fingerprint("owner/parent", self.allowlist),
                       "created_at": 1000, "plan": "not-a-plan"}, handle)
        self.assertIsNone(InventoryCache(self.path).read(
            parent_repository="owner/parent", allowlist_json=self.allowlist, now=1000))

    def test_refuses_to_cache_a_malformed_plan(self):
        with self.assertRaises(ValueError):
            InventoryCache(self.path).write(
                parent_repository="owner/parent", allowlist_json=self.allowlist,
                plan=[{"id": "alpha"}], now=1000)

    def test_ttl_is_bounded(self):
        cache = InventoryCache(self.path, ttl=10 ** 9)
        self.assertEqual(cache.ttl, MAX_TTL_SECONDS)

    def test_fingerprint_binds_parent_and_allowlist(self):
        self.assertEqual(
            allowlist_fingerprint("owner/parent", '["a"]'),
            allowlist_fingerprint("owner/parent", '["a"]'),
        )
        self.assertNotEqual(
            allowlist_fingerprint("owner/parent", '["a"]'),
            allowlist_fingerprint("owner/parent", '["b"]'),
        )
        self.assertNotEqual(
            allowlist_fingerprint("owner/a", '["a"]'),
            allowlist_fingerprint("owner/b", '["a"]'),
        )


if __name__ == "__main__":
    unittest.main()
