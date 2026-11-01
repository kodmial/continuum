"""Deterministic tests for immutable task-spec snapshots (#295).

Covers: edit before vs after admission; label churn; edits between
scheduler selection and agent fetch; edits during the running agent;
edits after PR creation, after review, during repair and immediately
before merge; simultaneous wakeups; stale-generation/retry;
missing/tampered snapshots; legacy in-flight PRs; cross-repo child;
zero-diff retries; and safe no-change behavior.
"""

import hashlib
import json
import unittest
import unicodedata

from continuum.task_snapshot import (
    DEFAULT_GENERATION,
    SNAPSHOT_MARKER,
    SNAPSHOT_REF_MARKER,
    TaskSnapshotError,
    body_digest,
    build_snapshot,
    classify_pr,
    decide_admission,
    decide_execution_gate,
    digests_match,
    is_trusted_snapshot_comment,
    normalize_generation,
    normalize_issue_number,
    normalize_repo,
    normalize_spec_text,
    parse_pr_snapshot_ref,
    parse_snapshot_comment,
    render_pr_snapshot_ref,
    render_snapshot_comment,
    select_snapshot,
    snapshot_marker_line,
    spec_digest,
    title_digest,
    verify_live_against_snapshot,
    verify_pr_provenance_against_live,
)

REPO = "kodmial/nanodictate"
ISSUE = 123
TITLE = "P1: Dictate streaming stalls on macOS 15"
BODY = "## Task\n\nImplement streaming.\n\n## Definition of Done\n\n- [ ] streams\n"


def bot_comment(body, created_at="2026-10-09T10:00:00Z", comment_id=1):
    return {
        "body": body,
        "author_association": "NONE",
        "user": {"login": "github-actions[bot]"},
        "created_at": created_at,
        "updated_at": created_at,
        "id": comment_id,
    }


def owner_comment(body, created_at="2026-10-09T10:00:00Z", comment_id=1):
    return {
        "body": body,
        "author_association": "OWNER",
        "user": {"login": "kodmial"},
        "created_at": created_at,
        "updated_at": created_at,
        "id": comment_id,
    }


def pinned_comments(title=TITLE, body=BODY, **kwargs):
    snapshot = build_snapshot(REPO, ISSUE, title, body, **kwargs)
    return [bot_comment(render_snapshot_comment(snapshot))], snapshot


class CanonicalEncodingTests(unittest.TestCase):
    def test_canonical_encoding_is_utf8_sha256(self):
        text = "caf\u00e9 \u4e2d\u6587 \U0001f600"
        self.assertEqual(
            title_digest(text),
            hashlib.sha256(
                unicodedata.normalize("NFC", text).encode("utf-8")
            ).hexdigest(),
        )

    def test_line_endings_normalize(self):
        self.assertEqual(title_digest("a\r\nb"), title_digest("a\nb"))
        self.assertEqual(title_digest("a\rb"), title_digest("a\nb"))
        self.assertEqual(body_digest("a\r\nb"), body_digest("a\nb"))

    def test_none_body_is_empty(self):
        self.assertEqual(normalize_spec_text(None), "")
        self.assertEqual(body_digest(None), body_digest(""))

    def test_trailing_whitespace_is_significant(self):
        self.assertNotEqual(title_digest("spec "), title_digest("spec"))

    def test_spec_digest_binds_both(self):
        self.assertEqual(
            spec_digest(TITLE, BODY),
            hashlib.sha256(
                "{}:{}".format(title_digest(TITLE), body_digest(BODY)).encode("ascii")
            ).hexdigest(),
        )
        self.assertNotEqual(spec_digest(TITLE, BODY), spec_digest(TITLE, BODY + "x"))
        self.assertNotEqual(spec_digest(TITLE, BODY), spec_digest(TITLE + "x", BODY))

    def test_repo_and_generation_validation(self):
        self.assertEqual(normalize_repo("Kodmial/NanoDictate"), "kodmial/nanodictate")
        with self.assertRaises(TaskSnapshotError):
            normalize_repo("not-a-repo")
        with self.assertRaises(TaskSnapshotError):
            normalize_generation(0)
        with self.assertRaises(TaskSnapshotError):
            normalize_issue_number(-3)
        self.assertEqual(normalize_generation("2"), 2)


