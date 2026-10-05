"""Mandatory regressions for the authoritative PR lifecycle (#282).

Proves the Definition of Done items that are decidable deterministically:
branding-independent command recognition, exact-HEAD safety, stale-history
supersession, WAIT vs ERROR separation, idempotent duplicate wakeups without
full-history scans, caller permission validation, and parent privacy.
"""

from __future__ import annotations

import unittest

from continuum import pr_lifecycle as pl

REPO = "acme/web"
HEAD_A = "a" * 40
HEAD_B = "b" * 40


def state(head=HEAD_A, **over):
    base = pl.LifecycleState(repo=REPO, pr=7, head=head, generation=3,
                             phase=pl.WAITING_REVIEW, provider="coderabbit",
                             last_event="e0")
    return base if not over else __import__("dataclasses").replace(base, **over)


def facts(head=HEAD_A, **over):
    params = {"event": "cron", "head": head}
    params.update(over)
    return pl.LifecycleFacts(**params)


class BrandingIndependenceTests(unittest.TestCase):
    def test_attribution_emoji_change_keeps_command_recognized(self):
        plain = "@coderabbitai full review"
        branded = (
            "@coderabbitai full review\n"
            "\u26a1 **Continuum \u00b7 scheduler**\n"
            "<!-- continuum-origin role=continuum component=scheduler -->\n"
            "Some prose with emoji \U0001f680 and extra branding."
        )
        other_brand = (
            "@coderabbitai full review\n"
            "\U0001f9be **Agent Coder \u00b7 repair**\n"
            "<!-- continuum-origin role=agent-coder component=repair -->\n"
        )
        self.assertTrue(pl.is_coderabbit_command_in_flight(plain))
        self.assertTrue(pl.is_coderabbit_command_in_flight(branded))
        self.assertTrue(pl.is_coderabbit_command_in_flight(other_brand))

    def test_prose_mention_is_not_a_command(self):
        self.assertFalse(pl.is_coderabbit_command_in_flight(
            "We asked for a full review yesterday @coderabbitai thanks"))
        self.assertFalse(pl.is_coderabbit_command_in_flight(
            "> @coderabbitai full review"))
        self.assertFalse(pl.is_coderabbit_command_in_flight(""))

    def test_state_marker_ignores_visible_prose(self):
        st = state()
        body = ("\u26a1 **Continuum \u00b7 scheduler**\n"
                + pl.render_state_marker(st)
                + "\nHuman prose that must never be parsed.")
        parsed = pl.parse_state_marker(body)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.phase, st.phase)
        self.assertEqual(parsed.head, HEAD_A)


class InFlightDedupeTests(unittest.TestCase):
    def test_one_branded_command_counts_in_flight_and_duplicates_coalesce(self):
        prior = state(phase=pl.REVIEW_IN_FLIGHT, review_attempt=1,
                      last_event="cmd1")
        bodies = ["@coderabbitai full review\n"
                  "\u26a1 **Continuum \u00b7 scheduler**\n"
                  "<!-- continuum-origin role=continuum component=scheduler -->"]
        self.assertTrue(any(pl.is_coderabbit_command_in_flight(b)
                            for b in bodies))
        dup = pl.reduce(prior, facts(event="workflow_run", head=HEAD_A,
                                    event_id="cmd1", ci="unknown"))
        self.assertEqual(dup.decision.action, "noop")
        self.assertFalse(dup.decision.emit_side_effect)
        self.assertFalse(dup.decision.workflow_failure)

    def test_second_wakeup_while_in_flight_waits_without_side_effect(self):
        prior = state(phase=pl.REVIEW_IN_FLIGHT, review_attempt=1,
                      last_event="e1")
        out = pl.reduce(prior, facts(event="cron", head=HEAD_A, event_id="e2",
                                    ci="unknown"))
        self.assertFalse(out.decision.emit_side_effect)
        self.assertFalse(out.decision.workflow_failure)
        self.assertIn(out.decision.action, ("wait", "noop"))