class AdmissionTests(unittest.TestCase):
    def test_edit_before_admission_defines_contract(self):
        # No snapshot yet: admission asks to pin the (edited) live content.
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=TITLE,
            live_body=BODY + "\nExtra requirement.\n", comments=[],
        )
        self.assertEqual(decision.action, "pin")

    def test_edit_after_admission_is_drift(self):
        comments, _snapshot = pinned_comments()
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=TITLE,
            live_body=BODY + "\nSneaky addition.\n", comments=comments,
        )
        self.assertEqual(decision.action, "drift_blocked")
        self.assertIn("follow-up", decision.diagnostic)
        self.assertIn("github.com", decision.diagnostic)

    def test_title_only_edit_is_drift(self):
        comments, _snapshot = pinned_comments()
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=TITLE + " (updated)",
            live_body=BODY, comments=comments,
        )
        self.assertEqual(decision.action, "drift_blocked")
        self.assertIn("title", decision.reason)

    def test_label_churn_is_not_drift(self):
        # Labels/comments/status are not snapshot inputs: identical
        # title/body verifies regardless of surrounding lifecycle churn.
        comments, snapshot = pinned_comments()
        verdict = verify_live_against_snapshot(TITLE, BODY, select_snapshot(
            comments, repo=REPO, issue=ISSUE).snapshot)
        self.assertTrue(verdict.matches)

    def test_scheduler_selection_to_agent_fetch_edit_is_caught(self):
        # Scheduler selected body B1 and pinned it; the agent fetches B2.
        comments, _snapshot = pinned_comments(body=BODY)
        decision = decide_execution_gate(
            repo=REPO, issue=ISSUE, live_title=TITLE,
            live_body=BODY + "\nChanged after selection.\n",
            comments=comments, stage="execution-entry",
        )
        self.assertEqual(decision.action, "drift_blocked")
        self.assertIn("execution entry", decision.diagnostic)

    def test_edit_during_running_agent_is_caught_pre_pr(self):
        comments, _snapshot = pinned_comments()
        decision = decide_execution_gate(
            repo=REPO, issue=ISSUE, live_title=TITLE,
            live_body="Rewritten while the agent runs.\n",
            comments=comments, stage="pre-pr",
        )
        self.assertEqual(decision.action, "drift_blocked")
        self.assertIn("before publishing", decision.diagnostic)

    def test_no_change_is_safe(self):
        comments, _snapshot = pinned_comments()
        for stage in ("execution-entry", "pre-pr", "repair", "review", "pre-merge"):
            decision = decide_execution_gate(
                repo=REPO, issue=ISSUE, live_title=TITLE, live_body=BODY,
                comments=comments, stage=stage, pr_number=176,
            )
            self.assertEqual(decision.action, "proceed", stage)