class ExactHeadSafetyTests(unittest.TestCase):
    def test_old_command_label_status_cannot_authorize_new_head(self):
        prior = state(head=HEAD_A, phase=pl.REVIEW_IN_FLIGHT,
                      review_attempt=1, last_event="old")
        out = pl.reduce(prior, facts(event="push", head=HEAD_B,
                                    event_id="push-new", ci="unknown"))
        self.assertEqual(out.state.head, HEAD_B)
        self.assertEqual(out.state.generation, prior.generation + 1)
        self.assertEqual(out.state.review_attempt, 0)
        self.assertEqual(out.state.phase, pl.WAITING_CI)
        # An old-HEAD command marker never counts for the new HEAD.
        self.assertFalse(pl.has_exact_head_command(
            [pl.coderabbit_command_marker(HEAD_A)], HEAD_B))

    def test_stale_review_fact_for_old_head_is_ignored(self):
        prior = state(phase=pl.WAITING_REVIEW, last_event="e1")
        stale = pl.ReviewFact(provider="coderabbit", decision="approved",
                              head=HEAD_A)
        out = pl.reduce(prior, facts(event="review", head=HEAD_B,
                                    event_id="e2", ci="pass",
                                    mergeable="clean", review=stale))
        # Stale review dropped -> new generation waits for CI, never merges.
        self.assertEqual(out.state.head, HEAD_B)
        self.assertNotEqual(out.decision.action, "merge")

    def test_current_exact_head_review_supersedes_stale_history(self):
        prior = state(phase=pl.WAITING_RECHECK, last_event="e1",
                      finding="f" * 64)
        current = pl.ReviewFact(provider="pr-agent", decision="approved",
                                head=HEAD_A)
        out = pl.reduce(prior, facts(event="review", head=HEAD_A,
                                    event_id="e2", ci="pass",
                                    mergeable="clean", review=current))
        self.assertEqual(out.decision.action, "merge")
        self.assertTrue(out.decision.emit_side_effect)

    def test_legacy_evidence_only_honored_for_exact_head(self):
        good = pl.coerce_legacy_evidence(
            {"head": HEAD_A, "decision": "approved", "provider": "coderabbit"},
            HEAD_A)
        self.assertIsNotNone(good)
        stale = pl.coerce_legacy_evidence(
            {"head": HEAD_A, "decision": "approved"}, HEAD_B)
        self.assertIsNone(stale)


class WaitVsErrorTests(unittest.TestCase):
    def test_quota_wait_is_wait_not_failure(self):
        prior = state(last_event="e1")
        review = pl.ReviewFact(provider="coderabbit", decision="quota_wait",
                               head=HEAD_A, quota_not_before=999)
        out = pl.reduce(prior, facts(event="review", head=HEAD_A,
                                    event_id="e2", ci="pass",
                                    mergeable="clean", review=review))
        self.assertEqual(out.state.phase, pl.RATE_LIMITED)
        self.assertFalse(out.decision.workflow_failure)
        self.assertNotEqual(out.decision.action, "error")

    def test_wip_full_and_computing_are_healthy_waits(self):
        prior = state(last_event="e1")
        wip = pl.reduce(prior, facts(event="cron", head=HEAD_A, event_id="e2",
                                    wip_full=True))
        self.assertFalse(wip.decision.workflow_failure)
        computing = pl.reduce(prior, facts(event="status", head=HEAD_A,
                                          event_id="e3",
                                          mergeable="computing"))
        self.assertFalse(computing.decision.workflow_failure)

    def test_malformed_and_config_failures_are_errors(self):
        prior = state(last_event="e1")
        bad_config = pl.reduce(
            prior, facts(event="cron", head=HEAD_A, event_id="e2",
                         config_valid=False))
        self.assertTrue(bad_config.decision.workflow_failure)
        self.assertEqual(bad_config.decision.action, "error")
        no_auth = pl.reduce(
            prior, facts(event="cron", head=HEAD_A, event_id="e3",
                         authenticated=False))
        self.assertTrue(no_auth.decision.workflow_failure)