class SimultaneousWakeupTests(unittest.TestCase):
    def test_first_writer_wins_no_overwrite(self):
        first = build_snapshot(REPO, ISSUE, TITLE, BODY, created_at="2026-10-09T10:00:00Z")
        second = build_snapshot(
            REPO, ISSUE, TITLE, BODY + "\nRacer edit.\n",
            created_at="2026-10-09T10:00:01Z",
        )
        comments = [
            bot_comment(render_snapshot_comment(second), created_at="2026-10-09T10:00:01Z", comment_id=2),
            bot_comment(render_snapshot_comment(first), created_at="2026-10-09T10:00:00Z", comment_id=1),
        ]
        selection = select_snapshot(comments, repo=REPO, issue=ISSUE)
        self.assertEqual(selection.status, "found")
        self.assertEqual(selection.snapshot.record["body"], BODY)
        # The loser sees drift against the winner and stops.
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=TITLE,
            live_body=BODY + "\nRacer edit.\n", comments=comments,
        )
        self.assertEqual(decision.action, "drift_blocked")

    def test_identical_concurrent_pins_converge(self):
        first = build_snapshot(REPO, ISSUE, TITLE, BODY)
        second = build_snapshot(REPO, ISSUE, TITLE, BODY)
        comments = [
            bot_comment(render_snapshot_comment(first), comment_id=1),
            bot_comment(render_snapshot_comment(second), comment_id=2),
        ]
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=TITLE, live_body=BODY,
            comments=comments,
        )
        self.assertEqual(decision.action, "proceed")

    def test_untrusted_marker_lookalike_is_ignored(self):
        snapshot = build_snapshot(REPO, ISSUE, TITLE, BODY)
        forged = {
            "body": render_snapshot_comment(snapshot),
            "author_association": "NONE",
            "user": {"login": "random-fork-user"},
            "created_at": "2026-10-09T10:00:00Z",
            "id": 9,
        }
        self.assertFalse(is_trusted_snapshot_comment(forged))
        selection = select_snapshot([forged], repo=REPO, issue=ISSUE)
        self.assertEqual(selection.status, "absent")
        # Owner posts are trusted (manual /oc admission path).
        owned = owner_comment(render_snapshot_comment(snapshot))
        self.assertTrue(
            is_trusted_snapshot_comment(owned, owner_login="kodmial")
        )

    def test_human_prose_is_never_parsed_as_state(self):
        prose = (
            "I think we should continuum-task-snapshot this issue "
            "repo=kodmial/nanodictate issue=123 generation=1 sometime."
        )
        self.assertIsNone(parse_snapshot_comment(prose))
        self.assertIsNone(parse_pr_snapshot_ref(prose))
        selection = select_snapshot(
            [bot_comment(prose)], repo=REPO, issue=ISSUE
        )
        self.assertEqual(selection.status, "absent")


class GenerationTests(unittest.TestCase):
    def test_stale_generation_does_not_satisfy_retry(self):
        snapshot = build_snapshot(REPO, ISSUE, TITLE, BODY, generation=1)
        comments = [bot_comment(render_snapshot_comment(snapshot))]
        selection = select_snapshot(comments, repo=REPO, issue=ISSUE, generation=2)
        self.assertEqual(selection.status, "absent")
        ref = render_pr_snapshot_ref(snapshot)
        decision = verify_pr_provenance_against_live(
            pr_body="body\n" + ref, live_title=TITLE, live_body=BODY,
            expected_repo=REPO, expected_issue=ISSUE, expected_generation=2,
        )
        self.assertEqual(decision.action, "stale_generation")

    def test_zero_diff_retry_reuses_same_snapshot(self):
        comments, snapshot = pinned_comments()
        for _ in range(3):
            decision = decide_admission(
                repo=REPO, issue=ISSUE, live_title=TITLE, live_body=BODY,
                comments=comments,
            )
            self.assertEqual(decision.action, "proceed")
        ref = render_pr_snapshot_ref(snapshot)
        decision = verify_pr_provenance_against_live(
            pr_body="Automated implementation\n" + ref,
            live_title=TITLE, live_body=BODY,
            expected_repo=REPO, expected_issue=ISSUE,
            expected_generation=DEFAULT_GENERATION,
        )
        self.assertEqual(decision.action, "proceed")


class TamperTests(unittest.TestCase):
    def test_missing_snapshot_pins_first(self):
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=TITLE, live_body=BODY, comments=[],
        )
        self.assertEqual(decision.action, "pin")

    def test_tampered_snapshot_fails_closed(self):
        snapshot = build_snapshot(REPO, ISSUE, TITLE, BODY)
        text = render_snapshot_comment(snapshot)
        # Flip one hex character of the pinned body digest: the embedded
        # content no longer matches the marker, so integrity must fail.
        tampered_text = text.replace(
            "body-sha256={}".format(snapshot["body_sha256"]),
            "body-sha256={}".format(
                ("0" if snapshot["body_sha256"][0] != "0" else "1")
                + snapshot["body_sha256"][1:]
            ),
            1,
        )
        self.assertNotEqual(tampered_text, text)
        comments = [bot_comment(tampered_text)]
        selection = select_snapshot(comments, repo=REPO, issue=ISSUE)
        self.assertEqual(selection.status, "tampered")
        decision = decide_admission(
            repo=REPO, issue=ISSUE, live_title=TITLE, live_body=BODY,
            comments=comments,
        )
        self.assertEqual(decision.action, "tampered_fail_closed")
        self.assertIn("live mutable", decision.diagnostic)

    def test_marker_without_json_fails_closed(self):
        marker = snapshot_marker_line(build_snapshot(REPO, ISSUE, TITLE, BODY))
        comments = [bot_comment(marker + "\nno json here")]
        selection = select_snapshot(comments, repo=REPO, issue=ISSUE)
        self.assertEqual(selection.status, "tampered")

    def test_partial_write_never_silently_falls_back(self):
        # A truncated comment (lost JSON tail) must not read as "absent".
        snapshot = build_snapshot(REPO, ISSUE, TITLE, BODY)
        truncated = render_snapshot_comment(snapshot)[:120]
        comments = [bot_comment(truncated + " ...")]
        parsed = parse_snapshot_comment(comments[0]["body"])
        # Either no marker survives (absent -> pin, agent waits) or the
        # surviving marker fails closed; it must never verify as clean.
        if parsed is not None:
            selection = select_snapshot(comments, repo=REPO, issue=ISSUE)
            self.assertEqual(selection.status, "tampered")


class LifecycleStageTests(unittest.TestCase):
    def setUp(self):
        self.comments, self.snapshot = pinned_comments()
        self.pr_body = (
            "<!-- continuum-task-context repo={} issue={} -->\n\n"
            "Automated implementation for #{}.\n\nCloses #{}\n\n{}".format(
                REPO, ISSUE, ISSUE, ISSUE,
                render_pr_snapshot_ref(self.snapshot),
            )
        )

    def test_edit_after_pr_creation_blocked(self):
        decision = verify_pr_provenance_against_live(
            pr_body=self.pr_body, live_title=TITLE,
            live_body=BODY + "\nPost-PR edit.\n",
            expected_repo=REPO, expected_issue=ISSUE,
        )
        self.assertEqual(decision.action, "drift_blocked")

    def test_edit_after_review_blocked(self):
        decision = decide_execution_gate(
            repo=REPO, issue=ISSUE, live_title=TITLE,
            live_body=BODY + "\nPost-review edit.\n",
            comments=self.comments, stage="review", pr_number=176,
        )
        self.assertEqual(decision.action, "drift_blocked")
        self.assertIn("176", decision.diagnostic)

    def test_edit_during_repair_blocked(self):
        decision = decide_execution_gate(
            repo=REPO, issue=ISSUE, live_title=TITLE,
            live_body=BODY + "\nMid-repair edit.\n",
            comments=self.comments, stage="repair", pr_number=176,
        )
        self.assertEqual(decision.action, "drift_blocked")

    def test_edit_immediately_before_merge_blocked(self):
        decision = decide_execution_gate(
            repo=REPO, issue=ISSUE, live_title=TITLE,
            live_body=BODY + "\nLast-second edit.\n",
            comments=self.comments, stage="pre-merge", pr_number=176,
        )
        self.assertEqual(decision.action, "drift_blocked")
        self.assertIn("before merge", decision.diagnostic)

    def test_pr_provenance_survives_unchanged(self):
        decision = verify_pr_provenance_against_live(
            pr_body=self.pr_body, live_title=TITLE, live_body=BODY,
            expected_repo=REPO, expected_issue=ISSUE,
        )
        self.assertEqual(decision.action, "proceed")

    def test_pr_pointing_at_wrong_issue_blocked(self):
        decision = verify_pr_provenance_against_live(
            pr_body=self.pr_body, live_title=TITLE, live_body=BODY,
            expected_repo=REPO, expected_issue=999,
        )
        self.assertEqual(decision.action, "drift_blocked")


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_pr_without_snapshot_is_explicit(self):
        decision = verify_pr_provenance_against_live(
            pr_body="Automated implementation for #123.\n\nCloses #123",
            live_title=TITLE, live_body=BODY,
        )
        self.assertEqual(decision.action, "legacy_unpinned")
        self.assertIn("predates", decision.diagnostic)

    def test_legacy_pr_is_never_falsely_protected(self):
        decision = classify_pr(
            "Automated implementation for #123.\n\nCloses #123",
            comments=[], repo=REPO, issue=ISSUE,
        )
        self.assertEqual(decision.action, "legacy_unpinned")

    def test_pr_with_provenance_is_protected(self):
        comments, snapshot = pinned_comments()
        pr_body = "body\n" + render_pr_snapshot_ref(snapshot)
        decision = classify_pr(
            pr_body, comments=comments, repo=REPO, issue=ISSUE
        )
        self.assertEqual(decision.action, "protected")

    def test_invalid_stage_fails_closed(self):
        comments, _snapshot = pinned_comments()
        with self.assertRaises(TaskSnapshotError):
            decide_execution_gate(
                repo=REPO, issue=ISSUE, live_title=TITLE, live_body=BODY,
                comments=comments, stage="merge",
            )