class IdempotencyTests(unittest.TestCase):
    def test_duplicate_wakeups_emit_at_most_one_side_effect(self):
        prior = state(phase=pl.WAITING_REVIEW, last_event="e1")
        first = pl.reduce(prior, facts(event="review_comment", head=HEAD_A,
                                      event_id="e2", ci="pass",
                                      mergeable="clean", review=None))
        self.assertTrue(first.decision.emit_side_effect)
        key = first.decision.idempotency_key
        self.assertEqual(key, f"{REPO}:7:{HEAD_A}:{first.state.phase}:"
                              f"{first.state.generation}")
        emitted = {key}
        # Replay of the same event identity coalesces.
        replay = pl.reduce(first.state, facts(event="review_comment",
                                             head=HEAD_A, event_id="e2",
                                             ci="pass", mergeable="clean"))
        self.assertFalse(replay.decision.emit_side_effect)
        self.assertIn(replay.decision.idempotency_key, emitted |
                      {replay.decision.idempotency_key})

    def test_bounded_window_only(self):
        bodies = ["noise %d" % i for i in range(100)]
        bodies.append(pl.render_state_marker(state()))
        got = pl.load_authoritative_state(bodies, repo=REPO, pr=7,
                                          max_comments=20)
        self.assertIsNotNone(got)
        # A state buried beyond the bounded window is not found: no
        # full-history scan is performed.
        buried = [pl.render_state_marker(state())] + \
            ["noise %d" % i for i in range(100)]
        self.assertIsNone(pl.load_authoritative_state(buried, repo=REPO, pr=7,
                                                      max_comments=20))
        self.assertEqual(pl.MAX_STATE_COMMENTS_SCANNED, 20)


class CredentialBoundaryTests(unittest.TestCase):
    def test_reads_use_github_token_writes_require_pat(self):
        for action in ("discover_pr", "read_comments", "read_status"):
            self.assertFalse(pl.requires_pat(action))
        for action in ("comment", "dispatch", "merge", "push",
                       "upsert_state", "status_write"):
            self.assertTrue(pl.requires_pat(action))
        self.assertTrue(pl.requires_pat("unknown-action"))

    def test_consumer_caller_permissions_validated(self):
        ok, missing = pl.validate_caller_permissions(
            {"issues": "write"}, "upsert_state")
        self.assertTrue(ok)
        self.assertEqual(missing, ())
        ok, missing = pl.validate_caller_permissions({}, "upsert_state")
        self.assertFalse(ok)
        self.assertTrue(missing)
        ok, missing = pl.validate_caller_permissions({"issues": "write"},
                                                     "nope")
        self.assertFalse(ok)


class ParentPrivacyTests(unittest.TestCase):
    def test_parent_state_never_contains_child_identity(self):
        raw = {"repo": "acme/parent", "pr": 3, "phase": pl.WAITING_REVIEW,
               "child_repo": "secret/child", "child_repository": "x/y",
               "child_id": "hidden", "head": HEAD_A}
        clean = pl.sanitize_parent_state(raw)
        text = repr(clean)
        self.assertNotIn("secret/child", text)
        self.assertNotIn("child_repo", text)
        self.assertNotIn("hidden", text)


class ProviderAdapterTests(unittest.TestCase):
    def test_both_providers_normalize_to_same_fact_shape(self):
        coderabbit = pl.normalize_coderabbit_review(
            {"decision": "APPROVED"}, HEAD_A)
        pragent = pl.normalize_pragent_review(
            {"merge_recommendation": "safe_to_merge"}, HEAD_A)
        self.assertEqual(coderabbit.decision, "approved")
        self.assertEqual(pragent.decision, "approved")
        self.assertEqual(coderabbit.head, pragent.head)
        quota_c = pl.normalize_coderabbit_review(
            {"decision": "quota_exceeded", "quota_not_before": 5}, HEAD_A)
        self.assertEqual(quota_c.decision, "quota_wait")


class MonotonicityTests(unittest.TestCase):
    def test_merged_is_terminal_within_generation(self):
        prior = state(phase=pl.MERGED, last_event="e1")
        out = pl.reduce(prior, facts(event="cron", head=HEAD_A, event_id="e2",
                                    ci="pass", mergeable="clean",
                                    review=pl.ReviewFact(
                                        provider="coderabbit",
                                        decision="approved", head=HEAD_A)))
        self.assertEqual(out.state.phase, pl.MERGED)

    def test_conflict_repair_dispatches_once_per_generation(self):
        prior = state(phase=pl.WAITING_CI, last_event="e1")
        first = pl.reduce(prior, facts(event="status", head=HEAD_A,
                                      event_id="e2", mergeable="dirty"))
        self.assertEqual(first.decision.action, "dispatch_conflict_repair")
        second = pl.reduce(first.state, facts(event="status", head=HEAD_A,
                                             event_id="e3",
                                             mergeable="dirty"))
        self.assertFalse(second.decision.emit_side_effect)


if __name__ == "__main__":
    unittest.main()