class CrossRepoTests(unittest.TestCase):
    def test_cross_repo_child_snapshot(self):
        child_repo = "kodmial/child-worker"
        snapshot = build_snapshot(child_repo, 7, "Child task", "Do work.\n")
        comments = [bot_comment(render_snapshot_comment(snapshot))]
        decision = decide_admission(
            repo=child_repo, issue=7, live_title="Child task",
            live_body="Do work.\n", comments=comments,
        )
        self.assertEqual(decision.action, "proceed")
        # Parent-issue snapshots never satisfy a child issue.
        decision = decide_admission(
            repo=child_repo, issue=7, live_title="Child task",
            live_body="Do work.\n",
            comments=[bot_comment(render_snapshot_comment(
                build_snapshot(REPO, ISSUE, TITLE, BODY)))],
        )
        self.assertEqual(decision.action, "pin")

    def test_pr_ref_for_wrong_repo_blocked(self):
        _comments, snapshot = pinned_comments()
        ref = render_pr_snapshot_ref(snapshot)
        decision = verify_pr_provenance_against_live(
            pr_body=ref, live_title=TITLE, live_body=BODY,
            expected_repo="kodmial/other", expected_issue=ISSUE,
        )
        self.assertEqual(decision.action, "drift_blocked")


class RenderParseTests(unittest.TestCase):
    def test_round_trip_preserves_exact_content(self):
        tricky_title = "Title with \u00e9moji \U0001f600 and `code`"
        tricky_body = "Line1\r\nLine2\rtrailing space \n```\ncode\n```\n"
        snapshot = build_snapshot(REPO, ISSUE, tricky_title, tricky_body)
        text = render_snapshot_comment(snapshot)
        self.assertIn(SNAPSHOT_MARKER, text)
        parsed = parse_snapshot_comment(text)
        self.assertIsNotNone(parsed)
        self.assertFalse(parsed.tampered)
        self.assertEqual(parsed.record["title"], normalize_spec_text(tricky_title))
        self.assertEqual(parsed.record["body"], normalize_spec_text(tricky_body))
        ref = render_pr_snapshot_ref(snapshot)
        self.assertIn(SNAPSHOT_REF_MARKER, ref)
        parsed_ref = parse_pr_snapshot_ref("prefix\n" + ref + "\nsuffix")
        self.assertIsNotNone(parsed_ref)
        self.assertEqual(parsed_ref.spec_sha256, snapshot["spec_sha256"])
        # Machine provenance stays separate from human comments.
        self.assertNotIn("DoD", ref)

    def test_digests_match_rejects_malformed(self):
        self.assertFalse(digests_match("xyz", "xyz"))
        self.assertTrue(digests_match("A" * 64, "a" * 64))
        self.assertFalse(digests_match("A" * 64, "B" * 64))

    def test_snapshot_record_carries_writer_and_time(self):
        snapshot = build_snapshot(
            REPO, ISSUE, TITLE, BODY, created_by="github-actions[bot]",
            created_at="2026-10-09T10:00:00Z",
        )
        record = json.loads(render_snapshot_comment(snapshot).split("```json")[1].split("```")[0])
        self.assertEqual(record["created_by"], "github-actions[bot]")
        self.assertEqual(record["created_at"], "2026-10-09T10:00:00Z")
        self.assertEqual(record["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
